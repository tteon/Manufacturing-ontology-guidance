"""Docker lifecycle + data loading for the two servers under test.

Both servers get the SAME pinned CPU set and memory limit; the load generator is pinned to
the disjoint CPU set. Oxigraph runs as its native HTTP SPARQL server (Rust, RocksDB);
Neo4j 5.26 Community runs with Bolt.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import httpx
from neo4j import GraphDatabase


# CPU pinning (default assumes 16 hardware threads where N and N+8 are hyper-thread siblings).
# Override for other hosts, e.g. MFG_SERVER_CPUS=0-3 MFG_CLIENT_CPUS=4,5,6,7
SERVER_CPUS = os.getenv("MFG_SERVER_CPUS", "0-3,8-11")
CLIENT_CPUS = [int(x) for x in os.getenv("MFG_CLIENT_CPUS", "4,5,6,7,12,13,14,15").split(",")]
MEMORY = os.getenv("MFG_SERVER_MEMORY", "8g")

OXI_IMAGE = "ghcr.io/oxigraph/oxigraph:0.5.13"
OXI_NAME, OXI_VOL, OXI_PORT = "mfg-load-oxigraph", "mfg-load-oxigraph-data", 17878
NEO_IMAGE = "neo4j:5.26-community"
NEO_NAME, NEO_VOL, NEO_BOLT, NEO_HTTP = "mfg-load-neo4j", "mfg-load-neo4j-data", 17687, 17474
NEO_AUTH = ("neo4j", "password123")

OXI_URL = f"http://127.0.0.1:{OXI_PORT}"
NEO_URI = f"bolt://127.0.0.1:{NEO_BOLT}"


def sh(*args, check=True, capture=True):
    return subprocess.run(list(args), check=check, capture_output=capture, text=True)


def container_id(name: str) -> str:
    return sh("docker", "inspect", "-f", "{{.Id}}", name).stdout.strip()


def cpu_usage_usec(name: str) -> int | None:
    """Container CPU time from cgroup v2 (systemd or cgroupfs driver). None if unavailable (e.g. Docker Desktop)."""
    cid = container_id(name)
    for path in (f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/cpu.stat", f"/sys/fs/cgroup/docker/{cid}/cpu.stat"):
        p = Path(path)
        if p.exists():
            for line in p.read_text().splitlines():
                if line.startswith("usage_usec"):
                    return int(line.split()[1])
    return None


def remove(name: str, volume: str | None = None):
    sh("docker", "rm", "-f", name, check=False)
    if volume:
        sh("docker", "volume", "rm", "-f", volume, check=False)


# --------------------------------------------------------------------------- Oxigraph
def start_oxigraph(trig: Path, fresh: bool = True) -> dict:
    t = {}
    if fresh:
        remove(OXI_NAME, OXI_VOL)
        t0 = time.time()
        sh("docker", "run", "--rm", "--cpuset-cpus", SERVER_CPUS, "--memory", MEMORY,
           "-v", f"{OXI_VOL}:/data", "-v", f"{trig.parent.resolve()}:/import:ro",
           OXI_IMAGE, "load", "--location", "/data", "--file", f"/import/{trig.name}")
        t["load_s"] = round(time.time() - t0, 2)
    else:
        remove(OXI_NAME)
    sh("docker", "run", "-d", "--name", OXI_NAME, "--cpuset-cpus", SERVER_CPUS, "--memory", MEMORY,
       "-p", f"127.0.0.1:{OXI_PORT}:7878", "-v", f"{OXI_VOL}:/data",
       OXI_IMAGE, "serve", "--location", "/data", "--bind", "0.0.0.0:7878")
    for _ in range(120):
        try:
            if httpx.get(f"{OXI_URL}/query", params={"query": "ASK {}"}, timeout=2).status_code == 200:
                return t
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError("oxigraph did not become ready")


# --------------------------------------------------------------------------- Neo4j
def start_neo4j(fresh: bool = True):
    if fresh:
        remove(NEO_NAME, NEO_VOL)
    else:
        remove(NEO_NAME)
    sh("docker", "run", "-d", "--name", NEO_NAME, "--cpuset-cpus", SERVER_CPUS, "--memory", MEMORY,
       "-p", f"127.0.0.1:{NEO_BOLT}:7687", "-p", f"127.0.0.1:{NEO_HTTP}:7474",
       "-e", f"NEO4J_AUTH={NEO_AUTH[0]}/{NEO_AUTH[1]}",
       "-e", "NEO4J_server_memory_heap_initial__size=4G", "-e", "NEO4J_server_memory_heap_max__size=4G",
       "-e", "NEO4J_server_memory_pagecache_size=2G",
       "-v", f"{NEO_VOL}:/data", NEO_IMAGE)
    for _ in range(180):
        try:
            with GraphDatabase.driver(NEO_URI, auth=NEO_AUTH) as d:
                d.verify_connectivity()
                return
        except Exception:  # noqa: BLE001
            time.sleep(1)
    raise RuntimeError("neo4j did not become ready")


def stop_all():
    remove(OXI_NAME)
    remove(NEO_NAME)
