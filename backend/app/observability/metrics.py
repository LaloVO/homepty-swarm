"""Read-only durable gauges for an internal collector; never a public route.

Run `python -m app.observability.metrics` inside the private runtime. A collector
may scrape this command's Prometheus text. It does not need Suite/Brain access.
"""
from app.ports.job_inbox import PostgresInbox
from app.runtime.settings import Settings

STATES = ("queued", "running", "cancelling", "succeeded", "failed", "expired", "cancelled", "cancelled_with_orphan_check_pending")


def collect(inbox):
    with inbox.connection() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        states = {row["state"]: row["n"] for row in conn.execute("SELECT state,count(*) n FROM swarm_inbox GROUP BY state").fetchall()}
        queue = conn.execute("""SELECT COALESCE(EXTRACT(epoch FROM clock_timestamp()-min(accepted_at)),0) age,
            count(*) FILTER (WHERE lease_until>clock_timestamp()) leased,
            COALESCE(sum(greatest(deliveries-1,0)),0) redeliveries,
            COALESCE(sum(reserved_tokens),0) tokens, COALESCE(sum(reserved_cost),0) cost
            FROM swarm_inbox WHERE state IN ('queued','running','cancelling')""").fetchone()
        steps = conn.execute("""SELECT count(*) FILTER (WHERE remote_pending) pending,
            count(*) FILTER (WHERE manifest IS NOT NULL) artifacts,
            COALESCE(sum((manifest->>'byteSize')::bigint),0) bytes FROM swarm_steps""").fetchone()
        dlq = conn.execute("SELECT count(*) n FROM swarm_dlq").fetchone()["n"]
        conflicts = conn.execute("SELECT count(*) n FROM swarm_conflicts").fetchone()["n"]
    values = {
        **{f'homepty_swarm_inbox_runs{{state="{state}"}}': states.get(state, 0) for state in STATES},
        "homepty_swarm_oldest_active_seconds": queue["age"],
        "homepty_swarm_live_leases": queue["leased"],
        "homepty_swarm_active_redeliveries": queue["redeliveries"],
        "homepty_swarm_reserved_tokens": queue["tokens"],
        # Reservation, NOT billing or measured infrastructure spend.
        "homepty_swarm_reserved_cost_usd": queue["cost"],
        "homepty_swarm_remote_pending": steps["pending"],
        "homepty_swarm_checkpoint_artifacts": steps["artifacts"],
        "homepty_swarm_checkpoint_bytes": steps["bytes"],
        "homepty_swarm_dlq_runs": dlq,
        "homepty_swarm_checksum_conflicts": conflicts,
    }
    return "".join(f"{name} {float(value):g}\n" for name, value in values.items())


def main():
    try:
        settings = Settings.from_env()
        print(collect(PostgresInbox(settings.inbox_url, settings)), end="")
    except Exception:
        # Fail visibly without connection strings, query text or database payloads.
        raise SystemExit("SWARM_METRICS_UNAVAILABLE") from None


if __name__ == "__main__":
    main()
