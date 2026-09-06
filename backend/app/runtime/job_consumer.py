"""Durable worker: PostgreSQL checkpoints, S3 immutable artifacts and fenced events."""
import concurrent.futures
import json
import logging
import multiprocessing
import os
import signal
import threading
import time
import uuid
from datetime import datetime, timezone
from app.ports.artifact_store import ArtifactError, S3ArtifactStore
from app.ports.job_inbox import InboxError, PostgresInbox, key
from app.runtime.plan_executor import ProviderError, child_execute, confirm_provider_cancel
from app.runtime.settings import Settings

log = logging.getLogger("swarm.worker")


def stop_process(process):
    if process.pid is None:
        return
    if process.is_alive():
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            process.terminate()
        process.join(timeout=1)
    if process.is_alive():
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            process.kill()
        process.join(timeout=2)
    if process.is_alive():
        raise InboxError("CANCEL_ORPHAN_PENDING")


class JobConsumer:
    def __init__(self, settings, inbox=None, artifacts=None):
        self.settings = settings
        self.inbox = inbox or PostgresInbox(settings.inbox_url, settings)
        self.artifacts = artifacts or S3ArtifactStore(settings)
        self.stopping = threading.Event()
        self.owner = "worker_"+uuid.uuid4().hex

    def _provider(self, lease, step, dependencies, lost, heartbeat_error):
        job = lease["job"]
        ref = step["capabilityRef"] if step["stepType"] == "query_capability" else "step:"+step["stepType"]
        provider = self.settings.providers.get(ref)
        if not provider:
            raise ProviderError("CAPABILITY_NOT_REGISTERED")
        remaining = (lease["deadline"]-datetime.now(timezone.utc)).total_seconds()
        timeout = min(remaining, step["worstCase"]["wallTimeMs"]/1000) if step["worstCase"]["wallTimeMs"] else remaining
        if timeout <= 0:
            raise InboxError("DEADLINE_EXCEEDED")
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=child_execute, args=(sender, job, step, dependencies,
                                   provider, self.settings.max_artifact_bytes, timeout), daemon=True)
        process.start()
        sender.close()
        end = time.monotonic()+timeout
        try:
            while not receiver.poll(0.1):
                if lost.is_set():
                    code = heartbeat_error[0] if heartbeat_error else "LEASE_LOST"
                    # Terminating our HTTP child cannot prove remote work stopped.
                    raise InboxError("REMOTE_CANCEL_UNCONFIRMED" if code == "CANCELLED" else code)
                if self.stopping.is_set():
                    raise InboxError("WORKER_DRAINING")
                if time.monotonic() >= end:
                    raise InboxError("DEADLINE_EXCEEDED")
                if not process.is_alive():
                    raise ProviderError("PROVIDER_EXECUTION_FAILED")
            result = json.loads(receiver.recv_bytes(self.settings.max_artifact_bytes+1024))
            if "error" in result:
                raise ProviderError(result["error"])
            return result["result"]
        finally:
            stop_process(process)
            receiver.close()
            process.close()

    def execute(self, lease):
        job = lease["job"]
        lost = threading.Event()
        completed = threading.Event()
        heartbeat_error = []

        def heartbeat():
            while not completed.wait(self.settings.lease_seconds/3):
                try:
                    if not self.inbox.heartbeat(lease):
                        heartbeat_error.append("CANCELLED")
                        lost.set()
                        return
                except Exception:
                    heartbeat_error.append("LEASE_LOST")
                    lost.set()
                    return

        pulse = threading.Thread(target=heartbeat, daemon=True)
        pulse.start()
        outputs = {}
        def confirm_cancellation():
            for step_id in self.inbox.cancelled_remote_steps(lease):
                step = next(s for s in job["plan"]["steps"] if s["stepId"] == step_id)
                ref = step["capabilityRef"] if step["stepType"] == "query_capability" else "step:"+step["stepType"]
                if confirm_provider_cancel(job, step, self.settings.providers.get(ref)):
                    self.inbox.remote_pending(lease, step, False)
        try:
            steps = {s["stepId"]: s for s in job["plan"]["steps"]}
            # Dependency scheduling is bounded by the job's concurrency, not agent count.
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(job["budget"]["maxConcurrency"], self.settings.concurrency)) as pool:
                pending = set(steps)
                running = {}

                def execute_step(step):
                    if lost.is_set() or self.stopping.is_set():
                        raise InboxError(heartbeat_error[0] if heartbeat_error else "WORKER_DRAINING")
                    saved = self.inbox.step(lease, step)
                    if saved:
                        return self.artifacts.get(key(job), saved)
                    dependencies = {dep: outputs[dep] for dep in step["dependsOn"]}
                    if step["stepType"] == "project_context":
                        content, kind, findings = job["snapshot"], "metrics", []
                    else:
                        self.inbox.remote_pending(lease, step, True)
                        result = self._provider(lease, step, dependencies, lost, heartbeat_error)
                        self.inbox.remote_pending(lease, step, False)
                        content, kind, findings = result["content"], result["kind"], result["findings"]
                    if lost.is_set() or self.stopping.is_set():
                        raise InboxError(heartbeat_error[0] if heartbeat_error else "WORKER_DRAINING")
                    manifest = self.artifacts.put(job, step, kind, content)
                    self.inbox.publish(lease, step, manifest, findings)
                    return content

                while pending or running:
                    if lost.is_set() or self.stopping.is_set():
                        raise InboxError(heartbeat_error[0] if heartbeat_error else "WORKER_DRAINING")
                    for step_id in job["plan"]["topologicalOrder"]:
                        if step_id in pending and set(steps[step_id]["dependsOn"]).issubset(outputs) and len(running) < self.settings.concurrency:
                            running[pool.submit(execute_step, steps[step_id])] = step_id
                            pending.remove(step_id)
                    if not running:
                        raise InboxError("PLAN_INVALID")
                    done, _ = concurrent.futures.wait(running, timeout=0.1, return_when=concurrent.futures.FIRST_COMPLETED)
                    for future in done:
                        step_id = running.pop(future)
                        try:
                            outputs[step_id] = future.result()
                        except Exception:
                            lost.set()  # Stop sibling providers before leaving the pool.
                            raise
            confirm_cancellation()
            self.inbox.finish(lease)
        except (InboxError, ProviderError, ArtifactError) as error:
            code = str(error)
            try:
                confirm_cancellation()
                if code in ("LEASE_LOST", "CANCEL_ORPHAN_PENDING"):
                    pass  # Never acknowledge or publish under a lost fence.
                elif code in ("PROVIDER_UNAVAILABLE", "WORKER_DRAINING", "ARTIFACT_STORE_UNAVAILABLE"):
                    self.inbox.retry(lease)
                else:
                    self.inbox.finish(lease, "cancelled" if code in ("CANCELLED", "REMOTE_CANCEL_UNCONFIRMED") else "failed", "CANCELLED" if code == "REMOTE_CANCEL_UNCONFIRMED" else code)
            except InboxError:
                pass  # Expiry/reclaim is durably resolved by the next claim.
            log.warning("execution_stopped code=%s", code)
        finally:
            completed.set()
            pulse.join(timeout=self.settings.lease_seconds)

    def run_once(self):
        lease = self.inbox.claim(self.owner)
        if lease:
            self.execute(lease)
        return lease is not None

    def run(self):
        while not self.stopping.is_set():
            try:
                if not self.run_once():
                    self.stopping.wait(0.5)
            except Exception:
                log.error("worker_poll_failed code=RUNTIME_UNAVAILABLE")
                self.stopping.wait(1)


def main():
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    consumer = JobConsumer(Settings.from_env())
    signal.signal(signal.SIGTERM, lambda *_: consumer.stopping.set())
    signal.signal(signal.SIGINT, lambda *_: consumer.stopping.set())
    consumer.run()


if __name__ == "__main__":
    main()
