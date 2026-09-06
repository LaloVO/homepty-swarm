"""HPY-143 cryptographic boundary. Durable nonce consumption belongs to the inbox.

Passing this verifier alone MUST NOT accept/execute a job: validate its domain schema
and consume (keyId, runId, attempt, nonce) atomically in the durable store first.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import unquote

import rfc8785
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class WorkloadAuthError(ValueError):
    """Safe error code only; never includes body, nonce, key or signature."""


class WorkloadEnvelopeV1(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    contractVersion: str
    keyId: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    algorithm: str
    issuedAt: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
    expiresAt: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
    nonce: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    bodySha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    signature: str = Field(pattern=r"^[a-f0-9]{64}$")
    body: Any


def _json_domain(value: Any, depth: int = 0) -> None:
    if depth > 64:
        raise WorkloadAuthError("WORKLOAD_BODY_INVALID")
    if value is None or type(value) is bool:
        return
    if type(value) is str:
        value.encode("utf-8", errors="strict")
    elif type(value) in (int, float):
        if not math.isfinite(value) or abs(value) > 9007199254740991:
            raise WorkloadAuthError("WORKLOAD_BODY_INVALID")
    elif type(value) is list:
        for item in value:
            _json_domain(item, depth + 1)
    elif type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise WorkloadAuthError("WORKLOAD_BODY_INVALID")
            _json_domain(key, depth + 1)
            _json_domain(item, depth + 1)
    else:
        raise WorkloadAuthError("WORKLOAD_BODY_INVALID")


def canonical_workload_body(value: Any) -> bytes:
    try:
        _json_domain(value)
        return rfc8785.dumps(value)
    except (ValueError, OverflowError, RecursionError):
        raise WorkloadAuthError("WORKLOAD_BODY_INVALID") from None


def validate_target(method: str, target: str) -> None:
    if (method not in ("GET", "POST") or len(target) > 2048 or
            not target.startswith("/v1/") or not re.fullmatch(r"[\x21-\x7e]+", target) or
            re.search(r"[#\\]", target) or re.search(r"%(?![a-fA-F0-9]{2})", target) or
            re.search(r"%(2f|5c|2e|25)", target, re.IGNORECASE)):
        raise WorkloadAuthError("WORKLOAD_TARGET_INVALID")
    path = target.split("?", 1)[0]
    if "//" in path or any(part in (".", "..") for part in path.split("/")):
        raise WorkloadAuthError("WORKLOAD_TARGET_INVALID")
    try:
        unquote(target, encoding="utf-8", errors="strict")
    except UnicodeError:
        raise WorkloadAuthError("WORKLOAD_TARGET_INVALID") from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WorkloadAuthError("WORKLOAD_ENVELOPE_INVALID")
        result[key] = value
    return result


def verify_workload_envelope(*, raw: bytes, method: str, target: str,
                             keys: Mapping[str, bytes], now: datetime) -> WorkloadEnvelopeV1:
    """Authenticate bytes; callers MUST perform durable replay/domain checks next."""
    validate_target(method, target)
    if len(raw) > 1_048_576:
        raise WorkloadAuthError("WORKLOAD_ENVELOPE_INVALID")
    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        envelope = WorkloadEnvelopeV1.model_validate(parsed)
    except (ValueError, ValidationError, RecursionError):
        raise WorkloadAuthError("WORKLOAD_ENVELOPE_INVALID") from None
    if envelope.contractVersion != "homepty-workload.v1" or envelope.algorithm != "HMAC-SHA256":
        raise WorkloadAuthError("WORKLOAD_ENVELOPE_INVALID")
    try:
        issued = datetime.fromisoformat(envelope.issuedAt.replace("Z", "+00:00"))
        expires = datetime.fromisoformat(envelope.expiresAt.replace("Z", "+00:00"))
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError()
        ttl = (expires - issued).total_seconds()
        if not 0 < ttl <= 300 or (issued - now).total_seconds() > 30 or expires <= now:
            raise ValueError()
    except ValueError:
        raise WorkloadAuthError("WORKLOAD_WINDOW_INVALID") from None
    secret = keys.get(envelope.keyId)
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise WorkloadAuthError("WORKLOAD_KEY_INVALID")
    checksum = hashlib.sha256(canonical_workload_body(envelope.body)).hexdigest()
    if not hmac.compare_digest(checksum, envelope.bodySha256):
        raise WorkloadAuthError("WORKLOAD_CHECKSUM_MISMATCH")
    transcript = "\n".join((method, target, envelope.issuedAt, envelope.expiresAt,
                            envelope.nonce, envelope.bodySha256)).encode("utf-8")
    signature = hmac.new(secret, transcript, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, envelope.signature):
        raise WorkloadAuthError("WORKLOAD_SIGNATURE_INVALID")
    return envelope
