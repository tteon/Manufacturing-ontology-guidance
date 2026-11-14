"""SPARQL (PyOxigraph, RocksDB-backed) implementations of the five scenarios."""

from __future__ import annotations

from pathlib import Path

import pyoxigraph as ox

from common import EX, PREFIXES, RDFS, local, safe_id, timed
from loader import ABOX_G, INF_G, TBOX_G, load_oxigraph

QUERY_TIME_FROM = "FROM ex:TBox_Graph FROM ex:ABox_Graph"


def _dir_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _num(term) -> float:
    return float(term.value)


class OxigraphEngine:
    name = "PyOxigraph"

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.store: ox.Store | None = None

    def load(self, n_synthetic: int) -> dict:
        if self.store is not None:
            del self.store
        self.store, timings = load_oxigraph(self.db_path, n_synthetic)
        return timings

    def _select(self, q: str):
        return list(self.store.query(PREFIXES + q))

    def noop(self):
        return self.store.query("ASK {}")

    # ------------------------------------------------------------------ S1
    S1_CODE = """SELECT ?cause ?equipment ?score {QFROM} WHERE {
  ex:{lot} ex:hasSymptom ex:{symptom} .
  << ex:{symptom} ex:causedBy ?mid >>       ex:confidence ?c1 .
  << ?mid         ex:causedBy ?cause >>     ex:confidence ?c2 .
  << ?cause       ex:attributedTo ?equipment >> ex:confidence ?c3 .
  BIND(?c1 * ?c2 * ?c3 AS ?score)
  FILTER(?score >= {minc})
} ORDER BY DESC(?score)"""

    def s1_rca(self, lot: str, symptom: str, min_conf: float):
        q = (self.S1_CODE.replace("{QFROM}", QUERY_TIME_FROM).replace("{lot}", safe_id(lot))
             .replace("{symptom}", safe_id(symptom)).replace("{minc}", repr(float(min_conf))))
        return [(local(r["cause"].value), local(r["equipment"].value), round(_num(r["score"]), 4))
                for r in self._select(q)]

    # ------------------------------------------------------------------ S2
    S2_CODE = """SELECT DISTINCT ?chamber ?lot {QFROM} WHERE {
  ex:{comp} ex:hasDefect ex:{defect} ;
            ex:partOf+ ?host .
  ?host a/rdfs:subClassOf* ex:ProcessChamber .
  ?host ex:feedsInto* ?chamber .
  ?lot  ex:processedIn ?chamber .
}"""

    def s2_propagation(self, component: str, defect: str):
        q = (self.S2_CODE.replace("{QFROM}", QUERY_TIME_FROM).replace("{comp}", safe_id(component))
             .replace("{defect}", safe_id(defect)))
        rows = self._select(q)
        return sorted({(local(r["chamber"].value), local(r["lot"].value)) for r in rows})

    # ------------------------------------------------------------------ S3
    S3_CODE = """SELECT DISTINCT ?eq ?val ?max ?category {QFROM} WHERE {
  ?obs sosa:hasFeatureOfInterest ?eq ; sosa:hasSimpleResult ?val .
  ?eq a ?cls .
  ?cls rdfs:subClassOf* ?th . ?th ex:maxAllowedDrift ?max .
  FILTER(?val > ?max)
  ?cls rdfs:subClassOf* ?category . ?category rdfs:subClassOf ex:Equipment .
}"""

    def s3_sensor(self):
        rows = self._select(self.S3_CODE.replace("{QFROM}", QUERY_TIME_FROM))
        return sorted((local(r["eq"].value), _num(r["val"]), _num(r["max"]), local(r["category"].value))
                      for r in rows)

    # ------------------------------------------------------------------ S4
    def _s4_tbox_quads(self, parent: str, subclasses: list[str], policy: str):
        p = ox.NamedNode(f"{EX}{safe_id(parent)}")
        quads = [ox.Quad(ox.NamedNode(f"{EX}{safe_id(c)}"), ox.NamedNode(f"{RDFS}subClassOf"), p, TBOX_G)
                 for c in subclasses]
        quads.append(ox.Quad(p, ox.NamedNode(f"{EX}requiredPolicy"), ox.Literal(safe_id(policy)), TBOX_G))
        return quads

    def s4_evolve(self, strategy: str, parent: str, subclasses: list[str],
                  policy: str = "Emergency_Interlock_Check") -> dict:
        """Apply the TBox change. Returns timings and #ABox facts written."""
        t = {}
        _, t["tbox_write_ms"] = timed(self.store.extend, self._s4_tbox_quads(parent, subclasses, policy))
        migrated = 0
        if strategy == "materialized":
            before = len(self.store)
            mig = PREFIXES + f"""INSERT {{ GRAPH ex:Inferred_Graph {{ ?x a ex:{parent} }} }}
WHERE {{ GRAPH ex:ABox_Graph {{ ?x a ?c }} GRAPH ex:TBox_Graph {{ ?c rdfs:subClassOf+ ex:{parent} }} }}"""
            _, t["abox_migration_ms"] = timed(self.store.update, mig)
            migrated = len(self.store) - before
        else:
            t["abox_migration_ms"] = 0.0
        _, t["flush_ms"] = timed(self.store.flush)
        t["write_total_ms"] = t["tbox_write_ms"] + t["abox_migration_ms"] + t["flush_ms"]
        t["abox_facts_written"] = migrated
        return t

    def s4_read(self, strategy: str, parent: str):
        parent = safe_id(parent)
        if strategy == "materialized":
            q = f"""SELECT ?eq ?eqType ?policy WHERE {{
  GRAPH ex:Inferred_Graph {{ ?eq a ex:{parent} }}
  GRAPH ex:ABox_Graph {{ ?eq a ?eqType }}
  GRAPH ex:TBox_Graph {{ ex:{parent} ex:requiredPolicy ?policy }} }}"""
        else:
            q = f"""SELECT ?eq ?eqType ?policy {QUERY_TIME_FROM} WHERE {{
  ?eq a ?eqType . ?eqType rdfs:subClassOf+ ex:{parent} .
  ex:{parent} ex:requiredPolicy ?policy }}"""
        return self._select(q)

    def s4_count(self, strategy: str, parent: str) -> int:
        """Same lookup as s4_read but returns only COUNT (excludes result-transfer cost)."""
        parent = safe_id(parent)
        if strategy == "materialized":
            q = f"SELECT (COUNT(?eq) AS ?n) WHERE {{ GRAPH ex:Inferred_Graph {{ ?eq a ex:{parent} }} }}"
        else:
            q = f"""SELECT (COUNT(?eq) AS ?n) {QUERY_TIME_FROM} WHERE {{
  ?eq a ?eqType . ?eqType rdfs:subClassOf+ ex:{parent} }}"""
        return int(self._select(q)[0]["n"].value)

    def s4_revert(self, parent: str, subclasses: list[str]):
        p = safe_id(parent)
        self.store.update(PREFIXES + f"""DELETE WHERE {{ GRAPH ex:TBox_Graph {{ ?c rdfs:subClassOf ex:{p} }} }};
DELETE WHERE {{ GRAPH ex:TBox_Graph {{ ex:{p} ex:requiredPolicy ?pol }} }};
DELETE WHERE {{ GRAPH ex:Inferred_Graph {{ ?x a ex:{p} }} }}""")
        self.store.flush()

    # ------------------------------------------------------------------ tool templates
    def list_equipment_by_class(self, class_id: str):
        c = safe_id(class_id)
        rows = self._select(f"""SELECT DISTINCT ?eq ?eqType ?policy {QUERY_TIME_FROM} WHERE {{
  ?eq a ?eqType . ?eqType rdfs:subClassOf* ex:{c} .
  OPTIONAL {{ ex:{c} ex:requiredPolicy ?policy }} }} ORDER BY ?eq""")
        return [{"eq": local(r["eq"].value), "eqType": local(r["eqType"].value),
                 "policy": r["policy"].value if r["policy"] else None} for r in rows]

    # ------------------------------------------------------------------ S5
    def find_observation(self, entity: str) -> str:
        rows = self._select(f"SELECT ?o WHERE {{ GRAPH ex:ABox_Graph {{ ?o sosa:hasFeatureOfInterest ex:{safe_id(entity)} }} }} ORDER BY ?o LIMIT 1")
        return local(rows[0]["o"].value)

    def s5_update(self, obs: str, value: float):
        self.store.update(PREFIXES + f"""DELETE {{ GRAPH ex:ABox_Graph {{ ex:{obs} sosa:hasSimpleResult ?old }} }}
INSERT {{ GRAPH ex:ABox_Graph {{ ex:{obs} sosa:hasSimpleResult "{value!r}"^^xsd:double }} }}
WHERE  {{ OPTIONAL {{ GRAPH ex:ABox_Graph {{ ex:{obs} sosa:hasSimpleResult ?old }} }} }}""")

    def s5_update_batch(self, obs: str, values: list[float]):
        """All overwrites as one SPARQL Update request (one transaction / one commit)."""
        ops = [f"""DELETE {{ GRAPH ex:ABox_Graph {{ ex:{obs} sosa:hasSimpleResult ?old }} }}
INSERT {{ GRAPH ex:ABox_Graph {{ ex:{obs} sosa:hasSimpleResult "{v!r}"^^xsd:double }} }}
WHERE  {{ OPTIONAL {{ GRAPH ex:ABox_Graph {{ ex:{obs} sosa:hasSimpleResult ?old }} }} }}""" for v in values]
        self.store.update(PREFIXES + " ;\n".join(ops))

    def s5_final(self, obs: str):
        rows = self._select(f"""SELECT ?v ?max WHERE {{ GRAPH ex:ABox_Graph {{ ex:{obs} sosa:hasSimpleResult ?v ;
  sosa:hasFeatureOfInterest ?eq . ?eq a ?c }} GRAPH ex:TBox_Graph {{ ?c rdfs:subClassOf* ?th . ?th ex:maxAllowedDrift ?max }} }}""")
        return [(_num(r["v"]), _num(r["max"])) for r in rows]

    def flush(self):
        self.store.flush()

    def disk_bytes(self) -> int:
        return _dir_bytes(self.db_path)

    def close(self):
        if self.store is not None:
            self.store.flush()
            del self.store
            self.store = None
