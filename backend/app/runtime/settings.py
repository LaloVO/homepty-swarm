from __future__ import annotations
import base64
import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    environment: str
    inbox_url: str
    keys: dict[str, bytes] = field(repr=False)
    artifact_endpoint: str
    artifact_bucket: str
    artifact_access_key: str = field(repr=False)
    artifact_secret_key: str = field(repr=False)
    source_commit: str = ""
    image_digest: str = ""
    allowed_policies: tuple[str, ...] = ()
    providers: dict = field(default_factory=dict, repr=False)
    lease_seconds: int = 30
    hard_deadline_seconds: int = 900
    max_deliveries: int = 3
    concurrency: int = 2
    max_job_bytes: int = 1_048_576
    max_artifact_bytes: int = 8_388_608
    max_jobs_per_tenant: int = 20
    artifact_region: str = "auto"
    artifact_addressing_style: str = "path"

    def __post_init__(self):
        if self.environment not in ("local", "preview", "qa", "production"):
            raise ValueError("SWARM_CONFIG_INVALID")
        if not 1 <= len(self.keys) <= 2 or any(
            not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", k) or not isinstance(v, bytes) or len(v) < 32
            for k, v in self.keys.items()
        ) or len(set(self.keys.values())) != len(self.keys):
            raise ValueError("SWARM_KEYS_INVALID")
        if not self.inbox_url or not self.artifact_bucket or not self.artifact_access_key or not self.artifact_secret_key:
            raise ValueError("SWARM_BINDINGS_REQUIRED")
        if self.artifact_addressing_style not in ("path", "virtual"):
            raise ValueError("SWARM_ARTIFACT_ADDRESSING_INVALID")
        endpoint = urlsplit(self.artifact_endpoint)
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or not endpoint.hostname:
            raise ValueError("SWARM_EGRESS_INVALID")
        if endpoint.scheme != "https" and not (self.environment == "local" and endpoint.hostname in ("127.0.0.1", "localhost")):
            raise ValueError("SWARM_EGRESS_INVALID")
        if not self.allowed_policies or not 1 <= self.concurrency <= 64 or not 3 <= self.lease_seconds <= 120:
            raise ValueError("SWARM_POLICY_REQUIRED")
        if not 1 <= self.max_deliveries <= 10 or not 1 <= self.hard_deadline_seconds <= 3600:
            raise ValueError("SWARM_BUDGET_INVALID")
        if not 1024 <= self.max_job_bytes <= 1_048_576 or not 1024 <= self.max_artifact_bytes <= 8_388_608:
            raise ValueError("SWARM_BUDGET_INVALID")
        if not 1 <= self.max_jobs_per_tenant <= 1000:
            raise ValueError("SWARM_BUDGET_INVALID")
        if self.environment != "local" and (not re.fullmatch(r"[a-f0-9]{40}", self.source_commit) or not re.fullmatch(r"sha256:[a-f0-9]{64}", self.image_digest)):
            raise ValueError("SWARM_PROVENANCE_REQUIRED")
        for ref, provider in self.providers.items():
            url = urlsplit(provider.get("url", ""))
            local = self.environment == "local" and url.hostname in ("127.0.0.1", "localhost")
            if not ref or not provider.get("version") or len(provider.get("secret", "")) < 32 or not provider.get("produces"):
                raise ValueError("SWARM_PROVIDER_INVALID")
            if url.username or url.password or url.query or url.fragment or not url.hostname:
                raise ValueError("SWARM_EGRESS_INVALID")
            if url.scheme not in ("http", "https") or (not local and not url.hostname.endswith(".railway.internal")):
                raise ValueError("SWARM_EGRESS_INVALID")
            if provider.get("cancelUrl"):
                cancel = urlsplit(provider["cancelUrl"])
                if (cancel.scheme, cancel.netloc) != (url.scheme, url.netloc) or cancel.query or cancel.fragment or cancel.username or cancel.password:
                    raise ValueError("SWARM_EGRESS_INVALID")
            elif self.environment != "local":
                raise ValueError("SWARM_PROVIDER_CANCELLATION_REQUIRED")

    @classmethod
    def from_env(cls):
        try:
            return cls(
                environment=os.environ["SWARM_ENV"], inbox_url=os.environ["SWARM_INBOX_URL"],
                keys={k: base64.b64decode(v, validate=True) for k, v in json.loads(os.environ["SWARM_WORKLOAD_KEYS"]).items()},
                artifact_endpoint=os.environ["SWARM_ARTIFACT_ENDPOINT"], artifact_bucket=os.environ["SWARM_ARTIFACT_BUCKET"],
                artifact_access_key=os.environ["SWARM_ARTIFACT_ACCESS_KEY"], artifact_secret_key=os.environ["SWARM_ARTIFACT_SECRET_KEY"],
                source_commit=os.environ.get("SWARM_SOURCE_COMMIT", ""), image_digest=os.environ.get("SWARM_IMAGE_DIGEST", ""),
                allowed_policies=tuple(json.loads(os.environ["SWARM_ALLOWED_PROJECTION_POLICIES"])),
                providers=json.loads(os.environ.get("SWARM_CAPABILITY_PROVIDERS", "{}")),
                lease_seconds=int(os.environ.get("SWARM_LEASE_SECONDS", "30")),
                hard_deadline_seconds=int(os.environ.get("SWARM_HARD_DEADLINE_SECONDS", "900")),
                concurrency=int(os.environ.get("SWARM_MAX_CONCURRENCY", "2")),
                max_deliveries=int(os.environ.get("SWARM_MAX_DELIVERIES", "3")),
                artifact_region=os.environ.get("SWARM_ARTIFACT_REGION", "auto"),
                artifact_addressing_style=os.environ.get("SWARM_ARTIFACT_ADDRESSING_STYLE", "path"),
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("SWARM_CONFIG_INVALID") from None
