"""LLM end-to-end concurrency test: N concurrent tool-calling agents (MARA, gpt-oss-120b) against the
Oxigraph HTTP server and Neo4j Bolt server started by loadtest/setup.py.

Concurrent admission is opt-in and specific to this load test: MARA calls are admitted by an
in-process semaphore of size C (instead of the one-call file lock used by src/llm_agent.py).
Safety retained:
  * refuses to start if the shared lock's .pending receipt (or any local pending receipt) exists
  * every request writes a per-request .pending receipt; it is removed only after an HTTP status is
    received. Ambiguous network outcomes leave it in place, are never retried, and stop new admissions.
  * hard budget on total API calls
  * received 429 / 5xx -> bounded exponential backoff retries (outcome known, safe to retry)

Questions are generated from the same graph (pools.json) with real IDs; tool results are graded with
precision/recall against ground truth. S4 uses a unique parent class per question (no cross-agent conflict)
and is reverted; S5 writes keep values on the same side of the threshold, so anomaly counts stay valid.

Usage: uv run python loadtest/llm_load.py --lines 1580 --conc 1,2,4,8,16,32 --per-level 32
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from neo4j import AsyncGraphDatabase

sys.path.insert(0, str(Path(__file__).parent))
import servers  # noqa: E402
from ops import canon_rows  # noqa: E402  (adds src/ to sys.path)
from common import PREFIXES, S1_MIN_CONF, S4_POLICY, precision_recall, safe_id  # noqa: E402
from llm_agent import DEFAULT_BASE_URL, load_env  # noqa: E402
from queries import SPARQL, cypher  # noqa: E402
from tool_agent import BACKEND_LABEL, SYSTEM, TOOLS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------------------- backend executors (async)
class Backend:
    def __init__(self, kind: str, pools: dict, n_parts_anomalous: int, conc: int):
        self.kind, self.pools = kind, pools
        if kind == "rdf":
            self.http = httpx.AsyncClient(limits=httpx.Limits(max_connections=conc * 2 + 4), timeout=120)
        else:
            self.drv = AsyncGraphDatabase.driver(servers.NEO_URI, auth=servers.NEO_AUTH,
                                                 max_connection_pool_size=conc * 2 + 4)
        self.anomalous_truth = n_parts_anomalous
        self.rng = random.Random(7)
        self.created = []

    async def exists(self, cls):
        if self.kind == "rdf":
            r = await self.http.post(f"{servers.OXI_URL}/query", data={"query": SPARQL["class_exists"]({"cls": cls})},
                                     headers={"Accept": "application/sparql-results+json"})
            r.raise_for_status()
            return bool(r.json()["boolean"])
        return bool((await self.cy(cypher("class_exists"), cls=cls))[0]["n"])

    async def close(self):
        if self.kind == "rdf":
            await self.http.aclose()
        else:
            await self.drv.close()

    async def sparql(self, q):
        r = await self.http.post(f"{servers.OXI_URL}/query", data={"query": q},
                                 headers={"Accept": "application/sparql-results+json"})
        r.raise_for_status()
        return r.json()["results"]["bindings"]

    async def sparql_update(self, u):
        (await self.http.post(f"{servers.OXI_URL}/update", data={"update": u})).raise_for_status()

    async def cy(self, q, **params):
        async with self.drv.session() as s:
            res = await s.run(q, **params)
            return await res.data()

    async def call(self, name, a):
        rdf = self.kind == "rdf"
        if name == "find_root_causes":
            p = {"lot": safe_id(a["lot_id"]), "min_conf": float(a.get("min_confidence", S1_MIN_CONF))}
            rows = await (self.sparql(SPARQL["S1"](p)) if rdf else self.cy(cypher("S1"), lot=p["lot"], min_conf=p["min_conf"]))
            return canon_rows("S1", rows)
        if name == "trace_contamination":
            c = safe_id(a["component_id"])
            rows = await (self.sparql(SPARQL["S2"]({"component": c})) if rdf else self.cy(cypher("S2"), component=c))
            return canon_rows("S2", rows)
        if name == "classify_sensor_anomalies":
            line = safe_id(a["line_id"])
            rows = await (self.sparql(SPARQL["S3"]({"line": line})) if rdf else self.cy(cypher("S3"), line=line))
            return canon_rows("S3", rows)
        if name == "count_sensor_anomalies":
            rows = await (self.sparql(SPARQL["S3_global_count"]({})) if rdf else self.cy(cypher("S3_global_count")))
            from queries import canon
            return {"anomalous_parts": canon("S3_global_count", rows)}
        if name == "add_superclass":
            parent, subs, pol = safe_id(a["new_parent_class"]), [safe_id(x) for x in a["subclasses"]], safe_id(a["required_policy"])
            if await self.exists(parent):
                raise ValueError(f"class {parent} already exists; choose a new parent class name")
            missing = [c for c in subs if not await self.exists(c)]
            if missing:
                raise ValueError(f"unknown subclasses: {missing}")
            self.created.append(parent)
            if rdf:
                ins = PREFIXES + "INSERT DATA { GRAPH ex:TBox_Graph { " + " ".join(
                    f"ex:{c} rdfs:subClassOf ex:{parent} ." for c in subs) + f' ex:{parent} ex:requiredPolicy "{pol}" . }} }}'
                await self.sparql_update(ins)
            else:
                async with self.drv.session() as s:
                    await (await s.run(cypher("S4_tbox_write"), parent=parent, subs=subs, policy=pol)).consume()
            return {"status": "ok", "abox_facts_written": 0}
        if name == "list_equipment_by_class":
            cls = safe_id(a["class_id"])
            rows = await (self.sparql(SPARQL["list_by_class"]({"cls": cls})) if rdf else self.cy(cypher("list_by_class"), cls=cls))
            return [{"eq": (r["eq"]["value"] if rdf else r["eq"]).rsplit("#", 1)[-1]} for r in rows]
        if name == "batch_stream_update":
            n = min(int(a["count"]), len(self.pools["W"]))
            rows = []
            for w in self.rng.sample(self.pools["W"], n):
                thr = w["thr"]
                rows.append((w["obs"], round(thr * (self.rng.uniform(1.05, 1.5) if w["above"] else self.rng.uniform(0.2, 0.95)), 3)))
            if rdf:
                await self.sparql_update(SPARQL["S5_batch"]({"rows": rows}))
            else:
                async with self.drv.session() as s:
                    await (await s.run(cypher("S5_batch"), rows=[{"o": o, "v": v} for o, v in rows])).consume()
            return {"updated_observations": n, "transactions": 1}
        if name == "check_sensor_threshold":
            e = safe_id(a["entity_id"])
            rows = await (self.sparql(SPARQL["obs_of"]({"entity": e})) if rdf else self.cy(cypher("obs_of"), entity=e))
            return [{k: (r[k]["value"] if rdf else r[k]) for k in ("o", "v", "max")} for r in rows]
        raise KeyError(name)

    async def revert_parent(self, parent):
        if self.kind == "rdf":
            await self.sparql_update(SPARQL["S4_revert"]({"parent": parent}))
        else:
            async with self.drv.session() as s:
                await (await s.run(cypher("S4_revert_node"), parent=parent)).consume()


# ----------------------------------------------------------------------------- MARA async gateway (semaphore)
class Gateway:
    def __init__(self, conc, budget, receipts: Path, state_dir: Path, model: str):
        load_env()
        self.key = os.environ.get("MARA_API_KEY")
        if not self.key:
            raise SystemExit("MARA_API_KEY missing")
        self.base = (os.getenv("MARA_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        shared = os.getenv("MARA_ADMISSION_LOCK")
        self.shared_pending = Path(shared).with_suffix(".pending") if shared else None
        self.state, self.receipts, self.model = state_dir, receipts, model
        self.state.mkdir(parents=True, exist_ok=True)
        self.sem = asyncio.Semaphore(conc)
        self.budget, self.calls, self.halted = budget, 0, None
        self.client = httpx.AsyncClient(timeout=180, limits=httpx.Limits(max_connections=conc + 2))
        self.check_clear()

    def check_clear(self):
        if self.shared_pending and self.shared_pending.exists():
            raise SystemExit("shared MARA pending receipt exists; refusing")
        if any(self.state.glob("*.pending")):
            raise SystemExit(f"unresolved local pending receipts in {self.state}; refusing")

    def _log(self, rec):
        with self.receipts.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False).replace(self.key, "[REDACTED]") + "\n")

    async def chat(self, body, tag):
        delay = 1.0
        for attempt in range(5):
            if self.halted:
                raise RuntimeError(f"halted: {self.halted}")
            if self.calls >= self.budget:
                raise RuntimeError("call budget exhausted")
            self.calls += 1
            pend = self.state / f"{uuid.uuid4()}.pending"
            async with self.sem:
                pend.write_text(json.dumps({"tag": tag, "attempt": attempt}))
                t0 = time.perf_counter()
                try:
                    r = await self.client.post(f"{self.base}/chat/completions", json=body,
                                               headers={"Authorization": f"Bearer {self.key}"})
                except httpx.HTTPError as e:
                    self.halted = f"ambiguous network outcome {type(e).__name__} ({tag}); pending kept: {pend.name}"
                    self._log({"tag": tag, "status": None, "error": self.halted})
                    raise RuntimeError(self.halted)
                lat = (time.perf_counter() - t0) * 1000
                pend.unlink()
            try:
                data = r.json()
            except ValueError:
                data = {"raw": r.text[:500]}
            msg = (data.get("choices") or [{}])[0].get("message", {}) if isinstance(data, dict) else {}
            self._log({"tag": tag, "status": r.status_code, "latency_ms": round(lat, 1), "attempt": attempt,
                       "usage": data.get("usage") if isinstance(data, dict) else None,
                       "tool_calls": msg.get("tool_calls"), "content": msg.get("content"),
                       "error_body": data if r.status_code != 200 else None})
            if r.status_code == 200:
                return data, lat
            if r.status_code == 429 or r.status_code >= 500:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if r.status_code == 400 and body.get("temperature", 0) == 0:
                body = dict(body, temperature=0.7)   # known gpt-oss incomplete tool call
                continue
            raise RuntimeError(f"HTTP {r.status_code}")
        raise RuntimeError("retries exhausted")


# ----------------------------------------------------------------------------- questions & grading
def make_question(rng, pools, qid):
    kind = rng.choice(["S1", "S2", "S3", "S4", "S5"])
    if kind == "S1":
        p = rng.choice(pools["S1"])
        return kind, f"{p['lot']}에서 이상 증상이 나왔어. 증상→원인→부품 경로의 누적 신뢰도가 {S1_MIN_CONF} 이상인 근본 원인과 책임 부품을 찾아줘.", p
    if kind == "S2":
        p = rng.choice(pools["S2"])
        return kind, f"{p['component']} 부품에서 결함이 발견됐어. partOf와 feedsInto를 따라 오염이 전파됐을 수 있는 의심 로트를 모두 추적해줘.", p
    if kind == "S3":
        p = rng.choice(pools["S3"])
        return kind, f"{p['line']} 라인에서 센서 측정값이 사양(maxAllowedDrift)을 초과한 부품을 찾아줘.", p
    if kind == "S4":
        parent = f"CriticalHazardSystem_Q{qid}"
        return kind, (f"새 안전 규정: GasSupplyModule과 PlasmaModule을 모두 {parent}라는 새로운 상위 클래스로 묶고, "
                      f"{parent}에 속한 설비는 {S4_POLICY} 대상이라고 스키마(TBox)를 업데이트한 뒤, 해당 설비 목록을 뽑아줘."), {"parent": parent}
    return kind, "센서 관측값 50개가 CDC 배치로 한 번에 들어온 상황을 시뮬레이션해서 반영하고, 반영 후 사양을 초과한 부품이 전체 몇 개인지 알려줘.", {}


EXPECTED = {"S1": {"find_root_causes"}, "S2": {"trace_contamination"}, "S3": {"classify_sensor_anomalies"},
            "S4": {"add_superclass", "list_equipment_by_class"}, "S5": {"batch_stream_update", "count_sensor_anomalies"}}


def grade(kind, trace, p, truth_s4, anomalous):
    used = {t["tool"] for t in trace if t["ok"]}
    res = {t["tool"]: t["result"] for t in trace if t["ok"]}
    if kind == "S1":
        pr = precision_recall([tuple(x) for x in res.get("find_root_causes", [])], p["truth"])
    elif kind == "S2":
        pr = precision_recall(res.get("trace_contamination", []), p["truth"])
    elif kind == "S3":
        pr = precision_recall(res.get("classify_sensor_anomalies", []), p["truth"])
    elif kind == "S4":
        pr = precision_recall(sorted(x["eq"] for x in res.get("list_equipment_by_class", [])), truth_s4)
    else:
        ok = res.get("count_sensor_anomalies", {}).get("anomalous_parts") == anomalous and \
            res.get("batch_stream_update", {}).get("updated_observations") == 50
        pr = (float(ok), float(ok))
    return {"tools_ok": EXPECTED[kind] <= used, "precision": pr[0], "recall": pr[1]}


async def agent(gw, be, backend, kind, question, p, qid, truth_s4, anomalous, model):
    messages = [{"role": "system", "content": SYSTEM.format(backend=BACKEND_LABEL[backend])},
                {"role": "user", "content": question}]
    trace, llm_ms, tool_ms, t0, err, answer, turns = [], 0.0, 0.0, time.perf_counter(), None, None, 0
    try:
        for turns in range(1, 7):
            data, lat = await gw.chat({"model": model, "messages": messages, "tools": TOOLS, "tool_choice": "auto",
                                       "reasoning_effort": "high", "temperature": 0}, f"{backend}/{kind}/q{qid}")
            llm_ms += lat
            msg = data["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            if not calls:
                answer = msg.get("content")
                break
            messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
            for c in calls:
                name = c["function"]["name"]
                ts = time.perf_counter()
                try:
                    args = json.loads(c["function"]["arguments"] or "{}")
                    result, ok = json.loads(json.dumps(await be.call(name, args))), True
                except Exception as e:  # noqa: BLE001
                    args, result, ok = c["function"]["arguments"], {"error": f"{type(e).__name__}: {e}"}, False
                tool_ms += (time.perf_counter() - ts) * 1000
                trace.append({"tool": name, "args": args, "ok": ok, "result": result})
                content = result if not isinstance(result, list) or len(result) <= 60 else {"rows": len(result), "first_60": result[:60]}
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(content, ensure_ascii=False)})
    except RuntimeError as e:
        err = str(e)
    finally:
        # Revert exactly the parent classes this question created (whatever name the model used).
        for t in trace:
            if t["tool"] == "add_superclass" and t["ok"]:
                try:
                    await be.revert_parent(t["args"]["new_parent_class"])
                except Exception:  # noqa: BLE001
                    pass
    g = grade(kind, trace, p, truth_s4, anomalous)
    return {"qid": qid, "kind": kind, "e2e_ms": round((time.perf_counter() - t0) * 1000, 1), "llm_ms": round(llm_ms, 1),
            "tool_ms": round(tool_ms, 2), "turns": turns, "error": err, "answered": bool(answer), **g,
            "tools": [t["tool"] for t in trace]}


async def run_level(gw, backend, conc, n, pools, truth_s4, anomalous, model, seed):
    be = Backend(backend, pools, anomalous, conc)
    rng = random.Random(seed)
    qs = [make_question(rng, pools, f"{backend}{conc}_{i}") for i in range(n)]
    sem = asyncio.Semaphore(conc)

    async def one(i, q):
        async with sem:
            return await agent(gw, be, backend, q[0], q[1], q[2], f"{backend}{conc}_{i}", truth_s4, anomalous, model)

    t0 = time.perf_counter()
    res = await asyncio.gather(*(one(i, q) for i, q in enumerate(qs)))
    wall = time.perf_counter() - t0
    await be.close()
    return res, wall


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(q * len(v)))] if v else None


async def main_async(args):
    work = ROOT / "loadtest" / "work" / f"L{args.lines}"
    pools = json.loads((work / "pools.json").read_text())
    setup = json.loads((work / "setup.json").read_text())
    truth_s4 = sorted(setup["truth_s4"]) if "truth_s4" in setup else None
    if truth_s4 is None or "anomalous" not in setup:
        from common import generate_connected_fab_graph
        kg = generate_connected_fab_graph(args.lines)
        truth_s4, anomalous = kg.truth_s4(), kg.truth_s3_global()
    else:
        anomalous = setup["anomalous"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "results" / "loadtest" / f"llm_L{args.lines}_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    concs = [int(c) for c in args.conc.split(",")]
    gw = Gateway(max(concs), args.budget, out / "mara_receipts.jsonl", ROOT / "results" / "llm_state", args.model)
    summary = []
    rng = random.Random(args.seed)
    cells = [(b, c) for b in args.backends.split(",") for c in concs]
    rng.shuffle(cells)
    for backend, conc in cells:
        gw.sem = asyncio.Semaphore(conc)
        res, wall = await run_level(gw, backend, conc, args.per_level, pools, truth_s4, anomalous, args.model,
                                    rng.randrange(1 << 30))
        e2e = [r["e2e_ms"] for r in res if not r["error"]]
        row = {"backend": backend, "conc": conc, "questions": len(res), "wall_s": round(wall, 1),
               "throughput_q_per_min": round(60 * len(res) / wall, 2),
               "e2e_p50_ms": pct(e2e, .5), "e2e_p90_ms": pct(e2e, .9), "e2e_p99_ms": pct(e2e, .99),
               "llm_share": round(sum(r["llm_ms"] for r in res) / max(1, sum(r["e2e_ms"] for r in res)), 3),
               "tool_ms_p50": pct([r["tool_ms"] for r in res], .5),
               "errors": sum(1 for r in res if r["error"]), "tools_ok": sum(r["tools_ok"] for r in res),
               "full_pr": sum(1 for r in res if r["precision"] == 1 and r["recall"] == 1),
               "answered": sum(r["answered"] for r in res), "mara_calls_total": gw.calls}
        summary.append(row)
        (out / f"{backend}_c{conc}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
        print(json.dumps(row), flush=True)
        if gw.halted:
            print("HALTED:", gw.halted)
            break
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    await gw.client.aclose()
    print("saved", out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lines", type=int, default=1580)
    ap.add_argument("--backends", default="rdf,lpg")
    ap.add_argument("--conc", default="1,2,4,8,16,32")
    ap.add_argument("--per-level", type=int, default=32)
    ap.add_argument("--budget", type=int, default=1600)
    ap.add_argument("--model", default="gpt-oss-120b")
    ap.add_argument("--seed", type=int, default=5)
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
