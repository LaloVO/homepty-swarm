"""Synthetic private provider; tests transport/isolation, never market accuracy."""
import hashlib
import hmac
import json
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from app.contracts.run_v1 import checksum, validate_job
from app.ports.job_inbox import key
from app.runtime.job_consumer import JobConsumer
from app.runtime.plan_executor import ProviderError, validate_result
from app.security.workload_auth import canonical_workload_body
from conftest import SECRET, signed


@pytest.fixture
def provider():
    behavior = {"delay": 0, "calls": 0, "arrived": threading.Event()}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            request_id = self.headers["Idempotency-Key"]
            expected = hmac.new(SECRET, request_id.encode()+b"\n"+raw, hashlib.sha256).hexdigest()
            assert hmac.compare_digest(expected, self.headers["X-Workload-Signature"])
            body = json.loads(raw)
            behavior["calls"] += 1
            behavior["arrived"].set()
            time.sleep(behavior["delay"])
            result = {"contractVersion": "homepty-step-result.v1", "producerVersion": "synthetic@1",
                "outputs": body["step"]["produces"], "kind": "metrics",
                "content": {"fixtureOnly": True, "nodeCount": len(body["snapshot"]["nodes"])}, "findings": [],
                "usage": {"tokens": 0, "costUsd": 0}}
            raw_result = canonical_workload_body(result)
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw_result)))
            self.send_header("X-Workload-Signature", hmac.new(SECRET, request_id.encode()+b"\n"+raw_result, hashlib.sha256).hexdigest())
            self.end_headers()
            try:
                self.wfile.write(raw_result)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"url": f"http://127.0.0.1:{server.server_port}/step", "version": "synthetic@1",
               "secret": SECRET.decode(), "produces": ["synthetic_metric"]}, behavior
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def provider_job(job, count=1):
    for i in range(count):
        step = {"stepId": f"fixture_{i}", "stepType": "query_capability", "dependsOn": ["project_context"],
            "consumes": ["context_snapshot"], "produces": ["synthetic_metric"], "capabilityRef": "synthetic",
            "stepKey": checksum(["synthetic", i]), "worstCase": {"tokens": 0, "costUsd": 0, "wallTimeMs": 15000}}
        job["plan"]["steps"].append(step)
        job["plan"]["topologicalOrder"].append(step["stepId"])
    job["payloadChecksum"] = checksum({"plan": job["plan"], "snapshot": job["snapshot"]})
    return job


def test_spawned_provider_execution_and_checkpoint(inbox, artifacts, settings, job, provider):
    binding, behavior = provider
    settings = replace(settings, providers={"synthetic": binding})
    job = provider_job(job, 3)
    validate_job(job, settings)
    inbox.submit(job, signed(job)[1])
    JobConsumer(settings, inbox, artifacts).run_once()
    assert inbox.read(key(job), signed({})[1], "status")["status"] == "succeeded"
    assert behavior["calls"] == 3
    assert len(inbox.read(key(job), signed({})[1], "result")["artifacts"]) == 4


def test_local_child_exit_cannot_fake_remote_cancel_ack(inbox, artifacts, settings, job, provider):
    binding, behavior = provider
    behavior["delay"] = 10
    settings = replace(settings, providers={"synthetic": binding})
    job = provider_job(job)
    inbox.submit(job, signed(job)[1])
    consumer = JobConsumer(settings, inbox, artifacts)
    thread = threading.Thread(target=consumer.run_once)
    thread.start()
    assert behavior["arrived"].wait(5)
    assert inbox.read(key(job), signed({})[1], "cancel")["acknowledged"] is False
    thread.join(timeout=5)
    assert not thread.is_alive(), "provider child did not stop within cancellation bound"
    receipt = inbox.read(key(job), signed({})[1], "cancel")
    assert receipt["acknowledged"] is False
    assert receipt["terminalStatus"] == "cancelled_with_orphan_check_pending"
    assert len(inbox.read(key(job), signed({})[1], "result")["artifacts"]) == 1


def test_declared_step_deadline_is_enforced(inbox, artifacts, settings, job, provider):
    binding, behavior = provider
    behavior["delay"] = 10
    settings = replace(settings, providers={"synthetic": binding})
    job = provider_job(job)
    job["plan"]["steps"][-1]["worstCase"]["wallTimeMs"] = 1000
    job["payloadChecksum"] = checksum({"plan": job["plan"], "snapshot": job["snapshot"]})
    inbox.submit(job, signed(job)[1])
    start = time.monotonic()
    JobConsumer(settings, inbox, artifacts).run_once()
    assert time.monotonic()-start < 5
    assert inbox.read(key(job), signed({})[1], "status")["status"] == "failed"


def test_result_cannot_exceed_reserved_budget(job, provider):
    binding, _ = provider
    step = provider_job(job)["plan"]["steps"][-1]
    result = {"contractVersion": "homepty-step-result.v1", "producerVersion": "synthetic@1",
        "outputs": step["produces"], "kind": "metrics", "content": {}, "findings": [], "usage": {"tokens": 1, "costUsd": 0}}
    with pytest.raises(ProviderError, match="BUDGET_EXCEEDED"):
        validate_result(result, job, step, binding)
