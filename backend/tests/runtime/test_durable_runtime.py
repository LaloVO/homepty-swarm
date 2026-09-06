import base64
import copy
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from app.api.runs_v1 import create_runtime_app
from app.contracts.run_v1 import ContractError, checksum, validate_job
from app.ports.job_inbox import InboxError, key
from app.ports.artifact_store import ArtifactError
from app.runtime.job_consumer import JobConsumer
from conftest import SECRET, signed


def test_protocol_numeric_keys_match_brain():
    vectors = json.loads((Path(__file__).resolve().parents[3]/"contracts/run-v1/protocol-checksums.fixture.json").read_text())
    for vector in vectors:
        assert checksum(vector["body"]) == vector["checksum"]


def test_bucket_addressing_is_explicit_and_validated(settings):
    from dataclasses import replace
    from app.ports.artifact_store import S3ArtifactStore
    for style in ("path", "virtual"):
        store = S3ArtifactStore(replace(settings, artifact_addressing_style=style))
        assert store.client.meta.config.s3["addressing_style"] == style
    with pytest.raises(ValueError, match="SWARM_ARTIFACT_ADDRESSING_INVALID"):
        replace(settings, artifact_addressing_style="automatic-guess")


def test_real_compiler_requires_approved_providers(compiled_job, settings):
    with pytest.raises(ContractError, match="CAPABILITY_NOT_REGISTERED"):
        validate_job(compiled_job, settings)


def test_compiler_plan_admission_with_explicit_providers(compiled_job, settings):
    from dataclasses import replace
    providers = {}
    for step in compiled_job["plan"]["steps"]:
        if step["stepType"] != "project_context":
            ref = step["capabilityRef"] or "step:"+step["stepType"]
            providers[ref] = {"url": "http://127.0.0.1:1/step", "version": "test", "secret": SECRET.decode(), "produces": step["produces"]}
    assert validate_job(compiled_job, replace(settings, providers=providers)) == compiled_job


def test_duplicate_admission_atomic_and_replay(inbox, settings, job):
    validate_job(job, settings)
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(lambda _: inbox.submit(job, signed(job)[1]), range(16)))
    assert len({r["leaseRef"] for r in receipts}) == 1
    env = signed(job)[1]
    inbox.submit(job, env)
    with pytest.raises(InboxError, match="WORKLOAD_REPLAY"):
        inbox.submit(job, env)
    conflicting = {**job, "nonce": "another-logical-job"}
    with pytest.raises(InboxError, match="IDEMPOTENCY_CONFLICT"):
        inbox.submit(conflicting, signed(conflicting)[1])
    with inbox.connection() as conn:
        assert conn.execute("SELECT count(*) n FROM swarm_conflicts").fetchone()["n"] == 1


def test_restart_checkpoints_events_and_s3(inbox, artifacts, settings, job):
    inbox.submit(job, signed(job)[1])
    assert JobConsumer(settings, inbox, artifacts).run_once()
    events = inbox.read(key(job), signed({})[1], "events")["events"]
    assert [r["event"]["eventType"] for r in events] == ["run.started", "step.started", "step.completed", "run.terminal"]
    assert events[-1]["event"]["payload"]["status"] == "succeeded"
    # New store and worker objects reconstruct solely from durable state.
    from app.ports.job_inbox import PostgresInbox
    restarted = PostgresInbox(settings.inbox_url, settings)
    assert not JobConsumer(settings, restarted, artifacts).run_once()
    manifest = restarted.read(key(job), signed({})[1], "result")["artifacts"][0]
    assert artifacts.get(key(job), manifest) == job["snapshot"]
    assert restarted.read(key(job), signed({})[1], "events")["events"] == events


def test_concurrent_claim_and_stale_fence(inbox, job):
    inbox.submit(job, signed(job)[1])
    with ThreadPoolExecutor(max_workers=2) as pool:
        winners = [v for v in pool.map(inbox.claim, ["a", "b"]) if v]
    assert len(winners) == 1
    old = winners[0]
    with inbox.connection() as conn:
        conn.execute("UPDATE swarm_inbox SET lease_until=clock_timestamp()-interval '1 second'")
    new = inbox.claim("replacement")
    assert new["fence"] == old["fence"]+1
    for action in (lambda: inbox.heartbeat(old), lambda: inbox.step(old, job["plan"]["steps"][0]), lambda: inbox.finish(old)):
        with pytest.raises(InboxError, match="LEASE_LOST"):
            action()


def test_database_crash_recovery_preserves_fencing(inbox, settings, job):
    import subprocess
    from psycopg.conninfo import conninfo_to_dict
    root = Path(conninfo_to_dict(settings.inbox_url)["host"])
    assert root.parent == Path("/tmp") and root.name.startswith("hpy143-inbox-")
    inbox.submit(job, signed(job)[1])
    old = inbox.claim("crashed-worker")
    with inbox.connection() as conn:
        conn.execute("UPDATE swarm_inbox SET lease_until=clock_timestamp()-interval '1 second'")
    subprocess.run(["pg_ctl", "-D", str(root/"pgdata"), "-m", "immediate", "-w", "stop"], check=True, capture_output=True, timeout=20)
    subprocess.run(["pg_ctl", "-D", str(root/"pgdata"), "-l", str(root/"pg.log"), "-o",
        f"-k {root} -c listen_addresses='' -p 55438", "-w", "start"], check=True, capture_output=True, timeout=20)
    fresh = inbox.claim("recovered-worker")
    assert fresh["fence"] > old["fence"]
    with pytest.raises(InboxError, match="LEASE_LOST"):
        inbox.finish(old)
    assert len(inbox.read(key(job), signed({})[1], "events")["events"]) == 1


def test_cancel_requires_ack_and_prevents_work(inbox, artifacts, settings, job):
    inbox.submit(job, signed(job)[1])
    receipt = inbox.read(key(job), signed({})[1], "cancel")
    assert receipt["acknowledged"] is False
    JobConsumer(settings, inbox, artifacts).run_once()
    assert inbox.read(key(job), signed({})[1], "cancel")["acknowledged"] is True
    events = inbox.read(key(job), signed({})[1], "events")["events"]
    assert len(events) == 1
    assert events[0]["event"]["payload"]["status"] == "cancelled"


def test_deadline_and_dlq(inbox, job):
    inbox.submit(job, signed(job)[1])
    with inbox.connection() as conn:
        conn.execute("UPDATE swarm_inbox SET deadline=clock_timestamp()-interval '1 second'")
    assert inbox.claim("worker") is None
    assert inbox.read(key(job), signed({})[1], "status")["status"] == "expired"
    another = {**job, "runId": "dlq_run"}
    inbox.submit(another, signed(another)[1])
    for _ in range(3):
        lease = inbox.claim("worker")
        inbox.retry(lease)
    assert inbox.claim("worker") is None
    with inbox.connection() as conn:
        assert conn.execute("SELECT error_code FROM swarm_dlq").fetchone()["error_code"] == "MAX_DELIVERIES"


def test_artifacts_immutable_scoped_and_corruption_checked(artifacts, settings, job):
    step = job["plan"]["steps"][0]
    manifest = artifacts.put(job, step, "metrics", {"10": "a", "2": "b"})
    assert artifacts.put(job, step, "metrics", {"2": "b", "10": "a"}) == manifest
    with pytest.raises(ArtifactError):
        artifacts.get(("other_tenant", job["runId"], 1), manifest)
    artifacts.client.put_object(Bucket=settings.artifact_bucket, Key=artifacts._key(key(job), manifest["artifactId"]), Body=b"corrupt")
    with pytest.raises(ArtifactError, match="ARTIFACT_CORRUPTION"):
        artifacts.get(key(job), manifest)


def test_private_http_signature_and_scope(inbox, artifacts, settings, job):
    client = create_runtime_app(settings, inbox, artifacts).test_client()
    assert client.get("/health/ready").status_code == 200
    assert client.get("/api/graph").status_code == 404
    assert client.post("/v1/runs", json=job).status_code == 401
    raw, envelope = signed(job)
    response = client.post("/v1/runs", data=raw, content_type="application/json")
    assert response.status_code == 202, response.json
    expected = hmac.new(SECRET, "\n".join(("homepty-workload-response.v1", "POST", "/v1/runs", envelope.nonce,
        "202", hashlib.sha256(response.data).hexdigest())).encode(), hashlib.sha256).hexdigest()
    assert response.headers["X-Workload-Signature"] == expected
    assert "Access-Control-Allow-Origin" not in response.headers
    scope = {"tenantScope": "other", "runId": job["runId"], "attempt": 1}
    target = f'/v1/runs/{job["runId"]}/attempts/1/status'
    raw, _ = signed(scope, "GET", target)
    response = client.get(target, headers={"X-Workload-Envelope": base64.urlsafe_b64encode(raw).decode().rstrip("=")})
    assert response.status_code == 404
    assert job["tenantScope"].encode() not in response.data


def test_artifact_publish_cas_and_atomic_events(inbox, artifacts, job):
    inbox.submit(job, signed(job)[1])
    lease = inbox.claim("worker")
    step = job["plan"]["steps"][0]
    inbox.step(lease, step)
    manifest = artifacts.put(job, step, "metrics", {})
    inbox.publish(lease, step, manifest, [])
    with pytest.raises(InboxError, match="ARTIFACT_CAS_CONFLICT"):
        inbox.publish(lease, step, manifest, [])
    assert len(inbox.read(key(job), signed({})[1], "events")["events"]) == 3


def test_real_http_typescript_adapter_and_brain_replay(inbox, artifacts, settings, job):
    import os
    import subprocess
    import threading
    from werkzeug.serving import make_server, WSGIRequestHandler
    brain = os.environ.get("HPY143_BRAIN_CHECKOUT")
    if not brain:
        pytest.skip("Cross-repository check requires explicit HPY143_BRAIN_CHECKOUT; CI runs it in Brain integration job")
    inbox.submit(job, signed(job)[1])
    JobConsumer(settings, inbox, artifacts).run_once()
    class QuietHandler(WSGIRequestHandler):
        def log_request(self, *args, **kwargs):
            pass
    server = make_server("127.0.0.1", 0, create_runtime_app(settings, inbox, artifacts), request_handler=QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(["pnpm", "exec", "tsx", "scripts/verify-swarm-http.ts", f"http://127.0.0.1:{server.server_port}"],
            cwd=brain, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout+result.stderr
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
