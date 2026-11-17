"""Aggregate a load-test summary.jsonl into Markdown tables (median over trials).

Usage: uv run python loadtest/report.py results/loadtest/L1580_main
Writes <dir>/REPORT.md and prints it.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def f(x, d=1):
    return "—" if x is None else f"{x:,.{d}f}"


def main():
    d = Path(sys.argv[1])
    rows = [json.loads(l) for l in (d / "summary.jsonl").read_text().splitlines() if l.strip()]
    closed = defaultdict(list)
    opened = defaultdict(list)
    for r in rows:
        (closed if r["phase"] == "closed" else opened)[
            (r["engine"], r["mix"], r["conc"] if r["phase"] == "closed" else r["frac"])].append(r)

    md = [f"# Load test report — `{d.name}`", "",
          "Medians over trials (n = trials completed for that cell). Throughput counts successful requests whose "
          "start time falls in the measurement window. Latency percentiles cover successful requests only. "
          "`client-bound` = client CPU > 70% of client processes. "
          "`dup obs` = observations left holding >1 value after the cell (write skew), max over trials.", ""]

    md += ["## Closed loop (concurrency sweep)", "",
           "| Engine | Mix | Conc | n | Throughput req/s | p50 ms | p99 ms | Errors | Wrong | Server CPU cores | Client-bound | dup obs (max values) |",
           "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|"]
    for (e, m, c), rs in sorted(closed.items()):
        valid = [r for r in rs if r["requests"] > 0]
        dups = [r["integrity"]["duplicate_value_observations"] for r in rs if r.get("integrity", {}).get("duplicate_value_observations") is not None]
        dmax = max((x[1] for r in rs for x in r.get("integrity", {}).get("duplicate_examples", []) or []), default=None)
        note = "" if valid else " (invalid: latency > window)"
        md.append(f"| {e} | {m} | {c} | {len(rs)} | {f(med([r['throughput_rps'] for r in valid]))}{note} | "
                  f"{f(med([r['lat_ms']['p50'] for r in valid]), 2)} | {f(med([r['lat_ms']['p99'] for r in valid]), 1)} | "
                  f"{sum(r['errors'] for r in rs)} | {sum(r['incorrect'] for r in rs)} | "
                  f"{f(med([r['server_cpu_cores'] for r in rs]), 2)} | "
                  f"{sum(1 for r in rs if r['client_bound_suspect'])}/{len(rs)} | "
                  f"{'' if not dups else str(max(dups)) + ('' if dmax is None else f' ({dmax})')} |")

    if opened:
        md += ["", "## Open loop (Poisson arrivals; latency from scheduled send time)", "",
               "| Engine | Mix | Offered (× closed peak) | n | Offered req/s | Achieved req/s | p50 ms | p99 ms | p99.9 ms | Dropped | Errors | Wrong |",
               "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for (e, m, fr), rs in sorted(opened.items()):
            md.append(f"| {e} | {m} | {fr} | {len(rs)} | {f(med([r['rate'] for r in rs]))} | "
                      f"{f(med([r['throughput_rps'] for r in rs]))} | {f(med([r['lat_ms']['p50'] for r in rs]), 2)} | "
                      f"{f(med([r['lat_ms']['p99'] for r in rs]), 1)} | {f(med([r['lat_ms']['p999'] for r in rs]), 1)} | "
                      f"{sum(r['dropped'] for r in rs)} | {sum(r['errors'] for r in rs)} | {sum(r['incorrect'] for r in rs)} |")

    # per-op latency at a representative closed-loop point
    md += ["", "## Per-operation p50 / p99 (closed loop, concurrency 8, R mix, median over trials)", "",
           "| Engine | Op | p50 ms | p99 ms |", "|---|---|---:|---:|"]
    for e in ("oxigraph", "neo4j"):
        rs = closed.get((e, "R", 8), [])
        ops = sorted({op for r in rs for op in r["per_op"]})
        for op in ops:
            md.append(f"| {e} | {op} | {f(med([r['per_op'][op]['p50'] for r in rs if op in r['per_op']]), 2)} | "
                      f"{f(med([r['per_op'][op]['p99'] for r in rs if op in r['per_op']]), 1)} |")
    text = "\n".join(md) + "\n"
    (d / "REPORT.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
