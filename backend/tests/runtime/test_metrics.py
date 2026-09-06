from app.observability.metrics import collect
from conftest import signed


def test_durable_gauges_do_not_disclose_job_or_tenant(inbox, job):
    _, envelope = signed(job)
    inbox.submit(job, envelope)
    text = collect(inbox)
    assert 'homepty_swarm_inbox_runs{state="queued"} 1' in text
    assert "homepty_swarm_dlq_runs 0" in text
    assert job["tenantScope"] not in text
    assert job["runId"] not in text
    assert "Zona" not in text
    lease = inbox.claim("metrics-fixture")
    assert "homepty_swarm_live_leases 1" in collect(inbox)
    inbox.finish(lease, "failed", "FIXTURE_FAILURE")
    assert "homepty_swarm_dlq_runs 1" in collect(inbox)
