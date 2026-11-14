# Manufacturing Ontology Guidance: RDF (PyOxigraph) vs LPG (Neo4j)

A small, reproducible benchmark that implements the same semiconductor-manufacturing ontology on two
stores and compares them:

* **PyOxigraph 0.5**: RDF 1.2 / SPARQL 1.2 on an on-disk RocksDB store, with TBox and ABox in separate named graphs.
* **Neo4j 5.26 Community**: a labeled property graph queried with Cypher over Bolt.

It covers five scenarios: three reasoning reads (S1–S3), runtime TBox schema evolution (S4) and high-frequency ABox
state overwrites (S5). An LLM agent answers natural-language questions through **template-based tool calling**.
The model picks a tool and its arguments; it never writes SPARQL or Cypher.

> The point is to make architectural trade-offs measurable, not to pick a winner. Read
> [Caveats](#caveats) before quoting any number.

---

## Quick start

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker (only for Neo4j), and optionally a
[MARA Cloud](https://api.cloud.mara.com) API key for the LLM parts.

```bash
git clone https://github.com/tteon/Manufacturing-ontology-guidance.git
cd Manufacturing-ontology-guidance
uv sync

# 1) Fastest smoke test: PyOxigraph only, no LLM, no Docker (~10 s)
uv run python src/benchmark_runner.py --no-neo4j --no-llm --scales 0,1000 --reps 2 --read-reps 5

# 2) Both engines
docker compose up -d                       # Neo4j 5.26 on 127.0.0.1:7474 / 7687 (neo4j / password123)
uv run python src/benchmark_runner.py --no-llm --scales 0,1000 --reps 2 --read-reps 5

# 3) Full sweep, as in the reference results below (~10 min)
uv run python src/benchmark_runner.py --no-llm

# 4) LLM tool-calling agent on both backends (needs a MARA key, ~25 API calls)
cp .env.example .env                       # then set MARA_API_KEY
uv run python src/tool_agent.py

docker compose down -v                     # stop Neo4j and delete its volume
```

If Neo4j is unreachable, the benchmark falls back to PyOxigraph-only mode. Without a MARA key,
`benchmark_runner.py` routes questions with a deterministic rule parser, while `tool_agent.py` exits with a message.

### CLI options

| Script | Option | Default | Meaning |
|---|---|---|---|
| `benchmark_runner.py` | `--scales` | `0,1000,10000,100000` | Synthetic equipment instances added to the base data |
| | `--reps` | `5` | S4 evolve/revert cycles and S5 runs per scale |
| | `--read-reps` / `--warmup` | `20` / `3` | Read samples per measurement / warm-up runs |
| | `--no-neo4j`, `--no-llm` | off | Single-engine mode / rule-based intent routing |
| | `--model` | `gpt-oss-120b` | MARA model for intent routing |
| | `--neo4j-uri`, `--neo4j-password`, `--neo4j-container` | local compose values | Neo4j connection; the container name is used for `du` disk measurement |
| `tool_agent.py` | `--backends` | `rdf,lpg` | Backends to run the agent against |
| | `--model`, `--max-calls` | `gpt-oss-120b`, `60` | Model and hard budget of API calls |

Environment variables (or `.env`): `MARA_API_KEY`, optional `MARA_BASE_URL` and optional `MARA_ADMISSION_LOCK`.
`MARA_ADMISSION_LOCK` is a lock file shared with other processes that call the API. Exported variables override `.env`.

---

## Data model

`data/mfg_knowledge.trig` is a toy knowledge base in the MASON style (equipment hierarchy), using SOSA (observations)
and FMEA-style causal chains.

```
TBox (ex:TBox_Graph)                          ABox (ex:ABox_Graph)
Equipment                                     Chamber_CVD_01 ─feedsInto→ Chamber_Etch_02
├─ ProcessChamber: CVD / Etch / Litho         ORing_Seal_04 ─partOf→ MFC_01 ─partOf→ GasBox_A ─partOf→ Chamber_CVD_01
├─ GasSupplyModule: MFC (maxDrift 5.0), GasBox      (hasDefect Micro_Leak)
├─ PlasmaModule: RFGenerator (maxDrift 3.0)   RF_Gen_01 ─partOf→ Chamber_Etch_02,  Heater_01 ─partOf→ Chamber_CVD_01
└─ ThermalModule: Heater (maxDrift 10.0)      Lot_101/102/103 ─processedIn→ chambers
partOf, feedsInto: owl:TransitiveProperty     Obs_01..03 (sosa:hasSimpleResult 6.2 / 3.8 / 4.0)
                                              Thickness_Anomaly ─causedBy→ DepositionRate_Drift ─causedBy→
                                                GasFlow_Instability (0.85) | Temperature_Drift (0.60) ─attributedTo→ MFC_01 | Heater_01
```

* **Confidences** use RDF 1.2 triple annotations: `<< s p o >> ex:confidence 0.85`. PyOxigraph 0.5 parses this as a
  reifier (`_:r rdf:reifies <<( s p o )>>`), and the same `<< … >>` pattern matches it in SPARQL.
* **Neo4j loader** (`src/loader.py`): each IRI becomes a `:Resource {id}` node and each IRI-object triple becomes a
  relationship. Numeric literals become float properties, and reifier confidences become relationship properties.
  Instance nodes additionally get their class and all superclasses as labels (`:Resource:MFC:GasSupplyModule:Equipment`),
  which is write-time materialization. The `[:type]` and `[:subClassOf]` edges stay in place, so query-time inference
  still works.
* **Scale-up**: `--scales` adds N synthetic instances spread round-robin over 8 classes. 3 of the 8 classes are affected by S4, so about 37.5% of the instances.

## Scenarios

| # | Question (as sent to the LLM) | Op | SPARQL side | Cypher side | Expected |
|---|---|---|---|---|---|
| S1 | Root cause of `Thickness_Anomaly` on `Lot_101` with cumulative confidence ≥ 0.70 | Read | Fixed 3-hop reifier pattern, product in `BIND` | `[:causedBy*1..3]` + `reduce()` | `GasFlow_Instability → MFC_01`, 0.7268 |
| S2 | Contamination from `Micro_Leak` on `ORing_Seal_04` | Read | `partOf+`, `feedsInto*`, `a/rdfs:subClassOf*` | `[:partOf*1..]`, `[:feedsInto*0..]` | Chambers CVD_01, Etch_02; lots 101, 102 |
| S3 | Equipment whose reading exceeds its class's `maxAllowedDrift` | Read | `rdfs:subClassOf*` threshold inheritance | `[:subClassOf*0..]` | MFC_01 (GasSupplyModule), RF_Gen_01 (PlasmaModule) |
| S4 | Add `CriticalHazardSystem` above `GasSupplyModule` and `PlasmaModule` with policy `Emergency_Interlock_Check`, then list affected equipment | Write + Read | See 2×2 below | See 2×2 below | MFC_01, GasBox_A, RF_Gen_01 (+ synthetic) |
| S5 | Overwrite `MFC_01`'s reading 100 times (4.00 → 6.97); does it exceed 5.0? | Write | `DELETE/INSERT/WHERE` per update | `SET o.hasSimpleResult` per update | 6.97 > 5.0 |

Every run checks each result against the expected answer and checks that both engines return the same result.

### Design choices that keep the comparison fair

* **S4 is 2×2**: engine × {**query_time** inference, write-time **materialized**}.
  - *query_time*: only the TBox changes. RDF gets 3 quads in `ex:TBox_Graph`; Neo4j gets a class node and 2 `subClassOf`
    edges. Reads walk the hierarchy.
  - *materialized*: the same TBox change, plus an ABox migration. RDF inserts `?x a ex:CriticalHazardSystem` into
    `ex:Inferred_Graph`; Neo4j runs `SET eq:CriticalHazardSystem`. Reads use the inferred fact or the label.
  - Reads are measured twice: once returning every row, and once with a **COUNT-only** query that excludes result transfer.
    Each rep runs evolve → first read → repeated reads → revert.
* **Round-trip baseline**: `ASK {}` (in-process) and `RETURN 1` (over Bolt) show the fixed per-call cost of each engine.
* **S5** is measured as 100 separate commits and also as 100 overwrites in **one transaction**, so the per-commit
  durability cost can be separated out. On-disk bytes are recorded before the updates, after them and after flush.
* **Repetition**: reads use 3 warmups and 20 samples (median/p95). Writes use 5 reps. The JSON keeps the TBox write,
  ABox migration and `flush()` timings separately.

## Template-based tool calling (`src/tool_agent.py`)

There is no text2sparql or text2cypher. Both backends expose the **same seven tools**, with identical names and
JSON-Schema parameters. Each tool fills a fixed, parameterized query template, and identifiers are validated
(`^[A-Za-z][A-Za-z0-9_]*$`) before substitution.

| Tool | Template behavior |
|---|---|
| `find_root_causes(lot_id, symptom_id, min_confidence)` | S1 template |
| `trace_contamination(component_id, defect_id)` | S2 template |
| `classify_sensor_anomalies()` | S3 template |
| `add_superclass(new_parent_class, subclasses[], required_policy)` | TBox write (query-time strategy) |
| `list_equipment_by_class(class_id)` | Instances of a class or its subclasses, with the class policy |
| `stream_sensor_updates(entity_id, iterations, start_value?, step?)` | Per-commit overwrite loop |
| `check_sensor_threshold(entity_id)` | Current value vs inherited threshold |

The protocol follows MARA's OpenAI-compatible
[function-calling docs](https://mara-cloud-docs-qa.vercel.app/api-features/function-calling-and-json-mode):
`tools`, `tool_choice: "auto"`, results sent back as `role: "tool"` messages with `tool_call_id`, and
`reasoning_effort: "high"` for `gpt-oss-120b`. An HTTP 400 is retried once at temperature 0.7. Each scenario's
writes are reverted before the next question.

**MARA call safety** (`src/llm_agent.py`):

* Calls are serialized by a file lock.
* An unresolved `.pending` receipt (an ambiguous network outcome) blocks further calls; these are never retried automatically.
* Each run has a hard call budget.
* Every call is logged to `results/*receipts*.jsonl`, and the API key is redacted from the log.

## Outputs

* `results/run_<UTC>.json`: full benchmark (versions, settings, routing, per-scale load/read/S4/S5 statistics)
* `results/tool_agent_<UTC>.json`: per-question tool calls, arguments, results, grades, LLM and tool times, final answer
* `results/*receipts*.jsonl`: raw MARA request outcomes (status, latency, usage, content or tool calls)
* A `rich` dashboard on the console for every scale

`results/` contains the reference runs quoted below.

## Reference results

Hardware: Intel i7-11700 (16 threads), NVMe, Ubuntu (Linux 6.8). Software: Python 3.14, pyoxigraph 0.5.13,
neo4j driver 6.4.1, Neo4j 5.26.31 in Docker. Run: `run_20261114T051516Z.json`. Values are medians in ms at
**100k synthetic instances**.

| Measurement | PyOxigraph | Neo4j |
|---|---|---|
| No-op round trip | 0.015 | 0.43 |
| S1 / S2 read | 0.28 / 0.18 | 0.57 / 0.76 |
| S3 read | **326** | **24** |
| S4 query_time: TBox write (+ PyOxigraph `flush()`) | 0.05 (+27) | 2.2 |
| S4 materialized: ABox migration of 37,503 facts (+ `flush()`) | 290 (+101) | 72 |
| S4 read returning 37,503 rows, query_time / materialized | 129 / 261 | 603 / 603 |
| S4 **COUNT-only** read, query_time / materialized | 10.1 / 7.1 | 3.5 / **0.34** |
| S5 100 overwrites, one commit each | 9.5 | 70 |
| S5 100 overwrites, one transaction | 24 | 24 |

What these results show:

1. **Schema evolution.** With query-time inference, neither engine touches instance data. The write-time gap reflects
   the call path (in-process vs a Bolt round trip and commit), not the data model. PyOxigraph's `flush()` (a RocksDB
   memtable flush) dominates its write time.
2. **Materialization.** In this setup, migrating 37.5k labels in Neo4j (72 ms) was faster than inserting the equivalent
   inferred triples in PyOxigraph (290 ms + flush).
3. **Reading after materialization.** Materialized labels make Neo4j's lookup the cheapest (0.34 ms COUNT). A query
   that returns 37.5k rows is dominated by transferring and decoding them in the client (about 600 ms on Neo4j).
4. **S3** slows down linearly with instance count on PyOxigraph (query plan) but stays almost flat on Neo4j.
5. **S5 mostly reflects durability, not "tombstones vs in-place".** Tracing PyOxigraph with `strace`
   (`src/fsync_probe.py`) showed 103 `write` calls but only 3 `fsync`/`fdatasync` calls across 100 commits, so its
   commits are not forced to disk. Neo4j forces its transaction log to disk on each commit by default. With a single
   commit, both take about 24 ms.

LLM tool calling (`tool_agent_20261114T051350Z.json`, `gpt-oss-120b`, 24 calls): on **both** backends, all 5
scenarios called the expected tools, got the expected results and produced an answer. S4 chained
`add_superclass` → `list_equipment_by_class` as intended. LLM time was 4–7 s per question, while tool execution took
2–106 ms. On S5, the RDF run also called `check_sensor_threshold`; the LPG run judged the threshold from the 5.0 stated in the question.

## Caveats

* **Durability differs between engines** (see point 5 above). Do not read the per-commit S5 numbers as a storage-model comparison.
* **Disk-byte deltas are not a storage-overhead metric.** Neo4j preallocates its store and log files, so its delta is ≈0. RocksDB
  compaction can make the PyOxigraph delta negative.
* **fsync-heavy timings are sensitive to other disk activity.** In the reference sweep, PyOxigraph `flush()` and
  Neo4j per-commit S5 both rose 5–10× at the 1k and 10k scales and returned to baseline at 100k, which points to
  host I/O interference rather than a scale effect. Re-run on a quiet machine.
* The data is a tiny synthetic dataset, and each engine is tested at one version on one host with default configurations.
  S1–S3 latencies at small scale mostly measure per-call overhead.
* S1 in SPARQL uses a fixed hop count; the Cypher version allows 1–3 `causedBy` hops. Both return the same answer on this data.
* The MARA function-calling link above points to a QA docs mirror; check the production docs for current behavior.

## Repository layout

```
├── data/mfg_knowledge.trig    TBox + ABox (named graphs) + RDF 1.2 confidence annotations
├── docker-compose.yml         Neo4j 5.26 Community (loopback ports, named volume)
├── pyproject.toml / uv.lock   Pinned Python dependencies
├── .env.example               MARA configuration template
├── src/
│   ├── common.py              Namespaces, synthetic data, timing helpers
│   ├── loader.py              TriG → PyOxigraph (RocksDB) and → Neo4j (nodes/edges/multi-labels)
│   ├── engine_oxigraph.py     SPARQL templates for S1–S5 and tools
│   ├── engine_neo4j.py        Cypher templates for S1–S5 and tools
│   ├── llm_agent.py           QueryIntent schema, rule parser, MARA gateway (lock, budget, receipts)
│   ├── benchmark_runner.py    Scale sweep, correctness and parity checks, dashboard and JSON
│   ├── tool_agent.py          Template-based tool-calling agent (RDF vs LPG)
│   └── fsync_probe.py         strace helper for PyOxigraph commit durability
└── results/                   Reference run outputs
```
