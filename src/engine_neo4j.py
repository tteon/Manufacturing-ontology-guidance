"""Cypher (Neo4j 5 LPG over Bolt) implementations of the five scenarios."""

from __future__ import annotations

import subprocess

from neo4j import GraphDatabase

from common import safe_id, timed
from loader import load_neo4j, wipe_neo4j


class Neo4jEngine:
    name = "Neo4j"

    def __init__(self, uri: str, user: str, password: str, container: str):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.driver.verify_connectivity()
        self.container = container

    def load(self, n_synthetic: int) -> dict:
        _, wipe_ms = timed(wipe_neo4j, self.driver)
        t = load_neo4j(self.driver, n_synthetic)
        t["wipe_ms"] = wipe_ms
        return t

    def _rows(self, cypher: str, **params):
        with self.driver.session() as s:
            return [r.data() for r in s.run(cypher, **params)]

    def _write(self, cypher: str, **params):
        """One auto-commit transaction (committed and durable on return)."""
        with self.driver.session() as s:
            return s.run(cypher, **params).consume()

    def noop(self):
        return self._rows("RETURN 1 AS x")

    # ------------------------------------------------------------------ S1
    S1_CODE = """MATCH (:Resource {id: $lot})-[:hasSymptom]->(s:Resource {id: $symptom})
MATCH p = (s)-[:causedBy*1..3]->(cause)-[:attributedTo]->(eq)
WITH cause, eq, reduce(x = 1.0, r IN relationships(p) | x * r.confidence) AS score
WHERE score >= $min_conf
RETURN cause.id AS cause, eq.id AS equipment, score ORDER BY score DESC"""

    def s1_rca(self, lot: str, symptom: str, min_conf: float):
        rows = self._rows(self.S1_CODE, lot=safe_id(lot), symptom=safe_id(symptom), min_conf=float(min_conf))
        return [(r["cause"], r["equipment"], round(r["score"], 4)) for r in rows]

    # ------------------------------------------------------------------ S2
    S2_CODE = """MATCH (c:Resource {id: $comp})-[:hasDefect]->(:Resource {id: $defect})
MATCH (c)-[:partOf*1..]->(host)-[:type]->()-[:subClassOf*0..]->(:Resource {id: 'ProcessChamber'})
MATCH (host)-[:feedsInto*0..]->(chamber)
MATCH (lot)-[:processedIn]->(chamber)
RETURN DISTINCT chamber.id AS chamber, lot.id AS lot"""

    def s2_propagation(self, component: str, defect: str):
        rows = self._rows(self.S2_CODE, comp=safe_id(component), defect=safe_id(defect))
        return sorted({(r["chamber"], r["lot"]) for r in rows})

    # ------------------------------------------------------------------ S3
    S3_CODE = """MATCH (obs)-[:hasFeatureOfInterest]->(eq)-[:type]->(cls)
MATCH (cls)-[:subClassOf*0..]->(th) WHERE th.maxAllowedDrift IS NOT NULL
  AND obs.hasSimpleResult > th.maxAllowedDrift
MATCH (cls)-[:subClassOf*0..]->(cat)-[:subClassOf]->(:Resource {id: 'Equipment'})
RETURN DISTINCT eq.id AS eq, obs.hasSimpleResult AS val, th.maxAllowedDrift AS max, cat.id AS category"""

    def s3_sensor(self):
        return sorted((r["eq"], r["val"], r["max"], r["category"]) for r in self._rows(self.S3_CODE))

    # ------------------------------------------------------------------ S4
    def s4_evolve(self, strategy: str, parent: str, subclasses: list[str],
                  policy: str = "Emergency_Interlock_Check") -> dict:
        parent = safe_id(parent)
        subclasses = [safe_id(c) for c in subclasses]
        t = {}
        _, t["tbox_write_ms"] = timed(self._write, """MERGE (p:Resource {id: $parent})
SET p.requiredPolicy = $policy
WITH p MATCH (sub:Resource) WHERE sub.id IN $subs
MERGE (sub)-[:subClassOf]->(p)""", parent=parent, subs=subclasses, policy=safe_id(policy))
        migrated = 0
        if strategy == "materialized":
            summary, t["abox_migration_ms"] = timed(self._write, f"""MATCH (eq:Resource)-[:type]->(:Resource)-[:subClassOf*1..]->(:Resource {{id: $parent}})
SET eq:`{parent}`""", parent=parent)
            migrated = summary.counters.labels_added
        else:
            t["abox_migration_ms"] = 0.0
        t["flush_ms"] = 0.0  # each auto-commit tx is durable on commit
        t["write_total_ms"] = t["tbox_write_ms"] + t["abox_migration_ms"]
        t["abox_facts_written"] = migrated
        return t

    def s4_read(self, strategy: str, parent: str):
        parent = safe_id(parent)
        if strategy == "materialized":
            q = f"""MATCH (p:Resource {{id: $parent}})
MATCH (eq:`{parent}`)-[:type]->(eqType)
RETURN eq.id AS eq, eqType.id AS eqType, p.requiredPolicy AS policy"""
        else:
            q = """MATCH (p:Resource {id: $parent})
MATCH (eq)-[:type]->(eqType)-[:subClassOf*1..]->(p)
RETURN eq.id AS eq, eqType.id AS eqType, p.requiredPolicy AS policy"""
        return self._rows(q, parent=parent)

    def s4_count(self, strategy: str, parent: str) -> int:
        """Same lookup as s4_read but returns only COUNT (excludes result-transfer cost)."""
        parent = safe_id(parent)
        if strategy == "materialized":
            q = f"MATCH (eq:`{parent}`) RETURN count(eq) AS n"
        else:
            q = """MATCH (p:Resource {id: $parent})
MATCH (eq)-[:type]->()-[:subClassOf*1..]->(p) RETURN count(eq) AS n"""
        return self._rows(q, parent=parent)[0]["n"]

    def list_equipment_by_class(self, class_id: str):
        return self._rows("""MATCH (c:Resource {id: $c})
MATCH (eq)-[:type]->(eqType)-[:subClassOf*0..]->(c)
RETURN DISTINCT eq.id AS eq, eqType.id AS eqType, c.requiredPolicy AS policy ORDER BY eq""", c=safe_id(class_id))

    def s5_update_batch(self, obs: str, values: list[float]):
        """All overwrites inside one explicit transaction (one commit)."""
        with self.driver.session() as s:
            with s.begin_transaction() as tx:
                for v in values:
                    tx.run("MATCH (o:Resource {id: $obs}) SET o.hasSimpleResult = $v", obs=obs, v=v).consume()
                tx.commit()

    def s4_revert(self, parent: str, subclasses: list[str]):
        parent = safe_id(parent)
        self._write(f"MATCH (n:`{parent}`) CALL (n) {{ REMOVE n:`{parent}` }} IN TRANSACTIONS OF 50000 ROWS")
        self._write("MATCH (p:Resource {id: $parent}) DETACH DELETE p", parent=parent)

    # ------------------------------------------------------------------ S5
    def find_observation(self, entity: str) -> str:
        rows = self._rows("""MATCH (o)-[:hasFeatureOfInterest]->(:Resource {id: $e})
RETURN o.id AS o ORDER BY o LIMIT 1""", e=safe_id(entity))
        return rows[0]["o"]

    def s5_update(self, obs: str, value: float):
        self._write("MATCH (o:Resource {id: $obs}) SET o.hasSimpleResult = $v", obs=obs, v=value)

    def s5_final(self, obs: str):
        rows = self._rows("""MATCH (o:Resource {id: $obs})-[:hasFeatureOfInterest]->(eq)-[:type]->(c)
MATCH (c)-[:subClassOf*0..]->(th) WHERE th.maxAllowedDrift IS NOT NULL
RETURN o.hasSimpleResult AS v, th.maxAllowedDrift AS max""", obs=obs)
        return [(r["v"], r["max"]) for r in rows]

    def flush(self):
        pass

    def disk_bytes(self) -> int:
        out = subprocess.run(
            ["docker", "exec", self.container, "du", "-sb",
             "/data/databases/neo4j", "/data/transactions/neo4j"],
            capture_output=True, text=True, check=True).stdout
        return sum(int(line.split()[0]) for line in out.strip().splitlines())

    def close(self):
        self.driver.close()
