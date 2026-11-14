# How the ontologies were combined, and where the competency questions came from

## 1. Sources and how faithfully each is used

| Source | What it is | How it is used here | Fidelity |
|---|---|---|---|
| **MASON** (Lemaignan et al., 2006, *MASON: A Proposal for an Ontology of Manufacturing Domain*) | OWL manufacturing upper ontology whose three top concepts are **Entity** (products, materials), **Operation** (processes) and **Resource** (machines, tools, people) | The three top concepts are reused **by name** as local `ex:` classes, and the domain classes are placed under them | **Name and role only.** The MASON OWL file is not imported or aligned with `owl:equivalentClass` |
| **SOSA / SSN** (W3C Recommendation 2017) | Sensor/observation vocabulary: `sosa:Observation`, `sosa:FeatureOfInterest`, `sosa:observedProperty`, `sosa:hasSimpleResult`, `ssn:Property`, `ssn:hasProperty` | **Real W3C IRIs** are used directly | **Standard terms.** The ontology files themselves are not imported (no reasoner is used, so their axioms would not be applied) |
| **FMEA** | An engineering *method* (IEC 60812, AIAG-VDA): a worksheet with failure mode, effect, cause, detection and action columns. There is **no single standard FMEA ontology** | The worksheet columns become local classes `FailureMode`, `Symptom`, `Cause`, `Action` and properties `causedBy`, `attributedTo`, `mitigatedBy` | **Local design** |

## 2. Each bridging decision and its rationale

| Axiom (in `data/mfg_knowledge.trig`) | Rationale | Weak spot / alternative |
|---|---|---|
| `Equipment ⊑ Resource`, `ProductionLine ⊑ Resource` | In MASON, machines and the production system are *Resources* | — |
| `Lot ⊑ Entity` | A lot is the material or product flowing through operations, which is MASON's *Entity* | — |
| `Action ⊑ Operation` | A corrective or preventive action is something performed, i.e. an operation | It is a maintenance operation rather than a manufacturing one; a dedicated `MaintenanceOperation` would be more precise |
| `Cause ⊑ Entity`, `FailureMode ⊑ Entity` | Needed a home in the upper level | **Weak.** MASON's Entity means product or material; FMEA concepts would fit better in a separate "quality knowledge" branch |
| `Symptom ⊑ FailureMode` | The observed abnormality is treated as how the failure shows itself | **Questionable.** In FMEA terms a symptom is an **effect** of a failure mode, not a failure mode. `Symptom ⊑ FailureEffect` would be more faithful. It was kept to stay close to the brief; it does not affect any query, because none use this subclass edge |
| `Part ⊑ sosa:FeatureOfInterest` | Parts are what the observations are about (`sosa:hasFeatureOfInterest`) | Standard SOSA usage |
| `DriftSpecification ⊑ ssn:Property` and `MFC ⊑ [owl:Restriction; onProperty ssn:hasProperty; hasValue ex:FlowDriftSpec]` | In SSN, features *have* properties (`ssn:hasProperty`). A class-level restriction says "every MFC has the flow-drift property", and the threshold sits on that property individual. This replaces v1's non-standard `maxAllowedDrift` on classes | **Partly a stretch.** A *specification* (an allowed limit) is not quite the observable property itself. SSN's System Capabilities module (`ssn-system:SystemCapability`, `ssn-system:Condition`) or a separate `Spec` linked to the property would be cleaner. The observation also points to the spec through `sosa:observedProperty` |
| `partOf`, `feedsInto` as `owl:TransitiveProperty` | Containment and process flow are transitive | Declared only; queries traverse them with `+`/`*` paths because no reasoner runs |
| Confidence as RDF 1.2 annotation `<< s causedBy o >> ex:confidence v` | The confidence belongs to the *statement*, not to either node | In the LPG it becomes a relationship property, which is the natural equivalent |

Not used even though the brief mentioned it: `sosa:Sensor` / `sosa:madeBySensor`. The observations have no sensor
instances, because no scenario needed them.

## 3. Where the competency questions came from

**They were not elicited from domain experts.** No real production competency questions were available, so they were
authored as follows:

1. **Origin.** The original project brief defined five scenarios: probabilistic RCA, transitive contamination,
   SOSA threshold classification, runtime TBox evolution, and high-frequency ABox mutation. v2 kept them and
   followed the v2 specification (connected graph, batch S5).
2. **From scenario to CQ template.** Each scenario became one parameterized question with a slot:

| CQ | Template (slot) | Answer type | Ontology elements exercised |
|---|---|---|---|
| CQ1 | "Which causes and parts explain the symptom(s) of lot **L**, with cumulative confidence ≥ 0.48?" | set of (cause, part, score) | FMEA chain, statement-level confidence |
| CQ2 | "Which lots may be contaminated by defective component **C**?" | set of lots | transitive `partOf`, class hierarchy, transitive `feedsInto`, `processedIn` |
| CQ3 | "Which parts in line **N** exceed their specification?" | set of parts | `partOf+`, SOSA observation, OWL restriction → SSN property → threshold |
| CQ4 | "After adding superclass **P** over GasSupplyModule and PlasmaModule (with a policy), which equipment falls under **P**?" | set of equipment | TBox change at runtime, `subClassOf+` vs materialization |
| CQ5 | "After a batch of **K** new readings, how many parts exceed their specification?" | count | ABox mutation and re-evaluation of CQ3 globally |

3. **Answers come from the data generator, not from hand-written values.** The generator plants the facts (for
   example 2 correct, 4 wrong and 2 decoy causes per symptom) and computes each CQ's answer in plain Python, without
   SPARQL or Cypher. Engines are graded by precision/recall against that answer.
4. **Natural-language versions.** `tool_agent.py` / `loadtest/llm_load.py` fill the slots with real IDs from the
   generated graph (e.g. *"Lot_14_7에서 이상 증상이 나왔어 …"*).

### Limitations of this approach

* The CQs test whether the **engines and the tool interface** answer correctly and how fast. They do **not** show
  that the ontology covers real engineering needs. That requires expert-elicited CQs, as in Grüninger & Fox's
  methodology or the NeOn ORSD.
* Questions, data and answers were designed together, so the data is "answer-friendly". Real data would add
  missing links, identifier noise and conflicting evidence.
* There is one wording per CQ. Robustness of the LLM to different phrasings was not measured.

### What a more rigorous process would look like

1. Collect real questions from process, equipment and quality engineers (or from FMEA worksheets and 8D reports).
2. Write them in an ORSD with priorities and answer types.
3. Model them by importing MASON / SOSA-SSN / an FMEA ontology module, with explicit alignment axioms.
4. Keep the generator but calibrate its distributions to real statistics.
5. Add paraphrase sets per CQ and measure LLM tool-selection accuracy across phrasings.
