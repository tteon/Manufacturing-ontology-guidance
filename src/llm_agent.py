"""Natural-language -> QueryIntent routing via the MARA Cloud API (OpenAI-compatible),
with a deterministic rule-based fallback.

Credential handling: the key is read from the project-root ``.env`` (or an
already-exported ``MARA_API_KEY``) and is never logged. Admission reuses the
shared MARA lock: one live call at a time across processes, refusal when an
unresolved ``.pending`` receipt exists, a bounded logical-call budget, and no
automatic retry of ambiguous (network/timeout) outcomes.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import List, Literal, Optional

import httpx
from pydantic import BaseModel, Field, ValidationError

from common import ROOT

ENV_FILES = [ROOT / ".env", ROOT.parent.parent / ".env"]  # project .env, then an enclosing repo's .env
ENV_KEYS = ("MARA_API_KEY", "MARA_ADMISSION_LOCK", "MARA_BASE_URL")
DEFAULT_LOCK = ROOT / "results" / ".mara.lock"
DEFAULT_BASE_URL = "https://api.cloud.mara.com/v1"


class QueryIntent(BaseModel):
    scenario_type: Literal[
        "rca_probabilistic",
        "impact_propagation",
        "sensor_classification",
        "tbox_schema_evolution",
        "abox_mutation_stress",
    ] = Field(description="Scenario that matches the user question")
    target_entity_id: Optional[str] = Field(default=None, description="Entity to analyse or update (local id, e.g. Lot_101, MFC_01, ORing_Seal_04)")
    symptom_or_defect_id: Optional[str] = Field(default=None, description="Symptom or defect id (e.g. Thickness_Anomaly, Micro_Leak)")
    min_confidence: float = Field(default=0.70, description="Minimum cumulative confidence for probabilistic RCA")
    new_parent_class: Optional[str] = Field(default=None, description="New superclass to add on TBox evolution")
    target_subclasses: Optional[List[str]] = Field(default=None, description="Existing classes to place under the new superclass")
    mutation_iterations: int = Field(default=100, description="Number of consecutive ABox state overwrites")


SCENARIO_QUERIES = {
    "S1": "Lot_101에서 Thickness_Anomaly 증상이 나왔어. 3단계 인과 체인의 누적 신뢰도가 0.70 이상인 근본 원인과 책임 설비를 찾아줘.",
    "S2": "ORing_Seal_04 부품에서 Micro_Leak 결함이 발견됐어. partOf와 feedsInto를 따라 오염이 전파됐을 수 있는 챔버와 오염 의심 로트를 추적해줘.",
    "S3": "센서 관측값(hasSimpleResult)이 설비 클래스의 maxAllowedDrift를 초과한 고위험 설비와 그 상위 카테고리를 분류해줘.",
    "S4": "새로운 환경 안전 규정에 따라 GasSupplyModule과 PlasmaModule을 모두 CriticalHazardSystem이라는 새로운 상위 클래스로 묶고, "
          "이 클래스에 속한 모든 설비는 즉시 비상 인터록 점검(Emergency_Interlock_Check) 대상이 된다고 스키마(TBox)를 업데이트해 줘. "
          "그리고 현재 이 규정에 해당하는 현장 설비 목록을 바로 뽑아줘.",
    "S5": "MFC_01 설비의 실시간 센서 편차값이 스트리밍으로 100번 연속 업데이트되는 상황을 시뮬레이션하고, 최종 상태값이 임계치(5.0)를 넘었는지 판별해 줘.",
}


# --------------------------------------------------------------------------- rules
def rule_parse(text: str) -> QueryIntent:
    ids = re.findall(r"\b[A-Z][A-Za-z]*_[A-Za-z0-9_]+\b", text, re.A)
    t = text.lower()
    if "tbox" in t or "스키마" in text or "상위 클래스" in text:
        classes = re.findall(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b", text, re.A)
        parent = next((c for c in classes if c not in ("GasSupplyModule", "PlasmaModule")), "CriticalHazardSystem")
        subs = [c for c in classes if c in ("GasSupplyModule", "PlasmaModule")]
        return QueryIntent(scenario_type="tbox_schema_evolution", new_parent_class=parent, target_subclasses=subs)
    if "스트리밍" in text or "연속 업데이트" in text:
        n = re.search(r"(\d+)\s*번", text)
        return QueryIntent(scenario_type="abox_mutation_stress", target_entity_id=ids[0] if ids else None,
                           mutation_iterations=int(n.group(1)) if n else 100)
    if "전파" in text or "오염" in text:
        return QueryIntent(scenario_type="impact_propagation", target_entity_id=ids[0], symptom_or_defect_id=ids[1])
    if "신뢰도" in text or "원인" in text:
        c = re.search(r"0\.\d+", text)
        return QueryIntent(scenario_type="rca_probabilistic", target_entity_id=ids[0], symptom_or_defect_id=ids[1],
                           min_confidence=float(c.group()) if c else 0.70)
    return QueryIntent(scenario_type="sensor_classification")


# --------------------------------------------------------------------------- MARA
def load_env() -> None:
    """Fill MARA_* variables from .env files without overriding exported values."""
    for env in ENV_FILES:
        if not env.exists():
            continue
        for line in env.read_text().splitlines():
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k in ENV_KEYS and v and not os.getenv(k):
                os.environ[k] = v


def load_env_key() -> Optional[str]:
    load_env()
    return os.getenv("MARA_API_KEY") or None


class MaraUnavailable(RuntimeError):
    pass


class MaraGateway:
    def __init__(self, receipts: Path, max_calls: int = 8, model: str = "gpt-oss-120b",
                 min_interval_s: float = 1.0, timeout_s: float = 90.0):
        self.key = load_env_key()
        if not self.key:
            raise MaraUnavailable("MARA_API_KEY not found in environment or .env")
        # Cross-process admission lock. Point MARA_ADMISSION_LOCK at a shared file to serialize
        # calls with other experiments; otherwise a project-local lock is created.
        self.lock = Path(os.getenv("MARA_ADMISSION_LOCK") or DEFAULT_LOCK)
        if not os.getenv("MARA_ADMISSION_LOCK"):
            self.lock.parent.mkdir(parents=True, exist_ok=True)
            self.lock.touch(exist_ok=True)
        if not self.lock.exists():
            raise MaraUnavailable(f"MARA admission lock missing: {self.lock}")
        self.base_url = (os.getenv("MARA_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.pending = self.lock.with_suffix(".pending")
        self.receipts, self.max_calls, self.model = receipts, max_calls, model
        self.min_interval_s, self.timeout_s = min_interval_s, timeout_s
        self.calls = 0
        self.tag = None  # free-form label copied into receipts (e.g. "rdf/S4")
        self.client = httpx.Client(timeout=timeout_s, follow_redirects=False)

    def _receipt(self, rec: dict):
        self.receipts.parent.mkdir(parents=True, exist_ok=True)
        with self.receipts.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False).replace(self.key, "[REDACTED]") + "\n")

    @contextmanager
    def _admission(self):
        with self.lock.open("a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                if self.pending.exists() or any(self.lock.parent.glob("*/http-state/*.pending")):
                    raise MaraUnavailable("Unresolved MARA outcome (pending receipt present); refusing admission")
                time.sleep(self.min_interval_s)
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        if self.calls >= self.max_calls:
            raise MaraUnavailable("logical call budget exhausted")
        self.calls += 1
        rhash = hashlib.sha256(json.dumps({"path": path, "body": body}, sort_keys=True).encode()).hexdigest()
        with self._admission():
            with self.pending.open("x") as f:
                json.dump({"run": "mfg_ontology_bench", "request_hash": rhash, "path": path}, f)
            t0 = time.perf_counter()
            try:
                resp = self.client.request(method, self.base_url + path, json=body,
                                           headers={"Authorization": f"Bearer {self.key}"})
            except httpx.HTTPError as e:
                # Ambiguous outcome: leave the pending receipt in place, never retry.
                self._receipt({"path": path, "request_hash": rhash, "status": None,
                               "error": f"{type(e).__name__}: ambiguous network outcome; .pending left in place"})
                raise MaraUnavailable(f"ambiguous network outcome ({type(e).__name__}); pending receipt kept")
            latency = round((time.perf_counter() - t0) * 1000, 1)
            self.pending.unlink()
        rec = {"path": path, "request_hash": rhash, "status": resp.status_code, "latency_ms": latency}
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text[:2000]}
        if path == "/chat/completions":
            rec["model"] = body["model"]
            rec["usage"] = data.get("usage")
            choice = (data.get("choices") or [{}])[0]
            rec["finish_reason"] = choice.get("finish_reason")
            rec["content"] = choice.get("message", {}).get("content")
            if choice.get("message", {}).get("tool_calls"):
                rec["tool_calls"] = choice["message"]["tool_calls"]
            if resp.status_code != 200:
                rec["error_body"] = data
            if self.tag:
                rec["tag"] = self.tag
        else:
            rec["models"] = [m.get("id") for m in data.get("data", [])] if isinstance(data, dict) else None
        self._receipt(rec)
        if resp.status_code != 200:
            raise MaraUnavailable(f"HTTP {resp.status_code}")
        return data

    def health(self) -> list[str]:
        data = self.request("GET", "/models")
        return [m.get("id") for m in data.get("data", [])]


SYSTEM_PROMPT = (
    "You route questions about a semiconductor manufacturing knowledge graph to one of five scenarios. "
    "Return ONLY a JSON object (no prose, no code fences) that validates against this JSON Schema:\n"
    + json.dumps(QueryIntent.model_json_schema(), ensure_ascii=False)
    + "\nEntity, symptom, defect and class identifiers must be copied verbatim from the question (e.g. Lot_101, "
    "Thickness_Anomaly, ORing_Seal_04, Micro_Leak, MFC_01, GasSupplyModule)."
)


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("no JSON object in model output")
    return json.loads(m.group())


class IntentRouter:
    """LLM router with rule fallback; records which path produced each intent."""

    def __init__(self, use_llm: bool, receipts: Path, model: str = "gpt-oss-120b"):
        self.gateway, self.status = None, "rules-only (LLM disabled)"
        self.model = model
        if use_llm:
            try:
                gw = MaraGateway(receipts, model=model)
                models = gw.health()
                if model not in models:
                    raise MaraUnavailable(f"{model} not listed by /models")
                self.gateway, self.status = gw, f"MARA {model} (healthy)"
            except MaraUnavailable as e:
                self.status = f"rules fallback: {e}"

    def parse(self, text: str) -> tuple[QueryIntent, dict]:
        rule = rule_parse(text)
        meta = {"source": "rules", "rule_intent": rule.model_dump()}
        if not self.gateway:
            return rule, meta
        body = {"model": self.model, "temperature": 0,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                             {"role": "user", "content": text}]}
        try:
            data = self.gateway.request("POST", "/chat/completions", body)
            intent = QueryIntent.model_validate(_extract_json(data["choices"][0]["message"]["content"]))
            meta.update(source="llm", llm_intent=intent.model_dump(),
                        agrees_with_rules=intent.model_dump() == rule.model_dump())
            return intent, meta
        except (MaraUnavailable, ValidationError, ValueError, KeyError, IndexError) as e:
            meta.update(source="rules (llm failed)", llm_error=str(e)[:300])
            return rule, meta
