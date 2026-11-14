"""MARA Cloud API gateway (OpenAI-compatible) used by the tool-calling agent.

Credential handling: the key is read from the project-root ``.env`` (or an
already-exported ``MARA_API_KEY``; ``MARA_ENV_FILE`` may point at another .env) and is never
logged. Admission uses a file lock (``MARA_ADMISSION_LOCK`` or results/.mara.lock): one live
call at a time across processes, refusal when an
unresolved ``.pending`` receipt exists, a bounded logical-call budget, and no
automatic retry of ambiguous (network/timeout) outcomes.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import httpx

from common import ROOT

ENV_FILES = [ROOT / ".env"] + ([Path(os.environ["MARA_ENV_FILE"])] if os.getenv("MARA_ENV_FILE") else [])
ENV_KEYS = ("MARA_API_KEY", "MARA_ADMISSION_LOCK", "MARA_BASE_URL")
DEFAULT_LOCK = ROOT / "results" / ".mara.lock"
DEFAULT_BASE_URL = "https://api.cloud.mara.com/v1"


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
                if self.pending.exists():
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
            self.last_latency_ms = latency  # pure HTTP time (excludes admission wait)
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
