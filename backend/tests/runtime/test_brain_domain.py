"""Real Python durable worker -> real Brain HTTP -> isolated S1 -> canonical graph/report."""
import json
import os
import subprocess
import threading
from dataclasses import replace
import pytest
from werkzeug.serving import make_server
from app.api.runs_v1 import create_runtime_app
from app.runtime.job_consumer import JobConsumer
from conftest import SECRET
from psycopg.conninfo import conninfo_to_dict
import getpass


@pytest.mark.parametrize("mode", ["complete", "cancel"])
def test_real_brain_s1_domain(inbox, artifacts, settings, mode):
    brain = os.environ.get("HPY143_BRAIN_CHECKOUT")
    if not brain:
        pytest.skip("Requires explicit HPY143_BRAIN_CHECKOUT")
    subprocess.run(["pnpm", "swarm:build-domain"], cwd=brain, check=True, capture_output=True, timeout=30)
    settings = replace(settings, allowed_policies=("projection-policy@v1",))
    server = make_server("127.0.0.1", 0, create_runtime_app(settings, inbox, artifacts), threaded=True)
    http = threading.Thread(target=server.serve_forever, daemon=True)
    http.start()
    process = subprocess.Popen(["pnpm", "exec", "tsx", "scripts/verify-swarm-domain.ts", f"http://127.0.0.1:{server.server_port}", mode],
        cwd=brain, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    consumer = JobConsumer(settings, inbox, artifacts)
    worker = threading.Thread(target=consumer.run, daemon=True)
    try:
        ready = json.loads(process.stdout.readline())
        settings.providers.update({ref: {"url": ready["providerUrl"], "cancelUrl": ready["providerUrl"].replace("/step", "/cancel"), "version": "homepty-brain-domain@1", "secret": SECRET.decode(), "produces": outputs}
                                   for ref, outputs in ready["providers"].items()})
        worker.start()
        output, errors = process.communicate("go\n", timeout=90)
        assert process.returncode == 0, errors
        result = json.loads(output.strip().splitlines()[-1])
        if mode == "cancel":
            assert result == {"outcome": "cancelled", "remoteTerminationConfirmed": True, "noVariantPublished": True, "replayVerified": True, "syntheticOnly": True}
        else:
            assert result == {"outcome": "succeeded", "steps": 8, "promotedArtifacts": 7,
                              "findings": result["findings"], "replayVerified": True, "syntheticOnly": True}
            assert result["findings"] > 10
    finally:
        consumer.stopping.set()
        if worker.is_alive():
            worker.join(timeout=10)
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        server.shutdown()
        http.join(timeout=5)


def test_brain_postgres_concurrency(postgres_url):
    brain = os.environ.get("HPY143_BRAIN_CHECKOUT")
    if not brain:
        pytest.skip("Requires explicit HPY143_BRAIN_CHECKOUT")
    config = conninfo_to_dict(postgres_url)
    result = subprocess.run(["pnpm", "exec", "tsx", "scripts/verify-swarm-postgres.ts", config["host"], getpass.getuser()],
                            cwd=brain, text=True, capture_output=True, timeout=45)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1])["singleWinner"] is True
