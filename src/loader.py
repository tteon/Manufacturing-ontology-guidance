"""Load the TriG knowledge base (+ optional synthetic scale-up) into both engines.

PyOxigraph: RocksDB-backed on-disk store, TBox and ABox in separate named graphs,
plus an ``Inferred_Graph`` that holds write-time materialized superclass types
(used only by the *materialized* strategy).

Neo4j: one ``:Resource`` node per IRI, one relationship per IRI-object triple,
numeric literals as float properties, RDF 1.2 reifier confidences as relationship
properties. Instance nodes additionally receive their class and all superclasses
as labels (write-time multi-label materialization). ``[:type]``/``[:subClassOf]``
edges are kept as well, so query-time inference remains possible.
"""

from __future__ import annotations

import shutil
from collections import defaultdict
from pathlib import Path

import pyoxigraph as ox

from common import EX, PREFIXES, RDF, RDFS, TRIG_PATH, local, synthetic_rows, timed

TBOX_G = ox.NamedNode(f"{EX}TBox_Graph")
ABOX_G = ox.NamedNode(f"{EX}ABox_Graph")
INF_G = ox.NamedNode(f"{EX}Inferred_Graph")
RDF_TYPE = ox.NamedNode(f"{RDF}type")

MATERIALIZE_SPARQL = PREFIXES + """
INSERT { GRAPH ex:Inferred_Graph { ?x a ?sup } }
WHERE  { GRAPH ex:ABox_Graph { ?x a ?c } GRAPH ex:TBox_Graph { ?c rdfs:subClassOf+ ?sup } }
"""


def parse_trig(path: Path = TRIG_PATH) -> list:
    return list(ox.parse(path=str(path), format=ox.RdfFormat.TRIG))


# --------------------------------------------------------------------------- Oxigraph
def load_oxigraph(db_path: Path, n_synthetic: int) -> tuple[ox.Store, dict]:
    if db_path.exists():
        shutil.rmtree(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = ox.Store(path=str(db_path))
    timings = {}
    _, timings["load_trig_ms"] = timed(store.load, path=str(TRIG_PATH), format=ox.RdfFormat.TRIG)
    if n_synthetic:
        quads = [
            ox.Quad(ox.NamedNode(f"{EX}{i}"), RDF_TYPE, ox.NamedNode(f"{EX}{c}"), ABOX_G)
            for i, c in synthetic_rows(n_synthetic)
        ]
        _, timings["load_synthetic_ms"] = timed(store.extend, quads)
    _, timings["materialize_ms"] = timed(store.update, MATERIALIZE_SPARQL)
    _, timings["flush_ms"] = timed(store.flush)
    return store, timings


# --------------------------------------------------------------------------- Neo4j
def _neo4j_rows(quads):
    """Convert parsed quads into node/edge/property/label rows."""
    nodes = set()
    edges = defaultdict(list)          # rel type -> [{s, o}]
    props = defaultdict(list)          # prop name -> [{id, v}]
    confidences = {}                   # (s, p, o) -> float
    reifies = {}                       # reifier bnode -> (s, p, o)
    reifier_props = defaultdict(dict)  # reifier bnode -> {prop: value}
    sub_class = defaultdict(set)
    inst_types = defaultdict(set)

    for q in quads:
        s, p, o = q.subject, q.predicate, q.object
        if isinstance(s, ox.BlankNode):
            if p.value == f"{RDF}reifies" and isinstance(o, ox.Triple):
                reifies[s.value] = (local(o.subject.value), local(o.predicate.value), local(o.object.value))
            elif isinstance(o, ox.Literal):
                reifier_props[s.value][local(p.value)] = float(o.value)
            continue
        sid, pid = local(s.value), local(p.value)
        nodes.add(sid)
        if isinstance(o, ox.NamedNode):
            oid = local(o.value)
            nodes.add(oid)
            edges[pid].append({"s": sid, "o": oid})
            if p.value == f"{RDFS}subClassOf":
                sub_class[sid].add(oid)
            if p.value == f"{RDF}type" and q.graph_name == ABOX_G:
                inst_types[sid].add(oid)
        elif isinstance(o, ox.Literal):
            try:
                val = float(o.value)
            except ValueError:
                val = o.value
            props[pid].append({"id": sid, "v": val})

    for bnode, spo in reifies.items():
        for k, v in reifier_props[bnode].items():
            confidences[(spo, k)] = v
    return nodes, edges, props, confidences, sub_class, inst_types


def ancestors(cls: str, sub_class: dict) -> set:
    out, stack = set(), [cls]
    while stack:
        for sup in sub_class.get(stack.pop(), ()):
            if sup not in out:
                out.add(sup)
                stack.append(sup)
    return out


def _run(session, cypher, **params):
    return session.run(cypher, **params).consume()


def load_neo4j(driver, n_synthetic: int, batch: int = 5000) -> dict:
    quads = parse_trig()
    nodes, edges, props, confidences, sub_class, inst_types = _neo4j_rows(quads)
    timings = {}

    def _load():
        with driver.session() as s:
            _run(s, "CREATE CONSTRAINT resource_id IF NOT EXISTS FOR (n:Resource) REQUIRE n.id IS UNIQUE")
            _run(s, "UNWIND $ids AS id CREATE (:Resource {id: id})", ids=sorted(nodes))
            for pname, rows in props.items():
                _run(s, f"UNWIND $rows AS r MATCH (n:Resource {{id: r.id}}) SET n.`{pname}` = r.v", rows=rows)
            for rel, rows in edges.items():
                for r in rows:
                    r["conf"] = confidences.get(((r["s"], rel, r["o"]), "confidence"))
                _run(s, f"""UNWIND $rows AS r
                    MATCH (a:Resource {{id: r.s}}), (b:Resource {{id: r.o}})
                    CREATE (a)-[e:`{rel}`]->(b)
                    FOREACH (_ IN CASE WHEN r.conf IS NULL THEN [] ELSE [1] END | SET e.confidence = r.conf)""",
                     rows=rows)
            # Write-time multi-label materialization for instance nodes.
            by_labels = defaultdict(list)
            for inst, types in inst_types.items():
                labels = set(types)
                for t in types:
                    labels |= ancestors(t, sub_class)
                labels = {lb for lb in labels if lb in sub_class or lb in {v for vs in sub_class.values() for v in vs}}
                if labels:
                    by_labels[tuple(sorted(labels))].append(inst)
            for labels, ids in by_labels.items():
                lbl = ":".join(f"`{x}`" for x in labels)
                _run(s, f"UNWIND $ids AS id MATCH (n:Resource {{id: id}}) SET n:{lbl}", ids=ids)

    _, timings["load_base_ms"] = timed(_load)

    if n_synthetic:
        def _syn():
            rows_by_cls = defaultdict(list)
            for i, c in synthetic_rows(n_synthetic):
                rows_by_cls[c].append(i)
            with driver.session() as s:
                for cls, ids in rows_by_cls.items():
                    lbl = ":".join(f"`{x}`" for x in sorted({cls} | ancestors(cls, sub_class)))
                    for k in range(0, len(ids), batch):
                        _run(s, f"""MATCH (c:Resource {{id: $cls}})
                            UNWIND $ids AS id CREATE (n:Resource:{lbl} {{id: id}})-[:type]->(c)""",
                             cls=cls, ids=ids[k:k + batch])
        _, timings["load_synthetic_ms"] = timed(_syn)
    return timings


def wipe_neo4j(driver):
    with driver.session() as s:
        _run(s, "MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS")
