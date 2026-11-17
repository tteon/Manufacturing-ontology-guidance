"""Storage-level CRUD study (how each engine persists and mutates TBox/ABox).

Sub-commands
------------
n10s        Load the same graph into Neo4j through neosemantics (n10s 5.26) with
            handleRDFTypes = LABELS and NODES, plus LABELS with write-time superclass labels
            ("LABELS+mat"), and into embedded PyOxigraph (query-time and materialized).
            Measures import, class counts (hierarchy-aware), TBox evolution, reclassification of
            500 instances (type change), and class deletion. Every count is checked vs ground truth.
s6          Append-only sensor time series: every reading is a NEW sosa:Observation with
            sosa:resultTime. Variant A: latest value by sort; variant B: append + `ex:latestObservation`
            pointer that is moved on every reading. Tracks append latency, on-disk growth, latest-value
            query cost and per-line "latest reading exceeds spec" (checked vs ground truth).
compaction  Repeated overwrite rounds (S5 batch, DELETE+INSERT in RDF / in-place SET in Neo4j) and
            what they leave on disk: Oxigraph SST/WAL bytes and SST count, read latency, before and
            after flush()+optimize(); Neo4j store and transaction-log bytes before/after checkpoint.

Usage: uv run python src/exp_storage.py {n10s|s6|compaction} [--lines N] ...
Outputs: results/storage/<cmd>_<UTC>.json and a Markdown table printed to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import statistics
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pyoxigraph as ox
from neo4j import GraphDatabase

from common import (DATA, EX, PREFIXES, RESULTS, TRIG_PATH, generate_connected_fab_graph, safe_id, timed)
from engine_neo4j import Neo4jEngine
from engine_oxigraph import OxigraphEngine
from queries import SPARQL

OUT = RESULTS / "storage"
N10S_NAME, N10S_BOLT, N10S_HTTP = "mfg-n10s-neo4j", 27687, 27474
N10S_URI, AUTH = f"bolt://127.0.0.1:{N10S_BOLT}", ("neo4j", "password123")
PLUGINS = Path(__file__).resolve().parent.parent / "plugins"


def sh(*a, check=True):
    return subprocess.run(list(a), check=check, capture_output=True, text=True)


def med(xs):
    return round(statistics.median(xs), 3) if xs else None


def du(container: str, *paths: str) -> dict:
    out = sh("docker", "exec", container, "du", "-sb", *paths).stdout
    return {line.split()[1]: int(line.split()[0]) for line in out.strip().splitlines()}


def dir_stats(path: Path) -> dict:
    files = [f for f in path.rglob("*") if f.is_file()]
    return {"bytes": sum(f.stat().st_size for f in files),
            "sst_files": sum(1 for f in files if f.suffix == ".sst"),
            "sst_bytes": sum(f.stat().st_size for f in files if f.suffix == ".sst"),
            "wal_bytes": sum(f.stat().st_size for f in files if f.suffix == ".log")}


def save(cmd: str, report: dict) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"{cmd}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    p.write_text(json.dumps(report, indent=1, default=str))
    return p


# ============================================================================ n10s
def try_checkpoint(s) -> str:
    """CALL db.checkpoint() exists only in Neo4j Enterprise; Community checkpoints on its own schedule."""
    try:
        t0 = time.perf_counter()
        s.run("CALL db.checkpoint()").consume()
        return f"ok {round((time.perf_counter() - t0) * 1000, 1)} ms"
    except Exception as e:  # noqa: BLE001
        return f"unavailable ({type(e).__name__}: Community edition)"


def turtle_star(kg) -> str:
    """ABox as Turtle-star (RDF-star syntax understood by RDF4J/n10s): plain triples plus
    `<< s p o >> ex:confidence v` annotations. Inferred types are NOT included."""
    xs = "<http://www.w3.org/2001/XMLSchema#double>"
    lines = [f"@prefix ex: <{EX}> .", "@prefix sosa: <http://www.w3.org/ns/sosa/> ."]
    for i, t in kg.types.items():
        lines.append(f"ex:{i} a ex:{t} .")
    for a, b in kg.part_of.items():
        lines.append(f"ex:{a} ex:partOf ex:{b} .")
    for a, b in kg.feeds.items():
        lines.append(f"ex:{a} ex:feedsInto ex:{b} .")
    for lot, chs in kg.processed.items():
        lines += [f"ex:{lot} ex:processedIn ex:{c} ." for c in chs]
    for lot, syms in kg.lot_symptoms.items():
        lines += [f"ex:{lot} ex:hasSymptom ex:{s} ." for s in syms]
    for c, d in kg.defect.items():
        lines.append(f"ex:{c} ex:hasDefect ex:{d} .")
    for o, (part, spec, v) in kg.obs.items():
        lines.append(f'ex:{o} a sosa:Observation ; sosa:hasFeatureOfInterest ex:{part} ; '
                     f'sosa:observedProperty ex:{spec} ; sosa:hasSimpleResult "{v!r}"^^{xs} .')
    for sym, lst in kg.caused_by.items():
        for cause, conf in lst:
            lines.append(f"ex:{sym} ex:causedBy ex:{cause} .")
            lines.append(f'<< ex:{sym} ex:causedBy ex:{cause} >> ex:confidence "{conf!r}"^^{xs} .')
    for cause, (part, conf) in kg.attributed.items():
        lines.append(f"ex:{cause} ex:attributedTo ex:{part} .")
        lines.append(f'<< ex:{cause} ex:attributedTo ex:{part} >> ex:confidence "{conf!r}"^^{xs} .')
    return "\n".join(lines)


def tbox_turtle() -> str:
    q = [x for x in ox.parse(path=str(TRIG_PATH), format=ox.RdfFormat.TRIG)]
    triples = [ox.Triple(x.subject, x.predicate, x.object) for x in q]
    return ox.serialize(triples, format=ox.RdfFormat.TURTLE).decode()


def start_n10s():
    sh("docker", "rm", "-f", N10S_NAME, check=False)
    cpus = ["--cpuset-cpus", os.environ["MFG_STORAGE_CPUS"]] if os.getenv("MFG_STORAGE_CPUS") else []
    sh("docker", "run", "-d", "--name", N10S_NAME, *cpus, "-p", f"127.0.0.1:{N10S_BOLT}:7687",
       "-p", f"127.0.0.1:{N10S_HTTP}:7474", "-e", f"NEO4J_AUTH={AUTH[0]}/{AUTH[1]}",
       "-e", "NEO4J_dbms_security_procedures_unrestricted=n10s.*",
       "-e", "NEO4J_server_memory_heap_max__size=4G", "-e", "NEO4J_server_memory_pagecache_size=2G",
       "-v", f"{PLUGINS}:/plugins:ro", "neo4j:5.26-community")
    for _ in range(120):
        try:
            with GraphDatabase.driver(N10S_URI, auth=AUTH) as d:
                d.verify_connectivity()
                return
        except Exception:  # noqa: BLE001
            time.sleep(1)
    raise RuntimeError("n10s container not ready")


def n10s_session(d):
    return d.session()


def n10s_reset(s, mode: str):
    s.run("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 20000 ROWS").consume()
    for c in s.run("SHOW CONSTRAINTS YIELD name RETURN name").values():
        s.run(f"DROP CONSTRAINT {c[0]}").consume()
    s.run("CREATE CONSTRAINT n10s_unique_uri IF NOT EXISTS FOR (r:Resource) REQUIRE r.uri IS UNIQUE").consume()
    s.run("CALL n10s.graphconfig.drop()").consume() if s.run(
        "MATCH (c:_GraphConfig) RETURN count(c) AS n").single()["n"] else None
    s.run("CALL n10s.graphconfig.init({handleVocabUris: 'IGNORE', handleRDFTypes: $m, keepLangTag: false, "
          "handleMultival: 'OVERWRITE'})", m=mode).consume()


def n10s_import(s, payload: str, fmt: str) -> dict:
    r = s.run("CALL n10s.rdf.import.inline($p, $f) YIELD terminationStatus, triplesLoaded, triplesParsed, extraInfo "
              "RETURN terminationStatus, triplesLoaded, triplesParsed, extraInfo", p=payload, f=fmt).single()
    return dict(r)


def U(local_name: str) -> str:
    return f"{EX}{safe_id(local_name)}"


def neo_count_by_class(s, mode: str, cls: str) -> int:
    """Hierarchy-aware instance count. LABELS: resolve subclass names via subClassOf edges, then label
    lookups; NODES: (x)-[:type]->()-[:subClassOf*0..]->(c); LABELS+mat: single label count."""
    if mode == "NODES":
        return s.run("MATCH (c:Resource {uri: $u})<-[:subClassOf*0..]-(t)<-[:type]-(x) RETURN count(DISTINCT x) AS n",
                     u=U(cls)).single()["n"]
    if mode == "LABELS+mat":
        return s.run(f"MATCH (x:`{safe_id(cls)}`) RETURN count(x) AS n").single()["n"]
    names = s.run("MATCH (c:Resource {uri: $u})<-[:subClassOf*0..]-(t) "
                  "RETURN collect(DISTINCT n10s.rdf.getIRILocalName(t.uri)) AS names", u=U(cls)).single()["names"]
    return s.run("MATCH (x:$any($names)) RETURN count(DISTINCT x) AS n", names=names).single()["n"]


def run_n10s(args):
    kg = generate_connected_fab_graph(args.lines)
    tbox, abox = tbox_turtle(), turtle_star(kg)
    rng = random.Random(1)
    reclass = sorted(rng.sample(sorted(i for i, t in kg.types.items() if t == "MFC"), args.reclass))
    classes = ["Part", "Module", "Equipment", "GasSupplyModule", "MFC"]
    truth = {c: kg.truth_count(c) for c in classes}
    truth_chs = len(kg.truth_s4())
    report = {"lines": args.lines, "stats": kg.stats(), "reclassified": len(reclass), "modes": {}}

    start_n10s()
    with GraphDatabase.driver(N10S_URI, auth=AUTH) as d, d.session() as s:
        report["n10s_jar"] = "neosemantics-5.26.0.jar"
        for mode in ("LABELS", "NODES", "LABELS+mat"):
            base = "LABELS" if mode == "LABELS+mat" else mode
            n10s_reset(s, base)
            r = {}
            _, r["import_tbox_ms"] = timed(n10s_import, s, tbox, "Turtle")
            info, r["import_abox_ms"] = timed(n10s_import, s, abox, "Turtle-star")
            r["import_info"] = info
            if mode == "LABELS+mat":   # write-time superclass labels (what our own loader does)
                t0 = time.perf_counter()
                for cls in sorted(set(kg.types.values())):
                    sup = sorted(a for a in kg.tbox.ancestors(cls) if a != "Resource")
                    if sup:
                        s.run(f"MATCH (x:`{cls}`) SET x:" + ":".join(f"`{a}`" for a in sup)).consume()
                r["materialize_superclass_labels_ms"] = round((time.perf_counter() - t0) * 1000, 2)
            r["checkpoint"] = try_checkpoint(s)
            r["nodes"] = s.run("MATCH (n) RETURN count(n) AS n").single()["n"]
            r["rels"] = s.run("MATCH ()-[x]->() RETURN count(x) AS n").single()["n"]
            r["labels_total"] = s.run("MATCH (n) RETURN sum(size(labels(n))) AS n").single()["n"]
            r["store_bytes"] = du(N10S_NAME, "/data/databases/neo4j", "/data/transactions/neo4j")
            # confidences imported as relationship properties?
            r["causedBy_with_confidence"] = s.run(
                "MATCH ()-[x:causedBy]->() RETURN count(x) AS n, count(x.confidence) AS c").single().data()
            # hierarchy-aware counts
            counts, lat = {}, {}
            for c in classes:
                neo_count_by_class(s, mode, c)  # warm
                samples = []
                for _ in range(5):
                    n, dt = timed(neo_count_by_class, s, mode, c)
                    samples.append(dt)
                counts[c], lat[c] = n, med(samples)
            r["count_correct"] = all(counts[c] == truth[c] for c in classes)
            r["count_ms"] = lat
            # TBox evolution: new parent class over GasSupplyModule + PlasmaModule
            evo = """MERGE (p:Resource {uri: $p}) WITH p
                     MATCH (c:Resource) WHERE c.uri IN $subs MERGE (c)-[:subClassOf]->(p)"""
            _, r["s4_tbox_write_ms"] = timed(lambda: s.run(evo, p=U("CriticalHazardSystem"),
                                                           subs=[U("GasSupplyModule"), U("PlasmaModule")]).consume())
            if mode == "LABELS+mat":
                summ, r["s4_label_migration_ms"] = timed(lambda: s.run(
                    "MATCH (x) WHERE x:GasSupplyModule OR x:PlasmaModule SET x:CriticalHazardSystem").consume())
                r["s4_labels_added"] = summ.counters.labels_added
            n, r["s4_count_ms"] = timed(neo_count_by_class, s, mode, "CriticalHazardSystem")
            r["s4_count_correct"] = n == truth_chs
            # Reclassification: 500 MFC instances become MFC_Gen2 (new subclass of MFC)
            s.run("MERGE (g:Resource {uri: $g}) WITH g MATCH (m:Resource {uri: $m}) MERGE (g)-[:subClassOf]->(m)",
                  g=U("MFC_Gen2"), m=U("MFC")).consume()
            uris = [U(i) for i in reclass]

            def neo_reclass(old, new):
                if mode == "NODES":
                    q = """UNWIND $uris AS u MATCH (x:Resource {uri: u})-[t:type]->(:Resource {uri: $old})
                           MATCH (g:Resource {uri: $new}) DELETE t CREATE (x)-[:type]->(g)"""
                    return s.run(q, uris=uris, old=U(old), new=U(new)).consume()
                if mode == "LABELS":
                    q = f"UNWIND $uris AS u MATCH (x:Resource {{uri: u}}) REMOVE x:`{old}` SET x:`{new}`"
                elif new == "MFC_Gen2":      # LABELS+mat: MFC stays as inferred superclass label
                    q = "UNWIND $uris AS u MATCH (x:Resource {uri: u}) SET x:MFC_Gen2"
                else:
                    q = "UNWIND $uris AS u MATCH (x:Resource {uri: u}) REMOVE x:MFC_Gen2"
                return s.run(q, uris=uris).consume()

            fwd, back = [], []
            for _ in range(3):   # forward/back cycles, then a final forward for the correctness check
                fwd.append(timed(neo_reclass, "MFC", "MFC_Gen2")[1])
                back.append(timed(neo_reclass, "MFC_Gen2", "MFC")[1])
            summ, last = timed(neo_reclass, "MFC", "MFC_Gen2")
            fwd.append(last)
            r["reclassify_ms_samples"] = {"forward": [round(x, 1) for x in fwd], "back": [round(x, 1) for x in back]}
            r["reclassify_ms"] = med(fwd)
            c = summ.counters
            r["reclassify_writes"] = {"labels_added": c.labels_added, "labels_removed": c.labels_removed,
                                      "rels_deleted": c.relationships_deleted, "rels_created": c.relationships_created}
            if mode == "NODES":
                r["mfc_class_node_degree"] = s.run("MATCH (:Resource {uri: $u})<-[t:type]-() RETURN count(t) AS n",
                                                   u=U("MFC_Gen2")).single()["n"] + truth["MFC"] - len(reclass)
            r["after_reclass_counts_correct"] = (neo_count_by_class(s, mode, "MFC") == truth["MFC"]
                                                 and neo_count_by_class(s, mode, "MFC_Gen2") == len(reclass))
            # Class deletion (TBox removal of CriticalHazardSystem)
            if mode == "LABELS+mat":
                summ, r["class_delete_ms"] = timed(lambda: s.run(
                    "MATCH (x:CriticalHazardSystem) REMOVE x:CriticalHazardSystem WITH count(*) AS c "
                    "MATCH (p:Resource {uri: $p}) DETACH DELETE p", p=U("CriticalHazardSystem")).consume())
                r["class_delete_writes"] = {"labels_removed": summ.counters.labels_removed,
                                            "rels_deleted": summ.counters.relationships_deleted}
            else:
                summ, r["class_delete_ms"] = timed(lambda: s.run(
                    "MATCH (p:Resource {uri: $p}) DETACH DELETE p", p=U("CriticalHazardSystem")).consume())
                r["class_delete_writes"] = {"rels_deleted": summ.counters.relationships_deleted}
            report["modes"][f"n10s {mode}"] = r
            print(mode, {k: v for k, v in r.items() if k not in ("import_info", "count_ms")}, flush=True)

    # PyOxigraph equivalents (query-time and materialized)
    for strategy in ("query_time", "materialized"):
        eng = OxigraphEngine(DATA / f"oxigraph_rocksdb_n10s_{strategy}")
        r = {}
        r["import_ms"] = eng.load(kg)["load_ms"]
        st = eng.store
        cq = "count_by_class_query_time" if strategy == "query_time" else "count_by_class_materialized"
        counts, lat = {}, {}
        for c in classes:
            eng.q(cq, cls=c)
            samples = []
            for _ in range(5):
                n, dt = timed(eng.q, cq, cls=c)
                samples.append(dt)
            counts[c], lat[c] = n, med(samples)
        r["count_correct"] = all(counts[c] == truth[c] for c in classes)
        r["count_ms"] = lat
        w = eng.s4_evolve(strategy, "CriticalHazardSystem", ["GasSupplyModule", "PlasmaModule"], "Policy")
        r["s4_tbox_write_ms"], r["s4_migration_ms"], r["s4_flush_ms"] = w["tbox_write_ms"], w["abox_migration_ms"], w["flush_ms"]
        r["s4_facts_written"] = w["abox_facts_written"]
        r["s4_count_correct"] = eng.s4_count(strategy, "CriticalHazardSystem") == truth_chs
        st.update(PREFIXES + "INSERT DATA { GRAPH ex:TBox_Graph { ex:MFC_Gen2 rdfs:subClassOf ex:MFC } }")
        vals = " ".join(f"ex:{i}" for i in reclass)
        upd = PREFIXES + f"""DELETE {{ GRAPH ex:ABox_Graph {{ ?x a ex:MFC }} }}
INSERT {{ GRAPH ex:ABox_Graph {{ ?x a ex:MFC_Gen2 }} }} WHERE {{ VALUES ?x {{ {vals} }} GRAPH ex:ABox_Graph {{ ?x a ex:MFC }} }}"""
        if strategy == "materialized":   # MFC becomes an inferred superclass type
            upd += f""" ;
INSERT {{ GRAPH ex:Inferred_Graph {{ ?x a ex:MFC }} }} WHERE {{ VALUES ?x {{ {vals} }} }}"""
        back_upd = PREFIXES + f"""DELETE {{ GRAPH ex:ABox_Graph {{ ?x a ex:MFC_Gen2 }} }}
INSERT {{ GRAPH ex:ABox_Graph {{ ?x a ex:MFC }} }} WHERE {{ VALUES ?x {{ {vals} }} GRAPH ex:ABox_Graph {{ ?x a ex:MFC_Gen2 }} }}"""
        if strategy == "materialized":
            back_upd += f""" ;
DELETE {{ GRAPH ex:Inferred_Graph {{ ?x a ex:MFC }} }} WHERE {{ VALUES ?x {{ {vals} }} GRAPH ex:Inferred_Graph {{ ?x a ex:MFC }} }}"""
        fwd, back, fl = [], [], []
        for _ in range(3):
            fwd.append(timed(st.update, upd)[1])
            fl.append(timed(st.flush)[1])
            back.append(timed(st.update, back_upd)[1])
            fl.append(timed(st.flush)[1])
        before = len(st)
        fwd.append(timed(st.update, upd)[1])
        r["reclassify_quad_delta"] = len(st) - before
        fl.append(timed(st.flush)[1])
        r["reclassify_ms_samples"] = {"forward": [round(x, 1) for x in fwd], "back": [round(x, 1) for x in back]}
        r["reclassify_ms"] = med(fwd)
        r["reclassify_flush_ms"] = med(fl)
        r["after_reclass_counts_correct"] = (eng.q(cq, cls="MFC") == truth["MFC"]
                                             and eng.q(cq, cls="MFC_Gen2") == len(reclass))
        before = len(st)
        _, r["class_delete_ms"] = timed(eng.s4_revert, "CriticalHazardSystem")
        r["class_delete_quad_delta"] = len(st) - before
        r["store"] = dir_stats(DATA / f"oxigraph_rocksdb_n10s_{strategy}")
        report["modes"][f"PyOxigraph {strategy}"] = r
        print(strategy, {k: v for k, v in r.items() if k != "count_ms"}, flush=True)
        eng.close()
        del st
    return report


# ============================================================================ S6 time series
def run_s6(args):
    """Append-only readings. Ground truth = Python reading history."""
    out = {"lines": args.lines, "rounds": args.rounds, "engines": {}}
    for eng_name in ("PyOxigraph", "Neo4j"):
        for variant in ("A_sort", "B_pointer"):
            kg = generate_connected_fab_graph(args.lines)
            parts = sorted(p for p, _, _ in kg.obs.values())
            spec_of = {p: s for p, s, _ in kg.obs.values()}
            thr = kg.tbox.threshold
            latest = {p: v for p, _, v in kg.obs.values()}       # round 0 = generator readings
            rng = random.Random(42)
            probe = rng.sample(parts, 20)
            lines_probe = rng.sample(kg.lines, min(5, len(kg.lines)))
            if eng_name == "PyOxigraph":
                eng = OxigraphEngine(DATA / f"oxigraph_rocksdb_s6_{variant}")
            else:
                eng = Neo4jEngine("bolt://127.0.0.1:7687", "neo4j", "password123", "mfg-bench-neo4j")
            eng.load(kg)
            if eng_name == "Neo4j":
                with eng.driver.session() as s:
                    s.run("CREATE INDEX obs_time IF NOT EXISTS FOR (o:Observation) ON (o.resultTime)").consume()
                    s.run("MATCH (o:Resource)-[:type]->(:Resource {id: 'Observation'}) SET o:Observation, o.resultTime = 0").consume()
                    if variant == "B_pointer":
                        s.run("MATCH (o:Observation)-[:hasFeatureOfInterest]->(p) CREATE (p)-[:latestObservation]->(o)").consume()
            else:
                upd = PREFIXES + """INSERT { GRAPH ex:ABox_Graph { ?o sosa:resultTime "1970-01-01T00:00:00Z"^^xsd:dateTime } }
WHERE { GRAPH ex:ABox_Graph { ?o a sosa:Observation } }"""
                eng.store.update(upd)
                if variant == "B_pointer":
                    eng.store.update(PREFIXES + """INSERT { GRAPH ex:ABox_Graph { ?p ex:latestObservation ?o } }
WHERE { GRAPH ex:ABox_Graph { ?o sosa:hasFeatureOfInterest ?p } }""")
                eng.store.flush()
            series = []
            appends = []
            for rnd in range(1, args.rounds + 1):
                ts = f"1970-01-01T{rnd // 3600:02d}:{rnd // 60 % 60:02d}:{rnd % 60:02d}Z"
                rows = []
                for p in parts:
                    t = thr[spec_of[p]]
                    v = round(max(0.0, rng.gauss(0.6 * t, 0.25 * t)), 3)
                    rows.append((f"Obs_{p}_r{rnd}", p, spec_of[p], v))
                    latest[p] = v
                if eng_name == "PyOxigraph":
                    data = " ".join(
                        f'ex:{o} a sosa:Observation ; sosa:hasFeatureOfInterest ex:{p} ; sosa:observedProperty ex:{sp} ; '
                        f'sosa:hasSimpleResult "{v!r}"^^xsd:double ; sosa:resultTime "{ts}"^^xsd:dateTime .'
                        for o, p, sp, v in rows)
                    q = PREFIXES + "INSERT DATA { GRAPH ex:ABox_Graph { " + data + " } }"
                    if variant == "B_pointer":
                        vals = " ".join(f"(ex:{p} ex:{o})" for o, p, _, _ in rows)
                        q += f""" ;
DELETE {{ GRAPH ex:ABox_Graph {{ ?p ex:latestObservation ?old }} }} INSERT {{ GRAPH ex:ABox_Graph {{ ?p ex:latestObservation ?new }} }}
WHERE {{ VALUES (?p ?new) {{ {vals} }} OPTIONAL {{ GRAPH ex:ABox_Graph {{ ?p ex:latestObservation ?old }} }} }}"""
                    _, dt = timed(eng.store.update, q)
                else:
                    q = """UNWIND $rows AS r MATCH (p:Resource {id: r.p}), (sp:Resource {id: r.sp}), (c:Resource {id: 'Observation'})
CREATE (o:Resource:Observation {id: r.o, hasSimpleResult: r.v, resultTime: $t})-[:hasFeatureOfInterest]->(p),
       (o)-[:observedProperty]->(sp), (o)-[:type]->(c)"""
                    if variant == "B_pointer":
                        q += """ WITH p, o OPTIONAL MATCH (p)-[l:latestObservation]->() DELETE l CREATE (p)-[:latestObservation]->(o)"""
                    _, dt = timed(eng._write, q, rows=[{"o": o, "p": p, "sp": sp, "v": v} for o, p, sp, v in rows], t=rnd)
                appends.append(dt)
                if rnd % args.every == 0 or rnd == 1:
                    eng.flush()
                    point = {"round": rnd, "observations": len(parts) * (rnd + 1),
                             "append_batch_ms_median_recent": med(appends[-args.every:]),
                             "disk_bytes": eng.disk_bytes()}
                    # latest value per part
                    lat, ok = [], True
                    for p in probe:
                        if eng_name == "PyOxigraph":
                            if variant == "A_sort":
                                q2 = PREFIXES + f"""SELECT ?v WHERE {{ GRAPH ex:ABox_Graph {{ ?o sosa:hasFeatureOfInterest ex:{p} ;
  sosa:resultTime ?t ; sosa:hasSimpleResult ?v }} }} ORDER BY DESC(?t) LIMIT 1"""
                            else:
                                q2 = PREFIXES + f"""SELECT ?v WHERE {{ GRAPH ex:ABox_Graph {{ ex:{p} ex:latestObservation ?o .
  ?o sosa:hasSimpleResult ?v }} }}"""
                            res, dt = timed(lambda: list(eng.store.query(q2)))
                            got = float(res[0]["v"].value) if res else None
                        else:
                            if variant == "A_sort":
                                q2 = """MATCH (:Resource {id: $p})<-[:hasFeatureOfInterest]-(o:Observation)
RETURN o.hasSimpleResult AS v ORDER BY o.resultTime DESC LIMIT 1"""
                            else:
                                q2 = "MATCH (:Resource {id: $p})-[:latestObservation]->(o) RETURN o.hasSimpleResult AS v"
                            res, dt = timed(eng._rows, q2, p=p)
                            got = res[0]["v"] if res else None
                        lat.append(dt)
                        ok &= got == latest[p]
                    point["latest_value_ms_median"] = med(lat)
                    point["latest_value_correct"] = ok
                    # per-line: parts whose LATEST reading exceeds spec
                    lat3, ok3 = [], True
                    for line in lines_probe:
                        truth = sorted(p for p in parts if line in kg.up(p) and latest[p] > thr[spec_of[p]])
                        if eng_name == "PyOxigraph":
                            if variant == "A_sort":
                                q3 = PREFIXES + f"""SELECT DISTINCT ?p WHERE {{
  {{ SELECT ?p (MAX(?t) AS ?mt) WHERE {{ GRAPH ex:ABox_Graph {{ ?p ex:partOf+ ex:{line} .
       ?o sosa:hasFeatureOfInterest ?p ; sosa:resultTime ?t }} }} GROUP BY ?p }}
  GRAPH ex:ABox_Graph {{ ?o sosa:hasFeatureOfInterest ?p ; sosa:resultTime ?mt ; sosa:hasSimpleResult ?v ;
       sosa:observedProperty ?spec }}
  GRAPH ex:TBox_Graph {{ ?spec ex:maxAllowedDrift ?max }} FILTER(?v > ?max) }}"""
                            else:
                                q3 = PREFIXES + f"""SELECT DISTINCT ?p WHERE {{ GRAPH ex:ABox_Graph {{ ?p ex:partOf+ ex:{line} ;
  ex:latestObservation ?o . ?o sosa:hasSimpleResult ?v ; sosa:observedProperty ?spec }}
  GRAPH ex:TBox_Graph {{ ?spec ex:maxAllowedDrift ?max }} FILTER(?v > ?max) }}"""
                            res, dt = timed(lambda: list(eng.store.query(q3)))
                            got = sorted(r["p"].value.rsplit("#", 1)[1] for r in res)
                        else:
                            if variant == "A_sort":
                                q3 = """MATCH (:Resource {id: $line})<-[:partOf*1..]-(p)<-[:hasFeatureOfInterest]-(o:Observation)
WITH p, o ORDER BY o.resultTime DESC WITH p, head(collect(o)) AS o
MATCH (o)-[:observedProperty]->(spec) WHERE o.hasSimpleResult > spec.maxAllowedDrift RETURN p.id AS p"""
                            else:
                                q3 = """MATCH (:Resource {id: $line})<-[:partOf*1..]-(p)-[:latestObservation]->(o)-[:observedProperty]->(spec)
WHERE o.hasSimpleResult > spec.maxAllowedDrift RETURN p.id AS p"""
                            res, dt = timed(eng._rows, q3, line=line)
                            got = sorted(r["p"] for r in res)
                        lat3.append(dt)
                        ok3 &= got == truth
                    point["line_latest_exceed_ms_median"] = med(lat3)
                    point["line_latest_exceed_correct"] = ok3
                    series.append(point)
                    print(eng_name, variant, point, flush=True)
            out["engines"][f"{eng_name} {variant}"] = series
            eng.close()
    return out


# ============================================================================ compaction / tombstones
def run_compaction(args):
    out = {"lines": args.lines, "rounds": args.rounds, "batch": args.batch, "engines": {}}
    for eng_name in ("PyOxigraph", "Neo4j"):
        kg = generate_connected_fab_graph(args.lines)
        ids = sorted(kg.obs)
        rng = random.Random(9)
        if eng_name == "PyOxigraph":
            db = DATA / "oxigraph_rocksdb_compaction"
            eng = OxigraphEngine(db)
        else:
            eng = Neo4jEngine("bolt://127.0.0.1:7687", "neo4j", "password123", "mfg-bench-neo4j")
        eng.load(kg)
        eng.flush()

        def measure(label, rnd):
            p = {"phase": label, "round": rnd}
            if eng_name == "PyOxigraph":
                p.update(dir_stats(db))
            else:
                p.update({k.rsplit("/", 1)[-1] if "transactions" not in k else "tx_log_bytes": v
                          for k, v in du("mfg-bench-neo4j", "/data/databases/neo4j", "/data/transactions/neo4j").items()})
            probe = rng.sample(ids, 500)
            rd = [timed(eng.s5_read, probe)[1] for _ in range(5)]
            cnt = [timed(eng.s3_global_count)[1] for _ in range(3)]
            ok = eng.s3_global_count() == kg.truth_s3_global()
            if eng_name == "PyOxigraph":
                scan = [timed(lambda: list(eng.store.query(PREFIXES + "SELECT (COUNT(*) AS ?n) WHERE { GRAPH ex:ABox_Graph "
                                                                   "{ ?o sosa:hasSimpleResult ?v } }")))[1] for _ in range(3)]
            else:
                scan = [timed(eng._rows, "MATCH (o) WHERE o.hasSimpleResult IS NOT NULL RETURN count(o) AS n")[1] for _ in range(3)]
            p.update({"read_500_values_ms": med(rd), "s3_global_count_ms": med(cnt), "scan_all_values_ms": med(scan),
                      "s3_correct": ok})
            print(eng_name, p, flush=True)
            return p

        series = [measure("loaded", 0)]
        writes = []
        for rnd in range(1, args.rounds + 1):
            rows = []
            for o in rng.sample(ids, args.batch):
                part, spec, _ = kg.obs[o]
                t = kg.tbox.threshold[spec]
                v = round(max(0.0, rng.gauss(0.6 * t, 0.25 * t)), 3)
                rows.append((o, v))
                kg.obs[o][2] = v
            if eng_name == "PyOxigraph":
                _, dt = timed(eng.store.update, SPARQL["S5_batch"]({"rows": rows}))   # commit only, no flush
            else:
                dt = eng.s5_batch(rows)["write_ms"]
            writes.append(dt)
            if rnd % args.every == 0:
                pt = measure("after_updates", rnd)
                pt["write_ms_median_recent"] = med(writes[-args.every:])
                series.append(pt)
        if eng_name == "PyOxigraph":
            _, f = timed(eng.store.flush)
            series.append(measure("after_flush", args.rounds) | {"flush_ms": round(f, 1)})
            _, o = timed(eng.store.optimize)
            series.append(measure("after_optimize", args.rounds) | {"optimize_ms": round(o, 1)})
        else:
            with eng.driver.session() as s:
                ck = try_checkpoint(s)
            series.append(measure("after_checkpoint_attempt", args.rounds) | {"checkpoint": ck})
        out["engines"][eng_name] = series
        eng.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("n10s")
    a.add_argument("--lines", type=int, default=158)
    a.add_argument("--reclass", type=int, default=500)
    b = sub.add_parser("s6")
    b.add_argument("--lines", type=int, default=16)
    b.add_argument("--rounds", type=int, default=200)
    b.add_argument("--every", type=int, default=25)
    c = sub.add_parser("compaction")
    c.add_argument("--lines", type=int, default=158)
    c.add_argument("--rounds", type=int, default=200)
    c.add_argument("--batch", type=int, default=500)
    c.add_argument("--every", type=int, default=25)
    args = ap.parse_args()
    fn = {"n10s": run_n10s, "s6": run_s6, "compaction": run_compaction}[args.cmd]
    report = fn(args) | {"cmd": args.cmd, "args": vars(args)}
    print("saved", save(args.cmd, report))
    for d in DATA.glob("oxigraph_rocksdb*"):
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    main()
