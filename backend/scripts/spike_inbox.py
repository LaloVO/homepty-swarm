# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["psycopg[binary]==3.2.10", "redis==6.4.0"]
# ///
"""Local-only HPY-143 spike. Own disposable clusters, Unix sockets, synthetic data.

Run: uv run --no-project backend/scripts/spike_inbox.py
Requires initdb, pg_ctl and redis-server already installed. This is NOT the runtime
adapter and measures neither Railway latency/cost nor artifact-store integration.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import redis


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=20)


def postgres_spike(root):
    data = root / "pgdata"
    command("initdb", "-D", str(data), "--auth=trust", "--no-locale", "--encoding=UTF8")
    options = f"-k {root} -c listen_addresses='' -p 55437"

    def start():
        command("pg_ctl", "-D", str(data), "-l", str(root / "pg.log"), "-o", options, "-w", "start")

    def connect():
        return psycopg.connect(host=str(root), port=55437, dbname="postgres", connect_timeout=5)

    start()
    try:
        with connect() as conn:
            conn.execute("""CREATE TABLE inbox (
                run_id text NOT NULL, attempt integer NOT NULL, checksum text NOT NULL,
                state text NOT NULL DEFAULT 'queued', fence bigint NOT NULL DEFAULT 0,
                owner text, lease_until timestamptz,
                PRIMARY KEY (run_id, attempt));
                CREATE INDEX claimable ON inbox (lease_until) WHERE state IN ('queued','running');
                CREATE TABLE events (run_id text NOT NULL, fence bigint NOT NULL);""")

        def submit(checksum):
            with connect() as conn:
                created = conn.execute("""INSERT INTO inbox(run_id,attempt,checksum)
                    VALUES ('fixture',1,%s) ON CONFLICT DO NOTHING RETURNING run_id""", (checksum,)).fetchone()
                if created is None and conn.execute(
                    "SELECT checksum FROM inbox WHERE run_id='fixture' AND attempt=1"
                ).fetchone()[0] != checksum:
                    raise ValueError("CHECKSUM_CONFLICT")
                return created

        before = time.perf_counter()
        with ThreadPoolExecutor(max_workers=8) as pool:
            accepted = sum(row is not None for row in pool.map(submit, ["checksum-a"] * 16))
        elapsed = (time.perf_counter() - before) * 1000
        assert accepted == 1
        try:
            submit("conflicting-checksum")
            raise AssertionError("conflicting duplicate accepted")
        except ValueError as exc:
            assert str(exc) == "CHECKSUM_CONFLICT"

        def claim(owner, ttl=0.5):
            with connect() as conn:
                return conn.execute("""WITH candidate AS (
                    SELECT run_id, attempt FROM inbox WHERE state='queued'
                        OR (state='running' AND lease_until < clock_timestamp())
                    ORDER BY run_id, attempt LIMIT 1 FOR UPDATE SKIP LOCKED
                ) UPDATE inbox i SET state='running', owner=%s, fence=i.fence+1,
                    lease_until=clock_timestamp()+(%s * interval '1 second')
                  FROM candidate c WHERE i.run_id=c.run_id AND i.attempt=c.attempt
                  RETURNING i.fence, i.owner""", (owner, ttl)).fetchone()

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = [row for row in pool.map(claim, ("worker-a", "worker-b")) if row]
        assert len(claims) == 1
        stale_fence, stale_owner = claims[0]
        # A real crash/recovery of our own database; fsync/synchronous_commit remain on.
        command("pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop")
        time.sleep(0.6)
        start()
        fresh_fence, _ = claim("worker-recovered", 30)
        assert fresh_fence == stale_fence + 1
        with connect() as conn:
            stale_ack = conn.execute("""UPDATE inbox SET state='completed'
                WHERE run_id='fixture' AND attempt=1 AND fence=%s AND owner=%s
                AND lease_until>clock_timestamp() RETURNING run_id""", (stale_fence, stale_owner)).fetchone()
            assert stale_ack is None
        with connect() as conn:
            try:
                with conn.transaction():
                    conn.execute("INSERT INTO events VALUES ('fixture',%s)", (fresh_fence,))
                    conn.execute("UPDATE inbox SET state='completed' WHERE run_id='fixture'")
                    raise RuntimeError("fixture failure before commit")
            except RuntimeError:
                pass
            assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0
            assert conn.execute("SELECT state FROM inbox").fetchone()[0] == "running"
        return {"engine": command("psql", "--version").stdout.strip(),
                "duplicate_deliveries": 16, "accepted": accepted,
                "submission_total_ms": round(elapsed, 2), "concurrent_claim_winners": 1,
                "conflicting_checksum_rejected": True,
                "crash_recovery": True, "fence_increased": True,
                "stale_ack_rejected": True, "event_and_status_rollback_atomic": True}
    finally:
        command("pg_ctl", "-D", str(data), "-m", "fast", "-w", "stop")


def redis_spike(root):
    socket_path = str(root / "redis.sock")
    client = redis.Redis(unix_socket_path=socket_path, socket_timeout=5, decode_responses=True)

    def start():
        proc = subprocess.Popen(["redis-server", "--port", "0", "--unixsocket", socket_path,
            "--unixsocketperm", "700", "--dir", str(root), "--appendonly", "yes",
            "--appendfsync", "always", "--save", ""], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                if client.ping():
                    return proc
            except redis.ConnectionError:
                pass
            time.sleep(0.05)
        proc.kill()
        proc.wait(timeout=5)
        raise RuntimeError("disposable Redis failed to start")

    proc = start()
    try:
        enqueue = client.register_script("""
            local existing=redis.call('GET',KEYS[1])
            if existing then
              if existing~=ARGV[1] then return redis.error_reply('CHECKSUM_CONFLICT') end
              return 'duplicate'
            end
            local id=redis.call('XADD',KEYS[2],'*','runId','fixture','attempt','1')
            redis.call('SET',KEYS[1],ARGV[1])
            return id
        """)

        def submit(_):
            return enqueue(keys=["dedupe:fixture:1", "inbox"], args=["checksum-a"])

        before = time.perf_counter()
        with ThreadPoolExecutor(max_workers=8) as pool:
            accepted = sum(result != "duplicate" for result in pool.map(submit, range(16)))
        elapsed = (time.perf_counter() - before) * 1000
        assert accepted == 1 and client.xlen("inbox") == 1
        try:
            enqueue(keys=["dedupe:fixture:1", "inbox"], args=["conflicting-checksum"])
            raise AssertionError("conflicting duplicate accepted")
        except redis.ResponseError as exc:
            assert "CHECKSUM_CONFLICT" in str(exc)
        client.xgroup_create("inbox", "workers", id="0")
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda owner: client.xreadgroup("workers", owner, {"inbox": ">"}, count=1),
                                   ("worker-a", "worker-b")))
        assert sum(bool(result) for result in claims) == 1
        proc.kill()
        proc.wait(timeout=5)
        proc = start()
        recovered = client.xautoclaim("inbox", "workers", "worker-recovered", min_idle_time=1, start_id="0-0")
        assert len(recovered[1]) == 1 and client.get("dedupe:fixture:1") == "checksum-a"
        return {"engine": command("redis-server", "--version").stdout.strip(),
                "duplicate_deliveries": 16, "accepted": accepted,
                "submission_total_ms": round(elapsed, 2), "concurrent_claim_winners": 1,
                "conflicting_checksum_rejected": True,
                "aof_always_crash_recovery": True, "pending_delivery_reclaimed": True,
                "additional_design_required": ["fencing beyond stream ownership", "atomic event/state/artifact authority"]}
    finally:
        client.close()
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == "__main__":
    for binary in ("initdb", "pg_ctl", "psql", "redis-server"):
        if not shutil.which(binary):
            raise SystemExit(f"Required local executable missing: {binary}")
    with tempfile.TemporaryDirectory(prefix="hpy143-inbox-", dir="/tmp") as directory:
        os.chmod(directory, 0o700)
        root = Path(directory)
        result = {"fixtureOnly": True, "environment": "local Unix sockets, not Railway",
                  "postgres": postgres_spike(root), "redis": redis_spike(root)}
        print(json.dumps(result, indent=2))
