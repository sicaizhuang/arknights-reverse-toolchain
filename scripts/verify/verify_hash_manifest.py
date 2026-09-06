#!/usr/bin/env python3
"""Verify an existing SHA-256 manifest without changing its referenced files."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    manifest_path = args.manifest.resolve()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"refusing to overwrite {output}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    records = []
    for entry in manifest.get("files", []):
        path = Path(entry["path"])
        exists = path.is_file()
        actual = sha256(path) if exists else None
        records.append({
            "path": str(path),
            "expected_sha256": entry.get("sha256"),
            "actual_sha256": actual,
            "exists": exists,
            "status": "passed" if exists and actual == entry.get("sha256") else "missing" if not exists else "mismatch",
        })
    missing = sum(item["status"] == "missing" for item in records)
    mismatches = sum(item["status"] == "mismatch" for item in records)
    report = {
        "schema_version": 1,
        "scope": "hash_manifest_verification",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "manifest": {"path": str(manifest_path), "sha256": sha256(manifest_path)},
        "file_count": len(records),
        "missing_count": missing,
        "mismatch_count": mismatches,
        "status": "passed" if missing == 0 and mismatches == 0 else "failed",
        "records": records,
        "read_only": True,
    }
    output.mkdir(parents=True)
    report_path = output / "hash_validation.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (output / "KEY_SHA256SUMS.txt").write_text(
        f"{sha256(manifest_path)}  {manifest_path}\n{sha256(report_path)}  {report_path}\n",
        encoding="ascii",
    )
    return 0 if report["status"] == "passed" else 7


if __name__ == "__main__":
    raise SystemExit(main())
