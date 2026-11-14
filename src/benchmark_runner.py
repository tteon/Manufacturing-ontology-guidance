"""v2 benchmark: connected fab graph at several scales, S1-S5 on PyOxigraph (embedded) and Neo4j (Bolt).

For every scale (``--scales`` = number of production lines):
  * generate the graph + ground truth in Python, load identical quads into both engines
  * S1-S3: every sampled parameter is timed (warm pass + --reps passes) and graded with set
    precision/recall against the generator's ground truth
  * S4: runtime TBox evolution, 2x2 (query_time vs materialized), write/list/count, graded vs truth
  * S5: batch upsert of 500 random observations in ONE transaction, --reps times; read-back and
    global S3 recount graded vs the updated ground truth
Outputs results/v2/run_<UTC>.json, a rich console table and results/v2/summary_<UTC>.md.
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import statistics
import sys
from datetime import datetime, timezone

import neo4j
import pyoxigraph
from rich.console import Console
from rich.table import Table

from common import (DATA, RESULTS, S1_MIN_CONF, S4_PARENT, S4_POLICY, S4_SUBCLASSES, generate_connected_fab_graph,
                    precision_recall, sample_params, summarize, timed)
from engine_oxigraph import OxigraphEngine

console = Console(width=200)


def macro_pr(pairs):
    """Mean of per-request precision/recall (macro average)."""
    p = statistics.fmean(x[0] for x in pairs) if pairs else 1.0
    r = statistics.fmean(x[1] for x in pairs) if pairs else 1.0
    return round(p, 6), round(r, 6)


def run_reads(eng, params, reps):
    out = {"noop": summarize([timed(eng.noop)[1] for _ in range(50)])}
    for op, fn in (("S1", lambda p: eng.s1(p["lot"], S1_MIN_CONF)),
                   ("S2", lambda p: eng.s2(p["component"])),
                   ("S3", lambda p: eng.s3(p["line"]))):
        for p in params[op]:  # warm pass
            fn(p)
        samples, prs = [], []
        for _ in range(reps):
            for p in params[op]:
                got, dt = timed(fn, p)
                samples.append(dt)
                prs.append(precision_recall(got, p["truth"]))
        prec, rec = macro_pr(prs)
        out[op] = {"latency": summarize(samples), "n_params": len(params[op]), "precision": prec, "recall": rec,
                   "mean_answer_size": round(statistics.fmean(len(p["truth"]) for p in params[op]), 1)}
    return out


def run_s4(eng, kg, reps, read_reps):
    truth = kg.truth_s4()
    out = {}
    for strategy in ("query_time", "materialized"):
        w, lists, counts, prs, ok_count = [], [], [], [], True
        for _ in range(reps):
            w.append(eng.s4_evolve(strategy, S4_PARENT, S4_SUBCLASSES, S4_POLICY))
            got = eng.s4_list(strategy, S4_PARENT)
            prs.append(precision_recall(got, truth))
            for _ in range(read_reps):
                lists.append(timed(eng.s4_list, strategy, S4_PARENT)[1])
                c, dt = timed(eng.s4_count, strategy, S4_PARENT)
                counts.append(dt)
                ok_count &= c == len(truth)
            eng.s4_revert(S4_PARENT)
        prec, rec = macro_pr(prs)
        out[strategy] = {k: summarize([x[k] for x in w]) for k in ("write_total_ms", "tbox_write_ms", "abox_migration_ms", "flush_ms")}
        out[strategy].update({"abox_facts_written": w[-1]["abox_facts_written"], "list": summarize(lists),
                              "count": summarize(counts), "precision": prec, "recall": rec,
                              "count_correct": ok_count, "expected_rows": len(truth)})
    return out


def run_s5(eng, kg, reps, batch, seed):
    rng = random.Random(seed)
    ids = sorted(kg.obs)
    n = min(batch, len(ids))
    writes, flushes, prs, s3_ok = [], [], [], True
    disk0 = eng.disk_bytes()
    for _ in range(reps):
        chosen = rng.sample(ids, n)
        rows = []
        for o in chosen:
            part, spec, _ = kg.obs[o]
            thr = kg.tbox.threshold[spec]
            rows.append((o, round(max(0.0, rng.gauss(0.6 * thr, 0.25 * thr)), 3)))
        t = eng.s5_batch(rows)
        for o, v in rows:  # update ground truth
            kg.obs[o][2] = v
        writes.append(t["write_ms"])
        flushes.append(t["flush_ms"])
        prs.append(precision_recall(eng.s5_read(chosen), sorted(rows)))
        s3_ok &= eng.s3_global_count() == kg.truth_s3_global()
    prec, rec = macro_pr(prs)
    return {"batch_size": n, "write": summarize(writes), "flush": summarize(flushes),
            "per_row_median_ms": round(statistics.median(writes) / n, 4), "precision": prec, "recall": rec,
            "s3_global_recount_correct": bool(s3_ok), "disk_bytes_delta": eng.disk_bytes() - disk0}


def md_table(report) -> str:
    lines = ["| Quads | Scenario | Op | PyOxigraph ms | Neo4j ms | P / R (Oxi) | P / R (Neo) | Notes |",
             "|---:|---|---|---:|---:|---|---|---|"]
    engines = list(report["engines"])
    for sc in report["scales"]:
        quads = report["datasets"][str(sc)]["quads_total"]
        per = {e: report["engines"][e][str(sc)] for e in engines if str(sc) in report["engines"][e]}

        def g(e, *path):
            if e not in per:
                return None
            cur = per[e]
            for p in path:
                cur = cur[p]
            return cur

        def m(e, *path):
            if path and callable(path[0]):
                return "—" if e not in per else path[0](per[e])
            v = g(e, *path)
            return "—" if v is None else f"{v['median_ms']:.2f}" if isinstance(v, dict) else f"{v:.2f}"

        def pr(e, *path):
            if not path:
                return "—"
            d = g(e, *path)
            return "—" if d is None else f"{d['precision']:.0%} / {d['recall']:.0%}"

        first = per[engines[0]]

        def commit(strategy):
            return lambda d: f"{d['S4'][strategy]['tbox_write_ms']['median_ms'] + d['S4'][strategy]['abox_migration_ms']['median_ms']:.2f}"

        def flush(d):
            f4, f5 = d["S4"]["query_time"]["flush_ms"]["median_ms"], d["S5"]["flush"]["median_ms"]
            return "n/a" if not (f4 or f5) else f"{f4:.1f} / {f5:.1f}"
        r = first["reads"]
        rows = [
            ("S1 RCA (2-hop confidence ≥ 0.48)", "Read", ("reads", "S1", "latency"), ("reads", "S1"),
             f"{r['S1']['n_params']} lots, avg {r['S1']['mean_answer_size']} answers"),
            ("S2 contamination (partOf+, feedsInto*)", "Read", ("reads", "S2", "latency"), ("reads", "S2"),
             f"{r['S2']['n_params']} seals, avg {r['S2']['mean_answer_size']} lots"),
            ("S3 per-line sensor anomalies (restriction → spec)", "Read", ("reads", "S3", "latency"), ("reads", "S3"),
             f"{r['S3']['n_params']} lines, avg {r['S3']['mean_answer_size']} parts"),
            ("S4 query_time: TBox write (commit)", "Write", (commit("query_time"),), ("S4", "query_time"),
             "0 ABox facts touched"),
            ("S4 query_time: list", "Read", ("S4", "query_time", "list"), ("S4", "query_time"),
             "COUNT: " + " / ".join(m(e, "S4", "query_time", "count") for e in per)),
            ("S4 materialized: TBox write + ABox migration (commit)", "Write", (commit("materialized"),), ("S4", "materialized"),
             f"{first['S4']['materialized']['abox_facts_written']} facts migrated"),
            ("S4 materialized: list", "Read", ("S4", "materialized", "list"), ("S4", "materialized"),
             "COUNT: " + " / ".join(m(e, "S4", "materialized", "count") for e in per)),
            (f"S5 batch upsert ({first['S5']['batch_size']} obs, 1 tx)", "Write", ("S5", "write"), ("S5",),
             "S3 recount ok: " + "/".join(str(per[e]["S5"]["s3_global_recount_correct"]) for e in per)),
            ("PyOxigraph flush() after write (S4 / S5)", "Write", (flush,), (),
             "Neo4j: no separate flush (commit already fsyncs its log); PyOxigraph commits are not fsynced"),
        ]
        for name, op, path, prpath, note in rows:
            lines.append(f"| {quads:,} | {name} | {op} | {m('PyOxigraph', *path)} | {m('Neo4j', *path)} | "
                         f"{pr('PyOxigraph', *prpath)} | {pr('Neo4j', *prpath)} | {note} |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scales", default="2,16,158", help="number of production lines per run")
    ap.add_argument("--params", type=int, default=20, help="sampled parameters per read scenario")
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--s4-read-reps", type=int, default=5)
    ap.add_argument("--s5-batch", type=int, default=500)
    ap.add_argument("--no-neo4j", action="store_true")
    ap.add_argument("--neo4j-uri", default="bolt://127.0.0.1:7687")
    ap.add_argument("--neo4j-user", default="neo4j")
    ap.add_argument("--neo4j-password", default="password123")
    ap.add_argument("--neo4j-container", default="mfg-bench-neo4j")
    args = ap.parse_args()
    scales = [int(s) for s in args.scales.split(",")]

    engines = [OxigraphEngine(DATA / "oxigraph_rocksdb")]
    if not args.no_neo4j:
        try:
            from engine_neo4j import Neo4jEngine
            engines.append(Neo4jEngine(args.neo4j_uri, args.neo4j_user, args.neo4j_password, args.neo4j_container))
            console.print("[bold]Neo4j:[/bold] connected")
        except Exception as e:  # noqa: BLE001
            console.print(f"[bold]Neo4j:[/bold] unavailable ({type(e).__name__}); PyOxigraph-only mode")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = RESULTS / "v2"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {"timestamp_utc": stamp, "scales": scales, "settings": vars(args) | {"neo4j_password": "***"},
              "versions": {"python": sys.version.split()[0], "pyoxigraph": pyoxigraph.__version__,
                           "neo4j_driver": neo4j.__version__, "platform": platform.platform()},
              "datasets": {}, "engines": {e.name: {} for e in engines}}
    for sf in scales:
        for eng in engines:
            kg = generate_connected_fab_graph(sf)       # fresh truth per engine (S5 mutates it)
            params = sample_params(kg, args.params)
            report["datasets"][str(sf)] = kg.stats()
            console.print(f"[cyan]{eng.name}[/cyan] lines={sf} quads={kg.stats()['quads_total']:,}: loading…")
            res = {"load": eng.load(kg)}
            res["reads"] = run_reads(eng, params, args.reps)
            res["S4"] = run_s4(eng, kg, args.reps, args.s4_read_reps)
            res["S5"] = run_s5(eng, kg, args.reps, args.s5_batch, seed=sf)
            report["engines"][eng.name][str(sf)] = res
            (out_dir / f"run_{stamp}.json").write_text(json.dumps(report, indent=1, default=str))
    for eng in engines:
        eng.close()

    md = md_table(report)
    (out_dir / f"summary_{stamp}.md").write_text(md + "\n")
    t = Table(title="v2 benchmark (median ms; P/R = precision/recall vs generator ground truth)")
    header, *body = [l.strip("|").split("|") for l in md.splitlines() if not l.startswith("|---")]
    for h in header:
        t.add_column(h.strip())
    for row in body:
        t.add_row(*[c.strip() for c in row])
    console.print(t)
    console.print(f"Saved: results/v2/run_{stamp}.json, results/v2/summary_{stamp}.md")


if __name__ == "__main__":
    main()
