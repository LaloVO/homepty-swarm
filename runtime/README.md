# Private Railway runtime

This is the deployable dependency boundary. The images do not install the research
`backend/pyproject.toml` or OASIS/CAMEL/Torch. Their legacy vulnerabilities remain in
`backend/dependency-audit.json`: isolation is not a claim that those packages were fixed.

## Verification

Requires uv, Python 3.12 and PostgreSQL tools (`initdb`, `pg_ctl`). These tests create their
own disposable PostgreSQL Unix-socket cluster and S3 emulator, with synthetic data only.

```sh
uv sync --project runtime --frozen
uv pip check --python runtime/.venv/bin/python
PYTHONPATH=backend runtime/.venv/bin/pytest backend/tests/runtime backend/tests/security -q
```

Set `HPY143_BRAIN_CHECKOUT` to the explicit Brain checkout to run the real Python HTTP /
TypeScript adapter / Brain reducer replay tests, including the actual S1 domain over
private HTTP and active cancellation. Without it, four cross-repository tests are
reported skipped. The numerical provider is real Brain code, with synthetic input;
no developer database, Railway environment or Suite data is used.

Images install the hashed production export, not a fresh resolution:

```sh
uv export --project runtime --frozen --no-dev --no-emit-project --format requirements-txt --output-file runtime/requirements.lock.txt
uvx pip-audit --require-hashes --disable-pip -r runtime/requirements.lock.txt
```

Python 3.12.14 Alpine 3.24 is pinned by its registry manifest digest. The former Bookworm
candidate was rejected by CI for OS HIGH/CRITICAL vulnerabilities, including unfixed
ones; no scan exception was added. Native musllinux wheels must be in the existing
hashed lock and install without compilation. The actual container tests revalidate
PostgreSQL, S3, process restart and transport on this base. A dependency audit
does not replace the image/OS scan. CI builds both images and scans their SBOMs; it does
not build containers on developer machines. The release workflow tests and inspects both
images, generates image SBOMs, scans vulnerabilities/secrets and publishes the exact
tested bytes to GHCR only after every gate passes. `approved-release-digests` records
immutable image references and the source/Actions run; Railway must use those references,
never `latest`, a rebuilt image or an unverified tag.

G0 containment (owner authorization 2026-09-06): the 195 tracked Graphify derivatives
are removed from the current Git tree; local ignored copies are preserved. CI rejects
reintroduction of derived, uploaded, cached, database and secret-file families. The build
context is deny-by-default. This does not erase historical commits or authorize real
customer data: historical public remediation remains a separate, explicitly scoped action.

## Required Railway bindings

| Variable | Constraint |
| --- | --- |
| `SWARM_ENV` | preview / qa / production; local only for fixtures |
| `SWARM_INBOX_URL` | Dedicated PostgreSQL, never Suite credentials |
| `SWARM_WORKLOAD_KEYS` | JSON keyId-to-base64-secret; current + optional previous, >=32 bytes |
| `SWARM_ARTIFACT_ENDPOINT` | HTTPS S3-compatible endpoint |
| `SWARM_ARTIFACT_BUCKET` | Private dedicated bucket |
| `SWARM_ARTIFACT_ACCESS_KEY`, `SWARM_ARTIFACT_SECRET_KEY` | Bucket-scoped credentials |
| `SWARM_ARTIFACT_REGION` | Provider region; default auto |
| `SWARM_ARTIFACT_ADDRESSING_STYLE` | `path` (legacy/default) or `virtual`; use the created Railway bucket's declared URL style |
| `SWARM_SOURCE_COMMIT` | Exact published 40-character source commit |
| `SWARM_IMAGE_DIGEST` | Exact deployed sha256 digest |
| `SWARM_ALLOWED_PROJECTION_POLICIES` | Nonempty JSON array of Brain-approved versions |
| `SWARM_CAPABILITY_PROVIDERS` | Explicit versioned allowlist; empty means no capabilities |
| `SWARM_MAX_CONCURRENCY` | 1..64, default 2 |
| `SWARM_LEASE_SECONDS` | 3..120, default 30 |
| `SWARM_HARD_DEADLINE_SECONDS` | 1..3600, default 900 |
| `SWARM_MAX_DELIVERIES` | 1..10, default 3 |

Brain: `SWARM_RUNTIME_ENABLED=false` by default, `SWARM_RUNTIME_URL`,
`SWARM_WORKLOAD_KEY_ID`, `SWARM_WORKLOAD_SECRET_BASE64`, `SWARM_RUNTIME_TIMEOUT_MS`,
`SWARM_RUNTIME_MAX_ATTEMPTS`. Enabling transport does not enable the capability registry
or automatic dispatch, and does not satisfy a release gate.

Deployment requires owner approval. Use `railway.api.json` and `railway.worker.json` as
separate config paths. Do not create public domains or deploy the mixed legacy Dockerfile.
Infrastructure must restrict egress to the dedicated inbox, S3 and approved providers;
application URL checks are not a network firewall.

Migrate the dedicated inbox explicitly before readiness (never startup DDL):

```sh
PYTHONPATH=backend runtime/.venv/bin/python -m app.ports.job_inbox --migrate
```

The inbox schema is an unreleased V1 baseline; no deployed schema upgrade is implied.
`/health/live` proves process liveness. `/health/ready` verifies inbox schema and bucket
access, reporting `execution:gated` with an empty registry. `/source` gives the configured
exact source commit and image digest. The worker has no public listener.

## Provider contract and gates

Bindings map a query capabilityRef or `step:<stepType>` to
`{url,cancelUrl,version,secret,produces}`. Only private Railway hosts are accepted outside local.
`cancelUrl` is required outside local and must share the provider's origin.
No provider is selected by default. `project_context` checkpoints the authorized snapshot;
it does not create or fetch a second business graph.

POST input: `homepty-step-request.v1`, runId, attempt, tenantScope, snapshot, compiled
step, dependency content, expiresAt. `Idempotency-Key` is the protocol hash of
`[tenantScope,runId,attempt,stepKey]`. The signature is HMAC-SHA256 of that key, newline,
and RFC8785 body bytes. A successful response signs the same key, newline and canonical
response bytes. It contains exactly:

```text
contractVersion: homepty-step-result.v1
producerVersion: exact registered version
outputs: exact declared step outputs
kind: findings | metrics | run_graph | structured_report
content: JSON artifact proposal
findings: FindingV1[] scoped to snapshot entities and evidence
usage: {tokens, costUsd} within the prior reservation
```

No ambient proxies, redirects, arbitrary job URLs, compressed/unbounded responses or
unsigned results are accepted. The child receives only the provider secret, not inbox/S3
credentials. Providers must independently enforce idempotency, budgets and deadlines.

SIGTERM stops claims and kills local provider children. Durable `remote_pending` tracks
calls whose completion is unconfirmed. Killing an HTTP client cannot prove remote work
stopped. The worker requests signed `homepty-step-cancel.v1` receipts from Brain's private
cancel endpoint after local children stop. Only `released` or `never_started` proofs clear
the pending call. Expiry, a missing receipt, or an unreleased predecessor generation
preserve `cancelled_with_orphan_check_pending`, never a clean ACK.

The owner-approved HPY-113 amendment serializes typed S1 baseline/intervention inputs;
admission validates subject, cutoff, bundle semantics and explicit provider bindings.
Missing inputs still fail closed. The real domain provider lives only in Brain: canonical
S1 algorithms, merge, Run Graph/clustering, structured report and A13. No proprietary
domain implementation is copied here. The generic production capability registry stays
gated; synthetic results never establish market forecasting accuracy or causal effects.

For exact bindings, Brain migration, durable volume topology, input provisioning,
artifact access and rollback, see Brain's `harness/docs/runbooks/hpy143-private-domain.md`.

## Durable operational gauges

`PYTHONPATH=backend runtime/.venv/bin/python -m app.observability.metrics` emits Prometheus
text for an internal collector, without opening an HTTP route. It reads durable inbox
states/age, live leases, active redeliveries, reservations, remote pending calls,
checkpoint counts/bytes, DLQ and checksum conflicts. No actor/tenant/run/subject labels.
Reservation cost is not billed cost. Collector/dashboard installation and Railway
restart/OOM/billing signals must be verified in the authorized environment.

## Release / rollback

Disable new Brain submissions first, request cancellation, and account for all missing
remote acknowledgements before declaring a clean drain. Reuse the inbox and bucket after
restart. Never erase a DLQ to hide failed jobs, replay changed content under an old attempt,
or promote unverified orphan objects.

Real data remains gated on derived-file containment, Corresponding Source publication,
actual Railway backup/restore and budgets, image scan, security/retention review and
release approval. Domain adapters, cancellation proofs, Brain dispatch/event pump and
canonical artifact/Run Graph integration now have local execution evidence, not Railway
deployment or production-validation evidence.
