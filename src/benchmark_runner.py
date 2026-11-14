"""Run the five scenarios on PyOxigraph (always) and Neo4j (when reachable).

Design corrections relative to the original brief:
  * S4 is a 2x2: engine x {query_time inference, write-time materialization}, so the
    engine effect is not confounded with the inference strategy.
  * S4/S5 are repeated over a synthetic scale sweep; reads use warmup + repeated
    samples (median/p95), and the first read after each write is reported separately.
  * Each engine's no-op round trip is measured, because Neo4j pays a Bolt round trip
    per call while PyOxigraph runs in-process.
  * S5 commits every update individually on both engines (default durability of
    each engine; fsync policies are not equalized) and reports on-disk byte deltas.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from datetime import datetime, timezone

import neo4j
import pyoxigraph
from rich.console import Console
from rich.table import Table

from common import DATA, RESULTS, bench_read, expected_affected, summarize, timed
from engine_oxigraph import OxigraphEngine
from llm_agent import SCENARIO_QUERIES, IntentRouter, QueryIntent, rule_parse

console = Console(width=200)

EXPECTED = {
    "S1": [("GasFlow_Instability", "MFC_01", 0.7268)],
    "S2": [("Chamber_CVD_01", "Lot_101"), ("Chamber_Etch_02", "Lot_102")],
    "S3": [("MFC_01", 6.2, 5.0, "GasSupplyModule"), ("RF_Gen_01", 3.8, 3.0, "PlasmaModule")],
}
S4_BASE = {"MFC_01", "GasBox_A", "RF_Gen_01"}


def merged(intent: QueryIntent, rule: QueryIntent) -> QueryIntent:
    """Field-wise fallback to the rule intent when the LLM left a field empty."""
    d = intent.model_dump()
    for k, v in rule.model_dump().items():
        if d.get(k) in (None, []):
            d[k] = v
    return QueryIntent(**d)


def connect_neo4j(args):
    if args.no_neo4j:
        return None, "disabled by flag"
    try:
        from engine_neo4j import Neo4jEngine
        return Neo4jEngine(args.neo4j_uri, args.neo4j_user, args.neo4j_password, args.neo4j_container), "connected"
    except Exception as e:  # noqa: BLE001 - any connection failure -> single-engine mode
        return None, f"unavailable ({type(e).__name__}); PyOxigraph-only mode"


def run_reads(eng, intents, warmup, reps):
    i1, i2 = intents["S1"], intents["S2"]
    out = {}
    out["noop"] = bench_read(eng.noop, warmup, reps)[1]
    for key, fn in (
        ("S1", lambda: eng.s1_rca(i1.target_entity_id, i1.symptom_or_defect_id, i1.min_confidence)),
        ("S2", lambda: eng.s2_propagation(i2.target_entity_id, i2.symptom_or_defect_id)),
        ("S3", eng.s3_sensor),
    ):
        res, stats = bench_read(fn, warmup, reps)
        out[key] = {"result": res, "latency": stats, "correct": res == EXPECTED[key]}
    return out


def run_s4(eng, intent, n, reps, warmup, read_reps):
    parent, subs = intent.new_parent_class, intent.target_subclasses
    out = {}
    for strategy in ("query_time", "materialized"):
        writes, first_reads, reads, counts, ok = [], [], [], [], True
        for _ in range(reps):
            w = eng.s4_evolve(strategy, parent, subs)
            res, first = timed(eng.s4_read, strategy, parent)
            _, stats = bench_read(lambda: eng.s4_read(strategy, parent), warmup, read_reps)
            cnt, cstats = bench_read(lambda: eng.s4_count(strategy, parent), warmup, read_reps)
            ok &= cnt == expected_affected(n)
            counts.append(cstats["median_ms"])
            ids = {r["eq"] if isinstance(r, dict) else r["eq"].value.rsplit("#", 1)[1] for r in res}
            ok &= len(ids) == expected_affected(n) and S4_BASE <= ids
            writes.append(w)
            first_reads.append(first)
            reads.append(stats["median_ms"])
            eng.s4_revert(parent, subs)
        out[strategy] = {
            "write_total": summarize([w["write_total_ms"] for w in writes]),
            "tbox_write": summarize([w["tbox_write_ms"] for w in writes]),
            "abox_migration": summarize([w["abox_migration_ms"] for w in writes]),
            "flush": summarize([w["flush_ms"] for w in writes]),
            "abox_facts_written": writes[-1]["abox_facts_written"],
            "first_read_after_write": summarize(first_reads),
            "read_median_of_medians_ms": round(sorted(reads)[len(reads) // 2], 4),
            "count_median_of_medians_ms": round(sorted(counts)[len(counts) // 2], 4),
            "rows": len(ids),
            "expected_rows": expected_affected(n),
            "correct": bool(ok),
        }
    return out


def run_s5(eng, intent, reps):
    obs = eng.find_observation(intent.target_entity_id)
    iters = intent.mutation_iterations
    eng.flush()
    disk0 = eng.disk_bytes()
    totals, noop_totals = [], []
    for _ in range(reps):
        t0 = time.perf_counter_ns()
        for i in range(iters):
            eng.s5_update(obs, round(4.0 + i * 0.03, 2))
        totals.append((time.perf_counter_ns() - t0) / 1e6)
        t0 = time.perf_counter_ns()
        for _ in range(iters):
            eng.noop()
        noop_totals.append((time.perf_counter_ns() - t0) / 1e6)
    # Same overwrites, but all in ONE transaction/commit (removes per-commit durability cost).
    batch_totals = []
    values = [round(4.0 + i * 0.03, 2) for i in range(iters)]
    for _ in range(reps):
        _, dt = timed(eng.s5_update_batch, obs, values)
        batch_totals.append(dt)
    disk_pre_flush = eng.disk_bytes()
    _, flush_ms = timed(eng.flush)
    disk1 = eng.disk_bytes()
    final = eng.s5_final(obs)
    expected_final = round(4.0 + (iters - 1) * 0.03, 2)
    return {
        "observation": obs,
        "iterations": iters,
        "total": summarize(totals),
        "per_update_median_ms": round(summarize(totals)["median_ms"] / iters, 4),
        "noop_roundtrip_total": summarize(noop_totals),
        "single_commit_total": summarize(batch_totals),
        "per_noop_median_ms": round(summarize(noop_totals)["median_ms"] / iters, 4),
        "final_flush_ms": round(flush_ms, 3),
        "disk_bytes_before": disk0,
        "disk_bytes_after_updates": disk_pre_flush,
        "disk_bytes_after_flush": disk1,
        "final_values": final,
        "exceeds_threshold": bool(final) and all(v > m for v, m in final),
        "correct": len(final) == 1 and abs(final[0][0] - expected_final) < 1e-9,
    }


def fmt(stats_or_ms):
    if stats_or_ms is None:
        return "—"
    v = stats_or_ms["median_ms"] if isinstance(stats_or_ms, dict) else stats_or_ms
    return f"{v:,.2f}"


def render(report):
    eng_names = list(report["engines"])
    for scale in report["scales"]:
        sc = str(scale)
        per = {e: report["engines"][e]["scales"][sc] for e in eng_names}
        t = Table(title=f"Scale: base + {scale:,} synthetic instances  (median ms; S4/S5 writes are per-rep medians)",
                  show_lines=True)
        t.add_column("Scenario")
        t.add_column("Op type")
        for e in ["PyOxigraph", "Neo4j"]:
            t.add_column(f"{e} ms", justify="right")
        t.add_column("Correct / parity")
        t.add_column("Result / note", max_width=70)

        def g(e, *path):
            if e not in per:
                return None
            cur = per[e]
            for p in path:
                cur = cur[p]
            return cur

        def corr(*path):
            vals = {e: g(e, *path) for e in per}
            return " / ".join(f"{e[:3]}:{'✓' if v else '✗'}" for e, v in vals.items())

        def parity(key):
            if len(per) < 2:
                return ""
            r = [per[e]["reads"][key]["result"] for e in per]
            return "  parity:" + ("✓" if r[0] == r[1] else "✗")

        t.add_row("baseline", "Read no-op round trip", fmt(g("PyOxigraph", "reads", "noop")), fmt(g("Neo4j", "reads", "noop")), "", "ASK {} vs RETURN 1")
        for key, name in (("S1", "S1 probabilistic RCA"), ("S2", "S2 transitive propagation"), ("S3", "S3 SOSA classification")):
            res = per[eng_names[0]]["reads"][key]["result"]
            t.add_row(name, "Read", fmt(g("PyOxigraph", "reads", key, "latency")), fmt(g("Neo4j", "reads", key, "latency")),
                      corr("reads", key, "correct") + parity(key), str(res))
        for strat in ("query_time", "materialized"):
            facts = {e: per[e]["S4"][strat]["abox_facts_written"] for e in per}
            t.add_row(f"S4 TBox evolution ({strat})", "Write (+migration)",
                      fmt(g("PyOxigraph", "S4", strat, "write_total")), fmt(g("Neo4j", "S4", strat, "write_total")),
                      corr("S4", strat, "correct"), f"ABox facts written {facts}; rows={per[eng_names[0]]['S4'][strat]['rows']}")
            t.add_row(f"S4 read after evolution ({strat})", "Read",
                      fmt(g("PyOxigraph", "S4", strat, "read_median_of_medians_ms")),
                      fmt(g("Neo4j", "S4", strat, "read_median_of_medians_ms")), "",
                      "COUNT only: " + ", ".join(f"{e}={per[e]['S4'][strat]['count_median_of_medians_ms']:.2f}" for e in per)
                      + "; first read after write: " + ", ".join(f"{e}={fmt(per[e]['S4'][strat]['first_read_after_write'])}" for e in per))
        s5 = {e: per[e]["S5"] for e in per}
        t.add_row(f"S5 {s5[eng_names[0]]['iterations']}× state overwrite", "Write (per-op commit)",
                  fmt(g("PyOxigraph", "S5", "total")), fmt(g("Neo4j", "S5", "total")), corr("S5", "correct"),
                  "per update: " + ", ".join(f"{e}={s5[e]['per_update_median_ms']:.3f} (no-op {s5[e]['per_noop_median_ms']:.3f})" for e in s5)
                  + "; Δdisk(after flush) " + ", ".join(f"{e}={s5[e]['disk_bytes_after_flush'] - s5[e]['disk_bytes_before']:+,}B" for e in s5)
                  + f"; final={s5[eng_names[0]]['final_values']} exceeds={s5[eng_names[0]]['exceeds_threshold']}")
        t.add_row("S5 same overwrites, 1 commit", "Write (single tx)", fmt(g("PyOxigraph", "S5", "single_commit_total")),
                  fmt(g("Neo4j", "S5", "single_commit_total")), "", "one SPARQL Update request / one explicit Cypher transaction")
        console.print(t)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scales", default="0,1000,10000,100000")
    ap.add_argument("--reps", type=int, default=5, help="S4 evolve/revert cycles and S5 runs")
    ap.add_argument("--read-reps", type=int, default=20)
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--model", default="gpt-oss-120b")
    ap.add_argument("--no-neo4j", action="store_true")
    ap.add_argument("--neo4j-uri", default="bolt://127.0.0.1:7687")
    ap.add_argument("--neo4j-user", default="neo4j")
    ap.add_argument("--neo4j-password", default="password123")
    ap.add_argument("--neo4j-container", default="mfg-bench-neo4j")
    args = ap.parse_args()
    scales = [int(s) for s in args.scales.split(",")]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    RESULTS.mkdir(exist_ok=True)
    router = IntentRouter(not args.no_llm, RESULTS / f"llm_receipts_{stamp}.jsonl", model=args.model)
    console.print(f"[bold]Intent router:[/bold] {router.status}")
    intents, routing = {}, {}
    for key, text in SCENARIO_QUERIES.items():
        intent, meta = router.parse(text)
        intents[key] = merged(intent, rule_parse(text))
        routing[key] = {"query": text, **meta, "executed_intent": intents[key].model_dump()}
        console.print(f"  {key}: {meta['source']:<20} -> {intents[key].scenario_type}"
                      + (f"  (agrees with rules: {meta.get('agrees_with_rules')})" if "agrees_with_rules" in meta else ""))

    engines = [OxigraphEngine(DATA / "oxigraph_rocksdb")]
    neo, neo_status = connect_neo4j(args)
    console.print(f"[bold]Neo4j:[/bold] {neo_status}")
    if neo:
        engines.append(neo)

    report = {
        "timestamp_utc": stamp, "scales": scales,
        "settings": vars(args) | {"neo4j_password": "***"},
        "versions": {"python": sys.version.split()[0], "pyoxigraph": pyoxigraph.__version__,
                     "neo4j_driver": neo4j.__version__, "platform": platform.platform()},
        "router": router.status, "routing": routing,
        "engines": {e.name: {"scales": {}} for e in engines},
    }
    for n in scales:
        for eng in engines:
            console.print(f"[cyan]{eng.name}[/cyan] scale={n:,}: loading…")
            load = eng.load(n)
            reads = run_reads(eng, intents, args.warmup, args.read_reps)
            s4 = run_s4(eng, intents["S4"], n, args.reps, args.warmup, args.read_reps)
            s5 = run_s5(eng, intents["S5"], args.reps)
            report["engines"][eng.name]["scales"][str(n)] = {"load": load, "reads": reads, "S4": s4, "S5": s5}
        out = RESULTS / f"run_{stamp}.json"
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    for eng in engines:
        eng.close()
    render(report)
    console.print(f"Saved: {out.relative_to(RESULTS.parent)}")


if __name__ == "__main__":
    main()
