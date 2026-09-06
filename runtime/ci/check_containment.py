"""Fail closed on Git-tracked private/derived families, without logging contents."""
import subprocess
from pathlib import PurePosixPath


def forbidden(path: str) -> bool:
    p = PurePosixPath(path)
    return (
        any(part in {"graphify-out", "uploads", "logs", "__pycache__", ".pytest_cache", ".venv", "node_modules"} for part in p.parts)
        or p.suffix.lower() in {".sqlite", ".sqlite3", ".db", ".pyc", ".pem", ".key"}
        or (p.name.startswith(".env") and p.name != ".env.example")
    )


if __name__ == "__main__":
    paths = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")
    count = sum(forbidden(p) for p in paths if p)
    if count:
        raise SystemExit(f"G0 blocked: {count} private/derived tracked paths; contents and paths withheld")
    print("G0 current-tree containment: no forbidden tracked paths (history is a separate scope)")
