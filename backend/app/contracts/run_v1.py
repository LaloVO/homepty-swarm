"""Generated Brain JSON Schema + explicit semantic checks absent from JSON Schema."""
import hashlib
import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker
from app.security.workload_auth import canonical_workload_body


class ContractError(ValueError):
    pass


def canonical_protocol_body(value):
    """Brain V1 JSON.stringify(sorted objects), NOT the workload's RFC8785.

    ECMAScript enumerates array-index object keys numerically before other keys.
    Preserve that existing public protocol instead of silently changing checksums.
    """
    canonical_workload_body(value)  # bounded interoperable JSON domain

    def encode(item):
        if isinstance(item, dict):
            indexes = sorted((k for k in item if len(k) <= 10 and k.isascii() and k.isdecimal()
                              and str(int(k)) == k and int(k) < 4294967295), key=int)
            others = sorted(set(item)-set(indexes), key=lambda k: k.encode("utf-16be"))
            return b"{" + b",".join(canonical_workload_body(k)+b":"+encode(item[k])
                                      for k in indexes+others) + b"}"
        if isinstance(item, list):
            return b"[" + b",".join(encode(v) for v in item) + b"]"
        return canonical_workload_body(item)
    return encode(value)


def checksum(value):
    return hashlib.sha256(canonical_protocol_body(value)).hexdigest()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@lru_cache
def validator(name):
    path = Path(__file__).resolve().parents[3] / "contracts" / "run-v1" / f"{name}.v1.schema.json"
    return Draft202012Validator(json.loads(path.read_text()), format_checker=FormatChecker())


def validate(name, value):
    if not validator(name).is_valid(value):
        raise ContractError("CONTRACT_INVALID")
    canonical_workload_body(value)
    return value


def validate_job(job, settings):
    validate("job", job)
    snapshot, plan, budget = job["snapshot"], job["plan"], job["budget"]
    if snapshot["tenantScope"] != job["tenantScope"]:
        raise ContractError("UNAUTHORIZED_TENANT")
    if snapshot["state"] == "blocked" or plan["blockers"] or snapshot["projectionPolicyVersion"] not in settings.allowed_policies:
        raise ContractError("NO_GOVERNED_SNAPSHOT")
    if checksum({"plan": plan, "snapshot": snapshot}) != job["payloadChecksum"]:
        raise ContractError("EVENT_CORRUPTION")
    if datetime.fromisoformat(job["expiresAt"].replace("Z", "+00:00")) <= datetime.now(timezone.utc):
        raise ContractError("JOB_EXPIRED")
    if len(snapshot["nodes"]) > snapshot["budget"]["maxNodes"] or len(snapshot["hyperedges"]) > snapshot["budget"]["maxHyperedges"]:
        raise ContractError("BUDGET_EXCEEDED")
    node_ids = {n["nodeId"] for n in snapshot["nodes"]}
    if len(node_ids) != len(snapshot["nodes"]) or any(b["nodeId"] not in node_ids for e in snapshot["hyperedges"] for b in e["roleBindings"]):
        raise ContractError("PLAN_INVALID")
    steps = {s["stepId"]: s for s in plan["steps"]}
    order = plan["topologicalOrder"]
    if not steps or len(steps) > 1000 or len(steps) != len(plan["steps"]) or len(order) != len(steps) or set(order) != set(steps) or "_swarm" in steps:
        raise ContractError("PLAN_INVALID")
    seen = set()
    for step_id in order:
        step = steps[step_id]
        if not set(step["dependsOn"]).issubset(seen):
            raise ContractError("PLAN_INVALID")
        seen.add(step_id)
        if step["stepType"] in ("simulate_variant", "load_deterministic_baseline"):
            inputs = step.get("executionInputs")
            expected = "s1_variant" if step["stepType"] == "simulate_variant" else "s1_baseline"
            if not inputs or inputs.get("kind") != expected:
                raise ContractError("STEP_INPUTS_UNAVAILABLE")
        inputs = step.get("executionInputs")
        if inputs and inputs["kind"].startswith("s1_"):
            bundle = inputs["bundle"]
            expected = {"s1_variant": "simulate_variant", "s1_baseline": "load_deterministic_baseline", "s1_value": "evaluate_value"}[inputs["kind"]]
            if step["stepType"] != expected or checksum(bundle["subjectRef"]) not in {checksum(n["entityRef"]) for n in snapshot["nodes"]}:
                raise ContractError("STEP_INPUTS_INVALID")
            if (bundle["subjectRef"] != {"type": "property", "id": bundle["context"]["propertyId"]}
                    or datetime.fromisoformat(bundle["sourceAsOf"].replace("Z", "+00:00")) > min(datetime.fromisoformat(snapshot["sourceAsOf"].replace("Z", "+00:00")), datetime.fromisoformat(snapshot["validAt"].replace("Z", "+00:00")))):
                raise ContractError("STEP_INPUTS_INVALID")
            variants = [bundle["baseline"], *bundle["interventions"]]
            if len({v["variantId"] for v in variants}) != len(variants):
                raise ContractError("STEP_INPUTS_INVALID")
            if inputs["kind"] == "s1_variant" and inputs["variantId"] not in {v["variantId"] for v in bundle["interventions"]}:
                raise ContractError("STEP_INPUTS_INVALID")
            if bundle["syntheticFixture"] and settings.environment != "local":
                raise ContractError("SYNTHETIC_INPUTS_FORBIDDEN")
            if bundle["params"]["provenance"] == "synthetic_ground_truth" and not bundle["syntheticFixture"]:
                raise ContractError("STEP_INPUTS_INVALID")
        if step["stepType"] != "project_context":
            ref = step["capabilityRef"] if step["stepType"] == "query_capability" else "step:"+step["stepType"]
            provider = settings.providers.get(ref)
            if provider is None or not set(step["produces"]).issubset(provider["produces"]):
                raise ContractError("CAPABILITY_NOT_REGISTERED")
        if step["stepType"] != "query_capability" and step["capabilityRef"] is not None:
            raise ContractError("PLAN_INVALID")
        produced = {p for dep in step["dependsOn"] for p in steps[dep]["produces"]}
        if not set(step["consumes"]).issubset(produced):
            raise ContractError("PLAN_INVALID")
    for maximum, metric in (("maxTokens", "tokens"), ("maxCostUsd", "costUsd")):
        if sum(s["worstCase"][metric] for s in steps.values()) > budget[maximum] or plan["reservedBudget"][maximum] > budget[maximum]:
            raise ContractError("BUDGET_EXCEEDED")
    # Parallel fan-out wall time is not the sum of all step latencies.
    if plan["reservedBudget"]["maxWallTimeMs"] > budget["maxWallTimeMs"]:
        raise ContractError("BUDGET_EXCEEDED")
    if plan["reservedBudget"]["maxConcurrency"] > budget["maxConcurrency"]:
        raise ContractError("BUDGET_EXCEEDED")
    return job


def make_event(job, step_id, sequence, event_type, payload):
    digest = checksum(payload)
    event = {"eventId": checksum([job["runId"], job["attempt"], step_id, sequence, event_type, digest]),
             "runId": job["runId"], "attempt": job["attempt"], "stepId": step_id,
             "stepSequence": sequence, "eventType": event_type, "payloadChecksum": digest,
             "payload": payload, "emittedAt": now_iso()}
    return validate("event", event)
