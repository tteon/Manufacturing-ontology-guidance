"""Shared constants, the connected fab-graph generator (v2), ground truth and timing utilities."""

from __future__ import annotations

import random
import re
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pyoxigraph as ox

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RESULTS = ROOT / "results"
TRIG_PATH = DATA / "mfg_knowledge.trig"

EX = "http://example.org/mfg#"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
OWL = "http://www.w3.org/2002/07/owl#"
SOSA = "http://www.w3.org/ns/sosa/"
SSN = "http://www.w3.org/ns/ssn/"
XSD = "http://www.w3.org/2001/XMLSchema#"

PREFIXES = f"""PREFIX ex: <{EX}>
PREFIX rdf: <{RDF}>
PREFIX rdfs: <{RDFS}>
PREFIX owl: <{OWL}>
PREFIX sosa: <{SOSA}>
PREFIX ssn: <{SSN}>
PREFIX xsd: <{XSD}>
"""

TBOX_G = ox.NamedNode(f"{EX}TBox_Graph")
ABOX_G = ox.NamedNode(f"{EX}ABox_Graph")
INF_G = ox.NamedNode(f"{EX}Inferred_Graph")

S1_MIN_CONF = 0.48   # correct paths >= 0.7*0.7 = 0.49; any path with a weak edge <= 0.5*0.9 = 0.45
S4_PARENT = "CriticalHazardSystem"
S4_SUBCLASSES = ["GasSupplyModule", "PlasmaModule"]
S4_POLICY = "Emergency_Interlock_Check"

MODULES = {  # module class -> leaf part classes (2 sensored parts + 2 seals each = 4 leaves)
    "GasSupplyModule": ["MFC", "MFC", "Seal", "Seal"],
    "ThermalModule": ["Heater", "Heater", "Seal", "Seal"],
    "PlasmaModule": ["RFGenerator", "RFGenerator", "Seal", "Seal"],
}
MODULE_ABBR = {"GasSupplyModule": "GSM", "ThermalModule": "TM", "PlasmaModule": "PM"}
DEFECTS = ["Micro_Leak", "Crack", "Swelling"]

_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")


def safe_id(value: str) -> str:
    """Validate an entity/class local name before it is placed in a query."""
    if not isinstance(value, str) or not _ID.match(value):
        raise ValueError(f"unsafe or invalid identifier: {value!r}")
    return value


def local(iri: str) -> str:
    for sep in ("#", "/"):
        if sep in iri:
            iri = iri.rsplit(sep, 1)[1]
    return iri


# --------------------------------------------------------------------------- TBox facts
@dataclass
class TBox:
    quads: list
    parents: dict          # class -> set of named superclasses
    spec_of_class: dict    # class -> spec (via owl:Restriction onProperty ssn:hasProperty hasValue)
    threshold: dict        # spec -> maxAllowedDrift

    def ancestors(self, cls: str) -> set:
        out, stack = set(), [cls]
        while stack:
            for p in self.parents.get(stack.pop(), ()):
                if p not in out:
                    out.add(p)
                    stack.append(p)
        return out

    def threshold_for(self, cls: str):
        for c in [cls, *self.ancestors(cls)]:
            if c in self.spec_of_class:
                return self.threshold[self.spec_of_class[c]]
        return None


def load_tbox(path: Path = TRIG_PATH) -> TBox:
    quads = list(ox.parse(path=str(path), format=ox.RdfFormat.TRIG))
    parents, restr, spec_of, thr = defaultdict(set), defaultdict(dict), {}, {}
    sub_restr = []
    for q in quads:
        s, p, o = q.subject, q.predicate.value, q.object
        if p == f"{RDFS}subClassOf":
            if isinstance(o, ox.NamedNode):
                parents[local(s.value)].add(local(o.value))
            else:
                sub_restr.append((local(s.value), o.value))
        elif isinstance(s, ox.BlankNode):
            restr[s.value][p] = o
        elif p == f"{EX}maxAllowedDrift":
            thr[local(s.value)] = float(o.value)
    for cls, b in sub_restr:
        r = restr[b]
        if r.get(f"{OWL}onProperty", ox.NamedNode("x:x")).value == f"{SSN}hasProperty":
            spec_of[cls] = local(r[f"{OWL}hasValue"].value)
    return TBox(quads, dict(parents), spec_of, thr)


# --------------------------------------------------------------------------- generator
@dataclass
class FabGraph:
    scale_factor: int
    seed: int
    tbox: TBox
    types: dict = field(default_factory=dict)                 # instance -> class
    part_of: dict = field(default_factory=dict)               # child -> parent
    feeds: dict = field(default_factory=dict)                 # chamber -> next chamber
    processed: dict = field(default_factory=lambda: defaultdict(list))  # lot -> [chamber]
    lot_symptoms: dict = field(default_factory=lambda: defaultdict(list))
    defect: dict = field(default_factory=dict)                # seal -> defect
    obs: dict = field(default_factory=dict)                   # observation -> [part, spec, value]
    caused_by: dict = field(default_factory=lambda: defaultdict(list))   # symptom -> [(cause, conf)]
    attributed: dict = field(default_factory=dict)            # cause -> (part, conf)
    mitigated: dict = field(default_factory=dict)             # cause -> action
    planted_correct: dict = field(default_factory=lambda: defaultdict(set))  # symptom -> {(cause, part)}
    lines: list = field(default_factory=list)
    chambers: list = field(default_factory=list)

    # ---------------------------------------------------------------- derived
    def up(self, node: str) -> list:
        out, c = [], self.part_of.get(node)
        while c:
            out.append(c)
            c = self.part_of.get(c)
        return out

    def chamber_of(self, node: str) -> str:
        return next(a for a in [node, *self.up(node)] if self.types.get(a) == "ProcessChamber")

    def downstream(self, ch: str) -> list:
        out = [ch]
        while out[-1] in self.feeds:
            out.append(self.feeds[out[-1]])
        return out

    # ---------------------------------------------------------------- ground truth (pure Python)
    def truth_s1(self, lot: str, min_conf: float = S1_MIN_CONF) -> list:
        out = set()
        for sym in self.lot_symptoms[lot]:
            for cause, c1 in self.caused_by[sym]:
                part, c2 = self.attributed[cause]
                if c1 * c2 >= min_conf:
                    out.add((cause, part, round(c1 * c2, 4)))
        return sorted(out)

    def planted_s1(self, lot: str) -> set:
        return set().union(*(self.planted_correct[s] for s in self.lot_symptoms[lot]))

    def truth_s2(self, component: str) -> list:
        affected = set(self.downstream(self.chamber_of(component)))
        return sorted(lot for lot, chs in self.processed.items() if affected & set(chs))

    def truth_s3(self, line: str) -> list:
        out = []
        for part, spec, v in self.obs.values():
            if line in self.up(part) and v > self.tbox.threshold[spec]:
                out.append(part)
        return sorted(out)

    def truth_s3_global(self) -> int:
        return sum(1 for part, spec, v in self.obs.values() if v > self.tbox.threshold[spec])

    def truth_count(self, cls: str) -> int:
        return sum(1 for t in self.types.values() if t == cls or cls in self.tbox.ancestors(t))

    def truth_s4(self, parent_classes=tuple(S4_SUBCLASSES)) -> list:
        return sorted(i for i, t in self.types.items()
                      if t in parent_classes or self.tbox.ancestors(t) & set(parent_classes))

    # ---------------------------------------------------------------- RDF export
    def abox_quads(self) -> list:
        ex = lambda x: ox.NamedNode(f"{EX}{x}")  # noqa: E731
        rdf_type = ox.NamedNode(f"{RDF}type")
        dbl = lambda v: ox.Literal(repr(float(v)), datatype=ox.NamedNode(f"{XSD}double"))  # noqa: E731
        g = ABOX_G
        q = []
        add = lambda s, p, o: q.append(ox.Quad(s, p, o, g))  # noqa: E731

        def annotated(s, p, o, conf):
            add(s, p, o)
            r = ox.BlankNode()
            add(r, ox.NamedNode(f"{RDF}reifies"), ox.Triple(s, p, o))
            add(r, ex("confidence"), dbl(conf))

        for i, t in self.types.items():
            add(ex(i), rdf_type, ex(t))
        for a, b in self.part_of.items():
            add(ex(a), ex("partOf"), ex(b))
        for a, b in self.feeds.items():
            add(ex(a), ex("feedsInto"), ex(b))
        for lot, chs in self.processed.items():
            for c in chs:
                add(ex(lot), ex("processedIn"), ex(c))
        for lot, syms in self.lot_symptoms.items():
            for s in syms:
                add(ex(lot), ex("hasSymptom"), ex(s))
        for c, d in self.defect.items():
            add(ex(c), ex("hasDefect"), ex(d))
        for o, (part, spec, v) in self.obs.items():
            add(ex(o), rdf_type, ox.NamedNode(f"{SOSA}Observation"))
            add(ex(o), ox.NamedNode(f"{SOSA}hasFeatureOfInterest"), ex(part))
            add(ex(o), ox.NamedNode(f"{SOSA}observedProperty"), ex(spec))
            add(ex(o), ox.NamedNode(f"{SOSA}hasSimpleResult"), dbl(v))
        for sym, lst in self.caused_by.items():
            for cause, conf in lst:
                annotated(ex(sym), ex("causedBy"), ex(cause), conf)
        for cause, (part, conf) in self.attributed.items():
            annotated(ex(cause), ex("attributedTo"), ex(part), conf)
        for cause, act in self.mitigated.items():
            add(ex(cause), ex("mitigatedBy"), ex(act))
        return q

    def inferred_quads(self) -> list:
        """Write-time materialized superclass types (used by the 'materialized' strategy)."""
        rdf_type = ox.NamedNode(f"{RDF}type")
        return [ox.Quad(ox.NamedNode(f"{EX}{i}"), rdf_type, ox.NamedNode(f"{EX}{a}"), INF_G)
                for i, t in self.types.items() for a in sorted(self.tbox.ancestors(t))]

    def all_quads(self) -> list:
        return self.tbox.quads + self.abox_quads() + self.inferred_quads()

    def stats(self) -> dict:
        n_abox = len(self.abox_quads())
        return {
            "scale_factor(lines)": self.scale_factor, "chambers": len(self.chambers),
            "equipment": sum(1 for t in self.types.values() if t not in ("Lot", "Symptom", "Cause", "Action")),
            "lots": len(self.processed), "processedIn": sum(len(v) for v in self.processed.values()),
            "observations": len(self.obs), "symptoms": len(self.caused_by),
            "symptomatic_lots": sum(1 for v in self.lot_symptoms.values() if v),
            "defective_seals": len(self.defect), "causes": len(self.attributed),
            "quads_tbox": len(self.tbox.quads), "quads_abox": n_abox, "quads_inferred": len(self.inferred_quads()),
            "quads_total": len(self.tbox.quads) + n_abox + len(self.inferred_quads()),
        }


def generate_connected_fab_graph(scale_factor: int, *, steps_per_line: int = 4, lots_per_line: int = 10,
                                 symptom_rate: float = 0.05, seed: int = 7, tbox: TBox | None = None) -> FabGraph:
    """Connected fab graph whose S1-S5 traversal paths all grow with ``scale_factor`` (= #lines)."""
    rng = random.Random(seed)
    kg = FabGraph(scale_factor=scale_factor, seed=seed, tbox=tbox or load_tbox())
    all_parts, parts_of_chamber, seals_of_line = [], defaultdict(list), defaultdict(list)
    for l in range(1, scale_factor + 1):
        line = f"Line_{l}"
        kg.types[line] = "ProductionLine"
        kg.lines.append(line)
        chambers = []
        for s in range(1, steps_per_line + 1):
            ch = f"Ch_{l}_{s}"
            kg.types[ch] = "ProcessChamber"
            kg.part_of[ch] = line
            if chambers:
                kg.feeds[chambers[-1]] = ch
            chambers.append(ch)
            kg.chambers.append(ch)
            for mcls, leaves in MODULES.items():
                mod = f"{MODULE_ABBR[mcls]}_{l}_{s}"
                kg.types[mod] = mcls
                kg.part_of[mod] = ch
                for k, pcls in enumerate(leaves, 1):
                    part = f"{pcls}_{l}_{s}_{MODULE_ABBR[mcls]}{k}"
                    kg.types[part] = pcls
                    kg.part_of[part] = mod
                    all_parts.append(part)
                    parts_of_chamber[ch].append(part)
                    if pcls == "Seal":
                        seals_of_line[line].append(part)
        # One guaranteed defective seal per line, plus ~3% background defects.
        forced = rng.choice(seals_of_line[line])
        for seal in seals_of_line[line]:
            if seal == forced or rng.random() < 0.03:
                kg.defect[seal] = rng.choice(DEFECTS)
        for n in range(1, lots_per_line + 1):
            lot = f"Lot_{l}_{n}"
            kg.types[lot] = "Lot"
            kg.processed[lot] = [c for c in chambers if rng.random() < 0.85] or [chambers[0]]
    # Sensor observation stream: one Observation per leaf part, normally distributed readings
    # (mean 0.6*threshold, sd 0.25*threshold -> ~5.5% above threshold).
    for part in all_parts:
        spec = kg.tbox.spec_of_class[kg.types[part]]
        thr = kg.tbox.threshold[spec]
        kg.obs[f"Obs_{part}"] = [part, spec, round(max(0.0, rng.gauss(0.6 * thr, 0.25 * thr)), 3)]
    # FMEA causal chains on ~symptom_rate of process steps (at least one).
    sym_chambers = [c for c in kg.chambers if rng.random() < symptom_rate] or [rng.choice(kg.chambers)]
    for ch in sym_chambers:
        sym = f"Sym_{ch[3:]}"
        kg.types[sym] = "Symptom"
        lots_here = [lot for lot, chs in kg.processed.items() if ch in chs]
        for lot in [x for x in lots_here if rng.random() < 0.5] or lots_here[:1]:
            kg.lot_symptoms[lot].append(sym)
        for k in range(1, 3):  # correct: both edges 0.7-0.9, part inside this chamber
            cause = f"Cause_{ch[3:]}_c{k}"
            part = rng.choice(parts_of_chamber[ch])
            kg.caused_by[sym].append((cause, round(rng.uniform(0.7, 0.9), 3)))
            kg.attributed[cause] = (part, round(rng.uniform(0.7, 0.9), 3))
            kg.mitigated[cause] = f"Action_{ch[3:]}_c{k}"
            kg.planted_correct[sym].add((cause, part))
        for k in range(1, 5):  # wrong: weak symptom->cause edge (0.1-0.5), random part anywhere
            cause = f"Cause_{ch[3:]}_w{k}"
            kg.caused_by[sym].append((cause, round(rng.uniform(0.1, 0.5), 3)))
            kg.attributed[cause] = (rng.choice(all_parts), round(rng.uniform(0.7, 0.9), 3))
        for k in range(1, 3):  # decoy: strong symptom->cause edge but weak cause->part edge (0.1-0.5)
            cause = f"Cause_{ch[3:]}_d{k}"
            kg.caused_by[sym].append((cause, round(rng.uniform(0.7, 0.9), 3)))
            kg.attributed[cause] = (rng.choice(all_parts), round(rng.uniform(0.1, 0.5), 3))
    for cause in kg.attributed:
        kg.types[cause] = "Cause"
    for act in kg.mitigated.values():
        kg.types[act] = "Action"
    # The independent score computation must recover exactly the planted correct causes.
    for lot, syms in kg.lot_symptoms.items():
        assert {(c, p) for c, p, _ in kg.truth_s1(lot)} == kg.planted_s1(lot), lot
    return kg


def sample_params(kg: FabGraph, n: int, seed: int = 11) -> dict:
    """Request parameters (with ground truth) for S1-S3."""
    rng = random.Random(seed)
    lots = sorted(l for l, s in kg.lot_symptoms.items() if s)
    seals = sorted(kg.defect)
    return {
        "S1": [{"lot": l, "truth": kg.truth_s1(l)} for l in rng.sample(lots, min(n, len(lots)))],
        "S2": [{"component": c, "truth": kg.truth_s2(c)} for c in rng.sample(seals, min(n, len(seals)))],
        "S3": [{"line": l, "truth": kg.truth_s3(l)} for l in rng.sample(kg.lines, min(n, len(kg.lines)))],
    }


def precision_recall(got, truth) -> tuple[float, float]:
    """Set-based precision/recall; S1 tuples compare (cause, part) and require |score diff| <= 1e-4."""
    def key(x):
        return tuple(x[:2]) if isinstance(x, (list, tuple)) and len(x) == 3 else x
    g = {key(x): x for x in got}
    t = {key(x): x for x in truth}
    tp = sum(1 for k in g if k in t and (not isinstance(g[k], (list, tuple)) or len(g[k]) != 3
                                         or abs(g[k][2] - t[k][2]) <= 1e-4))
    p = tp / len(got) if got else 1.0     # duplicate rows in `got` lower precision
    r = tp / len(t) if t else 1.0
    return p, r


# --------------------------------------------------------------------------- timing
def ms(ns: int) -> float:
    return ns / 1e6


def timed(fn: Callable, *args, **kwargs):
    t0 = time.perf_counter_ns()
    out = fn(*args, **kwargs)
    return out, ms(time.perf_counter_ns() - t0)


def summarize(samples: list[float]) -> dict:
    s = sorted(samples)
    p95 = s[min(len(s) - 1, max(0, int(round(0.95 * len(s))) - 1))]
    return {"n": len(s), "median_ms": round(statistics.median(s), 4), "p95_ms": round(p95, 4),
            "min_ms": round(s[0], 4), "max_ms": round(s[-1], 4)}


def bench_read(fn: Callable, warmup: int, reps: int):
    out = None
    for _ in range(warmup):
        out = fn()
    samples = []
    for _ in range(reps):
        out, dt = timed(fn)
        samples.append(dt)
    return out, summarize(samples)


if __name__ == "__main__":
    import json
    import sys
    for sf in [int(x) for x in (sys.argv[1:] or ["3", "30", "300"])]:
        kg = generate_connected_fab_graph(sf)
        print(json.dumps(kg.stats()))
