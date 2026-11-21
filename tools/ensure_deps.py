"""Ensure the project-local virtualenv has the current runtime dependencies.

This is called by run.bat. It avoids reinstalling on every launch while still
noticing a changed requirements.txt or a missing runtime import. Only the
standard library is used before dependencies are installed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
STAMP = ROOT / ".venv" / ".requirements.sha256"
MODULES = ("streamlit", "httpx", "dotenv", "PIL", "keyring")


def requirements_hash(path: Path = REQUIREMENTS) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def modules_present(modules: tuple[str, ...] = MODULES) -> bool:
    for module in modules:
        try:
            if importlib.util.find_spec(module) is None:
                return False
        except (ImportError, ValueError):
            return False
    return True


def stamp_matches(digest: str, stamp: Path = STAMP) -> bool:
    try:
        return stamp.read_text(encoding="utf-8").strip() == digest
    except OSError:
        return False


def write_stamp(digest: str, stamp: Path = STAMP) -> None:
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(digest + "\n", encoding="utf-8")


def main() -> int:
    if not REQUIREMENTS.is_file():
        print(f"[ERROR] requirements.txt not found: {REQUIREMENTS}")
        return 1
    digest = requirements_hash()
    if modules_present() and stamp_matches(digest):
        print("Dependencies are current; skipping pip install.")
        return 0
    if not modules_present():
        print("Runtime dependencies are missing; installing from requirements.txt ...")
    else:
        print("requirements.txt changed; refreshing dependencies ...")
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "-r",
        str(REQUIREMENTS),
    ]
    try:
        subprocess.check_call(command)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"[ERROR] Dependency installation failed: {exc}")
        return 1
    write_stamp(requirements_hash())
    print("Dependencies installed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
