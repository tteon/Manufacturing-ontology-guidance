"""Build the v2 connected fab graph, start + load both servers, and validate every pooled request
against the generator's ground truth before any load is applied.

Usage: uv run python loadtest/setup.py [--lines 1580] [--pool 200]
Writes loadtest/work/L<lines>/{kg.trig, pools.json, setup.json}.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx
import pyoxigraph as ox
from neo4j import GraphDatabase

sys.path.insert(0, str(Path(__file__).parent))
import servers  # noqa: E402
from ops import canon_rows, correct, cypher_for, make_pools, sparql_for  # noqa: E402
from common import generate_connected_fab_graph  # noqa: E402  (path added by ops)
from loader import load_neo4j  # noqa: E402

WORK = Path(__file__).parent / "work"


def validate(pools) -> dict:
    report = {}
    with httpx.Client() as cl, GraphDatabase.driver(servers.NEO_URI, auth=servers.NEO_AUTH) as d, d.session() as s:
        for op, key in (("S1", "S1"), ("S2", "S2"), ("S3", "S3"), ("Cq", "C"), ("Cm", "C")):
            wrong, t = {"oxigraph": 0, "neo4j": 0}, {"oxigraph": 0.0, "neo4j": 0.0}
            for p in pools[key]:
                t0 = time.perf_counter()
                r = cl.post(f"{servers.OXI_URL}/query", data={"query": sparql_for(op, p)},
                            headers={"Accept": "application/sparql-results+json"}, timeout=600)
                r.raise_for_status()
                got = canon_rows(op, r.json()["results"]["bindings"])
                t["oxigraph"] += time.perf_counter() - t0
                wrong["oxigraph"] += not correct(op, got, p["truth"])
                t0 = time.perf_counter()
                q, params = cypher_for(op, p)
                got = canon_rows(op, s.run(q, **params).data())
                t["neo4j"] += time.perf_counter() - t0
                wrong["neo4j"] += not correct(op, got, p["truth"])
            report[op] = {"n": len(pools[key]), "wrong": wrong,
                          "serial_mean_ms": {k: round(1000 * v / len(pools[key]), 2) for k, v in t.items()}}
            print(op, report[op], flush=True)
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lines", type=int, default=1580)
    ap.add_argument("--pool", type=int, default=200)
    args = ap.parse_args()
    out = WORK / f"L{args.lines}"
    out.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    kg = generate_connected_fab_graph(args.lines)
    pools = make_pools(kg, args.pool)
    (out / "pools.json").write_text(json.dumps(pools))
    trig = out / "kg.trig"
    ox.serialize(kg.all_quads(), output=str(trig), format=ox.RdfFormat.TRIG)
    setup = {"lines": args.lines, "stats": kg.stats(), "build_s": round(time.time() - t0, 2),
             "trig_bytes": trig.stat().st_size, "server_cpus": servers.SERVER_CPUS,
             "client_cpus": servers.CLIENT_CPUS, "memory": servers.MEMORY,
             "images": {"oxigraph": servers.OXI_IMAGE, "neo4j": servers.NEO_IMAGE}}
    setup["oxigraph_load"] = servers.start_oxigraph(trig, fresh=True)
    servers.start_neo4j(fresh=True)
    with GraphDatabase.driver(servers.NEO_URI, auth=servers.NEO_AUTH) as d:
        setup["neo4j_load"] = load_neo4j(d, kg)
    print(json.dumps(setup, indent=1), flush=True)
    setup["validation"] = validate(pools)
    (out / "setup.json").write_text(json.dumps(setup, indent=1))
    ok = all(sum(v["wrong"].values()) == 0 for v in setup["validation"].values())
    print("ALL CORRECT" if ok else "MISMATCHES FOUND")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
