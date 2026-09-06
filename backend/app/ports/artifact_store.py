"""Immutable tenant/run scoped S3 blobs. The inbox alone promotes manifests by CAS."""
import hashlib
import json
import re
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from app.contracts.run_v1 import canonical_protocol_body, checksum


class ArtifactError(ValueError):
    pass


class S3ArtifactStore:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or boto3.client(
            "s3", endpoint_url=settings.artifact_endpoint,
            aws_access_key_id=settings.artifact_access_key,
            aws_secret_access_key=settings.artifact_secret_key,
            region_name=settings.artifact_region,
            config=Config(connect_timeout=3, read_timeout=10,
                          retries={"max_attempts": 2, "mode": "standard"},
                          s3={"addressing_style": "path"}))

    def ready(self):
        self.client.head_bucket(Bucket=self.settings.artifact_bucket)
        return True

    def _key(self, scope, artifact_id):
        if not re.fullmatch(r"[a-f0-9]{64}", artifact_id):
            raise ArtifactError("ARTIFACT_INVALID")
        return f"runs/{checksum(list(scope))}/{artifact_id}.json"

    def put(self, job, step, kind, content):
        raw = canonical_protocol_body(content)
        if len(raw) > self.settings.max_artifact_bytes:
            raise ArtifactError("ARTIFACT_TOO_LARGE")
        digest = hashlib.sha256(raw).hexdigest()
        artifact_id = checksum({"runId": job["runId"], "attempt": job["attempt"],
                                "stepKey": step["stepKey"], "kind": kind, "checksum": digest})
        manifest = {"artifactId": artifact_id, "kind": kind, "checksum": digest, "byteSize": len(raw)}
        scope = (job["tenantScope"], job["runId"], job["attempt"])
        try:
            self.client.put_object(Bucket=self.settings.artifact_bucket, Key=self._key(scope, artifact_id),
                                   Body=raw, ContentType="application/json", IfNoneMatch="*",
                                   Metadata={"sha256": digest})
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
                raise ArtifactError("ARTIFACT_STORE_UNAVAILABLE") from None
        # Also verify existing objects after a duplicate immutable write.
        self.get(scope, manifest)
        return manifest

    def get(self, scope, manifest):
        if not 0 <= manifest["byteSize"] <= self.settings.max_artifact_bytes:
            raise ArtifactError("ARTIFACT_TOO_LARGE")
        try:
            response = self.client.get_object(Bucket=self.settings.artifact_bucket,
                                              Key=self._key(scope, manifest["artifactId"]))
            with response["Body"] as body:
                raw = body.read(self.settings.max_artifact_bytes+1)
            if (len(raw) != manifest["byteSize"] or
                    hashlib.sha256(raw).hexdigest() != manifest["checksum"] or
                    response.get("Metadata", {}).get("sha256") != manifest["checksum"]):
                raise ArtifactError("ARTIFACT_CORRUPTION")
            content = json.loads(raw)
            if canonical_protocol_body(content) != raw:
                raise ArtifactError("ARTIFACT_CORRUPTION")
            return content
        except ClientError:
            raise ArtifactError("ARTIFACT_STORE_UNAVAILABLE") from None
        except (UnicodeError, json.JSONDecodeError):
            raise ArtifactError("ARTIFACT_CORRUPTION") from None
