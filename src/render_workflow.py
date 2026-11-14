"""Render docs/WORKFLOW.md: for each scenario, the end-to-end path
user request -> LLM tool call (raw from MARA receipts) -> filled SPARQL / Cypher template -> engine result
-> LLM final answer -> grading against generator ground truth, with the relevant real data slice.

Usage: uv run python src/render_workflow.py [results/tool_agent_<UTC>.json]
"""

from __future__ import annotations

import glob
import json
import sys
from collections import defaultdict
from pathlib import Path

from common import ROOT, S1_MIN_CONF, generate_connected_fab_graph
from queries import SPARQL, cypher

TOOL_TO_QUERY = {
    "find_root_causes": ("S1", lambda a: {"lot": a["lot_id"], "min_conf": a.get("min_confidence", S1_MIN_CONF)},
                         lambda a: {"lot": a["lot_id"], "min_conf": a.get("min_confidence", S1_MIN_CONF)}),
    "trace_contamination": ("S2", lambda a: {"component": a["component_id"]}, lambda a: {"component": a["component_id"]}),
    "classify_sensor_anomalies": ("S3", lambda a: {"line": a["line_id"]}, lambda a: {"line": a["line_id"]}),
    "count_sensor_anomalies": ("S3_global_count", lambda a: {}, lambda a: {}),
    "list_equipment_by_class": ("list_by_class", lambda a: {"cls": a["class_id"]}, lambda a: {"cls": a["class_id"]}),
    "check_sensor_threshold": ("obs_of", lambda a: {"entity": a["entity_id"]}, lambda a: {"entity": a["entity_id"]}),
}


def render_queries(tool, args):
    if tool in TOOL_TO_QUERY:
        name, sp, cp = TOOL_TO_QUERY[tool]
        return SPARQL[name](sp(args)).split("\n", 7)[-1], cypher(name), cp(args)
    if tool == "add_superclass":
        p, subs, pol = args["new_parent_class"], args["subclasses"], args["required_policy"]
        sparql = ("# PyOxigraph: store.extend([...]) adds these quads to ex:TBox_Graph (no ABox change)\n" +
                  "\n".join(f"ex:{c} rdfs:subClassOf ex:{p} ." for c in subs) + f'\nex:{p} ex:requiredPolicy "{pol}" .')
        return sparql, cypher("S4_tbox_write"), {"parent": p, "subs": subs, "policy": pol}
    if tool == "batch_stream_update":
        rows = "(ex:Obs_<part> \"<new value>\"^^xsd:double) … × " + str(args["count"])
        sp = SPARQL["S5_batch"]({"rows": [("Obs_X", 1.0)]}).split("\n", 7)[-1].replace('(ex:Obs_X "1.0"^^xsd:double)', rows)
        return sp, cypher("S5_batch"), {"rows": f"[{{o: 'Obs_<part>', v: <new value>}}, …] × {args['count']}"}
    return "", "", {}


def short(v, n=12):
    if isinstance(v, list) and len(v) > n:
        return json.dumps(v[:n], ensure_ascii=False) + f" … (+{len(v) - n} more, {len(v)} total)"
    return json.dumps(v, ensure_ascii=False)


def data_slice(kg, key, trace):
    args = {t["tool"]: t["args"] for t in trace}
    out = []
    if key == "S1":
        lot = args["find_root_causes"]["lot_id"]
        out.append(f"`{lot}` processedIn {kg.processed[lot]} · hasSymptom {kg.lot_symptoms[lot]}")
        out.append("")
        out.append("| Symptom | Cause | conf(sym→cause) | Part | conf(cause→part) | product | Kind (planted) | ≥ 0.48 |")
        out.append("|---|---|---:|---|---:|---:|---|---|")
        for sym in kg.lot_symptoms[lot]:
            for cause, c1 in kg.caused_by[sym]:
                part, c2 = kg.attributed[cause]
                kind = {"c": "correct", "w": "wrong", "d": "decoy"}[cause.rsplit("_", 1)[1][0]]
                out.append(f"| {sym} | {cause} | {c1} | {part} | {c2} | {c1 * c2:.4f} | {kind} | {'✓' if c1 * c2 >= S1_MIN_CONF else ''} |")
    elif key == "S2":
        seal = args["trace_contamination"]["component_id"]
        ch = kg.chamber_of(seal)
        out.append(f"`{seal}` hasDefect {kg.defect[seal]} · partOf chain: {' → '.join([seal, *kg.up(seal)])}")
        out.append(f"host chamber `{ch}` · feedsInto* downstream: {kg.downstream(ch)}")
        out.append(f"lots processed in those chambers (truth): {kg.truth_s2(seal)}")
    elif key == "S3":
        line = args["classify_sensor_anomalies"]["line_id"]
        out.append("| Part | Class | Spec (via owl:hasValue restriction) | maxAllowedDrift | Reading | Exceeds |")
        out.append("|---|---|---|---:|---:|---|")
        rows = [(part, spec, v) for part, spec, v in kg.obs.values() if line in kg.up(part)]
        for part, spec, v in sorted(rows, key=lambda r: -r[2] / kg.tbox.threshold[r[1]])[:8]:
            thr = kg.tbox.threshold[spec]
            out.append(f"| {part} | {kg.types[part]} | {spec} | {thr} | {v} | {'✓' if v > thr else ''} |")
        out.append(f"\n({len(rows)} observed parts in {line}; top 8 by reading/threshold ratio shown)")
    elif key == "S4":
        truth = kg.truth_s4()
        out.append(f"Instances of GasSupplyModule ∪ PlasmaModule (truth): {len(truth)} → {short(truth, 8)}")
        out.append("Query-time strategy: no ABox triple/label is written; the new subClassOf edges are traversed at read time.")
    else:
        out.append(f"{len(kg.obs)} observations; anomalies before the batch (truth): {kg.truth_s3_global()}")
    return "\n".join(out)


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(sorted(glob.glob(str(ROOT / "results" / "tool_agent_*.json")))[-1])
    rep = json.loads(path.read_text())
    stamp = rep["timestamp_utc"]
    receipts = defaultdict(list)
    for line in (ROOT / "results" / f"tool_receipts_{stamp}.jsonl").read_text().splitlines():
        r = json.loads(line)
        if r.get("tag"):
            receipts[r["tag"]].append(r)
    kg = generate_connected_fab_graph(rep["lines"])
    md = [f"# Workflow walkthrough with real data (run `{path.name}`)", "",
          f"Graph: `generate_connected_fab_graph({rep['lines']})` (seed 7) — {kg.stats()['quads_total']:,} quads. "
          f"Model: `{rep['model']}` via MARA function calling, {rep['mara_calls']} API calls in total. "
          "Both backends received the same tool schemas; only the template behind each tool differs.", "",
          "```", "user request ─► LLM (tools=[8 JSON-schema functions], tool_choice=auto, reasoning_effort=high)",
          "      ─► tool_call {name, arguments}  ─► safe_id() validation ─► fill SPARQL template (RDF) | Cypher template (LPG)",
          "      ─► engine result ─► role:tool message ─► LLM ─► (more tool calls | final Korean answer)",
          "      ─► grading: tool set + precision/recall of tool results vs generator ground truth", "```", ""]
    for key in ("S1", "S2", "S3", "S4", "S5"):
        rdf, lpg = rep["backends"]["rdf"][key], rep["backends"]["lpg"][key]
        md += [f"## {key}", "", f"**User request** — {rdf['question']}", "", "**Relevant real data (generator)**", "",
               data_slice(kg, key, rdf["trace"]), ""]
        md += ["**LLM turns (raw from MARA receipts, RDF run)**", "", "| Turn | finish_reason | Tool call(s) | Prompt / completion (reasoning) tokens | Latency ms |",
               "|---|---|---|---|---:|"]
        for i, r in enumerate(receipts[f"rdf/{key}"]):
            calls = "; ".join(f"`{c['function']['name']}({c['function']['arguments']})`" for c in r.get("tool_calls") or []) or "— (final answer)"
            u = r.get("usage") or {}
            md.append(f"| {i + 1} | {r.get('finish_reason')} | {calls} | {u.get('prompt_tokens')} / {u.get('completion_tokens')} "
                      f"({(u.get('completion_tokens_details') or {}).get('reasoning_tokens')}) | {r.get('latency_ms')} |")
        md.append("")
        for t_r, t_l in zip(rdf["trace"], lpg["trace"]):
            sp, cy, params = render_queries(t_r["tool"], t_r["args"])
            same = "identical" if t_r["result"] == t_l["result"] else "DIFFERENT"
            md += [f"**Tool `{t_r['tool']}`** args (RDF run) `{json.dumps(t_r['args'], ensure_ascii=False)}` · "
                   f"args (LPG run) `{json.dumps(t_l['args'], ensure_ascii=False)}`", "",
                   "<table><tr><th>SPARQL template filled (PyOxigraph)</th><th>Cypher template (Neo4j) + parameters</th></tr>",
                   f"<tr><td><pre>{sp}</pre></td><td><pre>{cy}</pre><pre>{json.dumps(params, ensure_ascii=False)}</pre></td></tr></table>", "",
                   f"Result RDF: `{short(t_r['result'])}`", "", f"Result LPG: `{short(t_l['result'])}` — results {same}", ""]
        md += ["**Final answers (generation)**", "", f"- RDF: {rdf['answer']}".replace("\n", " "), f"- LPG: {lpg['answer']}".replace("\n", " "), "",
               f"**Grade** — RDF {rdf['grade']} · LPG {lpg['grade']} · LLM HTTP time (sum of receipt latencies) "
               f"{sum(r['latency_ms'] for r in receipts[f'rdf/{key}']):.0f} / {sum(r['latency_ms'] for r in receipts[f'lpg/{key}']):.0f} ms, "
               f"tool time {rdf['tool_exec_ms']:.1f} / {lpg['tool_exec_ms']:.1f} ms", "", "---", ""]
    out = ROOT / "docs" / "WORKFLOW.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(md))
    print("wrote", out, len(md), "lines")


if __name__ == "__main__":
    main()
