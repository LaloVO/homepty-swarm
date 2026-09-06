"""Linux CI only: exercise unmodified release images, not a test-only Docker stage."""
import base64
import getpass
import hashlib
import hmac
import json
import os
import socket
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from moto.server import ThreadedMotoServer
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from app.ports.artifact_store import S3ArtifactStore
from conftest import SECRET, signed


@pytest.mark.skipif(not os.environ.get("SWARM_CI_API_IMAGE"), reason="Image test runs only in CI; no local Docker required")
def test_release_images_complete_persist_restart_and_reject_unsigned(inbox, settings, job):
    def docker(*args):
        return subprocess.check_output(["docker", *args], text=True, timeout=45).strip()

    root = Path(conninfo_to_dict(settings.inbox_url)["host"])
    assert root.parent == Path("/tmp") and root.name.startswith("hpy143-inbox-")
    old_mode = root.stat().st_mode & 0o777
    # Only this disposable fixture socket becomes accessible to the container UID.
    root.chmod(0o755)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    moto = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    moto.start()
    _, s3_port = moto.get_host_and_port()
    settings = replace(settings, artifact_endpoint=f"http://127.0.0.1:{s3_port}",
                       inbox_url=make_conninfo(settings.inbox_url, user=getpass.getuser()))
    artifacts = S3ArtifactStore(settings)
    artifacts.client.create_bucket(Bucket=settings.artifact_bucket)
    env = {
        "SWARM_ENV": "local", "SWARM_INBOX_URL": settings.inbox_url,
        "SWARM_WORKLOAD_KEYS": json.dumps({"test": base64.b64encode(SECRET).decode()}),
        "SWARM_ARTIFACT_ENDPOINT": settings.artifact_endpoint, "SWARM_ARTIFACT_BUCKET": settings.artifact_bucket,
        "SWARM_ARTIFACT_ACCESS_KEY": "testing", "SWARM_ARTIFACT_SECRET_KEY": "testing",
        "SWARM_ARTIFACT_REGION": "us-east-1", "SWARM_ALLOWED_PROJECTION_POLICIES": json.dumps(settings.allowed_policies),
        "SWARM_CAPABILITY_PROVIDERS": "{}", "PORT": str(port), "SWARM_LEASE_SECONDS": "3",
    }
    args = ["--detach", "--network", "host", "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--mount", f"type=bind,source={root},target={root},readonly"]
    for k, v in env.items():
        args.extend(["--env", f"{k}={v}"])
    containers = []
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=3)

    def request(operation=None):
        target = "/v1/runs" if operation is None else f'/v1/runs/{job["runId"]}/attempts/1/{operation}'
        method = "POST" if operation is None else "GET"
        body = job if operation is None else {k: job[k] for k in ("tenantScope", "runId", "attempt")}
        raw, envelope = signed(body, method, target)
        response = (client.post(target, content=raw, headers={"Content-Type": "application/json"}) if operation is None
                    else client.get(target, headers={"X-Workload-Envelope": base64.urlsafe_b64encode(raw).decode().rstrip("=")}))
        assert response.status_code == (202 if operation is None else 200), response.text
        transcript = "\n".join(("homepty-workload-response.v1", method, target, envelope.nonce, str(response.status_code), hashlib.sha256(response.content).hexdigest()))
        assert response.headers["X-Workload-Signature"] == hmac.new(SECRET, transcript.encode(), hashlib.sha256).hexdigest()
        return response.json()

    try:
        for role in ("api", "worker"):
            containers.append(docker("run", *args, os.environ[f"SWARM_CI_{role.upper()}_IMAGE"]))
        deadline = time.monotonic() + 30
        while True:
            try:
                if client.get("/health/ready").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            assert time.monotonic() < deadline, "container readiness deadline"
            time.sleep(0.2)
        assert client.get("/api/graph").status_code == 404
        assert client.post("/v1/runs", json=job).status_code == 401
        first = request()
        assert request()["leaseRef"] == first["leaseRef"]
        while request("status")["status"] != "succeeded":
            assert time.monotonic() < deadline, "container execution deadline"
            time.sleep(0.2)
        before = request("events")
        assert [row["event"]["eventType"] for row in before["events"]] == ["run.started", "step.started", "step.completed", "run.terminal"]
        manifest = request("result")["artifacts"][0]
        assert artifacts.get((job["tenantScope"], job["runId"], 1), manifest) == job["snapshot"]
        docker("restart", containers[1])
        assert request("events") == before
        logs = "\n".join(docker("logs", container) for container in containers)
        assert SECRET.decode() not in logs and job["tenantScope"] not in logs
    finally:
        client.close()
        for container in containers:
            docker("rm", "--force", container)
        moto.stop()
        root.chmod(old_mode)
