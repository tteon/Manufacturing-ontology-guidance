"""Template-based tool calling (MARA function calling) against RDF and LPG backends (v2 graph).

The model never writes SPARQL or Cypher. Both backends expose the SAME tools (identical names and
JSON-Schema parameters); each tool fills fixed templates from queries.py. Questions are generated
from the connected fab graph with real identifiers, and every tool result is graded with
precision/recall against the generator's ground truth.

Protocol follows the MARA docs (function-calling-and-json-mode): OpenAI-style ``tools`` +
``tool_choice="auto"``, ``role: tool`` results with ``tool_call_id``, ``reasoning_effort="high"``
for gpt-oss-120b.

Usage:  uv run python src/tool_agent.py [--backends rdf,lpg] [--lines 16] [--model gpt-oss-120b]
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table

from common import DATA, RESULTS, S1_MIN_CONF, generate_connected_fab_graph, precision_recall, timed
from engine_oxigraph import OxigraphEngine
from llm_agent import MaraGateway, MaraUnavailable

console = Console(width=200)

ID = {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]*$"}
TOOL_SPECS = [
    ("find_root_causes",
     "FMEA root-cause analysis for a lot: follow its symptoms -causedBy-> causes -attributedTo-> parts, multiply the two "
     "edge confidences, return (cause, part, score) with score >= min_confidence (default 0.48).",
     {"lot_id": ID, "min_confidence": {"type": "number", "minimum": 0, "maximum": 1}}, ["lot_id"]),
    ("trace_contamination",
     "Contamination propagation from a defective component: partOf (transitive) up to its process chamber, then "
     "feedsInto (transitive) downstream; return the suspect lots processed in those chambers.",
     {"component_id": ID}, ["component_id"]),
    ("classify_sensor_anomalies",
     "Parts within a production line whose latest SOSA observation exceeds the maxAllowedDrift of the specification "
     "their class references (owl:Restriction on ssn:hasProperty).", {"line_id": ID}, ["line_id"]),
    ("count_sensor_anomalies", "Count all parts in the fab whose latest observation exceeds their specification.", {}, []),
    ("add_superclass",
     "SCHEMA (TBox) WRITE: make each listed existing class a subclass of a (possibly new) parent class and attach a "
     "required policy to that parent. Instance data is not modified.",
     {"new_parent_class": ID, "subclasses": {"type": "array", "items": ID, "minItems": 1}, "required_policy": ID},
     ["new_parent_class", "subclasses", "required_policy"]),
    ("list_equipment_by_class", "List equipment whose type is the given class or any subclass, with the class policy.",
     {"class_id": ID}, ["class_id"]),
    ("batch_stream_update",
     "Simulate a CDC batch: pick `count` random sensor observations and upsert new readings in ONE transaction.",
     {"count": {"type": "integer", "minimum": 1, "maximum": 5000}}, ["count"]),
    ("check_sensor_threshold", "Current reading(s) of a part and the threshold of its specification.",
     {"entity_id": ID}, ["entity_id"]),
]
TOOLS = [{"type": "function", "function": {"name": n, "description": d,
          "parameters": {"type": "object", "properties": p, "required": r}}} for n, d, p, r in TOOL_SPECS]

BACKEND_LABEL = {"rdf": "an RDF store (PyOxigraph, SPARQL)", "lpg": "a labeled property graph (Neo4j, Cypher)"}
SYSTEM = ("You operate a semiconductor-manufacturing knowledge graph stored in {backend}. You cannot write queries; "
          "use only the provided tools, which execute fixed parameterized query templates. Copy identifiers verbatim "
          "from the user's question. Call every tool needed to fully complete the request, then answer briefly in "
          "Korean using only tool results.")
EXPECTED_TOOLS = {"S1": {"find_root_causes"}, "S2": {"trace_contamination"}, "S3": {"classify_sensor_anomalies"},
                  "S4": {"add_superclass", "list_equipment_by_class"}, "S5": {"batch_stream_update", "count_sensor_anomalies"}}


def make_questions(kg, rng):
    """Natural-language questions with real identifiers drawn from the generated graph."""
    lot = rng.choice(sorted(l for l, s in kg.lot_symptoms.items() if s))
    seal = rng.choice(sorted(kg.defect))
    line = rng.choice(kg.lines)
    return {
        "S1": (f"{lot}에서 이상 증상이 나왔어. 증상→원인→부품 경로의 누적 신뢰도가 {S1_MIN_CONF} 이상인 근본 원인과 책임 부품을 찾아줘.",
               {"lot": lot}),
        "S2": (f"{seal} 부품에서 결함이 발견됐어. partOf와 feedsInto를 따라 오염이 전파됐을 수 있는 의심 로트를 모두 추적해줘.",
               {"component": seal}),
        "S3": (f"{line} 라인에서 센서 측정값이 사양(maxAllowedDrift)을 초과한 부품을 찾아줘.", {"line": line}),
        "S4": ("새로운 환경 안전 규정에 따라 GasSupplyModule과 PlasmaModule을 모두 CriticalHazardSystem이라는 새로운 상위 클래스로 묶고, "
               "이 클래스에 속한 모든 설비는 즉시 비상 인터록 점검(Emergency_Interlock_Check) 대상이 된다고 스키마(TBox)를 업데이트해 줘. "
               "그리고 현재 이 규정에 해당하는 현장 설비 목록을 바로 뽑아줘.", {}),
        "S5": ("센서 관측값 500개가 CDC 배치로 한 번에 들어온 상황을 시뮬레이션해서 반영하고, 반영 후 사양을 초과한 부품이 전체 몇 개인지 알려줘.", {}),
    }


class Executor:
    def __init__(self, eng, kg, seed):
        self.eng, self.kg, self.rng, self.evolved = eng, kg, random.Random(seed), []

    def __call__(self, name, a):
        e, kg = self.eng, self.kg
        if name == "find_root_causes":
            return e.s1(a["lot_id"], a.get("min_confidence", S1_MIN_CONF))
        if name == "trace_contamination":
            return e.s2(a["component_id"])
        if name == "classify_sensor_anomalies":
            return e.s3(a["line_id"])
        if name == "count_sensor_anomalies":
            return {"anomalous_parts": e.s3_global_count()}
        if name == "add_superclass":
            w = e.s4_evolve("query_time", a["new_parent_class"], a["subclasses"], a["required_policy"])
            self.evolved.append(a["new_parent_class"])
            return {"status": "ok", "abox_facts_written": w["abox_facts_written"]}
        if name == "list_equipment_by_class":
            return e.list_by_class(a["class_id"])
        if name == "batch_stream_update":
            ids = sorted(kg.obs)
            chosen = self.rng.sample(ids, min(int(a["count"]), len(ids)))
            rows = []
            for o in chosen:
                thr = kg.tbox.threshold[kg.obs[o][1]]
                rows.append((o, round(max(0.0, self.rng.gauss(0.6 * thr, 0.25 * thr)), 3)))
            e.s5_batch(rows)
            for o, v in rows:  # keep ground truth in sync
                kg.obs[o][2] = v
            return {"updated_observations": len(rows), "transactions": 1}
        if name == "check_sensor_threshold":
            return e.obs_of(a["entity_id"])
        raise KeyError(name)


def chat(gw, model, messages, temperature):
    return gw.request("POST", "/chat/completions", {"model": model, "messages": messages, "tools": TOOLS,
                                                    "tool_choice": "auto", "reasoning_effort": "high",
                                                    "temperature": temperature})


def run_question(gw, model, backend, key, question, ex, max_turns=6):
    messages = [{"role": "system", "content": SYSTEM.format(backend=BACKEND_LABEL[backend])},
                {"role": "user", "content": question}]
    trace, llm_ms, tool_ms, answer, error, turn = [], 0.0, 0.0, None, None, 0
    gw.tag = f"{backend}/{key}"
    for turn in range(max_turns):
        data = None
        for temp in (0.0, 0.7):  # one retry at higher temperature on HTTP 400 (known gpt-oss incomplete call)
            try:
                data = chat(gw, model, messages, temp)
                llm_ms += gw.last_latency_ms   # HTTP time only; excludes the 1 s admission spacing
                error = None
                break
            except MaraUnavailable as e:
                error = str(e)
                if "HTTP 400" not in error:
                    break
        if data is None:
            break
        msg = data["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            answer = msg.get("content")
            break
        messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
        for c in calls:
            name, args = c["function"]["name"], c["function"]["arguments"]
            try:
                args = json.loads(args or "{}")
                result, dt = timed(ex, name, args)
                tool_ms += dt
                ok = True
            except Exception as err:  # noqa: BLE001 - report tool errors back to the model
                result, ok = {"error": f"{type(err).__name__}: {err}"}, False
            result = json.loads(json.dumps(result))
            trace.append({"turn": turn, "tool": name, "args": args, "ok": ok, "result": result})
            content = result if not isinstance(result, list) or len(result) <= 60 else {"rows": len(result), "first_60": result[:60]}
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(content, ensure_ascii=False)})
    return {"trace": trace, "answer": answer, "error": error, "llm_ms": round(llm_ms, 1),
            "tool_exec_ms": round(tool_ms, 3), "model_turns": turn + 1}


def grade(key, rec, kg, qp):
    used = {t["tool"] for t in rec["trace"] if t["ok"]}
    res = {t["tool"]: t["result"] for t in rec["trace"] if t["ok"]}
    if key == "S1":
        p, r = precision_recall([tuple(x) for x in res.get("find_root_causes", [])], kg.truth_s1(qp["lot"]))
    elif key == "S2":
        p, r = precision_recall(res.get("trace_contamination", []), kg.truth_s2(qp["component"]))
    elif key == "S3":
        p, r = precision_recall(res.get("classify_sensor_anomalies", []), kg.truth_s3(qp["line"]))
    elif key == "S4":
        p, r = precision_recall(sorted(x["eq"] for x in res.get("list_equipment_by_class", [])), kg.truth_s4())
    else:
        got = res.get("count_sensor_anomalies", {}).get("anomalous_parts")
        upd = res.get("batch_stream_update", {}).get("updated_observations")
        p = r = float(got == kg.truth_s3_global() and upd == min(500, len(kg.obs)))
    return {"tools_ok": EXPECTED_TOOLS[key] <= used, "precision": round(p, 4), "recall": round(r, 4),
            "answered": bool(rec["answer"])}


def make_engine(backend, args):
    if backend == "rdf":
        return OxigraphEngine(DATA / "oxigraph_rocksdb")
    from engine_neo4j import Neo4jEngine
    return Neo4jEngine(args.neo4j_uri, "neo4j", args.neo4j_password, "mfg-bench-neo4j")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backends", default="rdf,lpg")
    ap.add_argument("--lines", type=int, default=16)
    ap.add_argument("--model", default="gpt-oss-120b")
    ap.add_argument("--max-calls", type=int, default=60)
    ap.add_argument("--seed", type=int, default=3)
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
    report = {"timestamp_utc": stamp, "model": args.model, "lines": args.lines, "tools": TOOLS, "backends": {}}
    questions = {}
    for backend in args.backends.split(","):
        kg = generate_connected_fab_graph(args.lines)      # fresh graph + truth per backend
        questions = make_questions(kg, random.Random(args.seed))
        eng = make_engine(backend, args)
        eng.load(kg)
        ex = Executor(eng, kg, seed=args.seed)
        report["backends"][backend] = {}
        for key, (q, qp) in questions.items():
            rec = run_question(gw, args.model, backend, key, q, ex)
            rec["grade"] = grade(key, rec, kg, qp)
            report["backends"][backend][key] = {"question": q, **rec}
            for parent in ex.evolved:  # restore the base TBox before the next question
                eng.s4_revert(parent)
            ex.evolved.clear()
            console.print(f"[cyan]{backend}[/cyan] {key}: tools={[t['tool'] for t in rec['trace']]} grade={rec['grade']}"
                          + (f" error={rec['error']}" if rec["error"] else ""))
        eng.close()
    report["mara_calls"] = gw.calls
    out = RESULTS / f"tool_agent_{stamp}.json"
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False))

    t = Table(title=f"Template tool calling via MARA {args.model} — v2 graph ({args.lines} lines)", show_lines=True)
    for c in ("Scenario", "Backend", "Tool calls (args)", "Tools / P / R", "LLM ms", "Tool ms", "Final answer"):
        t.add_column(c, max_width=58 if c in ("Tool calls (args)", "Final answer") else None)
    for key in questions:
        for backend, recs in report["backends"].items():
            r, g = recs[key], recs[key]["grade"]
            calls = "\n".join(f"{x['tool']}({json.dumps(x['args'], ensure_ascii=False)})" for x in r["trace"])
            t.add_row(key, backend, calls, f"{'✓' if g['tools_ok'] else '✗'} {g['precision']:.0%} {g['recall']:.0%}",
                      f"{r['llm_ms']:,.0f}", f"{r['tool_exec_ms']:,.1f}", (r["answer"] or r["error"] or "")[:280])
    console.print(t)
    console.print(f"MARA calls: {gw.calls}  Saved: {out.relative_to(RESULTS.parent)}")


if __name__ == "__main__":
    main()
