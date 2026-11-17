# Manufacturing Ontology Guidance: RDF (PyOxigraph) vs LPG (Neo4j)

A reproducible **guidance benchmark** that implements one semiconductor-manufacturing ontology on two stacks and
measures the trade-offs with ground truth attached:

* **PyOxigraph 0.5**: RDF 1.2 / SPARQL 1.2 on an on-disk RocksDB store, embedded in Python. For the load test,
  Oxigraph's own HTTP SPARQL server is used instead.
* **Neo4j 5.26 Community**: a labeled property graph queried with Cypher over Bolt. The benchmarks load it with a
  small custom RDF→LPG loader (`src/loader.py`). The storage study also loads it through **neosemantics (n10s)**
  with `handleRDFTypes=LABELS` and `NODES`.

What is included:
* A **connected fab-graph generator** that plants facts and computes every answer in plain Python, so
  **precision/recall** grades each engine response.
* Five scenarios: three reasoning reads, runtime TBox evolution and batch ABox mutation, measured at
  1.4k / 10k / 100k quads.
* **Template-based LLM tool calling**: the model picks a tool and its arguments and never writes SPARQL or Cypher.
* A **server concurrency/throughput load test** with CPU pinning, closed and open loops, and integrity checks.

Further reading:
* **[docs/WORKFLOW.md](docs/WORKFLOW.md)**: each scenario end to end with real data (user request → raw tool call
  → filled SPARQL/Cypher → result → generated answer → grade).
* **[docs/STORAGE.md](docs/STORAGE.md)**: how each engine persists TBox/ABox CRUD (LSM tombstones vs record store),
  n10s `LABELS` vs `NODES`, an append-only sensor time series (S6), and compaction.
* **[docs/ONTOLOGY_AND_CQ.md](docs/ONTOLOGY_AND_CQ.md)**: how MASON, SOSA/SSN and FMEA were combined and why,
  the weak spots, and how the competency questions were created.

> The competency questions and data are **synthetic and authored for this benchmark**. No production questions or
> data were used. Read [Guidance](#guidance-what-this-repo-does-and-does-not-tell-you) and [Caveats](#caveats)
> before quoting numbers.

---

## Quick start

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker (for Neo4j and the load test), and optionally a
[MARA Cloud](https://api.cloud.mara.com) API key for the LLM parts.

```bash
git clone https://github.com/tteon/Manufacturing-ontology-guidance.git
cd Manufacturing-ontology-guidance
uv sync

uv run python src/common.py 2 16 158                 # generated graph sizes for 2 / 16 / 158 production lines

# PyOxigraph only, smallest scale (~10 s, no Docker)
uv run python src/benchmark_runner.py --no-neo4j --scales 2 --reps 2

# Both engines, the scales used in the results below
docker compose up -d                                 # Neo4j on 127.0.0.1:7474 / 7687 (neo4j / password123)
uv run python src/benchmark_runner.py                # --scales 2,16,158 --params 20 --reps 5 --s5-batch 500

# LLM tool-calling agent on both backends (~25 MARA calls)
cp .env.example .env                                 # set MARA_API_KEY
uv run python src/tool_agent.py                      # --lines 16 --backends rdf,lpg
uv run python src/render_workflow.py                 # regenerate docs/WORKFLOW.md from the latest agent run

# Storage CRUD study (docs/STORAGE.md). s6/compaction use the compose Neo4j; n10s starts its own container
mkdir -p plugins && curl -L -o plugins/neosemantics-5.26.0.jar \
  https://github.com/neo4j-labs/neosemantics/releases/download/5.26.0/neosemantics-5.26.0.jar
uv run python src/exp_storage.py n10s          # --lines 158 --reclass 500
uv run python src/exp_storage.py s6            # --lines 16 --rounds 200 --every 25
uv run python src/exp_storage.py compaction    # --lines 158 --rounds 200 --batch 500

docker compose down -v
```

> **LLM endpoint status (checked 2026-11-18):** `https://api.cloud.mara.com/v1` now redirects `GET /v1/models`
> to `www.mara.com` (HTTP 301) and rejects `POST /v1/chat/completions` with HTTP 405, even though the public docs
> still list it. The recorded LLM results in `results/` were produced on 2026-11-14, when the endpoint answered
> normally. The gateway works with any OpenAI-compatible endpoint that supports `tools`: set `MARA_BASE_URL` and
> `MARA_API_KEY`, and pass `--model`. Results with another model are not comparable to the recorded ones.

If Neo4j is unreachable, `benchmark_runner.py` runs PyOxigraph alone. Output goes to `results/v2/run_<UTC>.json`
and to a Markdown table, `results/v2/summary_<UTC>.md`. The load test has its own section [below](#concurrency--throughput-load-test).

---

## 1. TBox (`data/mfg_knowledge.trig`, graph `ex:TBox_Graph`, 49 triples)

| Layer | Content |
|---|---|
| MASON-style upper level | `Resource`, `Operation`, `Entity`; `Equipment ⊑ Resource`, `ProductionLine ⊑ Resource`, `Lot ⊑ Entity` |
| Equipment hierarchy | `ProcessChamber`, `Module` (`GasSupplyModule`, `ThermalModule`, `PlasmaModule`), `Part` (`MFC`, `Seal`, `Heater`, `RFGenerator`) |
| SOSA/SSN (W3C IRIs) | `Part ⊑ sosa:FeatureOfInterest`; `ex:DriftSpecification ⊑ ssn:Property`; each part class has `rdfs:subClassOf [ a owl:Restriction ; owl:onProperty ssn:hasProperty ; owl:hasValue ex:FlowDriftSpec ]`; each spec carries `ex:maxAllowedDrift` |
| FMEA-style | `FailureMode`, `Symptom ⊑ FailureMode`, `Cause`, `Action` |
| Properties | `partOf`, `feedsInto` (`owl:TransitiveProperty`), `causedBy`, `attributedTo`, `mitigatedBy`, `processedIn` |

v1 attached `maxAllowedDrift` directly to classes, which is not standard OWL. v2 moves the threshold to an
`ssn:Property` individual, and each class refers to it through an OWL `hasValue` restriction. Each restriction
takes 4 triples, which is why the TBox is 49 triples rather than 30–40.

Limitations of this TBox:
* MASON and FMEA are **not imported**; their names are reused as local `ex:` classes. SOSA and SSN are the real W3C IRIs.
* Transitivity is **declared but not computed by a reasoner**. Queries traverse it with property paths
  (`partOf+`) and variable-length Cypher patterns (`[:partOf*]`).

### Why TriG (and not RDF/XML, Turtle or N-Quads)

| Format | Named graphs | RDF 1.2 triple terms / annotations | Readability | Fit here |
|---|---|---|---|---|
| RDF/XML | ✗ (one graph per document) | ✗ | Low (XML verbose) | Cannot keep TBox / ABox / Inferred apart or hold confidences; it is still the most widely supported format for OWL tooling (e.g. Protégé) |
| Turtle | ✗ | ✓ (Turtle 1.2) | High | Good for a single graph only |
| N-Triples / N-Quads | ✗ / ✓ | ✓ (1.2) | Low (one statement per line) | Good for bulk streaming |
| JSON-LD | ✓ | Limited | Medium | Web/API integration |
| **TriG** | ✓ | ✓ (TriG 1.2) | High (Turtle syntax) | **Chosen**: separate TBox/ABox/Inferred graphs and `<< s p o >> ex:confidence v` in one human-readable file |

Exporting to RDF/XML would lose both the graph separation and the confidence annotations.

## 2. Connected ABox generator (`src/common.py: generate_connected_fab_graph`)

`scale_factor` is the number of production lines. Each line contains:

```
Line_l ──partOf── Ch_l_s (4 process steps, Ch_l_1 ─feedsInto→ Ch_l_2 → … → Ch_l_4)
                    └─ partOf ─ GSM / TM / PM modules (GasSupply / Thermal / Plasma)
                                 └─ partOf ─ 4 leaf parts each:
                                    GSM: 2×MFC + 2×Seal · TM: 2×Heater + 2×Seal · PM: 2×RFGenerator + 2×Seal
Lot_l_n (10 per line) ─processedIn→ ~85% of the line's chambers
Seal ─hasDefect→ {Micro_Leak, Crack, Swelling}   (≥1 per line + ~3% background)
Obs_<part> a sosa:Observation ; hasFeatureOfInterest part ; observedProperty spec ;
           hasSimpleResult ~ Normal(0.6·thr, 0.25·thr)   (≈5.5% above threshold)
Sym_<chamber> (≈5% of steps) ← hasSymptom ← lots processed there
   ─causedBy→ 2 correct Causes (0.7–0.9) ─attributedTo→ a part in that chamber (0.7–0.9)
   ─causedBy→ 4 wrong Causes (0.1–0.5)   ─attributedTo→ random part anywhere (0.7–0.9)
   ─causedBy→ 2 decoy Causes (0.7–0.9)   ─attributedTo→ random part anywhere (0.1–0.5)
```

Every confidence is an RDF 1.2 annotation in RDF and a relationship property in Neo4j. Correct paths have a
product ≥ 0.49, and any path with a weak edge has a product ≤ 0.45, so S1 uses a threshold of **0.48**.
The generator asserts that recomputing the scores from the edges recovers exactly the planted correct causes.

**Ground truth** is computed in plain Python (`FabGraph.truth_s1…truth_s4`, `truth_s3_global`) without SPARQL or
Cypher. `precision_recall()` compares each engine's answer set against it.

Both engines receive the **same quad list**. Neo4j gets one `:Resource` node per IRI or blank node,
numeric literals as properties, reifier confidences as relationship properties, and every class plus its
superclasses as labels on each instance (write-time materialization, mirroring `ex:Inferred_Graph`).

| Lines | Total quads | ABox | Chambers | Lots | Observations | Symptoms / symptomatic lots | Defective seals |
|---:|---:|---:|---:|---:|---:|---|---:|
| 2 | 1,362 | 807 | 8 | 20 | 96 | 1 / 6 | 2 |
| 16 | 10,065 | 6,040 | 64 | 160 | 768 | 2 / 8 | 24 |
| 158 | 100,092 | 60,609 | 632 | 1,580 | 7,584 | 34 / 132 | 264 |

(Exact per-run counts are in `results/v2/run_*.json → datasets`.)

## 3. Scenarios (v2)

| # | Question | SPARQL (`src/queries.py`) | Cypher | Ground truth |
|---|---|---|---|---|
| S1 | Root causes of a lot's symptom(s) with cumulative confidence ≥ 0.48 | asserted edges, then `<< … >> ex:confidence` patterns; `BIND` product | `(lot)-[:hasSymptom]->()-[r1:causedBy]->()-[r2:attributedTo]->()`, `r1.confidence*r2.confidence` | Planted correct causes |
| S2 | Lots suspected from a defective seal | `partOf+`, `a/rdfs:subClassOf*`, `feedsInto*` | `[:partOf*1..]`, `[:subClassOf*0..]`, `[:feedsInto*0..]` | Lots of the seal's chamber and every downstream chamber |
| S3 | Parts in a line whose reading exceeds their spec | `partOf+ line`, then restriction → `owl:hasValue` spec → `maxAllowedDrift` | the same path over the restriction nodes | Python threshold check |
| S4 | Add a **new** class `CriticalHazardSystem ⊒ GasSupplyModule, PlasmaModule` (+ policy), then list its members | 2×2: query_time (`subClassOf+`) vs materialized (`Inferred_Graph` / labels) | the same 2×2 | All gas and plasma modules |
| S5 | **Batch upsert** of 500 random observations in **one transaction** | one `DELETE/INSERT … VALUES` request | one `UNWIND $rows … SET` transaction | Read-back of all 500 values, then a global S3 recount |

S4 refuses a parent class that already exists, so the revert can never delete base TBox edges.

## 4. Results (v2)

Hardware: Intel i7-11700, NVMe, Linux 6.8. Software: Python 3.14, pyoxigraph 0.5.13 (embedded), Neo4j 5.26 in Docker
over Bolt. Settings: up to 20 sampled parameters per read scenario, 1 warm pass and 5 timed passes; values are
medians. S4 and S5 use 5 reps. Run: `results/v2/run_20261114T060505Z.json`. P/R is precision/recall of the engine
result against generator ground truth (duplicate rows count against precision). Write rows show **commit time only**.
PyOxigraph's separate `flush()` is listed in its own row.

| Quads | Scenario | Op | PyOxigraph ms | Neo4j ms | P / R (Oxi) | P / R (Neo) | Notes |
|---:|---|---|---:|---:|---|---|---|
| 1,362 | S1 RCA (2-hop confidence ≥ 0.48) | Read | 0.52 | 1.73 | 100% / 100% | 100% / 100% | 6 lots, avg 2.0 answers |
| 1,362 | S2 contamination (partOf+, feedsInto*) | Read | 0.20 | 2.55 | 100% / 100% | 100% / 100% | 2 seals, avg 9.5 lots |
| 1,362 | S3 per-line sensor anomalies (restriction → spec) | Read | 1.88 | 3.04 | 100% / 100% | 100% / 100% | 2 lines, avg 2.0 parts |
| 1,362 | S4 query_time: TBox write (commit) | Write | 0.03 | 3.05 | 100% / 100% | 100% / 100% | 0 ABox facts touched |
| 1,362 | S4 query_time: list | Read | 0.19 | 1.94 | 100% / 100% | 100% / 100% | COUNT: 0.11 / 1.52 |
| 1,362 | S4 materialized: TBox write + ABox migration (commit) | Write | 0.24 | 5.53 | 100% / 100% | 100% / 100% | 16 facts migrated |
| 1,362 | S4 materialized: list | Read | 0.20 | 1.78 | 100% / 100% | 100% / 100% | COUNT: 0.06 / 1.18 |
| 1,362 | S5 batch upsert (96 obs, 1 tx) | Write | 1.49 | 5.02 | 100% / 100% | 100% / 100% | S3 recount ok: True/True |
| 1,362 | PyOxigraph flush() after write (S4 / S5) | Write | 28.4 / 28.0 | n/a | — | — | Neo4j: no separate flush (commit already fsyncs its log); PyOxigraph commits are not fsynced |
| 10,065 | S1 RCA (2-hop confidence ≥ 0.48) | Read | 0.55 | 0.97 | 100% / 100% | 100% / 100% | 8 lots, avg 2.0 answers |
| 10,065 | S2 contamination (partOf+, feedsInto*) | Read | 0.20 | 1.47 | 100% / 100% | 100% / 100% | 20 seals, avg 9.6 lots |
| 10,065 | S3 per-line sensor anomalies (restriction → spec) | Read | 12.17 | 2.19 | 100% / 100% | 100% / 100% | 16 lines, avg 1.8 parts |
| 10,065 | S4 query_time: TBox write (commit) | Write | 0.03 | 3.26 | 100% / 100% | 100% / 100% | 0 ABox facts touched |
| 10,065 | S4 query_time: list | Read | 0.71 | 3.85 | 100% / 100% | 100% / 100% | COUNT: 0.15 / 1.36 |
| 10,065 | S4 materialized: TBox write + ABox migration (commit) | Write | 0.95 | 5.85 | 100% / 100% | 100% / 100% | 128 facts migrated |
| 10,065 | S4 materialized: list | Read | 1.22 | 3.56 | 100% / 100% | 100% / 100% | COUNT: 0.09 / 0.98 |
| 10,065 | S5 batch upsert (500 obs, 1 tx) | Write | 7.86 | 9.58 | 100% / 100% | 100% / 100% | S3 recount ok: True/True |
| 10,065 | PyOxigraph flush() after write (S4 / S5) | Write | 29.4 / 23.8 | n/a | — | — | Neo4j: no separate flush (commit already fsyncs its log); PyOxigraph commits are not fsynced |
| 100,092 | S1 RCA (2-hop confidence ≥ 0.48) | Read | 0.55 | 0.57 | 100% / 100% | 100% / 100% | 20 lots, avg 2.1 answers |
| 100,092 | S2 contamination (partOf+, feedsInto*) | Read | 0.21 | 0.79 | 100% / 100% | 100% / 100% | 20 seals, avg 9.4 lots |
| 100,092 | S3 per-line sensor anomalies (restriction → spec) | Read | 119.11 | 7.64 | 100% / 100% | 100% / 100% | 20 lines, avg 3.0 parts |
| 100,092 | S4 query_time: TBox write (commit) | Write | 0.04 | 2.68 | 100% / 100% | 100% / 100% | 0 ABox facts touched |
| 100,092 | S4 query_time: list | Read | 5.73 | 22.18 | 100% / 100% | 100% / 100% | COUNT: 0.50 / 1.38 |
| 100,092 | S4 materialized: TBox write + ABox migration (commit) | Write | 7.99 | 7.86 | 100% / 100% | 100% / 100% | 1264 facts migrated |
| 100,092 | S4 materialized: list | Read | 11.49 | 22.12 | 100% / 100% | 100% / 100% | COUNT: 0.37 / 1.08 |
| 100,092 | S5 batch upsert (500 obs, 1 tx) | Write | 8.50 | 8.01 | 100% / 100% | 100% / 100% | S3 recount ok: True/True |
| 100,092 | PyOxigraph flush() after write (S4 / S5) | Write | 25.2 / 29.2 | n/a | — | — | Neo4j: no separate flush (commit already fsyncs its log); PyOxigraph commits are not fsynced |

`COUNT` lists PyOxigraph / Neo4j medians for the COUNT-only version of the same S4 lookup.

How to read this table (and what not to conclude):

1. **Correctness.** Precision and recall are 100% for every scenario, scale and engine.
2. **Sub-millisecond reads mostly measure the access path.** Embedded PyOxigraph has a no-op cost of about 0.015 ms.
   Neo4j's Bolt round trip costs 0.45–1.25 ms, which is about the same as its own S1/S2 latency. Treat S1/S2/S4-COUNT
   differences as *embedded vs network* rather than *RDF vs LPG*. Neo4j's S1/S2 times fall as scale grows
   (1.73 → 0.57 ms). The scales run in ascending order on one JVM, so this is most likely JIT/plan-cache warm-up,
   not a scale effect.
3. **Where the engines really differ: S3.** PyOxigraph grows linearly (1.9 → 12 → 119 ms) while Neo4j stays almost
   flat (3 → 2 → 8 ms). *Hypothesis, not verified with EXPLAIN:* Oxigraph's join order does not start from the bound
   line. Query formulation matters a lot on Oxigraph; see the S1 note in Caveats.
4. **S4 schema evolution.** With query-time inference, neither engine touches instance data. PyOxigraph commits the
   TBox change in 0.03 ms; Neo4j takes about 2.7 ms, including a Bolt round trip and a durable commit. Write-time
   materialization of 1,264 facts takes about 8 ms on both at 100k quads. After evolution, PyOxigraph is faster in
   every S4 list and COUNT cell measured here.
5. **S5 batch upsert (500 rows, 1 transaction).** About 8 ms on both engines, but the durability differs: Neo4j's
   commit is fsynced, PyOxigraph's is not (verified with `src/fsync_probe.py` under strace). A durable PyOxigraph
   write needs `flush()` on top, which costs 24–29 ms.

### LLM tool calling (v2, `results/tool_agent_20261114T060707Z.json`)

The questions use real IDs from a 16-line graph. Both backends expose **the same 8 tools**:
`find_root_causes`, `trace_contamination`, `classify_sensor_anomalies`, `count_sensor_anomalies`, `add_superclass`,
`list_equipment_by_class`, `batch_stream_update`, `check_sensor_threshold`.

With `gpt-oss-120b`, 25 MARA calls and temperature 0, all 10 runs (5 scenarios × 2 backends) chose the expected
tools and got 100% P/R on the **tool results**, and each produced an answer. S4 chained `add_superclass` →
`list_equipment_by_class`, and S5 chained `batch_stream_update` → `count_sensor_anomalies`. LLM HTTP time was
**2.1–6.3 s per question**, summed from receipt latencies. Tool execution took 1.5–87 ms. See
[docs/WORKFLOW.md](docs/WORKFLOW.md) for every turn.

Limits of this result:
* There is one question per scenario and backend, with one wording.
* P/R grades the tool output, not the free-text answer.
* Tool results over 60 rows are truncated before being sent back to the model, so the S4 answer lists 60 of 128 items.

## Concurrency / throughput load test

`loadtest/` runs **Oxigraph's HTTP SPARQL server (0.5.13)** against **Neo4j over Bolt** on a 1,580-line graph
(about 1M quads, 518k Neo4j relationships).

Setup and fairness controls:
* Both servers are pinned to the same 4 physical cores / 8 threads, each with 8 GB of memory. The load generator
  (multiple processes running asyncio) is pinned to the other 8 hardware threads.
* **Closed loop** sweeps the number of concurrent clients. **Open loop** sends Poisson arrivals at fractions of each
  engine's peak closed-loop throughput, and latency is measured from the *scheduled* send time. Arrivals that hit the
  outstanding-request cap, and requests still unfinished at the end, are counted as `dropped`. Errors and dropped
  requests are **not** included in the latency percentiles; read them next to the percentiles.
* Mixes:
  - `R`: S1, S2, S3 and class count (query-time and materialized), 20% each.
  - `W`: single-observation upsert, one transaction per request.
  - `M`: 90% reads / 10% writes.
  - `Whot`: every write targets the same observation.
* Every **read** response is graded against ground truth. Writes keep values on the same side of their threshold,
  so read answers stay valid while writes run. **Writes are checked by an integrity pass after each cell**: it counts
  observations holding more than one value and re-validates S3 on 20 lines. It then repairs any duplicates, so the
  next cell starts clean; the duplicate counts stay in `summary.jsonl`.
* Cells run in randomized order, with 3 trials. Server CPU comes from cgroup v2 and client CPU is recorded;
  `client_bound_suspect` flags cells whose client CPU exceeds 70% of the client processes.

```bash
# requirements: Linux, Docker with cgroup v2; 16 hardware threads by default
# (override with MFG_SERVER_CPUS=0-3 MFG_CLIENT_CPUS=4,5,6,7 MFG_SERVER_MEMORY=4g)
uv run python loadtest/setup.py --lines 1580                       # build, load both servers, validate all pooled params
uv run python loadtest/run_matrix.py --lines 1580 --phases closed --conc 1,2,4,8,16,32,64 --measure 15 \
       --out-dir results/loadtest/L1580_main                        # (the configuration used here)
uv run python loadtest/run_matrix.py --lines 1580 --phases open --measure 15 --out-dir results/loadtest/L1580_main
uv run python loadtest/llm_load.py --lines 1580 --conc 1,2,4,8,16,32 --per-level 32   # paid API calls; see docstring
```

**Before any load**, all 200 pooled requests per read scenario were correct on both servers. Serial means:
S1 1.7 / 3.3 ms, S2 1.1 / 3.4 ms, **S3 1,433 / 75 ms**, class counts about 8–14 ms (Oxigraph / Neo4j).

Full per-cell tables: [`results/loadtest/L1580_main/REPORT.md`](results/loadtest/L1580_main/REPORT.md).
Closed loop: 3 trials for trials 1–2 and part of trial 3 (137 of 168 cells; the third trial was cut by a time
limit). Open loop: 72/72 cells (3 trials).

**Finding 1: SPARQL Update read-modify-write is not serializable under concurrent HTTP clients (Oxigraph).**
Each upsert is a single `DELETE {old} INSERT {new} WHERE {OPTIONAL old}` request. Concurrent transactions read the
same old value, all delete it and all insert their own value, so the observation keeps several values (write skew):

| Oxigraph mix | Duplicates observed |
|---|---|
| `Whot` (all writes to one observation) | at every concurrency ≥ 2; the observation held roughly **c values** at concurrency *c* (2 / 4 / 8 / 16 / 32 / 62 values at c = 2 / 4 / 8 / 16 / 32 / 64) |
| `W` (writes spread across ~95k observations) | 1–2 observations per cell at concurrency ≥ 8 |
| `M` (10% writes) | none |

Neo4j's property `SET` takes a node lock and cannot hold two values. That makes this check one-sided, but it is
still a real constraint on an RDF write path: serialize writers per key, or model readings as append-only
observations ([docs/STORAGE.md](docs/STORAGE.md)).

**Finding 2: hot-key overwrites are slow on Oxigraph even without concurrency.** At concurrency 1, overwriting one
observation repeatedly (`Whot`) had a p50 of 12.5 ms, against 0.77 ms when writes were spread out (`W`). This is
consistent with the tombstone build-up measured in [docs/STORAGE.md §4](docs/STORAGE.md).

**Finding 3: reads at 1M quads.**
* Neo4j saturates at about 4 clients, at roughly 240–265 requests/s (R and M mixes, 8 server threads). In the open
  loop, p99 is 0.09–0.22 s up to 75% of peak load and 1.3–1.7 s at 100%. At 110% of peak there were
  58 `TransientError`s.
* Oxigraph's per-line S3 (about 1.4 s serial, 3.9 s at concurrency 8) dominates its read mix, giving 2–14
  requests/s with multi-second p99. At concurrency 64, single-request latency exceeded the 15 s window, so those
  cells are **invalid**, not "0 requests/s".
* Per operation, Oxigraph is fast on S1/S2 (p50 2.5 / 1.5 ms at concurrency 8). The difference is concentrated in
  S3 and the class counts.

**Caveats for the server load test:**
* Every Oxigraph `W` cell is client-bound (about 8 client cores against about 1.4 server cores). Do **not** compare
  write throughput between the engines from this harness.
* Oxigraph closed-loop read cells contain few samples (about 30 at concurrency 1), so their p99 is close to the maximum.
* The SPARQL protocol re-sends and re-parses the full query text on every request, while Bolt sends parameterized,
  plan-cached queries.
* Both servers ran at the same time on the same CPU set.
* Errors and dropped requests are excluded from the latency percentiles; they are reported separately.

### LLM end-to-end concurrency (`loadtest/llm_load.py`, `results/loadtest/llm_L1580_20261114T090154Z/`)

N concurrent tool-calling agents (`gpt-oss-120b`, MARA) answered questions generated from the 1M-quad graph
against the two servers, with 32 questions per level and one trial. In total there were 384 questions and 933 API
calls: 929 returned HTTP 200, and 4 got HTTP 429 and succeeded on retry.

| Backend | Concurrent agents | Questions | Throughput q/min | e2e p50 ms | e2e p90 ms | e2e p99 ms | LLM share of time | Tool p50 ms | Errors | Correct tools / full P-R |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| lpg | 1 | 32 | 20.7 | 2,479 | 4,627 | 5,322 | 96% | 75 | 0 | 32 / 32 |
| lpg | 2 | 32 | 37.23 | 2,498 | 5,029 | 6,771 | 95% | 81 | 0 | 32 / 32 |
| lpg | 4 | 32 | 78.26 | 2,813 | 3,382 | 6,288 | 96% | 78 | 0 | 32 / 32 |
| lpg | 8 | 32 | 122.81 | 3,358 | 5,115 | 6,814 | 98% | 5 | 0 | 32 / 32 |
| lpg | 16 | 32 | 100.36 | 6,743 | 9,651 | 10,647 | 94% | 207 | 0 | 32 / 32 |
| lpg | 32 | 32 | 160.75 | 6,819 | 9,851 | 11,935 | 94% | 52 | 0 | 32 / 32 |
| rdf | 1 | 32 | 13.55 | 4,928 | 6,216 | 7,460 | 70% | 501 | 0 | 32 / 32 |
| rdf | 2 | 32 | 28.85 | 4,881 | 5,711 | 6,418 | 71% | 533 | 0 | 32 / 32 |
| rdf | 4 | 32 | 46.34 | 5,632 | 5,998 | 7,107 | 54% | 3,655 | 0 | 32 / 32 |
| rdf | 8 | 32 | 83.2 | 5,501 | 7,864 | 8,721 | 76% | 513 | 0 | 32 / 32 |
| rdf | 16 | 32 | 120.05 | 6,457 | 8,845 | 9,143 | 77% | 932 | 0 | 32 / 32 |
| rdf | 32 | 32 | 133.89 | 11,858 | 13,659 | 14,331 | 66% | 1,737 | 0 | 32 / 32 |

* **All 384 questions had 0 errors, the expected tools, and 100% P/R on the tool results**, at every concurrency
  level on both backends.
* On LPG, LLM time is 94–98% of end-to-end time, so throughput scales with agent concurrency to about 161 q/min at 32.
* On RDF, Oxigraph's S3 tool calls (seconds at 1M quads) take 23–46% of the time. Under 32 agents its p50 rises to
  11.9 s, because tool calls queue on the server.

## Guidance: what this repo does and does not tell you

| If you care about… | What the evidence here suggests | Confidence |
|---|---|---|
| Statement-level metadata (confidence or provenance on an edge) | Both work. RDF 1.2 annotations (`<< s p o >> ex:confidence`) and LPG relationship properties both returned identical results | High (correctness), low (performance) |
| Hierarchy / transitive reasoning without a reasoner | Both are expressive enough (`subClassOf*` / `partOf+` vs variable-length patterns). **Performance depends on query formulation and the optimizer.** Oxigraph showed 100× swings between equivalent SPARQL versions and linear S3 growth | Medium |
| Runtime schema (TBox) evolution | Query-time inference needs zero ABox migration on both engines. Write-time materialization (superclass labels / inferred triples) costs about 8 ms (Oxigraph) and about 46 ms (n10s labels) per 1.3k affected instances, and the same again on class deletion. Choose by read pattern, not by engine | Medium |
| Type changes in an LPG (n10s) | Relabeling is cheap (9–17 ms per 500 nodes). **Storing `rdf:type` as relationships (`handleRDFTypes=NODES`) makes class nodes dense hubs, and reclassification was 17–31× slower** ([docs/STORAGE.md §2](docs/STORAGE.md)) | Medium |
| Real-time sensor streams | Append-only observations are cheap and flat on both engines (about 35–47 ms per 768 readings). Without a latest pointer, current-state queries grow with history (Oxigraph per-line query 25 ms → 2.8 s at 154k readings); **a latest pointer keeps them flat** at about 2.4× the append cost. On RDF, in-place overwrites accumulate tombstones (13× slower scans after 100k updates until flush/compaction) ([docs/STORAGE.md §3–4](docs/STORAGE.md)) | Medium–High |
| High-frequency updates of the same value | **Do not use concurrent SPARQL `DELETE/INSERT` read-modify-write on Oxigraph without serializing writers** (write skew observed). Neo4j's property `SET` is safe here. In RDF, prefer append-only observations | High (anomaly reproducible), low (throughput numbers) |
| Durability | PyOxigraph commits are not fsynced (flush is a separate ~25 ms). Neo4j fsyncs each commit. Compare only like-for-like durability | High |
| LLM integration | Template tool calling (no text2query) was 100% correct here with an identical tool interface on both backends. **The backend choice is invisible to the LLM.** LLM latency (seconds) dwarfs query latency (milliseconds) | Medium (n=1 per cell) |

This repo does **not** tell you:
* which store is "faster" in general (single host, single versions, default configurations, embedded vs network
  in the v2 sweep, client-bound writes in the load test);
* whether this ontology fits a real fab (the competency questions are synthetic);
* anything about OWL reasoning (none is used).

## Caveats

* The scenarios, questions and data are synthetic. MASON and FMEA are reused by name only, and only the SOSA/SSN
  IRIs are real. Weak modeling choices are listed in [docs/ONTOLOGY_AND_CQ.md](docs/ONTOLOGY_AND_CQ.md).
* No OWL reasoner is used. Transitivity and inheritance are evaluated inside the queries.
* **Query formulation matters on Oxigraph.** For S1, putting the RDF 1.2 reifier patterns first was about 100× slower
  than the asserted-edges-first form used here. Each template was written once, and no systematic per-engine tuning
  was done beyond that S1 fix.
* Modeling asymmetries in the LPG mapping:
  - the generic `:Resource` node label shares its name with the ontology class `ex:Resource`;
  - local names drop namespaces (`ssn:hasProperty` becomes `hasProperty`);
  - `sosa:Observation` instances get no label in Neo4j.
* `results/v1/` was produced by the v1 code (the toy dataset and an intent router); see the git history. It cannot
  be reproduced from the current code and is kept for reference only.

## Repository layout

```
├── data/mfg_knowledge.trig    Core TBox (TriG, ex:TBox_Graph)
├── docker-compose.yml         Neo4j 5.26 Community for the embedded benchmark (loopback ports, named volume)
├── docs/                      WORKFLOW.md, STORAGE.md, ONTOLOGY_AND_CQ.md
├── src/
│   ├── common.py              TBox loader, connected generator, ground truth, precision/recall, timing
│   ├── queries.py             All SPARQL and Cypher templates and result canonicalization
│   ├── loader.py              Identical quads into PyOxigraph (RocksDB) and Neo4j (nodes/edges/labels)
│   ├── engine_oxigraph.py     Embedded engine wrapper
│   ├── engine_neo4j.py        Bolt engine wrapper
│   ├── benchmark_runner.py    v2 scale sweep with P/R grading, JSON and Markdown output
│   ├── tool_agent.py          Template tool-calling agent (MARA function calling)
│   ├── llm_agent.py           MARA gateway (file lock, pending guard, budget, redacted receipts)
│   ├── render_workflow.py     Builds docs/WORKFLOW.md from a tool-agent run
│   ├── exp_storage.py         Storage CRUD study: n10s LABELS/NODES, S6 time series, compaction
│   └── fsync_probe.py         strace helper for commit durability
├── loadtest/                  setup.py, servers.py, ops.py, driver.py, run_matrix.py, report.py, llm_load.py
└── results/                   v1/ (archived), v2/, loadtest/, storage/, tool-agent runs and receipts
```
