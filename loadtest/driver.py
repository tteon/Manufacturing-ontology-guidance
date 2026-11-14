"""Load generator: multiprocess + asyncio clients pinned to CLIENT_CPUS.

closed loop : C concurrent clients, each issues the next request when the previous completes.
open loop   : Poisson arrivals at a fixed total rate; latency is measured from the *scheduled*
              send time (no coordinated omission). Outstanding requests are capped; arrivals
              that find the cap full are counted as "dropped" (offered but not served).

Every read response is canonicalized and compared to the generator's ground truth.
Each run returns per-request samples (op, t_rel_s, latency_ms, ok, correct) for the measured window
plus client CPU usage and server CPU usage (cgroup usage_usec).
"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import os
import random
import resource
import time

import httpx
from neo4j import AsyncGraphDatabase

from ops import canon_rows, correct, cypher_for, pick, sparql_for
import servers


class OxiClient:
    def __init__(self, conns: int):
        self.c = httpx.AsyncClient(limits=httpx.Limits(max_connections=conns, max_keepalive_connections=conns),
                                   timeout=httpx.Timeout(120.0))

    async def run(self, op, p):
        if op == "W":
            r = await self.c.post(f"{servers.OXI_URL}/update", data={"update": sparql_for(op, p)})
            r.raise_for_status()
            return None
        r = await self.c.post(f"{servers.OXI_URL}/query", data={"query": sparql_for(op, p)},
                              headers={"Accept": "application/sparql-results+json"})
        r.raise_for_status()
        return canon_rows(op, r.json()["results"]["bindings"])

    async def close(self):
        await self.c.aclose()


class NeoClient:
    def __init__(self, conns: int):
        self.d = AsyncGraphDatabase.driver(servers.NEO_URI, auth=servers.NEO_AUTH,
                                           max_connection_pool_size=max(conns, 1),
                                           connection_acquisition_timeout=120)

    async def run(self, op, p):
        q, params = cypher_for(op, p)
        async with self.d.session() as s:   # auto-commit transaction, no driver-side retries
            res = await s.run(q, **params)
            if op == "W":
                await res.consume()
                return None
            return canon_rows(op, await res.data())

    async def close(self):
        await self.d.close()


def make_client(engine, conns):
    return OxiClient(conns) if engine == "oxigraph" else NeoClient(conns)


async def _one(cli, op, p, truth, t_sched, t0, warm_end, end, out):
    try:
        got = await cli.run(op, p)
        ok, corr, err = True, correct(op, got, truth), None
    except asyncio.CancelledError:
        ok, corr, err = False, False, "Cancelled(still pending at end)"
    except Exception as e:  # noqa: BLE001
        ok, corr, err = False, False, type(e).__name__
    t_done = time.perf_counter()
    if warm_end <= t_sched < end:
        out.append((op, round(t_sched - t0, 4), round((t_done - t_sched) * 1000, 3), ok, corr, err))


async def _closed(engine, mix, pools, tasks, seed, t0, warm_end, end, out):
    cli = make_client(engine, tasks)

    async def worker(k):
        rng = random.Random(seed * 1000 + k)
        while time.perf_counter() < end:
            op, p, truth = pick(rng, mix, pools)
            await _one(cli, op, p, truth, time.perf_counter(), t0, warm_end, end, out)

    await asyncio.gather(*(worker(k) for k in range(tasks)))
    await cli.close()


async def _open(engine, mix, pools, rate, cap, seed, t0, warm_end, end, out, dropped):
    cli = make_client(engine, cap)
    rng = random.Random(seed)
    sem = asyncio.Semaphore(cap)
    pending = set()
    t_next = time.perf_counter()

    async def fire(op, p, truth, t_sched):
        try:
            await _one(cli, op, p, truth, t_sched, t0, warm_end, end, out)
        finally:
            sem.release()

    while t_next < end:
        delay = t_next - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        op, p, truth = pick(rng, mix, pools)
        if sem.locked():
            if warm_end <= t_next < end:
                dropped[0] += 1
        else:
            await sem.acquire()
            task = asyncio.create_task(fire(op, p, truth, t_next))
            pending.add(task)
            task.add_done_callback(pending.discard)
        t_next += rng.expovariate(rate)
    if pending:
        _, still = await asyncio.wait(pending, timeout=150)
        dropped[0] += len(still)          # never completed: counted, not silently lost
        for t in still:
            t.cancel()
    await cli.close()


def _proc(args, q):
    (engine, mode, mix, pools, tasks, rate, cap, seed, t0_wall, warmup, measure, cpu) = args
    os.sched_setaffinity(0, {cpu})
    # Align all processes on a shared wall-clock start.
    while time.time() < t0_wall:
        time.sleep(0.001)
    t0 = time.perf_counter()
    warm_end, end = t0 + warmup, t0 + warmup + measure
    out, dropped = [], [0]
    ru0 = resource.getrusage(resource.RUSAGE_SELF)
    if mode == "closed":
        asyncio.run(_closed(engine, mix, pools, tasks, seed, t0, warm_end, end, out))
    else:
        asyncio.run(_open(engine, mix, pools, rate, cap, seed, t0, warm_end, end, out, dropped))
    ru1 = resource.getrusage(resource.RUSAGE_SELF)
    cpu_s = (ru1.ru_utime - ru0.ru_utime) + (ru1.ru_stime - ru0.ru_stime)
    q.put({"samples": out, "dropped": dropped[0], "client_cpu_s": cpu_s, "wall_s": time.perf_counter() - t0})


def run(engine: str, mode: str, mix: str, pools: dict, *, concurrency: int = 1, rate: float = 0.0,
        warmup: float = 5.0, measure: float = 20.0, seed: int = 1, procs: int | None = None, cap: int = 512):
    procs = procs or (min(concurrency, len(servers.CLIENT_CPUS)) if mode == "closed" else len(servers.CLIENT_CPUS))
    name = servers.OXI_NAME if engine == "oxigraph" else servers.NEO_NAME
    ctx = mp.get_context("fork")
    q = ctx.Queue()
    t0_wall = time.time() + 1.0
    plist = []
    for i in range(procs):
        tasks = concurrency // procs + (1 if i < concurrency % procs else 0) if mode == "closed" else 0
        a = (engine, mode, mix, pools, tasks, rate / procs, max(1, cap // procs), seed * 100 + i,
             t0_wall, warmup, measure, servers.CLIENT_CPUS[i % len(servers.CLIENT_CPUS)])
        p = ctx.Process(target=_proc, args=(a, q))
        p.start()
        plist.append(p)
    while time.time() < t0_wall + warmup:
        time.sleep(0.01)
    srv0, w0 = servers.cpu_usage_usec(name), time.time()
    while time.time() < t0_wall + warmup + measure:
        time.sleep(0.01)
    srv1, w1 = servers.cpu_usage_usec(name), time.time()
    results = [q.get() for _ in plist]
    for p in plist:
        p.join()
    samples = [s for r in results for s in r["samples"]]
    return {
        "engine": engine, "mode": mode, "mix": mix, "concurrency": concurrency, "rate": rate,
        "warmup_s": warmup, "measure_s": measure, "procs": procs, "cap": cap,
        "samples": samples, "dropped": sum(r["dropped"] for r in results),
        "client_cpu_cores": round(sum(r["client_cpu_s"] for r in results) / max(r["wall_s"] for r in results), 3),
        "server_cpu_cores": None if srv0 is None or srv1 is None else round((srv1 - srv0) / 1e6 / (w1 - w0), 3),
    }
