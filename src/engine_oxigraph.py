"""Embedded PyOxigraph (RocksDB on disk) engine for the v2 scenarios. Queries live in queries.py."""

from __future__ import annotations

from pathlib import Path

import pyoxigraph as ox

from common import EX, RDFS, TBOX_G, FabGraph, safe_id, timed
from loader import load_oxigraph
from queries import SPARQL, canon


def _dir_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


class OxigraphEngine:
    name = "PyOxigraph"

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.store: ox.Store | None = None

    def load(self, kg: FabGraph) -> dict:
        self.store = None
        self.store, t = load_oxigraph(self.db_path, kg)
        return t

    def _rows(self, name: str, **p):
        return list(self.store.query(SPARQL[name](p)))

    def q(self, op: str, **p):
        return canon(op, self._rows(op, **p))

    def noop(self):
        return self.store.query("ASK {}")

    # S1-S3 -----------------------------------------------------------------
    def s1(self, lot, min_conf):
        return self.q("S1", lot=lot, min_conf=min_conf)

    def s2(self, component):
        return self.q("S2", component=component)

    def s3(self, line):
        return self.q("S3", line=line)

    def s3_global_count(self):
        return self.q("S3_global_count")

    # S4 --------------------------------------------------------------------
    def _tbox_quads(self, parent, subclasses, policy):
        p = ox.NamedNode(f"{EX}{safe_id(parent)}")
        qs = [ox.Quad(ox.NamedNode(f"{EX}{safe_id(c)}"), ox.NamedNode(f"{RDFS}subClassOf"), p, TBOX_G) for c in subclasses]
        qs.append(ox.Quad(p, ox.NamedNode(f"{EX}requiredPolicy"), ox.Literal(safe_id(policy)), TBOX_G))
        return qs

    def class_exists(self, cls) -> bool:
        return bool(self.store.query(SPARQL["class_exists"]({"cls": cls})))

    def s4_evolve(self, strategy, parent, subclasses, policy) -> dict:
        # The parent must be NEW (s4_revert removes every edge to it); all subclasses must exist.
        if self.class_exists(parent):
            raise ValueError(f"class {parent} already exists; choose a new parent class name")
        missing = [c for c in subclasses if not self.class_exists(c)]
        if missing:
            raise ValueError(f"unknown subclasses: {missing}")
        t = {}
        _, t["tbox_write_ms"] = timed(self.store.extend, self._tbox_quads(parent, subclasses, policy))
        before = len(self.store)
        if strategy == "materialized":
            _, t["abox_migration_ms"] = timed(self.store.update, SPARQL["S4_migrate"]({"parent": parent}))
        else:
            t["abox_migration_ms"] = 0.0
        t["abox_facts_written"] = len(self.store) - before
        _, t["flush_ms"] = timed(self.store.flush)
        t["write_total_ms"] = t["tbox_write_ms"] + t["abox_migration_ms"] + t["flush_ms"]
        return t

    def s4_list(self, strategy, parent):
        return self.q(f"S4_list_{strategy}", parent=parent)

    def s4_count(self, strategy, parent):
        return self.q(f"S4_count_{strategy}", parent=parent)

    def s4_revert(self, parent):
        self.store.update(SPARQL["S4_revert"]({"parent": parent}))
        self.store.flush()

    # S5 --------------------------------------------------------------------
    def s5_batch(self, rows: list) -> dict:
        t = {}
        _, t["write_ms"] = timed(self.store.update, SPARQL["S5_batch"]({"rows": rows}))
        _, t["flush_ms"] = timed(self.store.flush)
        return t

    def s5_single(self, obs, value):
        self.store.update(SPARQL["S5_single"]({"obs": obs, "value": value}))

    def s5_read(self, obs: list):
        return self.q("S5_read", obs=obs)

    # tool helpers -----------------------------------------------------------
    def list_by_class(self, cls):
        return [{"eq": local_or(r["eq"]), "eqType": local_or(r["eqType"]), "policy": r["policy"].value if r["policy"] else None}
                for r in self._rows("list_by_class", cls=cls)]

    def obs_of(self, entity):
        return [{"observation": local_or(r["o"]), "value": float(r["v"].value), "maxAllowedDrift": float(r["max"].value)}
                for r in self._rows("obs_of", entity=entity)]

    def flush(self):
        self.store.flush()

    def disk_bytes(self) -> int:
        return _dir_bytes(self.db_path)

    def close(self):
        if self.store is not None:
            self.store.flush()
            self.store = None


def local_or(term):
    return term.value.rsplit("#", 1)[-1]
