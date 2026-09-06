"""Inspect every image layer, including deleted files, plus final runtime config."""
import argparse
import json
import tarfile
from pathlib import PurePosixPath

FORBIDDEN_PARTS = {"frontend", "uploads", "graphify-out", "node_modules", ".git", ".cache", ".pytest_cache"}
APP_ROOTS = {"api", "contracts", "security", "ports", "runtime", "observability", "__init__.py"}


def forbidden(path: str) -> bool:
    # Tar paths may start with ./ or /; never strip dots from hidden filenames.
    path = path.lstrip("/")
    while path.startswith("./"):
        path = path[2:]
    p = PurePosixPath(path)
    if ".." in p.parts:
        return True
    # Observed Debian debconf executable and public CA bundles from the pinned base.
    # These are not a web frontend, application data, or private signing keys.
    if str(p) in {
        "usr/share/debconf/frontend", "usr/lib/ssl/cert.pem",
        "opt/runtime/lib/python3.12/site-packages/pip/_vendor/certifi/cacert.pem",
    }:
        return False
    if FORBIDDEN_PARTS.intersection(p.parts):
        return True
    if p.name.startswith(".env") or p.suffix in {".sqlite", ".sqlite3", ".db", ".key", ".pem"}:
        # System CA certificates are public trust roots, not workload secrets.
        return not str(p).startswith(("etc/ssl/", "usr/share/ca-certificates/", "usr/local/lib/python3.12/site-packages/pip/_vendor/certifi/", "opt/runtime/lib/python3.12/site-packages/certifi/", "opt/runtime/lib/python3.12/site-packages/botocore/cacert.pem"))
    if str(p).startswith("srv/backend/app/") and len(p.parts) > 3:
        return p.parts[3] not in APP_ROOTS or "__pycache__" in p.parts
    return False


def inspect_archive(archive: str):
    violations = 0
    rejected = []
    layers = 0
    with tarfile.open(archive) as image:
        manifest = json.load(image.extractfile("manifest.json"))
        if len(manifest) != 1:
            raise ValueError("one image required")
        config = json.load(image.extractfile(manifest[0]["Config"]))["config"]
        if config.get("User") != "65532:65532":
            raise ValueError("runtime must be non-root")
        allowed_env = {"PATH", "LANG", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED"}
        if any(entry.split("=", 1)[0] not in allowed_env for entry in config.get("Env", [])):
            raise ValueError("image contains unapproved baked environment variables; values withheld")
        for layer in manifest[0]["Layers"]:
            layers += 1
            with tarfile.open(fileobj=image.extractfile(layer), mode="r|*") as files:
                for member in files:
                    if forbidden(member.name):
                        violations += 1
                        if len(rejected) < 20:
                            rejected.append(member.name)
    if violations:
        # These are build-layer paths, never file contents or runtime variables.
        raise ValueError(f"image containment failed: {violations} forbidden layer entries: {json.dumps(rejected)}")
    return {"layersInspected": layers, "forbiddenEntries": 0, "user": config["User"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive")
    args = parser.parse_args()
    print(json.dumps(inspect_archive(args.archive)))
