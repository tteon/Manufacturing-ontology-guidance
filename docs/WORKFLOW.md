# Workflow walkthrough with real data (run `tool_agent_20261114T060707Z.json`)

Graph: `generate_connected_fab_graph(16)` (seed 7) — 10,065 quads. Model: `gpt-oss-120b` via MARA function calling, 25 API calls in total. Both backends received the same tool schemas; only the template behind each tool differs.

```
user request ─► LLM (tools=[8 JSON-schema functions], tool_choice=auto, reasoning_effort=high)
      ─► tool_call {name, arguments}  ─► safe_id() validation ─► fill SPARQL template (RDF) | Cypher template (LPG)
      ─► engine result ─► role:tool message ─► LLM ─► (more tool calls | final Korean answer)
      ─► grading: tool set + precision/recall of tool results vs generator ground truth
```

## S1

**User request** — Lot_14_7에서 이상 증상이 나왔어. 증상→원인→부품 경로의 누적 신뢰도가 0.48 이상인 근본 원인과 책임 부품을 찾아줘.

**Relevant real data (generator)**

`Lot_14_7` processedIn ['Ch_14_1', 'Ch_14_2', 'Ch_14_3', 'Ch_14_4'] · hasSymptom ['Sym_14_1']

| Symptom | Cause | conf(sym→cause) | Part | conf(cause→part) | product | Kind (planted) | ≥ 0.48 |
|---|---|---:|---|---:|---:|---|---|
| Sym_14_1 | Cause_14_1_c1 | 0.852 | Seal_14_1_TM4 | 0.735 | 0.6262 | correct | ✓ |
| Sym_14_1 | Cause_14_1_c2 | 0.822 | Seal_14_1_GSM3 | 0.842 | 0.6921 | correct | ✓ |
| Sym_14_1 | Cause_14_1_w1 | 0.425 | RFGenerator_13_2_PM1 | 0.762 | 0.3238 | wrong |  |
| Sym_14_1 | Cause_14_1_w2 | 0.104 | MFC_15_4_GSM1 | 0.847 | 0.0881 | wrong |  |
| Sym_14_1 | Cause_14_1_w3 | 0.263 | Seal_16_2_TM3 | 0.883 | 0.2322 | wrong |  |
| Sym_14_1 | Cause_14_1_w4 | 0.307 | Seal_8_2_TM4 | 0.767 | 0.2355 | wrong |  |
| Sym_14_1 | Cause_14_1_d1 | 0.868 | MFC_11_3_GSM1 | 0.136 | 0.1180 | decoy |  |
| Sym_14_1 | Cause_14_1_d2 | 0.782 | RFGenerator_11_1_PM1 | 0.153 | 0.1196 | decoy |  |

**LLM turns (raw from MARA receipts, RDF run)**

| Turn | finish_reason | Tool call(s) | Prompt / completion (reasoning) tokens | Latency ms |
|---|---|---|---|---:|
| 1 | tool_calls | `find_root_causes({"lot_id": "Lot_14_7", "min_confidence": 0.48})` | 599 / 249 (231) | 1092.5 |
| 2 | stop | — (final answer) | 702 / 568 (464) | 1429.8 |

**Tool `find_root_causes`** args (RDF run) `{"lot_id": "Lot_14_7", "min_confidence": 0.48}` · args (LPG run) `{"lot_id": "Lot_14_7"}`

<table><tr><th>SPARQL template filled (PyOxigraph)</th><th>Cypher template (Neo4j) + parameters</th></tr>
<tr><td><pre>SELECT DISTINCT ?cause ?part ?score FROM ex:TBox_Graph FROM ex:ABox_Graph WHERE {
  ex:Lot_14_7 ex:hasSymptom ?sym .
  ?sym ex:causedBy ?cause . ?cause ex:attributedTo ?part .
  << ?sym ex:causedBy ?cause >> ex:confidence ?c1 .
  << ?cause ex:attributedTo ?part >> ex:confidence ?c2 .
  BIND(?c1 * ?c2 AS ?score) FILTER(?score >= 0.48) }</pre></td><td><pre>MATCH (:Resource {id: $lot})-[:hasSymptom]->(sym)-[r1:causedBy]->(cause)-[r2:attributedTo]->(part)
WITH cause, part, r1.confidence * r2.confidence AS score WHERE score >= $min_conf
RETURN DISTINCT cause.id AS cause, part.id AS part, score</pre><pre>{"lot": "Lot_14_7", "min_conf": 0.48}</pre></td></tr></table>

Result RDF: `[["Cause_14_1_c1", "Seal_14_1_TM4", 0.6262], ["Cause_14_1_c2", "Seal_14_1_GSM3", 0.6921]]`

Result LPG: `[["Cause_14_1_c1", "Seal_14_1_TM4", 0.6262], ["Cause_14_1_c2", "Seal_14_1_GSM3", 0.6921]]` — results identical

**Final answers (generation)**

- RDF: Lot 14_7에 대한 근본 원인·책임 부품(신뢰도 ≥ 0.48)은 다음과 같습니다.   - 원인 **Cause_14_1_c1**, 부품 **Seal_14_1_TM4**, 신뢰도 0.6262   - 원인 **Cause_14_1_c2**, 부품 **Seal_14_1_GSM3**, 신뢰도 0.6921
- LPG: Lot_14_7의 근본 원인 및 책임 부품 (신뢰도 ≥ 0.48): - 원인: **Cause_14_1_c1**, 부품: **Seal_14_1_TM4**, 신뢰도: **0.6262** - 원인: **Cause_14_1_c2**, 부품: **Seal_14_1_GSM3**, 신뢰도: **0.6921**

**Grade** — RDF {'tools_ok': True, 'precision': 1.0, 'recall': 1.0, 'answered': True} · LPG {'tools_ok': True, 'precision': 1.0, 'recall': 1.0, 'answered': True} · LLM HTTP time (sum of receipt latencies) 2522 / 2642 ms, tool time 3.5 / 31.5 ms

---

## S2

**User request** — Seal_7_2_PM4 부품에서 결함이 발견됐어. partOf와 feedsInto를 따라 오염이 전파됐을 수 있는 의심 로트를 모두 추적해줘.

**Relevant real data (generator)**

`Seal_7_2_PM4` hasDefect Swelling · partOf chain: Seal_7_2_PM4 → PM_7_2 → Ch_7_2 → Line_7
host chamber `Ch_7_2` · feedsInto* downstream: ['Ch_7_2', 'Ch_7_3', 'Ch_7_4']
lots processed in those chambers (truth): ['Lot_7_1', 'Lot_7_10', 'Lot_7_2', 'Lot_7_3', 'Lot_7_4', 'Lot_7_5', 'Lot_7_6', 'Lot_7_7', 'Lot_7_8', 'Lot_7_9']

**LLM turns (raw from MARA receipts, RDF run)**

| Turn | finish_reason | Tool call(s) | Prompt / completion (reasoning) tokens | Latency ms |
|---|---|---|---|---:|
| 1 | tool_calls | `trace_contamination({"component_id": "Seal_7_2_PM4"})` | 595 / 147 (130) | 895.5 |
| 2 | stop | — (final answer) | 707 / 457 (383) | 1261.3 |

**Tool `trace_contamination`** args (RDF run) `{"component_id": "Seal_7_2_PM4"}` · args (LPG run) `{"component_id": "Seal_7_2_PM4"}`

<table><tr><th>SPARQL template filled (PyOxigraph)</th><th>Cypher template (Neo4j) + parameters</th></tr>
<tr><td><pre>SELECT DISTINCT ?lot FROM ex:TBox_Graph FROM ex:ABox_Graph WHERE {
  ex:Seal_7_2_PM4 ex:hasDefect ?d ; ex:partOf+ ?host .
  ?host a/rdfs:subClassOf* ex:ProcessChamber .
  ?host ex:feedsInto* ?ch . ?lot ex:processedIn ?ch . }</pre></td><td><pre>MATCH (c:Resource {id: $component})-[:hasDefect]->()
MATCH (c)-[:partOf*1..]->(host)-[:type]->()-[:subClassOf*0..]->(:Resource {id: 'ProcessChamber'})
MATCH (host)-[:feedsInto*0..]->(ch)<-[:processedIn]-(lot)
RETURN DISTINCT lot.id AS lot</pre><pre>{"component": "Seal_7_2_PM4"}</pre></td></tr></table>

Result RDF: `["Lot_7_1", "Lot_7_10", "Lot_7_2", "Lot_7_3", "Lot_7_4", "Lot_7_5", "Lot_7_6", "Lot_7_7", "Lot_7_8", "Lot_7_9"]`

Result LPG: `["Lot_7_1", "Lot_7_10", "Lot_7_2", "Lot_7_3", "Lot_7_4", "Lot_7_5", "Lot_7_6", "Lot_7_7", "Lot_7_8", "Lot_7_9"]` — results identical

**Final answers (generation)**

- RDF: 의심되는 로트: Lot_7_1, Lot_7_10, Lot_7_2, Lot_7_3, Lot_7_4, Lot_7_5, Lot_7_6, Lot_7_7, Lot_7_8, Lot_7_9.
- LPG: 다음 로트들이 의심됩니다: Lot_7_1, Lot_7_10, Lot_7_2, Lot_7_3, Lot_7_4, Lot_7_5, Lot_7_6, Lot_7_7, Lot_7_8, Lot_7_9.

**Grade** — RDF {'tools_ok': True, 'precision': 1.0, 'recall': 1.0, 'answered': True} · LPG {'tools_ok': True, 'precision': 1.0, 'recall': 1.0, 'answered': True} · LLM HTTP time (sum of receipt latencies) 2157 / 2087 ms, tool time 1.5 / 64.8 ms

---

## S3

**User request** — Line_5 라인에서 센서 측정값이 사양(maxAllowedDrift)을 초과한 부품을 찾아줘.

**Relevant real data (generator)**

| Part | Class | Spec (via owl:hasValue restriction) | maxAllowedDrift | Reading | Exceeds |
|---|---|---|---:|---:|---|
| Seal_5_4_TM4 | Seal | LeakRateSpec | 1.0 | 1.136 | ✓ |
| Heater_5_1_TM2 | Heater | TempDriftSpec | 10.0 | 10.868 | ✓ |
| Seal_5_4_GSM3 | Seal | LeakRateSpec | 1.0 | 1.049 | ✓ |
| Seal_5_1_PM4 | Seal | LeakRateSpec | 1.0 | 1.005 | ✓ |
| Seal_5_1_TM3 | Seal | LeakRateSpec | 1.0 | 0.989 |  |
| MFC_5_3_GSM2 | MFC | FlowDriftSpec | 5.0 | 4.891 |  |
| MFC_5_4_GSM2 | MFC | FlowDriftSpec | 5.0 | 4.616 |  |
| Seal_5_4_GSM4 | Seal | LeakRateSpec | 1.0 | 0.87 |  |

(48 observed parts in Line_5; top 8 by reading/threshold ratio shown)

**LLM turns (raw from MARA receipts, RDF run)**

| Turn | finish_reason | Tool call(s) | Prompt / completion (reasoning) tokens | Latency ms |
|---|---|---|---|---:|
| 1 | tool_calls | `classify_sensor_anomalies({"line_id": "Line_5"})` | 580 / 292 (272) | 1047.8 |
| 2 | stop | — (final answer) | 662 / 325 (268) | 1104.4 |

**Tool `classify_sensor_anomalies`** args (RDF run) `{"line_id": "Line_5"}` · args (LPG run) `{"line_id": "Line_5"}`

<table><tr><th>SPARQL template filled (PyOxigraph)</th><th>Cypher template (Neo4j) + parameters</th></tr>
<tr><td><pre>SELECT DISTINCT ?part FROM ex:TBox_Graph FROM ex:ABox_Graph WHERE {
  ?part ex:partOf+ ex:Line_5 .
  ?obs sosa:hasFeatureOfInterest ?part ; sosa:hasSimpleResult ?v .
  ?part a ?cls . ?cls rdfs:subClassOf* ?c . ?c rdfs:subClassOf ?r .
  ?r owl:onProperty ssn:hasProperty ; owl:hasValue ?spec . ?spec ex:maxAllowedDrift ?max .
  FILTER(?v > ?max) }</pre></td><td><pre>MATCH (:Resource {id: $line})<-[:partOf*1..]-(part)<-[:hasFeatureOfInterest]-(obs)
MATCH (part)-[:type]->()-[:subClassOf*0..]->()-[:subClassOf]->(r)-[:onProperty]->(:Resource {id: 'hasProperty'})
MATCH (r)-[:hasValue]->(spec) WHERE obs.hasSimpleResult > spec.maxAllowedDrift
RETURN DISTINCT part.id AS part</pre><pre>{"line": "Line_5"}</pre></td></tr></table>

Result RDF: `["Heater_5_1_TM2", "Seal_5_1_PM4", "Seal_5_4_GSM3", "Seal_5_4_TM4"]`

Result LPG: `["Heater_5_1_TM2", "Seal_5_1_PM4", "Seal_5_4_GSM3", "Seal_5_4_TM4"]` — results identical

**Final answers (generation)**

- RDF: Line_5 라인에서 사양을 초과한 부품은 Heater_5_1_TM2, Seal_5_1_PM4, Seal_5_4_GSM3, Seal_5_4_TM4 입니다.
- LPG: Line 5 라인에서 사양을 초과한 부품은 Heater_5_1_TM2, Seal_5_1_PM4, Seal_5_4_GSM3, Seal_5_4_TM4 입니다.

**Grade** — RDF {'tools_ok': True, 'precision': 1.0, 'recall': 1.0, 'answered': True} · LPG {'tools_ok': True, 'precision': 1.0, 'recall': 1.0, 'answered': True} · LLM HTTP time (sum of receipt latencies) 2152 / 2200 ms, tool time 17.4 / 86.9 ms

---

## S4

**User request** — 새로운 환경 안전 규정에 따라 GasSupplyModule과 PlasmaModule을 모두 CriticalHazardSystem이라는 새로운 상위 클래스로 묶고, 이 클래스에 속한 모든 설비는 즉시 비상 인터록 점검(Emergency_Interlock_Check) 대상이 된다고 스키마(TBox)를 업데이트해 줘. 그리고 현재 이 규정에 해당하는 현장 설비 목록을 바로 뽑아줘.

**Relevant real data (generator)**

Instances of GasSupplyModule ∪ PlasmaModule (truth): 128 → ["GSM_10_1", "GSM_10_2", "GSM_10_3", "GSM_10_4", "GSM_11_1", "GSM_11_2", "GSM_11_3", "GSM_11_4"] … (+120 more, 128 total)
Query-time strategy: no ABox triple/label is written; the new subClassOf edges are traversed at read time.

**LLM turns (raw from MARA receipts, RDF run)**

| Turn | finish_reason | Tool call(s) | Prompt / completion (reasoning) tokens | Latency ms |
|---|---|---|---|---:|
| 1 | tool_calls | `add_superclass({"new_parent_class": "CriticalHazardSystem", "required_policy": "Emergency_Interlock_Check", "subclasses": ["GasSupplyModule", "PlasmaModule"]})` | 643 / 729 (712) | 1657.1 |
| 2 | tool_calls | `list_equipment_by_class({"class_id": "CriticalHazardSystem"})` | 723 / 162 (143) | 949.2 |
| 3 | stop | — (final answer) | 2697 / 1765 (634) | 3201.8 |

**Tool `add_superclass`** args (RDF run) `{"new_parent_class": "CriticalHazardSystem", "required_policy": "Emergency_Interlock_Check", "subclasses": ["GasSupplyModule", "PlasmaModule"]}` · args (LPG run) `{"new_parent_class": "CriticalHazardSystem", "required_policy": "Emergency_Interlock_Check", "subclasses": ["GasSupplyModule", "PlasmaModule"]}`

<table><tr><th>SPARQL template filled (PyOxigraph)</th><th>Cypher template (Neo4j) + parameters</th></tr>
<tr><td><pre># PyOxigraph: store.extend([...]) adds these quads to ex:TBox_Graph (no ABox change)
ex:GasSupplyModule rdfs:subClassOf ex:CriticalHazardSystem .
ex:PlasmaModule rdfs:subClassOf ex:CriticalHazardSystem .
ex:CriticalHazardSystem ex:requiredPolicy "Emergency_Interlock_Check" .</pre></td><td><pre>MERGE (p:Resource {id: $parent}) SET p.requiredPolicy = $policy
WITH p MATCH (sub:Resource) WHERE sub.id IN $subs MERGE (sub)-[:subClassOf]->(p)</pre><pre>{"parent": "CriticalHazardSystem", "subs": ["GasSupplyModule", "PlasmaModule"], "policy": "Emergency_Interlock_Check"}</pre></td></tr></table>

Result RDF: `{"status": "ok", "abox_facts_written": 0}`

Result LPG: `{"status": "ok", "abox_facts_written": 0}` — results identical

**Tool `list_equipment_by_class`** args (RDF run) `{"class_id": "CriticalHazardSystem"}` · args (LPG run) `{"class_id": "CriticalHazardSystem"}`

<table><tr><th>SPARQL template filled (PyOxigraph)</th><th>Cypher template (Neo4j) + parameters</th></tr>
<tr><td><pre>SELECT DISTINCT ?eq ?eqType ?policy FROM ex:TBox_Graph FROM ex:ABox_Graph WHERE {
  ?eq a ?eqType . ?eqType rdfs:subClassOf* ex:CriticalHazardSystem .
  OPTIONAL { ex:CriticalHazardSystem ex:requiredPolicy ?policy } } ORDER BY ?eq</pre></td><td><pre>MATCH (c:Resource {id: $cls})
MATCH (eq)-[:type]->(eqType)-[:subClassOf*0..]->(c)
RETURN DISTINCT eq.id AS eq, eqType.id AS eqType, c.requiredPolicy AS policy ORDER BY eq</pre><pre>{"cls": "CriticalHazardSystem"}</pre></td></tr></table>

Result RDF: `[{"eq": "GSM_10_1", "eqType": "GasSupplyModule", "policy": "Emergency_Interlock_Check"}, {"eq": "GSM_10_2", "eqType": "GasSupplyModule", "policy": "Emergency_Interlock_Check"}, {"eq": "GSM_10_3", "eqType": "GasSupplyModule", "policy": "Emergency_Interlock_Check"}, {"eq": "GSM_10_4", "eqType": "GasSupplyModule", "policy": "Emergency_Interlock_Check"}, {"eq": "GSM_11_1", "eqType": "GasSupplyModule", "policy": "Emergency_Interlock_Check"}, {"eq": "GSM_11_2", "eqType": "GasSupplyModule", "policy": "Emergency_Interlock_Check"}, {"eq": "GSM_11_3", "eqType": "GasSupplyModule", "policy": "Emergency_Interlock_Check"}, {"eq": "GSM_11_4", "eqType": "GasSupplyModule", "policy": "Emergency_Interlock_Check"}, {"eq": "GSM_12_1", "eqType": "GasSupplyModule", "policy": "Emergency_Interlock_Check"}, {"eq": "GSM_12_2", "eqType": "GasSupplyModule", "policy": "Emergency_Interlock_Check"}, {"eq": "GSM_12_3", "eqType": "GasSupplyModule", "policy": "Emergency_Interlock_Check"}, {"eq": "GSM_12_4", "eqType": "GasSupplyModule", "policy": "Emergency_Interlock_Check"}] … (+116 more, 128 total)`

Result LPG: `[{"eq": "GSM_10_1", "eqType": "GasSupplyModule", "policy": "Emergency_Interlock_Check"}, {"eq": "GSM_10_2", "eqType": "GasSupplyModule", "policy": "Emergency_Interlock_Check"}, {"eq": "GSM_10_3", "eqType": "GasSupplyModule", "policy": "Emergency_Interlock_Check"}, {"eq": "GSM_10_4", "eqType": "GasSupplyModule", "policy": "Emergency_Interlock_Check"}, {"eq": "GSM_11_1", "eqType": "GasSupplyModule", "policy": "Emergency_Interlock_Check"}, {"eq": "GSM_11_2", "eqType": "GasSupplyModule", "policy": "Emergency_Interlock_Check"}, {"eq": "GSM_11_3", "eqType": "GasSupplyModule", "policy": "Emergency_Interlock_Check"}, {"eq": "GSM_11_4", "eqType": "GasSupplyModule", "policy": "Emergency_Interlock_Check"}, {"eq": "GSM_12_1", "eqType": "GasSupplyModule", "policy": "Emergency_Interlock_Check"}, {"eq": "GSM_12_2", "eqType": "GasSupplyModule", "policy": "Emergency_Interlock_Check"}, {"eq": "GSM_12_3", "eqType": "GasSupplyModule", "policy": "Emergency_Interlock_Check"}, {"eq": "GSM_12_4", "eqType": "GasSupplyModule", "policy": "Emergency_Interlock_Check"}] … (+116 more, 128 total)` — results identical

**Final answers (generation)**

- RDF: 새로운 상위 클래스 **CriticalHazardSystem**을 만들고 **GasSupplyModule**·**PlasmaModule**을 하위 클래스로 지정했습니다.   이 클래스에 속하는 설비는 현재 **128개**이며, 도구가 반환한 첫 60개의 설비는 다음과 같습니다.  | 설비 ID | 설비 유형 | 적용 정책 | |--------|----------|------------| | GSM_10_1 | GasSupplyModule | Emergency_Interlock_Check | | GSM_10_2 | GasSupplyModule | Emergency_Interlock_Check | | GSM_10_3 | GasSupplyModule | Emergency_Interlock_Check | | GSM_10_4 | GasSupplyModule | Emergency_Interlock_Check | | GSM_11_1 | GasSupplyModule | Emergency_Interlock_Check | | GSM_11_2 | GasSupplyModule | Emergency_Interlock_Check | | GSM_11_3 | GasSupplyModule | Emergency_Interlock_Check | | GSM_11_4 | GasSupplyModule | Emergency_Interlock_Check | | GSM_12_1 | GasSupplyModule | Emergency_Interlock_Check | | GSM_12_2 | GasSupplyModule | Emergency_Interlock_Check | | GSM_12_3 | GasSupplyModule | Emergency_Interlock_Check | | GSM_12_4 | GasSupplyModule | Emergency_Interlock_Check | | GSM_13_1 | GasSupplyModule | Emergency_Interlock_Check | | GSM_13_2 | GasSupplyModule | Emergency_Interlock_Check | | GSM_13_3 | GasSupplyModule | Emergency_Interlock_Check | | GSM_13_4 | GasSupplyModule | Emergency_Interlock_Check | | GSM_14_1 | GasSupplyModule | Emergency_Interlock_Check | | GSM_14_2 | GasSupplyModule | Emergency_Interlock_Check | | GSM_14_3 | GasSupplyModule | Emergency_Interlock_Check | | GSM_14_4 | GasSupplyModule | Emergency_Interlock_Check | | GSM_15_1 | GasSupplyModule | Emergency_Interlock_Check | | GSM_15_2 | GasSupplyModule | Emergency_Interlock_Check | | GSM_15_3 | GasSupplyModule | Emergency_Interlock_Check | | GSM_15_4 | GasSupplyModule | Emergency_Interlock_Check | | GSM_16_1 | GasSupplyModule | Emergency_Interlock_Check | | GSM_16_2 | GasSupplyModule | Emergency_Interlock_Check | | GSM_16_3 | GasSupplyModule | Emergency_Interlock_Check | | GSM_16_4 | GasSupplyModule | Emergency_Interlock_Check | | GSM_1_1 | GasSupplyModule | Emergency_Interlock_Check | | GSM_1_2 | GasSupplyModule | Emergency_Interlock_Check | | GSM_1_3 | GasSupplyModule | Emergency_Interlock_Check | | GSM_1_4 | GasSupplyModule | Emergency_Interlock_Check | | GSM_2_1 | GasSupplyModule | Emergency_Interlock_Check | | GSM_2_2 | GasSupplyModule | Emergency_Interlock_Check | | GSM_2_3 | GasSupplyModule | Emergency_Interlock_Check | | GSM_2_4 | GasSupplyModule | Emergency_Interlock_Check | | GSM_3_1 | GasSupplyModule | Emergency_Interlock_Check | | GSM_3_2 | GasSupplyModule | Emergency_Interlock_Check | | GSM_3_3 | GasSupplyModule | Emergency_Interlock_Check | | GSM_3_4 | GasSupplyModule | Emergency_Interlock_Check | | GSM_4_1 | GasSupplyModule | Emergency_Interlock_Check | | GSM_4_2 | GasSupplyModule | Emergency_Interlock_Check | | GSM_4_3 | GasSupplyModule | Emergency_Interlock_Check | | GSM_4_4 | GasSupplyModule | Emergency_Interlock_Check | | GSM_5_1 | GasSupplyModule | Emergency_Interlock_Check | | GSM_5_2 | GasSupplyModule | Emergency_Interlock_Check | | GSM_5_3 | GasSupplyModule | Emergency_Interlock_Check | | GSM_5_4 | GasSupplyModule | Emergency_Interlock_Check | | GSM_6_1 | GasSupplyModule | Emergency_Interlock_Check | | GSM_6_2 | GasSupplyModule | Emergency_Interlock_Check | | GSM_6_3 | GasSupplyModule | Emergency_Interlock_Check | | GSM_6_4 | GasSupplyModule | Emergency_Interlock_Check | | GSM_7_1 | GasSupplyModule | Emergency_Interlock_Check | | GSM_7_2 | GasSupplyModule | Emergency_Interlock_Check | | GSM_7_3 | GasSupplyModule | Emergency_Interlock_Check | | GSM_7_4 | GasSupplyModule | Emergency_Interlock_Check | | GSM_8_1 | GasSupplyModule | Emergency_Interlock_Check | | GSM_8_2 | GasSupplyModule | Emergency_Interlock_Check | | GSM_8_3 | GasSupplyModule | Emergency_Interlock_Check | | GSM_8_4 | GasSupplyModule | Emergency_Interlock_Check |  (이 외에 나머지 68개의 설비도 동일 정책이 적용됩니다.)
- LPG: 스키마 업데이트가 정상적으로 완료되었습니다. **CriticalHazardSystem**에 해당하는 설비는 총 **128개**이며, 현재 확인된 일부(첫 60개) 설비는 다음과 같습니다.  | 설비 ID | 설비 유형 | 적용 정책 | |--------|----------|-----------| | GSM_10_1 | GasSupplyModule | Emergency_Interlock_Check | | GSM_10_2 | GasSupplyModule | Emergency_Interlock_Check | | GSM_10_3 | GasSupplyModule | Emergency_Interlock_Check | | GSM_10_4 | GasSupplyModule | Emergency_Interlock_Check | | GSM_11_1 | GasSupplyModule | Emergency_Interlock_Check | | GSM_11_2 | GasSupplyModule | Emergency_Interlock_Check | | GSM_11_3 | GasSupplyModule | Emergency_Interlock_Check | | GSM_11_4 | GasSupplyModule | Emergency_Interlock_Check | | GSM_12_1 | GasSupplyModule | Emergency_Interlock_Check | | GSM_12_2 | GasSupplyModule | Emergency_Interlock_Check | | GSM_12_3 | GasSupplyModule | Emergency_Interlock_Check | | GSM_12_4 | GasSupplyModule | Emergency_Interlock_Check | | GSM_13_1 | GasSupplyModule | Emergency_Interlock_Check | | GSM_13_2 | GasSupplyModule | Emergency_Interlock_Check | | GSM_13_3 | GasSupplyModule | Emergency_Interlock_Check | | GSM_13_4 | GasSupplyModule | Emergency_Interlock_Check | | GSM_14_1 | GasSupplyModule | Emergency_Interlock_Check | | GSM_14_2 | GasSupplyModule | Emergency_Interlock_Check | | GSM_14_3 | GasSupplyModule | Emergency_Interlock_Check | | GSM_14_4 | GasSupplyModule | Emergency_Interlock_Check | | GSM_15_1 | GasSupplyModule | Emergency_Interlock_Check | | GSM_15_2 | GasSupplyModule | Emergency_Interlock_Check | | GSM_15_3 | GasSupplyModule | Emergency_Interlock_Check | | GSM_15_4 | GasSupplyModule | Emergency_Interlock_Check | | GSM_16_1 | GasSupplyModule | Emergency_Interlock_Check | | GSM_16_2 | GasSupplyModule | Emergency_Interlock_Check | | GSM_16_3 | GasSupplyModule | Emergency_Interlock_Check | | GSM_16_4 | GasSupplyModule | Emergency_Interlock_Check | | GSM_1_1  | GasSupplyModule | Emergency_Interlock_Check | | GSM_1_2  | GasSupplyModule | Emergency_Interlock_Check | | GSM_1_3  | GasSupplyModule | Emergency_Interlock_Check | | GSM_1_4  | GasSupplyModule | Emergency_Interlock_Check | | GSM_2_1  | GasSupplyModule | Emergency_Interlock_Check | | GSM_2_2  | GasSupplyModule | Emergency_Interlock_Check | | GSM_2_3  | GasSupplyModule | Emergency_Interlock_Check | | GSM_2_4  | GasSupplyModule | Emergency_Interlock_Check | | GSM_3_1  | GasSupplyModule | Emergency_Interlock_Check | | GSM_3_2  | GasSupplyModule | Emergency_Interlock_Check | | GSM_3_3  | GasSupplyModule | Emergency_Interlock_Check | | GSM_3_4  | GasSupplyModule | Emergency_Interlock_Check | | GSM_4_1  | GasSupplyModule | Emergency_Interlock_Check | | GSM_4_2  | GasSupplyModule | Emergency_Interlock_Check | | GSM_4_3  | GasSupplyModule | Emergency_Interlock_Check | | GSM_4_4  | GasSupplyModule | Emergency_Interlock_Check | | GSM_5_1  | GasSupplyModule | Emergency_Interlock_Check | | GSM_5_2  | GasSupplyModule | Emergency_Interlock_Check | | GSM_5_3  | GasSupplyModule | Emergency_Interlock_Check | | GSM_5_4  | GasSupplyModule | Emergency_Interlock_Check | | GSM_6_1  | GasSupplyModule | Emergency_Interlock_Check | | GSM_6_2  | GasSupplyModule | Emergency_Interlock_Check | | GSM_6_3  | GasSupplyModule | Emergency_Interlock_Check | | GSM_6_4  | GasSupplyModule | Emergency_Interlock_Check | | GSM_7_1  | GasSupplyModule | Emergency_Interlock_Check | | GSM_7_2  | GasSupplyModule | Emergency_Interlock_Check | | GSM_7_3  | GasSupplyModule | Emergency_Interlock_Check | | GSM_7_4  | GasSupplyModule | Emergency_Interlock_Check | | GSM_8_1  | GasSupplyModule | Emergency_Interlock_Check | | GSM_8_2  | GasSupplyModule | Emergency_Interlock_Check | | GSM_8_3  | GasSupplyModule | Emergency_Interlock_Check | | GSM_8_4  | GasSupplyModule | Emergency_Interlock_Check |  ※ 위 목록은 전체 128개 설비 중 앞부분(60개)이며, 나머지 설비도 동일한 정책이 적용됩니다. 필요 시 전체 목록을 별도로 제공해 드릴 수 있습니다.

**Grade** — RDF {'tools_ok': True, 'precision': 1.0, 'recall': 1.0, 'answered': True} · LPG {'tools_ok': True, 'precision': 1.0, 'recall': 1.0, 'answered': True} · LLM HTTP time (sum of receipt latencies) 5808 / 6337 ms, tool time 47.9 / 83.1 ms

---

## S5

**User request** — 센서 관측값 500개가 CDC 배치로 한 번에 들어온 상황을 시뮬레이션해서 반영하고, 반영 후 사양을 초과한 부품이 전체 몇 개인지 알려줘.

**Relevant real data (generator)**

768 observations; anomalies before the batch (truth): 29

**LLM turns (raw from MARA receipts, RDF run)**

| Turn | finish_reason | Tool call(s) | Prompt / completion (reasoning) tokens | Latency ms |
|---|---|---|---|---:|
| 1 | tool_calls | `batch_stream_update({"count": 500})` | 602 / 210 (193) | 942.5 |
| 2 | tool_calls | `count_sensor_anomalies({})` | 651 / 157 (138) | 903.7 |
| 3 | stop | — (final answer) | 693 / 28 (0) | 408.1 |

**Tool `batch_stream_update`** args (RDF run) `{"count": 500}` · args (LPG run) `{"count": 500}`

<table><tr><th>SPARQL template filled (PyOxigraph)</th><th>Cypher template (Neo4j) + parameters</th></tr>
<tr><td><pre>DELETE { GRAPH ex:ABox_Graph { ?o sosa:hasSimpleResult ?old } }
INSERT { GRAPH ex:ABox_Graph { ?o sosa:hasSimpleResult ?new } }
WHERE { VALUES (?o ?new) { (ex:Obs_<part> "<new value>"^^xsd:double) … × 500 }
  OPTIONAL { GRAPH ex:ABox_Graph { ?o sosa:hasSimpleResult ?old } } }</pre></td><td><pre>UNWIND $rows AS r MATCH (o:Resource {id: r.o}) SET o.hasSimpleResult = r.v</pre><pre>{"rows": "[{o: 'Obs_<part>', v: <new value>}, …] × 500"}</pre></td></tr></table>

Result RDF: `{"updated_observations": 500, "transactions": 1}`

Result LPG: `{"updated_observations": 500, "transactions": 1}` — results identical

**Tool `count_sensor_anomalies`** args (RDF run) `{}` · args (LPG run) `{}`

<table><tr><th>SPARQL template filled (PyOxigraph)</th><th>Cypher template (Neo4j) + parameters</th></tr>
<tr><td><pre>SELECT (COUNT(DISTINCT ?part) AS ?n) FROM ex:TBox_Graph FROM ex:ABox_Graph WHERE {
  ?obs sosa:hasFeatureOfInterest ?part ; sosa:hasSimpleResult ?v .
  ?part a ?cls . ?cls rdfs:subClassOf* ?c . ?c rdfs:subClassOf ?r .
  ?r owl:onProperty ssn:hasProperty ; owl:hasValue ?spec . ?spec ex:maxAllowedDrift ?max .
  FILTER(?v > ?max) }</pre></td><td><pre>MATCH (part)<-[:hasFeatureOfInterest]-(obs)
MATCH (part)-[:type]->()-[:subClassOf*0..]->()-[:subClassOf]->(r)-[:onProperty]->(:Resource {id: 'hasProperty'})
MATCH (r)-[:hasValue]->(spec) WHERE obs.hasSimpleResult > spec.maxAllowedDrift
RETURN count(DISTINCT part) AS n</pre><pre>{}</pre></td></tr></table>

Result RDF: `{"anomalous_parts": 33}`

Result LPG: `{"anomalous_parts": 33}` — results identical

**Final answers (generation)**

- RDF: 시뮬레이션이 적용된 후 사양을 초과한 부품은 **33개**입니다.
- LPG: 시뮬레이션이 적용된 후 사양을 초과한 부품은 **33개**입니다.

**Grade** — RDF {'tools_ok': True, 'precision': 1.0, 'recall': 1.0, 'answered': True} · LPG {'tools_ok': True, 'precision': 1.0, 'recall': 1.0, 'answered': True} · LLM HTTP time (sum of receipt latencies) 2254 / 2677 ms, tool time 58.3 / 82.2 ms

---
