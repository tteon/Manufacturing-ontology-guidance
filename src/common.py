"""Shared constants, dataset helpers and timing utilities."""

from __future__ import annotations

import re
import statistics
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RESULTS = ROOT / "results"
TRIG_PATH = DATA / "mfg_knowledge.trig"

EX = "http://example.org/mfg#"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
SOSA = "http://www.w3.org/ns/sosa/"
XSD = "http://www.w3.org/2001/XMLSchema#"

PREFIXES = f"""PREFIX ex: <{EX}>
PREFIX rdf: <{RDF}>
PREFIX rdfs: <{RDFS}>
PREFIX sosa: <{SOSA}>
PREFIX xsd: <{XSD}>
"""

# Synthetic equipment classes used by the scale sweep. 3 of 8 classes fall under
# GasSupplyModule/PlasmaModule, so ~37.5% of synthetic instances are affected by
# the Scenario 4 schema change.
SYNTHETIC_CLASSES = [
    "MFC", "GasBox", "RFGenerator", "Heater",
    "CVDChamber", "EtchChamber", "LithoChamber", "Seal",
]
AFFECTED_CLASSES = {"MFC", "GasBox", "RFGenerator"}

_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")


def safe_id(value: str) -> str:
    """Validate an entity/class local name before it is placed in a query."""
    if not isinstance(value, str) or not _ID.match(value):
        raise ValueError(f"unsafe or invalid identifier: {value!r}")
    return value


def local(iri: str) -> str:
    for sep in ("#", "/"):
        if sep in iri:
            iri = iri.rsplit(sep, 1)[1]
    return iri


def synthetic_rows(n: int):
    """Deterministic synthetic instances: (local_id, class_local_name)."""
    return [(f"Syn_{i:07d}", SYNTHETIC_CLASSES[i % len(SYNTHETIC_CLASSES)]) for i in range(n)]


def expected_affected(n: int) -> int:
    base = 3  # MFC_01, GasBox_A, RF_Gen_01
    return base + sum(1 for _, c in synthetic_rows(n) if c in AFFECTED_CLASSES)


def ms(ns: int) -> float:
    return ns / 1e6


def timed(fn: Callable, *args, **kwargs):
    t0 = time.perf_counter_ns()
    out = fn(*args, **kwargs)
    return out, ms(time.perf_counter_ns() - t0)


def summarize(samples: list[float]) -> dict:
    s = sorted(samples)
    p95 = s[min(len(s) - 1, max(0, int(round(0.95 * len(s))) - 1))]
    return {
        "n": len(s),
        "median_ms": round(statistics.median(s), 4),
        "p95_ms": round(p95, 4),
        "min_ms": round(s[0], 4),
        "max_ms": round(s[-1], 4),
    }


def bench_read(fn: Callable, warmup: int, reps: int):
    """Run fn warmup+reps times; return (last_result, summary)."""
    out = None
    for _ in range(warmup):
        out = fn()
    samples = []
    for _ in range(reps):
        out, dt = timed(fn)
        samples.append(dt)
    return out, summarize(samples)
