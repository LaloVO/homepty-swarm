import copy
import json
from datetime import datetime
from pathlib import Path

import pytest

from app.security.workload_auth import (
    WorkloadAuthError, canonical_workload_body, verify_workload_envelope,
)

VECTORS = json.loads((Path(__file__).resolve().parents[3] / "contracts/run-v1/workload-vectors.json").read_text())


def verify(vector, **overrides):
    return verify_workload_envelope(**{
        "raw": json.dumps(vector["envelope"], ensure_ascii=True).encode(),
        "method": vector["method"], "target": vector["target"],
        "keys": {"fixture-current": bytes.fromhex(VECTORS["valid"][0]["secretHex"]),
                 "fixture-previous": bytes.fromhex(VECTORS["valid"][3]["secretHex"])},
        "now": datetime.fromisoformat(vector["now"].replace("Z", "+00:00")),
        **overrides,
    })


@pytest.mark.parametrize("vector", VECTORS["valid"], ids=lambda v: v["name"])
def test_accepts_brain_golden_signature_and_canonical_bytes(vector):
    assert canonical_workload_body(vector["body"]).decode() == vector["canonicalBody"]
    assert verify(vector).body == vector["envelope"]["body"]


@pytest.mark.parametrize("vector", VECTORS["invalid"], ids=lambda v: v["name"])
def test_rejects_tampering_and_invalid_windows(vector):
    with pytest.raises(WorkloadAuthError):
        verify(vector)


def test_revoked_rotation_key_and_weak_key_are_rejected():
    with pytest.raises(WorkloadAuthError, match="WORKLOAD_KEY_INVALID"):
        verify(VECTORS["valid"][3], keys={})
    with pytest.raises(WorkloadAuthError, match="WORKLOAD_KEY_INVALID"):
        verify(VECTORS["valid"][0], keys={"fixture-current": b"short"})


def test_key_id_substitution_cannot_select_a_different_rotation_key():
    vector = copy.deepcopy(VECTORS["valid"][0])
    vector["envelope"]["keyId"] = "fixture-previous"
    with pytest.raises(WorkloadAuthError, match="WORKLOAD_SIGNATURE_INVALID"):
        verify(vector)


def test_duplicate_json_keys_rejected_before_authentication():
    vector = VECTORS["valid"][0]
    raw = json.dumps(vector["envelope"]).replace('"attempt": 1', '"attempt": 2, "attempt": 1').encode()
    with pytest.raises(WorkloadAuthError, match="WORKLOAD_ENVELOPE_INVALID"):
        verify(vector, raw=raw)


def test_signatures_bind_the_exact_query_order():
    with pytest.raises(WorkloadAuthError, match="WORKLOAD_SIGNATURE_INVALID"):
        verify(VECTORS["valid"][2], target=VECTORS["valid"][2]["target"] + "&extra=1")


@pytest.mark.parametrize("body", [float("nan"), float("inf"), 9007199254740992, "\ud800", {1: "key"}])
def test_no_lossy_json_values(body):
    with pytest.raises(WorkloadAuthError, match="WORKLOAD_BODY_INVALID"):
        canonical_workload_body(body)


def test_oversize_and_deep_bodies_fail_closed():
    with pytest.raises(WorkloadAuthError, match="WORKLOAD_ENVELOPE_INVALID"):
        verify(VECTORS["valid"][0], raw=b" " * 1_048_577)
    body = None
    for _ in range(66):
        body = [body]
    with pytest.raises(WorkloadAuthError, match="WORKLOAD_BODY_INVALID"):
        canonical_workload_body(body)


def test_error_contains_no_body_or_secret():
    vector = copy.deepcopy(VECTORS["valid"][0])
    vector["envelope"]["body"] = {"private": "do-not-log-fixture"}
    with pytest.raises(WorkloadAuthError) as error:
        verify(vector)
    assert str(error.value) == "WORKLOAD_CHECKSUM_MISMATCH"
