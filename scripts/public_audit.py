#!/usr/bin/env python3
"""Reject private data and generated artifacts from the public boundary."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT_NAMES = {".git", ".venv", "venv", "__pycache__", "bin", "obj", "Library", "Logs", "UserSettings"}
FORBIDDEN_PARTS = {"captures", "evidence", "exports", "reports", "work", "inputs", "outputs"}
PRIVATE_MARKERS = ("C:\\Users\\", "D:\\Arknights_Reverse_Toolchain", "generated.7z")
SECRET_PATTERNS = (re.compile(r"(?i)(api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]"), re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"))
TEXT_EXTS = {".py", ".ps1", ".psm1", ".md", ".json", ".txt", ".cs", ".csproj", ".sln", ".yml", ".yaml", ".toml"}


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    findings: list[str] = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if rel.as_posix() == "scripts/public_audit.py":
            continue
        if any(part in ROOT_NAMES for part in rel.parts):
            continue
        if path.is_file() and path.stat().st_size > 50 * 1024 * 1024:
            findings.append(f"large file: {rel}")
        if any(part in FORBIDDEN_PARTS for part in rel.parts):
            findings.append(f"private/generated directory: {rel}")
        if path.is_file() and path.suffix.casefold() in TEXT_EXTS:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError as exc:
                findings.append(f"unreadable: {rel}: {exc}")
                continue
            for marker in PRIVATE_MARKERS:
                if marker.casefold() in text.casefold():
                    findings.append(f"private marker {marker}: {rel}")
            for pattern in SECRET_PATTERNS:
                if pattern.search(text):
                    findings.append(f"secret-like pattern: {rel}")
    if findings:
        print("PUBLIC AUDIT FAILED")
        print("\n".join(sorted(set(findings))))
        return 2
    print(f"PUBLIC AUDIT PASSED: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
