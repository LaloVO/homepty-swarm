"""Execute only registered, versioned step providers over authorized job projections.

Provider processes never get inbox credentials or artifact-store credentials. Canonical
graph/report algorithms remain in Brain; their private providers return proposals here.
"""
import hashlib
import hmac
import json
import os
import resource
import time
import httpx
from app.contracts.run_v1 import ContractError, checksum, validate
from app.security.workload_auth import canonical_workload_body


class ProviderError(ValueError):
    pass


def confirm_provider_cancel(job, step, provider):
    """An expired lease or a disconnected HTTP child is not termination evidence."""
    if not provider or not provider.get("cancelUrl"):
        return False
    request_id = checksum([job["tenantScope"], job["runId"], job["attempt"], step["stepKey"]])
    body = {"contractVersion": "homepty-step-cancel-request.v1", "tenantScope": job["tenantScope"],
            "runId": job["runId"], "attempt": job["attempt"], "stepKey": step["stepKey"]}
    raw = canonical_workload_body(body)
    signature = hmac.new(provider["secret"].encode(), request_id.encode()+b"\n"+raw, hashlib.sha256).hexdigest()
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=3) as client:
            with client.stream("POST", provider["cancelUrl"], content=raw, headers={"Content-Type": "application/json",
                    "Idempotency-Key": request_id, "X-Workload-Signature": signature, "Accept-Encoding": "identity"}) as response:
                if response.status_code != 200 or response.headers.get("Content-Encoding", "identity") != "identity":
                    return False
                payload = bytearray()
                for chunk in response.iter_raw():
                    payload.extend(chunk)
                    if len(payload) > 4096:
                        return False
                expected = hmac.new(provider["secret"].encode(), request_id.encode()+b"\n"+payload, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(expected, response.headers.get("X-Workload-Signature", "")):
                    return False
                result = json.loads(payload)
                return (canonical_workload_body(result) == payload and set(result) == {"contractVersion", "requestId", "acknowledged", "proof"}
                        and result["contractVersion"] == "homepty-step-cancel.v1" and result["requestId"] == request_id
                        and result["acknowledged"] is True and result["proof"] in ("released", "never_started"))
    except (httpx.HTTPError, ValueError, TypeError):
        return False


def validate_result(result, job, step, provider):
    if not isinstance(result, dict) or set(result) != {"contractVersion", "producerVersion", "outputs", "kind", "content", "findings", "usage"}:
        raise ProviderError("PROVIDER_CONTRACT_INVALID")
    if (result["contractVersion"] != "homepty-step-result.v1" or result["producerVersion"] != provider["version"]
            or set(result["outputs"]) != set(step["produces"]) or result["kind"] not in ("run_graph", "structured_report", "findings", "metrics")
            or not isinstance(result["findings"], list) or len(result["findings"]) > 1000):
        raise ProviderError("PROVIDER_CONTRACT_INVALID")
    usage = result["usage"]
    if (not isinstance(usage, dict) or set(usage) != {"tokens", "costUsd"}
            or type(usage["tokens"]) is not int or usage["tokens"] < 0
            or type(usage["costUsd"]) not in (int, float) or usage["costUsd"] < 0
            or usage["tokens"] > step["worstCase"]["tokens"] or usage["costUsd"] > step["worstCase"]["costUsd"]):
        raise ProviderError("BUDGET_EXCEEDED")
    entities = {checksum(n["entityRef"]) for n in job["snapshot"]["nodes"]}
    evidence = {checksum(e) for edge in job["snapshot"]["hyperedges"] for e in edge["evidenceRefs"]}
    for finding in result["findings"]:
        validate("finding", finding)
        if checksum(finding["subjectRef"]) not in entities:
            raise ProviderError("ENTITY_OUTSIDE_SNAPSHOT")
        if any(checksum(e) not in evidence for e in finding["evidenceRefs"]):
            raise ProviderError("EVIDENCE_OUTSIDE_SNAPSHOT")
        if finding["population"] in ("observed", "derived") and not finding["evidenceRefs"]:
            raise ProviderError("EVIDENCE_REQUIRED")
        uncertainty = finding["uncertainty"]
        if uncertainty and uncertainty["lower"] > uncertainty["upper"]:
            raise ProviderError("PROVIDER_CONTRACT_INVALID")
    canonical_workload_body(result)
    return result


def execute_provider(job, step, dependencies, provider, max_bytes, timeout):
    body = {"contractVersion": "homepty-step-request.v1", "runId": job["runId"], "attempt": job["attempt"],
            "tenantScope": job["tenantScope"], "snapshot": job["snapshot"], "step": step,
            "dependencies": dependencies, "expiresAt": job["expiresAt"]}
    raw = canonical_workload_body(body)
    if len(raw) > max_bytes:
        raise ProviderError("PROVIDER_INPUT_TOO_LARGE")
    # Scoped idempotency also covers a lost response after the provider incurred cost.
    request_id = checksum([job["tenantScope"], job["runId"], job["attempt"], step["stepKey"]])
    signature = hmac.new(provider["secret"].encode(), request_id.encode()+b"\n"+raw, hashlib.sha256).hexdigest()
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=min(timeout, 180)) as client:
            with client.stream("POST", provider["url"], content=raw, headers={
                "Content-Type": "application/json", "Idempotency-Key": request_id,
                "X-Workload-Signature": signature, "Accept-Encoding": "identity",
            }) as response:
                if response.status_code != 200:
                    raise ProviderError("PROVIDER_UNAVAILABLE" if response.status_code in (429, 502, 503, 504) else "PROVIDER_REJECTED")
                if response.headers.get("Content-Encoding", "identity") != "identity":
                    raise ProviderError("PROVIDER_CONTRACT_INVALID")
                raw_result = bytearray()
                for chunk in response.iter_raw():
                    raw_result.extend(chunk)
                    if len(raw_result) > max_bytes:
                        raise ProviderError("ARTIFACT_TOO_LARGE")
                expected = hmac.new(provider["secret"].encode(), request_id.encode()+b"\n"+raw_result, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(expected, response.headers.get("X-Workload-Signature", "")):
                    raise ProviderError("PROVIDER_SIGNATURE_INVALID")
                result = json.loads(raw_result)
                if canonical_workload_body(result) != raw_result:
                    raise ProviderError("PROVIDER_CONTRACT_INVALID")
                return validate_result(result, job, step, provider)
    except httpx.HTTPError:
        raise ProviderError("PROVIDER_UNAVAILABLE") from None


def child_execute(connection, job, step, dependencies, provider, max_bytes, timeout):
    """Spawned isolated child. Parent kills the entire process group on lost lease/cancel."""
    try:
        os.setsid()
        os.environ.clear()
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (max(1, int(timeout)+1), max(2, int(timeout)+2)))
        if hasattr(resource, "RLIMIT_AS") and os.uname().sysname == "Linux":
            resource.setrlimit(resource.RLIMIT_AS, (512*1024*1024, 512*1024*1024))
        result = execute_provider(job, step, dependencies, provider, max_bytes, timeout)
        connection.send_bytes(canonical_workload_body({"result": result}))
    except (ProviderError, ContractError) as error:
        connection.send_bytes(canonical_workload_body({"error": str(error)}))
    except Exception:
        connection.send_bytes(b'{"error":"PROVIDER_EXECUTION_FAILED"}')
    finally:
        connection.close()
