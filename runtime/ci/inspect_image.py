"""Inspect every image layer, including deleted files, plus final runtime config."""
import argparse
import json
import tarfile
from pathlib import PurePosixPath

FORBIDDEN_PARTS = {"frontend", "uploads", "graphify-out", "node_modules", ".git", ".cache", ".pytest_cache"}
APP_ROOTS = {"api", "contracts", "security", "ports", "runtime", "observability", "__init__.py"}


def forbidden(path: str) -> bool:
    p = PurePosixPath(path.lstrip("./"))
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
    layers = 0
    with tarfile.open(archive) as image:
        manifest = json.load(image.extractfile("manifest.json"))
        if len(manifest) != 1:
            raise ValueError("one image required")
        config = json.load(image.extractfile(manifest[0]["Config"]))["config"]
        if config.get("User") != "65532:65532":
            raise ValueError("runtime must be non-root")
        for layer in manifest[0]["Layers"]:
            layers += 1
            with tarfile.open(fileobj=image.extractfile(layer), mode="r|*") as files:
                for member in files:
                    violations += int(forbidden(member.name))
    if violations:
        raise ValueError(f"image containment failed: {violations} forbidden layer entries (paths withheld)")
    return {"layersInspected": layers, "forbiddenEntries": 0, "user": config["User"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive")
    args = parser.parse_args()
    print(json.dumps(inspect_archive(args.archive)))
