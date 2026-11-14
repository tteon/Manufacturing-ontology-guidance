"""Load one FabGraph (TBox quads from data/mfg_knowledge.trig + generated ABox + inferred types)
into both engines from the SAME quad list.

PyOxigraph: RocksDB-backed on-disk store; TBox / ABox / Inferred in separate named graphs.
Neo4j    : one :Resource node per IRI or blank node (restriction nodes get id "_b_<n>"),
           one relationship per IRI/blank-object triple, numeric literals -> float properties,
           RDF 1.2 reifier confidences -> relationship properties. Instances also get their class
           and all superclasses as labels (write-time materialization, mirrors Inferred_Graph).
"""

from __future__ import annotations

import shutil
from collections import defaultdict
from pathlib import Path

import pyoxigraph as ox

from common import RDF, FabGraph, local, timed


# --------------------------------------------------------------------------- Oxigraph
def load_oxigraph(db_path: Path, kg: FabGraph) -> tuple[ox.Store, dict]:
    if db_path.exists():
        shutil.rmtree(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = ox.Store(path=str(db_path))
    t = {}
    quads = kg.all_quads()
    _, t["load_ms"] = timed(store.extend, quads)
    _, t["flush_ms"] = timed(store.flush)
    t["quads"] = len(store)
    return store, t


# --------------------------------------------------------------------------- Neo4j
def neo4j_rows(quads):
    """Generic quad -> LPG conversion (Inferred_Graph quads are skipped; labels replace them)."""
    def nid(term):
        return f"_b_{term.value}" if isinstance(term, ox.BlankNode) else local(term.value)

    nodes, edges, props = set(), defaultdict(list), defaultdict(list)
    reifies, reifier_conf = {}, {}
    for q in quads:
        if q.graph_name.value.endswith("Inferred_Graph"):
            continue
        s, p, o = q.subject, q.predicate, q.object
        if p.value == f"{RDF}reifies" and isinstance(o, ox.Triple):
            reifies[s.value] = (nid(o.subject), local(o.predicate.value), nid(o.object))
            continue
        if isinstance(s, ox.BlankNode) and local(p.value) == "confidence":  # reifier annotation
            reifier_conf[s.value] = float(o.value)
            continue
        sid = nid(s)
        nodes.add(sid)
        if isinstance(o, ox.Literal):
            try:
                v = float(o.value)
            except ValueError:
                v = o.value
            props[local(p.value)].append({"id": sid, "v": v})
        else:
            oid = nid(o)
            nodes.add(oid)
            edges[local(p.value)].append({"s": sid, "o": oid})
    conf = {spo: reifier_conf[b] for b, spo in reifies.items() if b in reifier_conf}
    for rel, rows in edges.items():
        for r in rows:
            r["c"] = conf.get((r["s"], rel, r["o"]))
    return nodes, edges, props


def _batches(rows, n=10000):
    for i in range(0, len(rows), n):
        yield rows[i:i + n]


def load_neo4j(driver, kg: FabGraph) -> dict:
    def _load():
        nodes, edges, props = neo4j_rows(kg.tbox.quads + kg.abox_quads())
        with driver.session() as s:
            s.run("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS").consume()
            s.run("CREATE CONSTRAINT resource_id IF NOT EXISTS FOR (n:Resource) REQUIRE n.id IS UNIQUE").consume()
            for b in _batches(sorted(nodes)):
                s.run("UNWIND $ids AS id CREATE (:Resource {id: id})", ids=b).consume()
            for pname, rows in props.items():
                for b in _batches(rows):
                    s.run(f"UNWIND $rows AS r MATCH (n:Resource {{id: r.id}}) SET n.`{pname}` = r.v", rows=b).consume()
            for rel, rows in edges.items():
                for b in _batches(rows):
                    s.run(f"""UNWIND $rows AS r MATCH (a:Resource {{id: r.s}}), (b:Resource {{id: r.o}})
                              CREATE (a)-[e:`{rel}`]->(b) SET e.confidence = r.c""", rows=b).consume()
            by_labels = defaultdict(list)
            for inst, cls in kg.types.items():
                by_labels[tuple(sorted({cls} | kg.tbox.ancestors(cls)))].append(inst)
            for labels, ids in by_labels.items():
                lbl = ":".join(f"`{x}`" for x in labels)
                for b in _batches(ids):
                    s.run(f"UNWIND $ids AS id MATCH (n:Resource {{id: id}}) SET n:{lbl}", ids=b).consume()
            n = s.run("MATCH (n) RETURN count(n) AS n").single()["n"]
            r = s.run("MATCH ()-[r]->() RETURN count(r) AS n").single()["n"]
        return n, r

    (n, r), ms = timed(_load)
    return {"load_ms": ms, "nodes": n, "relationships": r}
