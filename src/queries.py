"""Single source of SPARQL and Cypher templates (v2) plus response canonicalization.

Used by the embedded engines (engine_oxigraph / engine_neo4j), the tool-calling agent and the
server load test. Identifiers are validated with ``safe_id`` before substitution.

Formulation note: SPARQL S1 lists the asserted edges before the RDF 1.2 reifier patterns. Putting
the reifier patterns first is semantically identical but was ~100x slower on Oxigraph 0.5.
"""

from __future__ import annotations

from common import EX, PREFIXES, safe_id

QT_FROM = "FROM ex:TBox_Graph FROM ex:ABox_Graph"   # query-time inference dataset


def _f(x) -> str:
    return repr(float(x))


SPARQL = {
    "S1": lambda p: PREFIXES + f"""SELECT DISTINCT ?cause ?part ?score {QT_FROM} WHERE {{
  ex:{safe_id(p['lot'])} ex:hasSymptom ?sym .
  ?sym ex:causedBy ?cause . ?cause ex:attributedTo ?part .
  << ?sym ex:causedBy ?cause >> ex:confidence ?c1 .
  << ?cause ex:attributedTo ?part >> ex:confidence ?c2 .
  BIND(?c1 * ?c2 AS ?score) FILTER(?score >= {_f(p['min_conf'])}) }}""",
    "S2": lambda p: PREFIXES + f"""SELECT DISTINCT ?lot {QT_FROM} WHERE {{
  ex:{safe_id(p['component'])} ex:hasDefect ?d ; ex:partOf+ ?host .
  ?host a/rdfs:subClassOf* ex:ProcessChamber .
  ?host ex:feedsInto* ?ch . ?lot ex:processedIn ?ch . }}""",
    "S3": lambda p: PREFIXES + f"""SELECT DISTINCT ?part {QT_FROM} WHERE {{
  ?part ex:partOf+ ex:{safe_id(p['line'])} .
  ?obs sosa:hasFeatureOfInterest ?part ; sosa:hasSimpleResult ?v .
  ?part a ?cls . ?cls rdfs:subClassOf* ?c . ?c rdfs:subClassOf ?r .
  ?r owl:onProperty ssn:hasProperty ; owl:hasValue ?spec . ?spec ex:maxAllowedDrift ?max .
  FILTER(?v > ?max) }}""",
    "S3_global_count": lambda p: PREFIXES + f"""SELECT (COUNT(DISTINCT ?part) AS ?n) {QT_FROM} WHERE {{
  ?obs sosa:hasFeatureOfInterest ?part ; sosa:hasSimpleResult ?v .
  ?part a ?cls . ?cls rdfs:subClassOf* ?c . ?c rdfs:subClassOf ?r .
  ?r owl:onProperty ssn:hasProperty ; owl:hasValue ?spec . ?spec ex:maxAllowedDrift ?max .
  FILTER(?v > ?max) }}""",
    "S4_list_query_time": lambda p: PREFIXES + f"""SELECT DISTINCT ?eq ?eqType ?policy {QT_FROM} WHERE {{
  ?eq a ?eqType . ?eqType rdfs:subClassOf+ ex:{safe_id(p['parent'])} .
  OPTIONAL {{ ex:{safe_id(p['parent'])} ex:requiredPolicy ?policy }} }}""",
    "S4_list_materialized": lambda p: PREFIXES + f"""SELECT DISTINCT ?eq ?eqType ?policy WHERE {{
  GRAPH ex:Inferred_Graph {{ ?eq a ex:{safe_id(p['parent'])} }}
  GRAPH ex:ABox_Graph {{ ?eq a ?eqType }}
  GRAPH ex:TBox_Graph {{ ex:{safe_id(p['parent'])} ex:requiredPolicy ?policy }} }}""",
    "S4_count_query_time": lambda p: PREFIXES + f"""SELECT (COUNT(DISTINCT ?eq) AS ?n) {QT_FROM} WHERE {{
  ?eq a ?t . ?t rdfs:subClassOf+ ex:{safe_id(p['parent'])} }}""",
    "S4_count_materialized": lambda p: PREFIXES + f"""SELECT (COUNT(?eq) AS ?n) WHERE {{
  GRAPH ex:Inferred_Graph {{ ?eq a ex:{safe_id(p['parent'])} }} }}""",
    "S4_migrate": lambda p: PREFIXES + f"""INSERT {{ GRAPH ex:Inferred_Graph {{ ?x a ex:{safe_id(p['parent'])} }} }}
WHERE {{ GRAPH ex:ABox_Graph {{ ?x a ?c }} GRAPH ex:TBox_Graph {{ ?c rdfs:subClassOf+ ex:{safe_id(p['parent'])} }} }}""",
    "S4_revert": lambda p: PREFIXES + f"""DELETE WHERE {{ GRAPH ex:TBox_Graph {{ ?c rdfs:subClassOf ex:{safe_id(p['parent'])} }} }};
DELETE WHERE {{ GRAPH ex:TBox_Graph {{ ex:{safe_id(p['parent'])} ex:requiredPolicy ?pol }} }};
DELETE WHERE {{ GRAPH ex:Inferred_Graph {{ ?x a ex:{safe_id(p['parent'])} }} }}""",
    # One request = one transaction: delete old values and insert new ones for every row.
    "S5_batch": lambda p: PREFIXES + """DELETE { GRAPH ex:ABox_Graph { ?o sosa:hasSimpleResult ?old } }
INSERT { GRAPH ex:ABox_Graph { ?o sosa:hasSimpleResult ?new } }
WHERE { VALUES (?o ?new) { """ + " ".join(f'(ex:{safe_id(o)} "{_f(v)}"^^xsd:double)' for o, v in p["rows"]) + """ }
  OPTIONAL { GRAPH ex:ABox_Graph { ?o sosa:hasSimpleResult ?old } } }""",
    "S5_single": lambda p: PREFIXES + f"""DELETE {{ GRAPH ex:ABox_Graph {{ ex:{safe_id(p['obs'])} sosa:hasSimpleResult ?old }} }}
INSERT {{ GRAPH ex:ABox_Graph {{ ex:{safe_id(p['obs'])} sosa:hasSimpleResult "{_f(p['value'])}"^^xsd:double }} }}
WHERE {{ OPTIONAL {{ GRAPH ex:ABox_Graph {{ ex:{safe_id(p['obs'])} sosa:hasSimpleResult ?old }} }} }}""",
    "S5_read": lambda p: PREFIXES + "SELECT ?o ?v WHERE { VALUES ?o { " + " ".join(f"ex:{safe_id(o)}" for o in p["obs"])
                                    + " } GRAPH ex:ABox_Graph { ?o sosa:hasSimpleResult ?v } }",
    "count_by_class_query_time": lambda p: PREFIXES + f"""SELECT (COUNT(DISTINCT ?x) AS ?n) {QT_FROM} WHERE {{
  ?x a/rdfs:subClassOf* ex:{safe_id(p['cls'])} }}""",
    "count_by_class_materialized": lambda p: PREFIXES + f"""SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE {{
  {{ GRAPH ex:ABox_Graph {{ ?x a ex:{safe_id(p['cls'])} }} }} UNION {{ GRAPH ex:Inferred_Graph {{ ?x a ex:{safe_id(p['cls'])} }} }} }}""",
    "class_exists": lambda p: PREFIXES + f"""ASK {{ GRAPH ex:TBox_Graph {{
  {{ ex:{safe_id(p['cls'])} ?p ?o }} UNION {{ ?s ?p2 ex:{safe_id(p['cls'])} }} }} }}""",
    "list_by_class": lambda p: PREFIXES + f"""SELECT DISTINCT ?eq ?eqType ?policy {QT_FROM} WHERE {{
  ?eq a ?eqType . ?eqType rdfs:subClassOf* ex:{safe_id(p['cls'])} .
  OPTIONAL {{ ex:{safe_id(p['cls'])} ex:requiredPolicy ?policy }} }} ORDER BY ?eq""",
    "obs_of": lambda p: PREFIXES + f"""SELECT ?o ?v ?max WHERE {{
  GRAPH ex:ABox_Graph {{ ?o sosa:hasFeatureOfInterest ex:{safe_id(p['entity'])} ; sosa:hasSimpleResult ?v .
                         ex:{safe_id(p['entity'])} a ?cls }}
  GRAPH ex:TBox_Graph {{ ?cls rdfs:subClassOf* ?c . ?c rdfs:subClassOf ?r .
    ?r owl:onProperty ssn:hasProperty ; owl:hasValue ?spec . ?spec ex:maxAllowedDrift ?max }} }}""",
}

CYPHER = {
    "S1": """MATCH (:Resource {id: $lot})-[:hasSymptom]->(sym)-[r1:causedBy]->(cause)-[r2:attributedTo]->(part)
WITH cause, part, r1.confidence * r2.confidence AS score WHERE score >= $min_conf
RETURN DISTINCT cause.id AS cause, part.id AS part, score""",
    "S2": """MATCH (c:Resource {id: $component})-[:hasDefect]->()
MATCH (c)-[:partOf*1..]->(host)-[:type]->()-[:subClassOf*0..]->(:Resource {id: 'ProcessChamber'})
MATCH (host)-[:feedsInto*0..]->(ch)<-[:processedIn]-(lot)
RETURN DISTINCT lot.id AS lot""",
    "S3": """MATCH (:Resource {id: $line})<-[:partOf*1..]-(part)<-[:hasFeatureOfInterest]-(obs)
MATCH (part)-[:type]->()-[:subClassOf*0..]->()-[:subClassOf]->(r)-[:onProperty]->(:Resource {id: 'hasProperty'})
MATCH (r)-[:hasValue]->(spec) WHERE obs.hasSimpleResult > spec.maxAllowedDrift
RETURN DISTINCT part.id AS part""",
    "S3_global_count": """MATCH (part)<-[:hasFeatureOfInterest]-(obs)
MATCH (part)-[:type]->()-[:subClassOf*0..]->()-[:subClassOf]->(r)-[:onProperty]->(:Resource {id: 'hasProperty'})
MATCH (r)-[:hasValue]->(spec) WHERE obs.hasSimpleResult > spec.maxAllowedDrift
RETURN count(DISTINCT part) AS n""",
    "S4_tbox_write": """MERGE (p:Resource {id: $parent}) SET p.requiredPolicy = $policy
WITH p MATCH (sub:Resource) WHERE sub.id IN $subs MERGE (sub)-[:subClassOf]->(p)""",
    "S4_list_query_time": """MATCH (p:Resource {id: $parent})
MATCH (eq)-[:type]->(eqType)-[:subClassOf*1..]->(p)
RETURN DISTINCT eq.id AS eq, eqType.id AS eqType, p.requiredPolicy AS policy""",
    "S4_count_query_time": """MATCH (p:Resource {id: $parent})
MATCH (eq)-[:type]->()-[:subClassOf*1..]->(p) RETURN count(DISTINCT eq) AS n""",
    # Label-based variants embed the (validated) label in the query text.
    "S4_migrate": "MATCH (eq:Resource)-[:type]->(:Resource)-[:subClassOf*1..]->(:Resource {{id: $parent}}) SET eq:`{label}`",
    "S4_list_materialized": """MATCH (p:Resource {{id: $parent}})
MATCH (eq:`{label}`)-[:type]->(eqType)
RETURN eq.id AS eq, eqType.id AS eqType, p.requiredPolicy AS policy""",
    "S4_count_materialized": "MATCH (eq:`{label}`) RETURN count(eq) AS n",
    "S4_revert_labels": "MATCH (n:`{label}`) CALL (n) {{ REMOVE n:`{label}` }} IN TRANSACTIONS OF 50000 ROWS",
    "S4_revert_node": "MATCH (p:Resource {id: $parent}) DETACH DELETE p",
    "S5_batch": "UNWIND $rows AS r MATCH (o:Resource {id: r.o}) SET o.hasSimpleResult = r.v",
    "S5_single": "MATCH (o:Resource {id: $obs}) SET o.hasSimpleResult = $value",
    "S5_read": "UNWIND $obs AS id MATCH (o:Resource {id: id}) RETURN o.id AS o, o.hasSimpleResult AS v",
    "count_by_class_query_time": """MATCH (:Resource {id: $cls})<-[:subClassOf*0..]-()<-[:type]-(x)
RETURN count(DISTINCT x) AS n""",
    "count_by_class_materialized": "MATCH (x:`{label}`) RETURN count(x) AS n",
    "class_exists": "MATCH (c:Resource {id: $cls}) RETURN count(c) > 0 AS n",
    "list_by_class": """MATCH (c:Resource {id: $cls})
MATCH (eq)-[:type]->(eqType)-[:subClassOf*0..]->(c)
RETURN DISTINCT eq.id AS eq, eqType.id AS eqType, c.requiredPolicy AS policy ORDER BY eq""",
    "obs_of": """MATCH (o)-[:hasFeatureOfInterest]->(e:Resource {id: $entity})-[:type]->()-[:subClassOf*0..]->()
      -[:subClassOf]->(r)-[:onProperty]->(:Resource {id: 'hasProperty'})
MATCH (r)-[:hasValue]->(spec)
RETURN o.id AS o, o.hasSimpleResult AS v, spec.maxAllowedDrift AS max""",
}


def cypher(name: str, label: str | None = None) -> str:
    q = CYPHER[name]
    return q.format(label=safe_id(label)) if label else q


def _loc(term) -> str:
    v = term if isinstance(term, str) else term.value if hasattr(term, "value") else term["value"]
    return v.rsplit("#", 1)[-1]


def canon(op: str, rows) -> object:
    """Canonical answer from engine rows. Rows are dict-like (name -> term/value)."""
    def val(r, k):
        x = r[k]
        if isinstance(x, (int, float, str)) or x is None:
            return x
        return x.value if hasattr(x, "value") else x["value"]

    if op == "S1":
        return sorted({(_loc(val(r, "cause")), _loc(val(r, "part")), round(float(val(r, "score")), 4)) for r in rows})
    if op == "S2":
        return sorted(_loc(val(r, "lot")) for r in rows)
    if op == "S3":
        return sorted(_loc(val(r, "part")) for r in rows)
    if op in ("S3_global_count", "S4_count_query_time", "S4_count_materialized",
              "count_by_class_query_time", "count_by_class_materialized"):
        return int(float(val(rows[0], "n")))
    if op.startswith("S4_list") or op == "list_by_class":
        return sorted(_loc(val(r, "eq")) for r in rows)
    if op == "S5_read":
        return sorted((_loc(val(r, "o")), float(val(r, "v"))) for r in rows)
    raise KeyError(op)


__all__ = ["SPARQL", "CYPHER", "cypher", "canon", "EX"]
