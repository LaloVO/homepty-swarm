"""Own disposable PostgreSQL cluster and S3 emulator. Never use developer databases."""
import base64
import hashlib
import hmac
import json
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from moto import mock_aws
from app.contracts.run_v1 import checksum
from app.runtime.settings import Settings
from app.ports.job_inbox import PostgresInbox
from app.ports.artifact_store import S3ArtifactStore
from app.security.workload_auth import canonical_workload_body, verify_workload_envelope

SECRET = b"local-fixture-only-never-production-key"


def signed(body, method="POST", target="/v1/runs"):
    now = datetime.now(timezone.utc)
    envelope = {"contractVersion": "homepty-workload.v1", "algorithm": "HMAC-SHA256", "keyId": "test",
                "issuedAt": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "expiresAt": (now+timedelta(seconds=120)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "nonce": uuid.uuid4().hex, "body": body,
                "bodySha256": hashlib.sha256(canonical_workload_body(body)).hexdigest()}
    transcript = "\n".join((method, target, envelope["issuedAt"], envelope["expiresAt"], envelope["nonce"], envelope["bodySha256"]))
    envelope["signature"] = hmac.new(SECRET, transcript.encode(), hashlib.sha256).hexdigest()
    raw = canonical_workload_body(envelope)
    verified = verify_workload_envelope(raw=raw, method=method, target=target, keys={"test": SECRET}, now=now)
    return raw, verified


@pytest.fixture(scope="session")
def postgres_url():
    def command(*args):
        return subprocess.run(args, check=True, capture_output=True, timeout=20)
    with tempfile.TemporaryDirectory(prefix="hpy143-inbox-", dir="/tmp") as root:
        data = str(Path(root)/"pgdata")
        command("initdb", "-D", data, "--auth=trust", "--no-locale", "--encoding=UTF8")
        command("pg_ctl", "-D", data, "-l", str(Path(root)/"pg.log"), "-o", f"-k {root} -c listen_addresses='' -p 55438", "-w", "start")
        try:
            yield f"dbname=postgres host={root} port=55438"
        finally:
            command("pg_ctl", "-D", data, "-m", "immediate", "-w", "stop")


@pytest.fixture
def settings(postgres_url):
    return Settings(environment="local", inbox_url=postgres_url, keys={"test": SECRET},
        artifact_endpoint="https://s3.amazonaws.com", artifact_bucket="hpy143-fixture",
        artifact_access_key="testing", artifact_secret_key="testing", artifact_region="us-east-1",
        allowed_policies=("fixture-policy@v1",), lease_seconds=3)


@pytest.fixture
def inbox(settings):
    box = PostgresInbox(settings.inbox_url, settings)
    box.migrate()
    with box.connection() as conn:
        conn.execute("TRUNCATE swarm_inbox,swarm_steps,swarm_events,swarm_nonces,swarm_rate,swarm_conflicts,swarm_dlq RESTART IDENTITY CASCADE")
    return box


@pytest.fixture
def artifacts(settings):
    with mock_aws():
        store = S3ArtifactStore(settings)
        store.client.create_bucket(Bucket=settings.artifact_bucket)
        yield store


@pytest.fixture
def compiled_job():
    root = Path(__file__).resolve().parents[3]
    job = json.loads((root/"contracts/run-v1/compiled-job.fixture.json").read_text())
    now = datetime.now(timezone.utc)
    job["issuedAt"] = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    job["expiresAt"] = (now+timedelta(seconds=120)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return job


@pytest.fixture
def job(compiled_job):
    # Infrastructure fixture explicitly contains only projection; not a product scenario.
    compiled_job["plan"]["steps"] = [s for s in compiled_job["plan"]["steps"] if s["stepType"] == "project_context"]
    compiled_job["plan"]["topologicalOrder"] = ["project_context"]
    compiled_job["payloadChecksum"] = checksum({"plan": compiled_job["plan"], "snapshot": compiled_job["snapshot"]})
    return compiled_job
