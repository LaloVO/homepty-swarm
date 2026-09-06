import importlib.util
from pathlib import Path


def load(name):
    path = Path(__file__).resolve().parents[3] / "runtime" / "ci" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_current_tree_rejects_derived_secrets_and_databases():
    check = load("check_containment").forbidden
    for path in ("graphify-out/cache/a.json", "backend/uploads/a.pdf", "backend/logs/a.txt", ".env.production", "x/state.sqlite3", "x/private.key"):
        assert check(path)
    for path in ("backend/app/runtime/settings.py", ".env.example", "runtime/requirements.lock.txt"):
        assert not check(path)


def test_layer_gate_includes_hidden_and_deleted_layer_families():
    check = load("inspect_image").forbidden
    for path in ("srv/frontend/app.js", "srv/backend/uploads/file", "root/.cache/pip/x", "srv/.env", "srv/backend/app/services/legacy.py", "srv/backend/app/runtime/__pycache__/x.pyc"):
        assert check(path)
    for path in ("srv/backend/app/runtime/settings.py", "etc/ssl/certs/ca-certificates.crt", "etc/ssl/cert.pem", "opt/runtime/lib/python3.12/site-packages/certifi/cacert.pem"):
        assert not check(path)
