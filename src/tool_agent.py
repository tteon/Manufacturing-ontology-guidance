"""Template-based tool calling (MARA function calling) against RDF and LPG backends.

The model never writes SPARQL or Cypher. Each backend exposes the SAME seven tools
(identical names and JSON-Schema parameters); each tool fills fixed, parameterized
query templates in engine_oxigraph.py (SPARQL) or engine_neo4j.py (Cypher).
Identifiers are validated by ``safe_id`` before reaching a template.

Protocol follows the MARA docs (function-calling-and-json-mode): OpenAI-style
``tools`` + ``tool_choice="auto"``, tool results returned as ``role: tool`` messages
with ``tool_call_id``, and ``reasoning_effort="high"`` for gpt-oss-120b.

Usage:  uv run python src/tool_agent.py [--backends rdf,lpg] [--model gpt-oss-120b]
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table

from common import DATA, RESULTS, timed
from engine_oxigraph import OxigraphEngine
from llm_agent import SCENARIO_QUERIES, MaraGateway, MaraUnavailable

console = Console(width=200)

ID = {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]*$"}
TOOL_SPECS = [
    ("find_root_causes",
     "Probabilistic root-cause analysis: from a lot's observed symptom, follow causedBy -> causedBy -> attributedTo "
     "chains, multiply edge confidences, and return (cause, equipment, score) with score >= min_confidence.",
     {"lot_id": ID, "symptom_id": ID, "min_confidence": {"type": "number", "minimum": 0, "maximum": 1}},
     ["lot_id", "symptom_id"]),
    ("trace_contamination",
     "Contamination propagation: from a defective component, follow partOf (transitive) up to its process chamber, "
     "then feedsInto (transitive) downstream; return affected (chamber, lot) pairs.",
     {"component_id": ID, "defect_id": ID}, ["component_id", "defect_id"]),
    ("classify_sensor_anomalies",
     "Return equipment whose latest SOSA observation exceeds the maxAllowedDrift threshold inherited from its class "
     "hierarchy, with the top-level equipment category.", {}, []),
    ("add_superclass",
     "SCHEMA (TBox) WRITE: make each listed existing class a subclass of a (possibly new) parent class and attach a "
     "required policy to that parent. Instance data is not modified.",
     {"new_parent_class": ID, "subclasses": {"type": "array", "items": ID, "minItems": 1}, "required_policy": ID},
     ["new_parent_class", "subclasses", "required_policy"]),
    ("list_equipment_by_class",
     "List equipment instances whose type is the given class or any of its subclasses, with the class's policy.",
     {"class_id": ID}, ["class_id"]),
    ("stream_sensor_updates",
     "Simulate a CDC stream: overwrite the current sensor reading of the given equipment's observation `iterations` "
     "times (each update committed separately); values are start_value + i*step. Defaults simulate 4.00 -> 6.97.",
     {"entity_id": ID, "iterations": {"type": "integer", "minimum": 1, "maximum": 1000},
      "start_value": {"type": "number"}, "step": {"type": "number"}}, ["entity_id", "iterations"]),
    ("check_sensor_threshold",
     "Return the current sensor reading of the given equipment and its inherited maxAllowedDrift threshold.",
     {"entity_id": ID}, ["entity_id"]),
]
TOOLS = [{"type": "function", "function": {"name": n, "description": d,
          "parameters": {"type": "object", "properties": p, "required": r}}} for n, d, p, r in TOOL_SPECS]

BACKEND_LABEL = {"rdf": "an RDF store (PyOxigraph, SPARQL)", "lpg": "a labeled property graph (Neo4j, Cypher)"}
SYSTEM = ("You operate a semiconductor-manufacturing knowledge graph stored in {backend}. You cannot write queries; "
          "use only the provided tools, which execute fixed parameterized query templates. Copy identifiers verbatim "
          "from the user's question (e.g. Lot_101, Thickness_Anomaly, ORing_Seal_04, MFC_01, GasSupplyModule). "
          "Call every tool needed to fully complete the request, then answer briefly in Korean using only tool results.")

EXPECTED_TOOLS = {"S1": {"find_root_causes"}, "S2": {"trace_contamination"}, "S3": {"classify_sensor_anomalies"},
                  "S4": {"add_superclass", "list_equipment_by_class"}, "S5": {"stream_sensor_updates"}}
EXPECTED_RESULT = {
    "S1": [["GasFlow_Instability", "MFC_01", 0.7268]],
    "S2": [["Chamber_CVD_01", "Lot_101"], ["Chamber_Etch_02", "Lot_102"]],
    "S3": [["MFC_01", 6.2, 5.0, "GasSupplyModule"], ["RF_Gen_01", 3.8, 3.0, "PlasmaModule"]],
}


class Executor:
    """Maps tool calls to an engine's template methods. Records every call."""

    def __init__(self, eng):
        self.eng, self.calls, self.evolved = eng, [], []

    def __call__(self, name: str, args: dict):
        e = self.eng
        if name == "find_root_causes":
            out = e.s1_rca(args["lot_id"], args["symptom_id"], args.get("min_confidence", 0.70))
        elif name == "trace_contamination":
            out = e.s2_propagation(args["component_id"], args["defect_id"])
        elif name == "classify_sensor_anomalies":
            out = e.s3_sensor()
        elif name == "add_superclass":
            w = e.s4_evolve("query_time", args["new_parent_class"], args["subclasses"], args["required_policy"])
            self.evolved.append((args["new_parent_class"], args["subclasses"]))
            out = {"status": "ok", "abox_facts_written": w["abox_facts_written"]}
        elif name == "list_equipment_by_class":
            out = e.list_equipment_by_class(args["class_id"])
        elif name == "stream_sensor_updates":
            obs = e.find_observation(args["entity_id"])
            n, v0, st = int(args["iterations"]), float(args.get("start_value", 4.0)), float(args.get("step", 0.03))
            for i in range(n):
                e.s5_update(obs, round(v0 + i * st, 4))
            e.flush()
            out = {"observation": obs, "updates": n, "final_value": round(v0 + (n - 1) * st, 4)}
        elif name == "check_sensor_threshold":
            obs = e.find_observation(args["entity_id"])
            out = [{"value": v, "maxAllowedDrift": m, "exceeds": v > m} for v, m in e.s5_final(obs)]
        else:
            raise KeyError(f"unknown tool {name}")
        return json.loads(json.dumps(out))  # tuples -> lists


def chat(gw, model, messages, temperature):
    body = {"model": model, "messages": messages, "tools": TOOLS, "tool_choice": "auto",
            "reasoning_effort": "high", "temperature": temperature}
    return gw.request("POST", "/chat/completions", body)


def run_question(gw, model, backend, key, question, ex: Executor, max_turns=6):
    messages = [{"role": "system", "content": SYSTEM.format(backend=BACKEND_LABEL[backend])},
                {"role": "user", "content": question}]
    trace, llm_ms, tool_ms, answer, error = [], 0.0, 0.0, None, None
    gw.tag = f"{backend}/{key}"
    for turn in range(max_turns):
        data = None
        for temp in (0.0, 0.7):  # one retry at higher temperature on HTTP 400 (known gpt-oss incomplete call)
            try:
                t0 = time.perf_counter()
                data = chat(gw, model, messages, temp)
                llm_ms += (time.perf_counter() - t0) * 1000
                break
            except MaraUnavailable as e:
                error = str(e)
                if "HTTP 400" not in error:
                    break
        if data is None:
            break
        error = None
        msg = data["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            answer = msg.get("content")
            break
        messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
        for c in calls:
            name, args = c["function"]["name"], c["function"]["arguments"]
            try:
                args = json.loads(c["function"]["arguments"] or "{}")
                result, dt = timed(ex, name, args)
                tool_ms += dt
                ok = True
            except Exception as err:  # noqa: BLE001 - report tool errors back to the model
                result, ok = {"error": f"{type(err).__name__}: {err}"}, False
            trace.append({"turn": turn, "tool": name, "args": args, "ok": ok, "result": result})
            content = json.dumps(result if not isinstance(result, list) or len(result) <= 50
                                 else {"rows": len(result), "first_50": result[:50]}, ensure_ascii=False)
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": content})
    return {"trace": trace, "answer": answer, "error": error, "llm_ms": round(llm_ms, 1),
            "tool_exec_ms": round(tool_ms, 3), "model_turns": turn + 1}


def grade(key, rec):
    used = {t["tool"] for t in rec["trace"] if t["ok"]}
    tools_ok = EXPECTED_TOOLS[key] <= used
    res = {t["tool"]: t["result"] for t in rec["trace"] if t["ok"]}
    if key in EXPECTED_RESULT:
        name = next(iter(EXPECTED_TOOLS[key]))
        result_ok = res.get(name) == EXPECTED_RESULT[key]
    elif key == "S4":
        ids = {r["eq"] for r in res.get("list_equipment_by_class", [])}
        result_ok = ids == {"MFC_01", "GasBox_A", "RF_Gen_01"}
    else:
        s = res.get("stream_sensor_updates", {})
        result_ok = s.get("updates") == 100 and s.get("final_value") == 6.97
    return {"tools_ok": tools_ok, "result_ok": bool(result_ok), "answered": bool(rec["answer"])}


def make_engine(backend, args):
    if backend == "rdf":
        return OxigraphEngine(DATA / "oxigraph_rocksdb")
    from engine_neo4j import Neo4jEngine
    return Neo4jEngine(args.neo4j_uri, "neo4j", args.neo4j_password, "mfg-bench-neo4j")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backends", default="rdf,lpg")
    ap.add_argument("--model", default="gpt-oss-120b")
    ap.add_argument("--max-calls", type=int, default=60)
    ap.add_argument("--neo4j-uri", default="bolt://127.0.0.1:7687")
    ap.add_argument("--neo4j-password", default="password123")
    args = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        gw = MaraGateway(RESULTS / f"tool_receipts_{stamp}.jsonl", max_calls=args.max_calls, model=args.model)
    except MaraUnavailable as e:
        raise SystemExit(f"MARA unavailable: {e}. Set MARA_API_KEY (see .env.example).")
    models = gw.health()
    if args.model not in models:
        raise SystemExit(f"{args.model} not listed by /models: {models}")
    report = {"timestamp_utc": stamp, "model": args.model, "tools": TOOLS, "backends": {}}
    for backend in args.backends.split(","):
        eng = make_engine(backend, args)
        eng.load(0)
        ex = Executor(eng)
        report["backends"][backend] = {}
        for key, q in SCENARIO_QUERIES.items():
            rec = run_question(gw, args.model, backend, key, q, ex)
            rec["grade"] = grade(key, rec)
            report["backends"][backend][key] = {"question": q, **rec}
            for parent, subs in ex.evolved:  # restore the schema so later scenarios start from the base TBox
                eng.s4_revert(parent, subs)
            ex.evolved.clear()
            console.print(f"[cyan]{backend}[/cyan] {key}: tools={[t['tool'] for t in rec['trace']]} grade={rec['grade']}"
                          + (f" error={rec['error']}" if rec["error"] else ""))
        eng.close()
    report["mara_calls"] = gw.calls
    out = RESULTS / f"tool_agent_{stamp}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    t = Table(title=f"Template tool calling via MARA {args.model} (same 7 tools; SPARQL vs Cypher templates)", show_lines=True)
    for c in ("Scenario", "Backend", "Tool calls (args)", "Tools/Result/Answer", "LLM ms", "Tool exec ms", "Final answer"):
        t.add_column(c, max_width=60 if c in ("Tool calls (args)", "Final answer") else None)
    for key in SCENARIO_QUERIES:
        for backend, recs in report["backends"].items():
            r = recs[key]
            g = r["grade"]
            calls = "\n".join(f"{x['tool']}({json.dumps(x['args'], ensure_ascii=False)})" for x in r["trace"])
            t.add_row(key, backend, calls,
                      " ".join("✓" if g[k] else "✗" for k in ("tools_ok", "result_ok", "answered")),
                      f"{r['llm_ms']:,.0f}", f"{r['tool_exec_ms']:,.2f}", (r["answer"] or r["error"] or "")[:300])
    console.print(t)
    console.print(f"MARA calls: {gw.calls}  Saved: {out.relative_to(RESULTS.parent)}")


if __name__ == "__main__":
    main()
