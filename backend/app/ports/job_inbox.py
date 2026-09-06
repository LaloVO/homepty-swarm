"""Private PostgreSQL inbox. All publication is fenced and transactional.

This store holds delivery observations, never Brain's canonical run state.
Schema installation is an explicit operator command, not an API startup side effect.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import uuid
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from app.contracts.run_v1 import checksum, make_event

TERMINAL = ("succeeded", "failed", "expired", "cancelled", "cancelled_with_orphan_check_pending")


class InboxError(ValueError):
    pass


def key(job):
    return (job["tenantScope"], job["runId"], job["attempt"])


class PostgresInbox:
    def __init__(self, url, settings):
        self.url, self.settings = url, settings

    @contextmanager
    def connection(self):
        with psycopg.connect(self.url, connect_timeout=5, row_factory=dict_row,
                            options="-c statement_timeout=10000 -c lock_timeout=3000") as conn:
            yield conn

    def migrate(self):
        with self.connection() as conn:
            conn.execute(Path(__file__).with_name("inbox.sql").read_text())

    def ready(self):
        with self.connection() as conn:
            return conn.execute("SELECT version FROM swarm_schema WHERE version=1").fetchone() is not None

    def _nonce(self, conn, envelope):
        # Globally unique per key, stronger than per run/attempt, also covers read requests.
        conn.execute("DELETE FROM swarm_nonces WHERE expires_at < clock_timestamp()")
        inserted = conn.execute("""INSERT INTO swarm_nonces(key_id,nonce,expires_at) VALUES (%s,%s,%s)
            ON CONFLICT DO NOTHING RETURNING nonce""", (envelope.keyId, envelope.nonce, envelope.expiresAt)).fetchone()
        if not inserted:
            raise InboxError("WORKLOAD_REPLAY")
        rate = conn.execute("""INSERT INTO swarm_rate(key_id,minute,count) VALUES (%s,date_trunc('minute',clock_timestamp()),1)
            ON CONFLICT(key_id,minute) DO UPDATE SET count=swarm_rate.count+1
            WHERE swarm_rate.count<600 RETURNING count""", (envelope.keyId,)).fetchone()
        if not rate:
            raise InboxError("RATE_LIMITED")
        conn.execute("DELETE FROM swarm_rate WHERE minute<clock_timestamp()-interval '2 minutes'")

    def submit(self, job, envelope):
        conflict = False
        with self.connection() as conn:
            self._nonce(conn, envelope)
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,143))", (job["tenantScope"],))
            previous = conn.execute("SELECT * FROM swarm_inbox WHERE tenant=%s AND run_id=%s AND attempt=%s", key(job)).fetchone()
            if previous:
                conflict = previous["job_checksum"] != checksum(job)
                if conflict:
                    conn.execute("INSERT INTO swarm_conflicts(tenant,run_id,attempt,checksum) VALUES (%s,%s,%s,%s)", (*key(job), checksum(job)))
                receipt = {"acceptedAt": previous["accepted_at"].isoformat(), "leaseRef": previous["lease_ref"]}
            else:
                depth = conn.execute("SELECT count(*) n FROM swarm_inbox WHERE tenant=%s AND state IN ('queued','running','cancelling')", (job["tenantScope"],)).fetchone()["n"]
                if depth >= self.settings.max_jobs_per_tenant:
                    raise InboxError("RATE_LIMITED")
                row = conn.execute("""INSERT INTO swarm_inbox(tenant,run_id,attempt,job,job_checksum,lease_ref,deadline)
                    VALUES (%s,%s,%s,%s,%s,%s,LEAST(%s::timestamptz,clock_timestamp()+(%s*interval '1 millisecond')))
                    RETURNING accepted_at,lease_ref""", (*key(job), Jsonb(job), checksum(job), str(uuid.uuid4()), job["expiresAt"],
                    min(job["budget"]["maxWallTimeMs"], self.settings.hard_deadline_seconds*1000))).fetchone()
                receipt = {"acceptedAt": row["accepted_at"].isoformat(), "leaseRef": row["lease_ref"]}
        if conflict:
            raise InboxError("IDEMPOTENCY_CONFLICT")
        return receipt

    def read(self, scope, envelope, operation, after=None):
        with self.connection() as conn:
            self._nonce(conn, envelope)
            row = conn.execute("SELECT * FROM swarm_inbox WHERE tenant=%s AND run_id=%s AND attempt=%s FOR UPDATE", scope).fetchone()
            if not row:
                raise InboxError("RUN_NOT_FOUND")
            if operation == "status":
                return {"runId": row["run_id"], "attempt": row["attempt"], "status": row["state"], "lastCursor": None}
            if operation == "cancel":
                if row["state"] not in TERMINAL:
                    conn.execute("UPDATE swarm_inbox SET cancel_requested=true,state='cancelling' WHERE tenant=%s AND run_id=%s AND attempt=%s", scope)
                return {"runId": row["run_id"], "attempt": row["attempt"], "acknowledged": row["state"] == "cancelled",
                        "terminalStatus": row["state"] if row["state"] in ("cancelled", "cancelled_with_orphan_check_pending") else None}
            if operation == "result":
                artifacts = conn.execute("SELECT manifest FROM swarm_steps WHERE tenant=%s AND run_id=%s AND attempt=%s AND manifest IS NOT NULL ORDER BY step_id", scope).fetchall()
                return {"runId": row["run_id"], "attempt": row["attempt"], "artifacts": [a["manifest"] for a in artifacts]}
            if operation == "events":
                position = 0
                if after:
                    cursor = conn.execute("SELECT delivery_sequence FROM swarm_events WHERE tenant=%s AND run_id=%s AND attempt=%s AND event_id=%s", (*scope, after)).fetchone()
                    if not cursor:
                        raise InboxError("CURSOR_INVALID")
                    position = cursor["delivery_sequence"]
                rows = conn.execute("SELECT receipt FROM swarm_events WHERE tenant=%s AND run_id=%s AND attempt=%s AND delivery_sequence>%s ORDER BY delivery_sequence LIMIT 256", (*scope, position)).fetchall()
                return {"events": [r["receipt"] for r in rows]}
            if operation == "artifact":
                artifact = conn.execute("SELECT manifest FROM swarm_steps WHERE tenant=%s AND run_id=%s AND attempt=%s AND manifest->>'artifactId'=%s", (*scope, after)).fetchone()
                if not artifact:
                    raise InboxError("RUN_NOT_FOUND")
                return artifact["manifest"]
            raise InboxError("CONTRACT_INVALID")

    def _event(self, conn, row, step, kind, payload):
        seq = conn.execute("SELECT COALESCE(max(step_sequence),-1)+1 n FROM swarm_events WHERE tenant=%s AND run_id=%s AND attempt=%s AND step_id=%s", (*key(row["job"]), step)).fetchone()["n"]
        event = make_event(row["job"], step, seq, kind, payload)
        receipt = {"event": event, "workerId": row["owner"] or "inbox", "leaseRef": row["lease_ref"],
                   "fenceToken": row["fence"], "producerVersion": "homepty-swarm-runtime@1"}
        conn.execute("""INSERT INTO swarm_events(tenant,run_id,attempt,step_id,step_sequence,event_id,receipt)
            VALUES (%s,%s,%s,%s,%s,%s,%s)""", (*key(row["job"]), step, seq, event["eventId"], Jsonb(receipt)))

    def claim(self, owner):
        with self.connection() as conn:
            row = conn.execute("""SELECT * FROM swarm_inbox WHERE state IN ('queued','running','cancelling')
                AND (lease_until IS NULL OR lease_until<clock_timestamp())
                ORDER BY accepted_at LIMIT 1 FOR UPDATE SKIP LOCKED""").fetchone()
            if not row:
                return None
            row["owner"] = owner
            row["fence"] += 1
            if row["cancel_requested"] or row["deadline"] <= datetime.now(timezone.utc) or row["deliveries"] >= self.settings.max_deliveries:
                status = "cancelled" if row["cancel_requested"] else "expired" if row["deadline"] <= datetime.now(timezone.utc) else "failed"
                self._terminal(conn, row, status, "CANCELLED" if status=="cancelled" else "DEADLINE_EXCEEDED" if status=="expired" else "MAX_DELIVERIES")
                return None
            row = conn.execute("""UPDATE swarm_inbox SET owner=%s,fence=%s,state='running',deliveries=deliveries+1,
                lease_until=LEAST(deadline,clock_timestamp()+(%s*interval '1 second'))
                WHERE tenant=%s AND run_id=%s AND attempt=%s RETURNING *""", (owner, row["fence"], self.settings.lease_seconds, *key(row["job"]))).fetchone()
            exists = conn.execute("SELECT 1 FROM swarm_events WHERE tenant=%s AND run_id=%s AND attempt=%s AND step_id='_swarm'", key(row["job"])).fetchone()
            if not exists:
                self._event(conn, row, "_swarm", "run.started", {})
            return row

    def _owned(self, conn, lease):
        row = conn.execute("""SELECT * FROM swarm_inbox WHERE tenant=%s AND run_id=%s AND attempt=%s
            AND owner=%s AND fence=%s AND lease_until>clock_timestamp() AND deadline>clock_timestamp()
            AND state IN ('running','cancelling') FOR UPDATE""", (*key(lease["job"]), lease["owner"], lease["fence"])).fetchone()
        if not row:
            raise InboxError("LEASE_LOST")
        return row

    def heartbeat(self, lease):
        with self.connection() as conn:
            row = self._owned(conn, lease)
            conn.execute("UPDATE swarm_inbox SET lease_until=LEAST(deadline,clock_timestamp()+(%s*interval '1 second')) WHERE tenant=%s AND run_id=%s AND attempt=%s",
                         (self.settings.lease_seconds, *key(lease["job"])))
            return not row["cancel_requested"]

    def step(self, lease, step):
        with self.connection() as conn:
            row = self._owned(conn, lease)
            if row["cancel_requested"]:
                raise InboxError("CANCELLED")
            saved = conn.execute("SELECT * FROM swarm_steps WHERE tenant=%s AND run_id=%s AND attempt=%s AND step_id=%s", (*key(lease["job"]), step["stepId"])).fetchone()
            if saved and saved["manifest"]:
                return saved["manifest"]
            # Reserve before execution; a crash cannot erase spend already incurred.
            reserve = step["worstCase"]
            budget = lease["job"]["budget"]
            if row["reserved_tokens"]+reserve["tokens"]>budget["maxTokens"] or float(row["reserved_cost"])+reserve["costUsd"]>budget["maxCostUsd"]:
                raise InboxError("BUDGET_EXCEEDED")
            conn.execute("UPDATE swarm_inbox SET reserved_tokens=reserved_tokens+%s,reserved_cost=reserved_cost+%s WHERE tenant=%s AND run_id=%s AND attempt=%s",
                         (reserve["tokens"], reserve["costUsd"], *key(lease["job"])))
            if not saved:
                conn.execute("INSERT INTO swarm_steps(tenant,run_id,attempt,step_id,step_key) VALUES (%s,%s,%s,%s,%s)", (*key(lease["job"]), step["stepId"], step["stepKey"]))
                self._event(conn, row, step["stepId"], "step.started", {})
            return None

    def publish(self, lease, step, manifest, findings):
        with self.connection() as conn:
            row = self._owned(conn, lease)
            if row["cancel_requested"]:
                raise InboxError("CANCELLED")
            updated = conn.execute("""UPDATE swarm_steps SET manifest=%s WHERE tenant=%s AND run_id=%s AND attempt=%s
                AND step_id=%s AND step_key=%s AND manifest IS NULL RETURNING step_id""", (Jsonb(manifest), *key(lease["job"]), step["stepId"], step["stepKey"])).fetchone()
            if not updated:
                raise InboxError("ARTIFACT_CAS_CONFLICT")
            for finding in findings:
                self._event(conn, row, step["stepId"], "finding.emitted", finding)
            self._event(conn, row, step["stepId"], "step.completed", manifest)

    def remote_pending(self, lease, step, pending):
        with self.connection() as conn:
            self._owned(conn, lease)
            conn.execute("UPDATE swarm_steps SET remote_pending=%s WHERE tenant=%s AND run_id=%s AND attempt=%s AND step_id=%s",
                         (pending, *key(lease["job"]), step["stepId"]))

    def cancelled_remote_steps(self, lease):
        with self.connection() as conn:
            row = self._owned(conn, lease)
            if not row["cancel_requested"]:
                return []
            return [r["step_id"] for r in conn.execute("SELECT step_id FROM swarm_steps WHERE tenant=%s AND run_id=%s AND attempt=%s AND remote_pending", key(lease["job"])).fetchall()]

    def _terminal(self, conn, row, status, reason):
        if status == "cancelled":
            pending = conn.execute("SELECT 1 FROM swarm_steps WHERE tenant=%s AND run_id=%s AND attempt=%s AND remote_pending LIMIT 1", key(row["job"])).fetchone()
            if pending:
                status, reason = "cancelled_with_orphan_check_pending", "REMOTE_CANCEL_UNCONFIRMED"
        self._event(conn, row, "_swarm", "run.terminal", {"status": status, "reason": reason})
        conn.execute("UPDATE swarm_inbox SET state=%s,lease_until=NULL,owner=NULL,error_code=%s WHERE tenant=%s AND run_id=%s AND attempt=%s", (status, reason, *key(row["job"])))
        if status == "failed":
            conn.execute("INSERT INTO swarm_dlq(tenant,run_id,attempt,error_code) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING", (*key(row["job"]), reason))

    def finish(self, lease, status="succeeded", reason="completed"):
        with self.connection() as conn:
            row = self._owned(conn, lease)
            if row["cancel_requested"]:
                if reason == "REMOTE_CANCEL_UNCONFIRMED":
                    status = "cancelled_with_orphan_check_pending"
                else:
                    status, reason = "cancelled", "CANCELLED"
            self._terminal(conn, row, status, reason)

    def retry(self, lease):
        with self.connection() as conn:
            row = self._owned(conn, lease)
            conn.execute("UPDATE swarm_inbox SET lease_until=clock_timestamp(),owner=NULL WHERE tenant=%s AND run_id=%s AND attempt=%s", key(row["job"]))


if __name__ == "__main__":
    import sys
    from app.runtime.settings import Settings
    if sys.argv[1:] != ["--migrate"]:
        raise SystemExit("Use --migrate explicitly")
    settings = Settings.from_env()
    PostgresInbox(settings.inbox_url, settings).migrate()
