# How each engine persists and mutates TBox / ABox — storage CRUD study

Scripts: `src/exp_storage.py {n10s|s6|compaction}`. Raw results: `results/storage/*.json`.

The goal is to understand *how* each store handles create/update/delete on disk, because sensor data is a real-time
time series and the update path decides whether a design survives production load.

Experiment conditions:
* Embedded PyOxigraph 0.5.13 and Neo4j 5.26 Community (Docker), both pinned to the same 8 hardware threads.
* The n10s experiment uses neosemantics 5.26.0 with Neo4j 5.26.
* Every count and value below was checked against the generator's ground truth. All checks passed.

## 1. What happens on disk

| | PyOxigraph (RocksDB, LSM tree) | Neo4j 5 (record store) |
|---|---|---|
| Physical layout | Each quad is stored as keys in several **sorted index permutations** (SPOG, POSG, OSPG, GSPO, GPOS, GOSP and default-graph variants), plus a string dictionary. There are no "nodes": a resource is an ID that appears in keys | **Fixed-size records**: node store, relationship store (each node's relationships form doubly linked chains), property store. Labels sit inline in the node record (or spill to a label store) and are indexed by a token-lookup index |
| Create | Append keys to the WAL and memtable for every index permutation, which amplifies each write | Allocate records, link them into relationship chains, append to the transaction log |
| Update | **No in-place update.** A changed value means deleting the old triple (a *tombstone* in every index) and inserting the new triple | **In place**: overwrite the property record. Label add/remove rewrites the node record and the label index |
| Delete | Tombstones, physically removed only by compaction | Records are marked free and reused; relationship chains are relinked |
| Durability | Commit goes to the WAL **without fsync** (verified with strace, `src/fsync_probe.py`). `flush()` writes the memtable to SST files | Each commit **fsyncs** its transaction log; a checkpoint later flushes the page cache to the store files. `db.checkpoint()` is Enterprise-only |
| Space reclaim | Compaction (`optimize()`) | Record reuse; transaction-log rotation and pruning |

*The engine internals above come from the engines' documented designs. Only fsync behavior was verified
directly; the tables below show the observable consequences.*

## 2. Neo4j via n10s: `handleRDFTypes = LABELS` vs `NODES` (and LABELS + materialized superclass labels)

Graph: 158 lines (100k quads, about 19.9k nodes). Neo4j data was imported with
`n10s.rdf.import.inline(..., 'Turtle-star')`. All 272 RDF-star confidences became relationship properties.
The reclassification moves **500 MFC instances to a new subclass `MFC_Gen2 ⊑ MFC`**. It was run 3× forward and
3× back, and the medians are shown; the first, cold run is in the JSON.

| Operation | n10s LABELS | n10s NODES | n10s LABELS+mat | PyOxigraph query-time | PyOxigraph materialized |
|---|---:|---:|---:|---:|---:|
| Stored type information | 1 label per instance | 1 `:type` relationship per instance (class nodes become **dense hubs**: MFC has 1,264 incoming `:type` edges) | 39.7k → **68.8k labels** | 1 `rdf:type` triple | + 39k inferred-type triples |
| Hierarchy-aware class count (5 classes), ms | 8.4–19.6 *(subclass names → `MATCH (x:$any(names))`)* | 1.7–4.4 *(traverse `type`/`subClassOf`)* | **1.4–2.1** *(one label)* | 0.27–3.2 | 0.22–2.5 |
| TBox: add superclass over 2 modules | edge write only | edge write only | edge write + **label migration 1,264 nodes: 46 ms** | 0.04 ms commit (+21 ms flush) | 0.04 + **8.1 ms migration** (1,264 quads) (+29 ms flush) |
| **ABox: reclassify 500 instances**, ms (median) | **16.6** (−500/+500 labels) | **275** (250–452; 500 relationships deleted and recreated at a dense class node) | **8.8** (+500 labels; MFC stays as an inferred label) | 8.2 (+28 ms flush) | 17.8 (+31 ms flush; +500 inferred quads) |
| TBox: delete the superclass | node + 2 edges | node + 2 edges | **remove 1,264 labels** + node: 35 ms | 3 quads | **1,267 quads** |

What this shows:
* **Changing a label is not expensive in itself.** Relabeling 500 nodes took 9–17 ms.
* **The costly LPG pattern was `rdf:type` as relationships (`NODES`).** Reclassification there was 17–31× slower,
  because every type change edits the relationship chain of a high-degree class node.
* **Write-time materialization (superclass labels in Neo4j, inferred triples in RDF) moves the cost from reads to
  writes on both engines.** Counts become one label lookup (1.4–2.1 ms), but every TBox change or deletion touches
  every affected instance (1,264 labels in Neo4j; 1,264–1,267 quads in Oxigraph).
* PyOxigraph needs no relationship-chain maintenance for any of this. Its costs come from writing to several index
  permutations, and making a write durable needs `flush()`.
* *Caveat:* the modes ran in a fixed order (LABELS → NODES → LABELS+mat) on one JVM, so import times include
  warm-up and are not compared here. Neo4j store sizes are not reported: Community cannot force a checkpoint, and
  transaction logs are preallocated.

## 3. S6: append-only sensor time series (every reading is a new `sosa:Observation`)

Graph: 16 lines, 768 sensored parts. Each round appends **768 readings in one transaction** (each reading = type,
feature of interest, observed property, value and `sosa:resultTime`), for 200 rounds, ending at 154k observations.

Two variants:
* **A_sort**: the latest value is found by sorting each part's observations by time.
* **B_pointer**: in the same transaction, each part's `ex:latestObservation` pointer is moved to the new reading.

| Round (observations) | Engine / variant | Append ms / 768 readings | Latest value of a part, ms | Line: parts whose latest reading exceeds spec, ms |
|---|---|---:|---:|---:|
| 1 (1.5k) | PyOxigraph A_sort | 26 | 0.10 | 25 |
| 100 (78k) | PyOxigraph A_sort | 37 | 1.20 | 1,358 |
| 200 (154k) | PyOxigraph A_sort | 35 | **2.26** | **2,836** |
| 200 (154k) | PyOxigraph B_pointer | 86 | **0.09** | **1.8** |
| 1 (1.5k) | Neo4j A_sort | 284 (cold) | 4.4 (cold) | 3.0 |
| 200 (154k) | Neo4j A_sort | 47 | 0.69 | 7.1 |
| 200 (154k) | Neo4j B_pointer | 106 | 0.53 | 0.93 |

What this shows:
* **Appending is cheap and flat on both engines.** PyOxigraph took about 35 ms per 768 readings at every size;
  Neo4j about 47 ms (after warm-up).
* **Without a "latest" pointer, reading the current state gets more expensive as history grows.** The cost grows
  linearly on PyOxigraph (per-line query 25 ms → 2.8 s, using `GROUP BY ?p MAX(?t)`) and mildly on Neo4j
  (3 → 7 ms), because Neo4j follows each part's relationship chain.
* **A latest pointer keeps current-state reads flat** (0.09 ms and 1.8 ms in RDF; 0.5 ms and 0.9 ms in Neo4j). It
  costs about 2.3–2.5× more per append, because the pointer is an update: delete+insert in RDF, relationship
  delete and create in Neo4j.
* Disk growth: PyOxigraph reached 388 MB (A) and 421 MB (B) at 154k readings, about 2.5 KB per reading including
  the WAL. Neo4j grows in 256 MB transaction-log preallocation steps, so per-reading cost cannot be read off here.

## 4. Repeated overwrites: tombstones and compaction

Graph: 158 lines (7,584 observations). There were 200 rounds of 500 random overwrites, 100k updates in total, each
round one transaction. PyOxigraph committed without `flush()` until the end.

| Phase | PyOxigraph WAL MB | SST files / MB | Batch write ms | Scan all values ms | Global S3 count ms | Read 500 values ms |
|---|---:|---:|---:|---:|---:|---:|
| loaded | 67 | 10 / 31 | — | 1.57 | 65 | 4.3 |
| after 100 rounds | 115 | 9 / 31 | 15.0 | 8.46 | 84 | 4.7 |
| after 200 rounds | 163 | 9 / 31 | **19.1** (from 9.1) | **20.5** (13×) | 101 | 5.6 |
| after `flush()` (287 ms) | 163 | 13 / 41 | — | **1.53** | 77 | 4.5 |
| after `optimize()` (6 ms) | 163 | 9 / 31 | — | 1.49 | 77 | 4.9 |

| Phase | Neo4j store MB | tx log MB | Batch write ms | Scan all values ms | Global S3 count ms |
|---|---:|---:|---:|---:|---:|
| loaded | 53.1 | 807 (preallocated) | — | 28.2 (cold) | 17.8 |
| after 200 rounds | **53.1** (unchanged) | 807 | 7.1 | 4.6 | 6.0 |

What this shows:
* **Append-only has a hidden cost on PyOxigraph.** Overwrites pile up delete markers and versions in the
  in-memory LSM layer. Writes slowed 2.1× (each DELETE/INSERT must find the current value), and a full value scan
  slowed 13× over 100k updates.
* A `flush()` (memtable → SST, 287 ms here) restored scan speed. `optimize()` then merged the SST files back to
  the pre-update count.
* The same effect showed up in the server load test. Hammering **one** observation (`Whot`) gave a p50 of
  **12.5 ms** at concurrency 1, against 0.77 ms when writes were spread out (`W`).
* **Neo4j's in-place property update kept the store size constant** (53 MB), with stable write and read latency.
* Practical rule for RDF: do not overwrite hot values in place at high frequency. Use append + pointer (§3), batch
  the updates, and run flush/compaction regularly. Also serialize writers: concurrent DELETE/INSERT produced write
  skew (see the README load-test section).

## 5. Summary for sensor (real-time time-series) design

| Claim | Status |
|---|---|
| "Neo4j's multi-labels make updates and deletes expensive" | **Not supported as stated.** Relabeling was cheap (9–17 ms per 500 nodes). The expensive cases were (a) write-time materialized superclass labels on TBox changes, a migration proportional to affected instances, which RDF also pays when it materializes, and (b) `rdf:type` stored as relationships (`NODES`), where dense class nodes made reclassification 17–31× slower |
| "PyOxigraph is append-only, so it has no such burden" | **Partly.** Schema changes with query-time inference touch no instance data (true for both engines). But updates are delete+insert across several indexes, tombstones degrade reads and writes until flush/compaction, commits are not durable without `flush()`, and concurrent read-modify-write is not serializable |
| What to do for real-time sensor data | Model readings as **append-only observations** (both engines append cheaply). Keep a **latest pointer** (or a separate current-state property) for real-time queries. Batch writes. In RDF, serialize writers and schedule flush/compaction. Keep long raw history in a time-series store, and the topology plus current state in the graph |
