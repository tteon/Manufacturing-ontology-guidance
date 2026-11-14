"""Load-test operations on the v2 graph, reusing the exact templates from src/queries.py.

Read ops : S1 (lot RCA), S2 (defective seal contamination), S3 (per-line anomalies),
           Cq (class count, query-time hierarchy), Cm (class count, write-time materialization).
Write op : W  = upsert of one observation (one transaction). The new value stays on the same side
           of the part's threshold, so read ground truth stays valid under concurrent writes.
Mixes    : R (reads only), W (writes only), M (90% reads / 10% writes), Whot (all writes to one row).
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from common import S1_MIN_CONF, precision_recall  # noqa: E402
from queries import SPARQL, canon, cypher  # noqa: E402

OP_SPARQL = {"S1": "S1", "S2": "S2", "S3": "S3", "Cq": "count_by_class_query_time",
             "Cm": "count_by_class_materialized", "W": "S5_single"}
OP_CYPHER = OP_SPARQL


def sparql_for(op: str, p: dict) -> str:
    return SPARQL[OP_SPARQL[op]](p)


def cypher_for(op: str, p: dict):
    name = OP_CYPHER[op]
    if op == "Cm":
        return cypher(name, p["cls"]), {}
    q = cypher(name)
    if op == "S1":
        return q, {"lot": p["lot"], "min_conf": p["min_conf"]}
    if op == "S2":
        return q, {"component": p["component"]}
    if op == "S3":
        return q, {"line": p["line"]}
    if op == "Cq":
        return q, {"cls": p["cls"]}
    return q, {"obs": p["obs"], "value": p["value"]}


def canon_rows(op: str, rows):
    return None if op == "W" else canon(OP_SPARQL[op], rows)


def correct(op: str, got, truth) -> bool:
    if op == "W":
        return True
    if op in ("Cq", "Cm"):
        return got == truth
    p, r = precision_recall(got, truth)
    return p == 1.0 and r == 1.0


MIXES = {
    "R": {"S1": 0.2, "S2": 0.2, "S3": 0.2, "Cq": 0.2, "Cm": 0.2},
    "W": {"W": 1.0},
    "M": {"S1": 0.18, "S2": 0.18, "S3": 0.18, "Cq": 0.18, "Cm": 0.18, "W": 0.10},
    "Whot": {"W": 1.0},
}


def make_pools(kg, n: int, seed: int = 11) -> dict:
    rng = random.Random(seed)
    lots = sorted(l for l, s in kg.lot_symptoms.items() if s)
    seals = sorted(kg.defect)
    classes = ["Equipment", "Module", "Part", "ProcessChamber", "GasSupplyModule", "ThermalModule",
               "PlasmaModule", "MFC", "Seal", "Heater", "RFGenerator"]
    return {
        "S1": [{"lot": l, "min_conf": S1_MIN_CONF, "truth": kg.truth_s1(l)} for l in rng.sample(lots, min(n, len(lots)))],
        "S2": [{"component": c, "truth": kg.truth_s2(c)} for c in rng.sample(seals, min(n, len(seals)))],
        "S3": [{"line": l, "truth": kg.truth_s3(l)} for l in rng.sample(kg.lines, min(n, len(kg.lines)))],
        "C": [{"cls": c, "truth": kg.truth_count(c)} for c in classes],
        "W": [{"obs": o, "thr": kg.tbox.threshold[spec], "above": v > kg.tbox.threshold[spec]}
              for o, (part, spec, v) in sorted(kg.obs.items())],
    }


def new_value(rng: random.Random, w: dict) -> float:
    thr = w["thr"]
    return round(thr * (rng.uniform(1.05, 1.5) if w["above"] else rng.uniform(0.2, 0.95)), 3)


def pick(rng: random.Random, mix: str, pools: dict):
    r, acc = rng.random(), 0.0
    for op, w in MIXES[mix].items():
        acc += w
        if r <= acc:
            break
    if op == "W":
        w = pools["W"][0] if mix == "Whot" else rng.choice(pools["W"])
        return op, {"obs": w["obs"], "value": new_value(rng, w)}, None
    item = rng.choice(pools["C" if op in ("Cq", "Cm") else op])
    return op, item, item["truth"]
