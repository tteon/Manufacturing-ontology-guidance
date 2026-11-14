"""Run the full load-test matrix with randomized cell order per trial.

closed phase : engines x mixes x concurrency levels, `--trials` repetitions, order shuffled per trial.
open phase   : engines x {R, M} x offered rates (fractions of that engine's peak closed-loop throughput).
After every cell containing writes, an integrity check runs (duplicate sensor values on Oxigraph,
re-validation of all S3 answers on both engines); any duplicates are reported and repaired.

Outputs: results/loadtest/L<lines>_<UTC>/summary.jsonl (one line per cell) and raw samples
         under loadtest/work/L<lines>/raw_<UTC>/ (gitignored).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from neo4j import GraphDatabase

sys.path.insert(0, str(Path(__file__).parent))
import driver  # noqa: E402
import servers  # noqa: E402
from ops import canon_rows, correct, cypher_for, sparql_for  # noqa: E402
from common import PREFIXES as PFX  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def pct(sorted_vals, q):
    if not sorted_vals:
        return None
    return sorted_vals[min(len(sorted_vals) - 1, int(q * len(sorted_vals)))]


def summarize(res: dict) -> dict:
    s = res.pop("samples")
    ok = [x for x in s if x[3]]
    lat = sorted(x[2] for x in ok)
    per_op = {}
    for op in sorted({x[0] for x in s}):
        l = sorted(x[2] for x in ok if x[0] == op)
        per_op[op] = {"n": len(l), "p50": pct(l, .5), "p99": pct(l, .99),
                      "errors": sum(1 for x in s if x[0] == op and not x[3]),
                      "incorrect": sum(1 for x in s if x[0] == op and x[3] and not x[4])}
    errs = {}
    for x in s:
        if not x[3]:
            errs[x[5]] = errs.get(x[5], 0) + 1
    res.update({
        "requests": len(s), "ok": len(ok), "errors": len(s) - len(ok), "error_types": errs,
        "incorrect": sum(1 for x in ok if not x[4]),
        "throughput_rps": round(len(ok) / res["measure_s"], 2),
        "lat_ms": {"p50": pct(lat, .5), "p90": pct(lat, .9), "p99": pct(lat, .99), "p999": pct(lat, .999),
                   "max": lat[-1] if lat else None, "mean": round(sum(lat) / len(lat), 3) if lat else None},
        "per_op": per_op,
        "client_bound_suspect": res["client_cpu_cores"] > 0.7 * res["procs"],
    })
    return res, s


def integrity(engine: str, pools: dict) -> dict:
    out = {}
    if engine == "oxigraph":
        q = PFX + """SELECT ?o (COUNT(?v) AS ?n) WHERE { GRAPH ex:ABox_Graph { ?o sosa:hasSimpleResult ?v } }
GROUP BY ?o HAVING (COUNT(?v) > 1)"""
        r = httpx.post(f"{servers.OXI_URL}/query", data={"query": q},
                       headers={"Accept": "application/sparql-results+json"}, timeout=120).json()
        dups = [(b["o"]["value"].rsplit("#", 1)[1], int(b["n"]["value"])) for b in r["results"]["bindings"]]
        out["duplicate_value_observations"] = len(dups)
        out["duplicate_examples"] = dups[:5]
        for o, _ in dups:  # repair: keep exactly one (any) value
            vals = httpx.post(f"{servers.OXI_URL}/query", headers={"Accept": "application/sparql-results+json"},
                              data={"query": PFX + f"SELECT ?v WHERE {{ GRAPH ex:ABox_Graph {{ ex:{o} sosa:hasSimpleResult ?v }} }}"},
                              timeout=60).json()["results"]["bindings"]
            keep = vals[0]["v"]["value"]
            httpx.post(f"{servers.OXI_URL}/update", timeout=60, data={"update": PFX + f"""
DELETE {{ GRAPH ex:ABox_Graph {{ ex:{o} sosa:hasSimpleResult ?v }} }} WHERE {{ GRAPH ex:ABox_Graph {{ ex:{o} sosa:hasSimpleResult ?v }} }} ;
INSERT DATA {{ GRAPH ex:ABox_Graph {{ ex:{o} sosa:hasSimpleResult "{keep}"^^xsd:double }} }}"""}).raise_for_status()
        bad = 0
        for p in pools["S3"][:20]:
            r = httpx.post(f"{servers.OXI_URL}/query", data={"query": sparql_for("S3", p)},
                           headers={"Accept": "application/sparql-results+json"}, timeout=120).json()
            bad += not correct("S3", canon_rows("S3", r["results"]["bindings"]), p["truth"])
    else:
        bad = 0
        with GraphDatabase.driver(servers.NEO_URI, auth=servers.NEO_AUTH) as d, d.session() as s:
            for p in pools["S3"][:20]:
                q, params = cypher_for("S3", p)
                bad += not correct("S3", canon_rows("S3", s.run(q, **params).data()), p["truth"])
    out["s3_wrong_after"] = bad
    return out


def save_raw(raw_dir: Path, cell_id: str, samples):
    raw_dir.mkdir(parents=True, exist_ok=True)
    with gzip.open(raw_dir / f"{cell_id}.csv.gz", "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["op", "t_sched_s", "latency_ms", "ok", "correct", "error"])
        w.writerows(samples)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lines", type=int, default=1580)
    ap.add_argument("--engines", default="oxigraph,neo4j")
    ap.add_argument("--mixes", default="R,W,M,Whot")
    ap.add_argument("--conc", default="1,2,4,8,16,32,64,128")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--warmup", type=float, default=5)
    ap.add_argument("--measure", type=float, default=20)
    ap.add_argument("--open-fracs", default="0.25,0.5,0.75,0.9,1.0,1.1")
    ap.add_argument("--open-mixes", default="R,M")
    ap.add_argument("--phases", default="closed,open")
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--out-dir", default=None, help="reuse an existing results dir (e.g. open phase after closed)")
    args = ap.parse_args()

    work = Path(__file__).parent / "work" / f"L{args.lines}"
    pools = json.loads((work / "pools.json").read_text())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out_dir) if args.out_dir else ROOT / "results" / "loadtest" / f"L{args.lines}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = work / f"raw_{stamp}"
    (out_dir / f"config_{stamp}.json").write_text(json.dumps(vars(args) | {
        "server_cpus": servers.SERVER_CPUS, "client_cpus": servers.CLIENT_CPUS, "memory": servers.MEMORY,
        "images": [servers.OXI_IMAGE, servers.NEO_IMAGE]}, indent=1))
    summary = (out_dir / "summary.jsonl").open("a")
    rng = random.Random(args.seed)
    engines, mixes = args.engines.split(","), args.mixes.split(",")
    concs = [int(c) for c in args.conc.split(",")]

    def do(cell: dict, **kw):
        cell_id = "_".join(str(v) for v in cell.values())
        t0 = time.time()
        res = driver.run(cell["engine"], cell["mode"], cell["mix"], pools, warmup=args.warmup, measure=args.measure,
                         seed=rng.randrange(1 << 30), **kw)
        res, samples = summarize(res)
        res.update(cell)
        if cell["mix"] in ("W", "M", "Whot"):
            res["integrity"] = integrity(cell["engine"], pools)
        res["cell_wall_s"] = round(time.time() - t0, 1)
        save_raw(raw_dir, cell_id, samples)
        summary.write(json.dumps(res) + "\n")
        summary.flush()
        print(f"{cell_id:40s} thr={res['throughput_rps']:>9} p50={res['lat_ms']['p50']} p99={res['lat_ms']['p99']} "
              f"err={res['errors']} wrong={res['incorrect']} srvCPU={res['server_cpu_cores']} cliCPU={res['client_cpu_cores']}"
              + (f" integ={res['integrity']}" if "integrity" in res else ""), flush=True)
        time.sleep(2)
        return res

    peak = {}
    if "closed" in args.phases:
        for trial in range(1, args.trials + 1):
            cells = [(e, m, c) for e in engines for m in mixes for c in concs]
            rng.shuffle(cells)
            for e, m, c in cells:
                r = do({"phase": "closed", "trial": trial, "engine": e, "mode": "closed", "mix": m, "conc": c},
                       concurrency=c)
                key = (e, m)
                peak.setdefault(key, {}).setdefault(c, []).append(r["throughput_rps"])
        (out_dir / "closed_peaks.json").write_text(json.dumps(
            {f"{e}/{m}": max(sorted(v)[len(v) // 2] for v in d.values()) for (e, m), d in peak.items()}, indent=1))
    if "open" in args.phases:
        peaks = json.loads((out_dir / "closed_peaks.json").read_text()) if (out_dir / "closed_peaks.json").exists() else {}
        fracs = [float(f) for f in args.open_fracs.split(",")]
        for trial in range(1, args.trials + 1):
            cells = [(e, m, f) for e in engines for m in args.open_mixes.split(",") for f in fracs]
            rng.shuffle(cells)
            for e, m, f in cells:
                rate = round(peaks[f"{e}/{m}"] * f, 2)
                do({"phase": "open", "trial": trial, "engine": e, "mode": "open", "mix": m, "frac": f},
                   rate=rate, cap=1024)
    summary.close()
    print("saved", out_dir)


if __name__ == "__main__":
    main()
