#!/usr/bin/env python3
"""Merge Phase 1 AnimeStudio CAB maps without reparsing target bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from datetime import datetime, timezone
from pathlib import Path


def read_7bit(stream) -> int:
    value = 0
    shift = 0
    while shift < 35:
        raw = stream.read(1)
        if not raw:
            raise EOFError("truncated 7-bit integer")
        byte = raw[0]
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value
        shift += 7
    raise ValueError("invalid 7-bit integer")


def read_string(stream) -> str:
    return stream.read(read_7bit(stream)).decode("utf-8")


def write_7bit(stream, value: int) -> None:
    while value >= 0x80:
        stream.write(bytes(((value & 0x7F) | 0x80,)))
        value >>= 7
    stream.write(bytes((value,)))


def write_string(stream, value: str) -> None:
    raw = value.encode("utf-8")
    write_7bit(stream, len(raw))
    stream.write(raw)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def parse_map(path: Path) -> tuple[str, list[dict]]:
    with path.open("rb") as stream:
        base = read_string(stream)
        count = struct.unpack("<i", stream.read(4))[0]
        entries = []
        for _ in range(count):
            cab = read_string(stream)
            relative = read_string(stream).replace("\\", "/")
            offset = struct.unpack("<q", stream.read(8))[0]
            dep_count = struct.unpack("<i", stream.read(4))[0]
            dependencies = [read_string(stream) for _ in range(dep_count)]
            entries.append({"cab": cab, "path": relative, "offset": offset, "dependencies": dependencies})
        if stream.read(1):
            raise ValueError("unexpected trailing CAB map bytes")
    return base, entries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory-root", required=True, type=Path)
    parser.add_argument("--bundle-root", required=True, type=Path)
    parser.add_argument("--output-map", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()

    inventory_root = args.inventory_root.resolve()
    bundle_root = args.bundle_root.resolve()
    map_files = sorted(inventory_root.glob("batches/batch_*/Maps/asset_inventory.bin"))
    evidence = []
    merged: dict[str, dict] = {}
    duplicate_same = 0
    conflicts = []
    missing_paths = []

    for map_path in map_files:
        base, entries = parse_map(map_path)
        evidence.append({
            "path": str(map_path),
            "sha256": sha256(map_path),
            "declared_base": base,
            "entry_count": len(entries),
        })
        for entry in entries:
            normalized = entry["path"].lstrip("/")
            entry["path"] = normalized
            existing = merged.get(entry["cab"])
            if existing is not None:
                if existing == entry:
                    duplicate_same += 1
                else:
                    conflicts.append({"cab": entry["cab"], "first": existing, "second": entry})
                continue
            merged[entry["cab"]] = entry
            if not (bundle_root / Path(normalized)).is_file():
                missing_paths.append(normalized)

    status = "completed" if map_files and not conflicts and not missing_paths else "failed"
    args.output_map.parent.mkdir(parents=True, exist_ok=True)
    if status == "completed":
        with args.output_map.open("wb") as stream:
            write_string(stream, str(bundle_root))
            stream.write(struct.pack("<i", len(merged)))
            for cab in sorted(merged):
                entry = merged[cab]
                write_string(stream, cab)
                write_string(stream, entry["path"].replace("/", "\\"))
                stream.write(struct.pack("<q", int(entry["offset"])))
                dependencies = list(entry["dependencies"])
                stream.write(struct.pack("<i", len(dependencies)))
                for dependency in dependencies:
                    write_string(stream, dependency)

    report = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "method": "merge_existing_phase1_cab_maps_no_bundle_reparse",
        "inventory_root": str(inventory_root),
        "bundle_root": str(bundle_root),
        "source_map_count": len(map_files),
        "source_entry_count": sum(item["entry_count"] for item in evidence),
        "merged_entry_count": len(merged),
        "duplicate_identical_count": duplicate_same,
        "conflict_count": len(conflicts),
        "missing_bundle_path_count": len(missing_paths),
        "conflicts": conflicts[:50],
        "missing_bundle_paths": missing_paths[:200],
        "output_map": str(args.output_map) if status == "completed" else None,
        "output_map_sha256": sha256(args.output_map) if status == "completed" else None,
        "source_maps": evidence,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8-sig")
    print(json.dumps({key: report[key] for key in (
        "status", "source_map_count", "source_entry_count", "merged_entry_count",
        "conflict_count", "missing_bundle_path_count", "output_map_sha256",
    )}, ensure_ascii=False))
    return 0 if status == "completed" else 4


if __name__ == "__main__":
    raise SystemExit(main())
