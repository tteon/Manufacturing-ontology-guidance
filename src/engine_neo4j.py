"""Neo4j 5 (Bolt) engine for the v2 scenarios. Queries live in queries.py."""

from __future__ import annotations

import subprocess

from neo4j import GraphDatabase

from common import FabGraph, safe_id, timed
from loader import load_neo4j
from queries import canon, cypher


class Neo4jEngine:
    name = "Neo4j"

    def __init__(self, uri: str, user: str, password: str, container: str):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.driver.verify_connectivity()
        self.container = container

    def load(self, kg: FabGraph) -> dict:
        return load_neo4j(self.driver, kg)

    def _rows(self, q: str, **params):
        with self.driver.session() as s:
            return [r.data() for r in s.run(q, **params)]

    def _write(self, q: str, **params):
        with self.driver.session() as s:      # auto-commit transaction, durable on return
            return s.run(q, **params).consume()

    def q(self, op: str, label: str | None = None, **params):
        return canon(op, self._rows(cypher(op, label), **params))

    def noop(self):
        return self._rows("RETURN 1 AS x")

    # S1-S3 -----------------------------------------------------------------
    def s1(self, lot, min_conf):
        return self.q("S1", lot=safe_id(lot), min_conf=float(min_conf))

    def s2(self, component):
        return self.q("S2", component=safe_id(component))

    def s3(self, line):
        return self.q("S3", line=safe_id(line))

    def s3_global_count(self):
        return self.q("S3_global_count")

    # S4 --------------------------------------------------------------------
    def class_exists(self, cls) -> bool:
        return bool(self._rows(cypher("class_exists"), cls=safe_id(cls))[0]["n"])

    def s4_evolve(self, strategy, parent, subclasses, policy) -> dict:
        # The parent must be NEW (s4_revert detach-deletes it); all subclasses must exist.
        if self.class_exists(parent):
            raise ValueError(f"class {parent} already exists; choose a new parent class name")
        missing = [c for c in subclasses if not self.class_exists(c)]
        if missing:
            raise ValueError(f"unknown subclasses: {missing}")
        t = {}
        _, t["tbox_write_ms"] = timed(self._write, cypher("S4_tbox_write"), parent=safe_id(parent),
                                      subs=[safe_id(c) for c in subclasses], policy=safe_id(policy))
        written = 0
        if strategy == "materialized":
            summ, t["abox_migration_ms"] = timed(self._write, cypher("S4_migrate", parent), parent=parent)
            written = summ.counters.labels_added
        else:
            t["abox_migration_ms"] = 0.0
        t["abox_facts_written"] = written
        t["flush_ms"] = 0.0
        t["write_total_ms"] = t["tbox_write_ms"] + t["abox_migration_ms"]
        return t

    def s4_list(self, strategy, parent):
        label = parent if strategy == "materialized" else None
        return self.q(f"S4_list_{strategy}", label, parent=safe_id(parent))

    def s4_count(self, strategy, parent):
        label = parent if strategy == "materialized" else None
        return self.q(f"S4_count_{strategy}", label, parent=safe_id(parent))

    def s4_revert(self, parent):
        self._write(cypher("S4_revert_labels", parent))
        self._write(cypher("S4_revert_node"), parent=safe_id(parent))

    # S5 --------------------------------------------------------------------
    def s5_batch(self, rows: list) -> dict:
        _, ms = timed(self._write, cypher("S5_batch"), rows=[{"o": safe_id(o), "v": float(v)} for o, v in rows])
        return {"write_ms": ms, "flush_ms": 0.0}

    def s5_single(self, obs, value):
        self._write(cypher("S5_single"), obs=safe_id(obs), value=float(value))

    def s5_read(self, obs: list):
        return self.q("S5_read", obs=[safe_id(o) for o in obs])

    # tool helpers -----------------------------------------------------------
    def list_by_class(self, cls):
        return self._rows(cypher("list_by_class"), cls=safe_id(cls))

    def obs_of(self, entity):
        return [{"observation": r["o"], "value": r["v"], "maxAllowedDrift": r["max"]}
                for r in self._rows(cypher("obs_of"), entity=safe_id(entity))]

    def flush(self):
        pass

    def disk_bytes(self) -> int:
        """On-disk bytes via `docker exec <container> du`; -1 if Neo4j does not run in that container."""
        try:
            return self._du()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return -1

    def _du(self) -> int:
        out = subprocess.run(["docker", "exec", self.container, "du", "-sb", "/data/databases/neo4j",
                              "/data/transactions/neo4j"], capture_output=True, text=True, check=True).stdout
        return sum(int(line.split()[0]) for line in out.strip().splitlines())

    def close(self):
        self.driver.close()
