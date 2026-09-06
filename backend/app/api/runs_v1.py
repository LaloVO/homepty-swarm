"""Private command/query ingress. No research routes, CORS, cookies or business logs."""
import base64
import hashlib
import hmac
import re
from datetime import datetime, timezone
from flask import Flask, Response, request
from werkzeug.exceptions import HTTPException
from app.contracts.run_v1 import ContractError, validate_job
from app.ports.artifact_store import ArtifactError, S3ArtifactStore
from app.ports.job_inbox import InboxError, PostgresInbox
from app.runtime.settings import Settings
from app.security.workload_auth import WorkloadAuthError, canonical_workload_body, verify_workload_envelope


def create_runtime_app(settings=None, inbox=None, artifacts=None):
    settings = settings or Settings.from_env()
    inbox = inbox or PostgresInbox(settings.inbox_url, settings)
    artifacts = artifacts or S3ArtifactStore(settings)
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=settings.max_job_bytes, DEBUG=False, PROPAGATE_EXCEPTIONS=False)

    def reply(body, status=200, envelope=None, target=None):
        raw = canonical_workload_body(body)
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if envelope:
            digest = hashlib.sha256(raw).hexdigest()
            transcript = "\n".join(("homepty-workload-response.v1", request.method, target,
                                     envelope.nonce, str(status), digest)).encode()
            headers["X-Workload-Signature"] = hmac.new(settings.keys[envelope.keyId], transcript, hashlib.sha256).hexdigest()
        return Response(raw, status=status, headers=headers, content_type="application/json")

    @app.get("/health/live")
    def live():
        return reply({"status": "live"})

    @app.get("/health/ready")
    def ready():
        try:
            if not inbox.ready() or not artifacts.ready():
                raise ValueError()
            return reply({"status": "ready", "contractVersion": 1,
                          "execution": "gated" if not settings.providers else "configured"})
        except Exception:
            return reply({"status": "not_ready"}, 503)

    @app.get("/source")
    def source():
        return reply({"license": "AGPL-3.0-only", "commit": settings.source_commit or None,
                      "imageDigest": settings.image_digest or None,
                      "url": f"https://github.com/LaloVO/homepty-swarm/tree/{settings.source_commit}" if settings.source_commit else None})

    @app.route("/v1/runs", methods=["POST"])
    @app.route("/v1/runs/<run_id>/attempts/<int:attempt>/<operation>", methods=["GET", "POST"])
    def runs(run_id=None, attempt=None, operation=None):
        envelope, target = None, None
        try:
            if request.headers.get("Cookie") or request.headers.get("Content-Encoding"):
                raise WorkloadAuthError("WORKLOAD_ENVELOPE_INVALID")
            # RAW_URI preserves the signed literal target; never reconstruct decoded paths.
            target = request.environ.get("RAW_URI") or request.environ.get("REQUEST_URI")
            if not target:
                target = request.path + ("?"+request.query_string.decode("ascii") if request.query_string else "")
            if request.method == "GET":
                header = request.headers.get("X-Workload-Envelope", "")
                if len(header) > 8192 or not re.fullmatch(r"[A-Za-z0-9_-]+", header) or request.get_data():
                    raise WorkloadAuthError("WORKLOAD_ENVELOPE_INVALID")
                raw = base64.b64decode(header+"="*((-len(header))%4), altchars=b"-_", validate=True)
            else:
                if request.mimetype != "application/json":
                    raise WorkloadAuthError("WORKLOAD_ENVELOPE_INVALID")
                raw = request.get_data(cache=False)
            envelope = verify_workload_envelope(raw=raw, method=request.method, target=target,
                                                keys=settings.keys, now=datetime.now(timezone.utc))
            body = envelope.body
            if run_id is None:
                return reply(inbox.submit(validate_job(body, settings), envelope), 202, envelope, target)
            expected_method = "POST" if operation == "cancel" else "GET"
            if request.method != expected_method or operation not in ("status", "cancel", "events", "result", "artifact"):
                raise ContractError("CONTRACT_INVALID")
            expected_keys = {"tenantScope", "runId", "attempt"} | ({"reason"} if operation == "cancel" else set())
            if (not isinstance(body, dict) or set(body) != expected_keys or body.get("runId") != run_id
                    or type(body.get("attempt")) is not int or body["attempt"] != attempt or attempt < 1
                    or not isinstance(body.get("tenantScope"), str) or not 1 <= len(body["tenantScope"]) <= 256):
                raise ContractError("CONTRACT_INVALID")
            if operation == "cancel" and (not isinstance(body["reason"], str) or not 1 <= len(body["reason"]) <= 256):
                raise ContractError("CONTRACT_INVALID")
            param = "afterEventId" if operation == "events" else "artifactId" if operation == "artifact" else None
            if any(k != param or len(request.args.getlist(k)) != 1 for k in request.args):
                raise ContractError("CONTRACT_INVALID")
            after = request.args.get(param) if param else None
            if (after is not None and not re.fullmatch(r"[a-f0-9]{64}", after)) or (operation == "artifact" and not after):
                raise ContractError("CONTRACT_INVALID")
            scope = (body["tenantScope"], run_id, attempt)
            result = inbox.read(scope, envelope, operation, after)
            if operation == "artifact":
                result = {"manifest": result, "content": artifacts.get(scope, result)}
            return reply(result, 200, envelope, target)
        except (WorkloadAuthError, ContractError, InboxError, ArtifactError) as error:
            code = str(error)
            status = 401 if isinstance(error, WorkloadAuthError) else 404 if code=="RUN_NOT_FOUND" else 429 if code=="RATE_LIMITED" else 409 if code in ("IDEMPOTENCY_CONFLICT", "WORKLOAD_REPLAY") else 422
            return reply({"error": code}, status, envelope, target)
        except Exception:
            return reply({"error": "RUNTIME_UNAVAILABLE"}, 503, envelope, target)

    @app.errorhandler(Exception)
    def error(error):
        return reply({"error": "REQUEST_REJECTED"}, error.code if isinstance(error, HTTPException) else 503)

    return app
