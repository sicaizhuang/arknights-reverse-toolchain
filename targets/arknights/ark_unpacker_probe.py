#!/usr/bin/env python3
"""Read-only Arknights Unity bundle probe using the Ark-Unpacker LZ4AK adapter."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import UnityPy

from vendor.ark_unpacker_lz4ak import install_unitypy_patch


SOURCE = {
    "project": "isHarryh/Ark-Unpacker",
    "version": "5.1.0",
    "commit": "8b4101f36bc9ccb283fff272928c3f0b23583980",
    "license": "BSD-3-Clause",
    "implementation": "src/lz4ak/Block.py",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return value[:120] or "unnamed"


def normalize(value: Any) -> Any:
    if isinstance(value, bytes | bytearray | memoryview):
        data = bytes(value)
        result = {
            "$binary_size": len(data),
            "$binary_sha256": hashlib.sha256(data).hexdigest().upper(),
            "$binary_prefix_hex": data[:64].hex().upper(),
        }
        if len(data) <= 4096:
            result["$binary_hex"] = data.hex().upper()
        return result
    if isinstance(value, dict):
        return {str(key): normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def collect_references(value: Any, field: str = "$", output: list[dict] | None = None) -> list[dict]:
    if output is None:
        output = []
    if isinstance(value, dict):
        if "m_FileID" in value and "m_PathID" in value:
            output.append(
                {
                    "field": field,
                    "file_id": int(value.get("m_FileID", 0)),
                    "path_id": str(value.get("m_PathID", 0)),
                }
            )
        for key, item in value.items():
            collect_references(item, f"{field}.{key}", output)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            collect_references(item, f"{field}[{index}]", output)
    return output


def object_name(reader: Any) -> str:
    try:
        name = reader.peek_name()
        return "" if name is None else str(name)
    except Exception:
        return ""


def text_asset_payload(reader: Any) -> bytes:
    value = reader.read().m_Script
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8", errors="surrogateescape")
    raise TypeError(f"Unsupported TextAsset payload type: {type(value).__name__}")


def analyze_bundle(
    path: Path,
    output: Path,
    terms: list[str],
    retained_path_ids: set[str],
) -> tuple[dict, list[dict]]:
    record: dict[str, Any] = {
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "status": "pending",
    }
    object_rows: list[dict] = []
    try:
        environment = UnityPy.load(str(path))
        type_counts: Counter[str] = Counter()
        type_tree_success = 0
        type_tree_failure = 0
        reference_count = 0
        match_count = 0
        tree_dir = output / "typetrees" / safe_name(path.name)

        for reader in environment.objects:
            unity_type = reader.type.name
            name = object_name(reader)
            type_counts[unity_type] += 1
            row: dict[str, Any] = {
                "bundle": str(path),
                "path_id": str(reader.path_id),
                "type": unity_type,
                "name": name,
                "type_tree_status": "not_attempted",
                "references": [],
            }
            tree = None
            try:
                tree = reader.read_typetree()
                row["type_tree_status"] = "read"
                type_tree_success += 1
                normalized = normalize(tree)
                references = collect_references(tree)
                row["references"] = references
                reference_count += len(references)
                searchable = json.dumps(normalized, ensure_ascii=False).casefold()
                matched_terms = sorted({term for term in terms if term.casefold() in searchable or term.casefold() in name.casefold()})
                retained_by_path_id = str(reader.path_id) in retained_path_ids
                if matched_terms or retained_by_path_id:
                    if matched_terms:
                        row["matched_terms"] = matched_terms
                    if retained_by_path_id:
                        row["retained_by_path_id"] = True
                    tree_dir.mkdir(parents=True, exist_ok=True)
                    tree_file = tree_dir / f"{safe_name(unity_type)}_{reader.path_id}_{safe_name(name)}.json"
                    tree_file.write_text(json.dumps(normalized, ensure_ascii=True, indent=2), encoding="utf-8-sig")
                    row["type_tree_file"] = str(tree_file)
            except Exception as exc:
                type_tree_failure += 1
                row["type_tree_status"] = "failed"
                row["type_tree_error"] = f"{type(exc).__name__}: {exc}"
                retained_by_path_id = str(reader.path_id) in retained_path_ids
                if any(term.casefold() in name.casefold() for term in terms) or retained_by_path_id:
                    row["matched_terms"] = sorted({term for term in terms if term.casefold() in name.casefold()})
                    if retained_by_path_id:
                        row["retained_by_path_id"] = True
            if unity_type == "TextAsset" and (row.get("matched_terms") or row.get("retained_by_path_id")):
                try:
                    payload = text_asset_payload(reader)
                    payload_dir = output / "payloads" / safe_name(path.name)
                    payload_dir.mkdir(parents=True, exist_ok=True)
                    payload_file = payload_dir / f"TextAsset_{reader.path_id}_{safe_name(name)}.bytes"
                    payload_file.write_bytes(payload)
                    row.update(
                        {
                            "payload_file": str(payload_file),
                            "payload_size": len(payload),
                            "payload_sha256": hashlib.sha256(payload).hexdigest().upper(),
                        }
                    )
                except Exception as exc:
                    row["payload_error"] = f"{type(exc).__name__}: {exc}"
            if row.get("matched_terms") or row.get("retained_by_path_id"):
                match_count += 1
            object_rows.append(row)

        record.update(
            {
                "status": "parsed",
                "object_count": len(object_rows),
                "type_counts": dict(sorted(type_counts.items())),
                "type_tree_success": type_tree_success,
                "type_tree_failure": type_tree_failure,
                "reference_count": reference_count,
                "matched_object_count": match_count,
                "files": sorted(str(name) for name in environment.files),
            }
        )
    except Exception as exc:
        record.update(
            {
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
    return record, object_rows


def write_manifest(output: Path, files: list[Path]) -> None:
    rows = []
    for path in sorted({item.resolve() for item in files if item.is_file()}, key=lambda item: str(item).casefold()):
        rows.append({"path": str(path), "size": path.stat().st_size, "sha256": sha256_file(path)})
    (output / "sha256_manifest.json").write_text(
        json.dumps({"schema_version": 1, "files": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8-sig",
    )
    text = "".join(f"{row['sha256']}  {row['path']}\n" for row in rows)
    (output / "KEY_SHA256SUMS.txt").write_text(text, encoding="utf-8-sig")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--match",
        action="append",
        default=[],
        help="Case-insensitive term used to retain a matching TypeTree",
    )
    parser.add_argument(
        "--retain-path-id",
        action="append",
        default=[],
        help="Serialized object PathID whose TypeTree must be retained",
    )
    args = parser.parse_args()

    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"Refusing existing output: {output}")
    output.mkdir(parents=True)
    inputs = [path.resolve() for path in args.input]
    for path in inputs:
        if not path.is_file():
            raise SystemExit(f"Input Bundle is missing: {path}")

    terms = args.match or ["ulpia", "skill_02", "s2", "wave", "flow", "shuilang"]
    patch = install_unitypy_patch()
    bundles = []
    all_objects: list[dict] = []
    retained_path_ids = {str(value) for value in args.retain_path_id}
    for path in inputs:
        bundle, rows = analyze_bundle(path, output, terms, retained_path_ids)
        bundles.append(bundle)
        all_objects.extend(rows)

    objects_path = output / "objects.jsonl"
    with objects_path.open("w", encoding="utf-8") as stream:
        for row in all_objects:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    parsed = sum(bundle["status"] == "parsed" for bundle in bundles)
    report = {
        "schema_version": 1,
        "created_utc": utc_now(),
        "status": "completed" if parsed == len(bundles) else "partial" if parsed else "blocked",
        "adapter": "arknights_lz4ak_unitypy",
        "source": SOURCE,
        "unitypy_version": getattr(UnityPy, "__version__", "unknown"),
        "python": sys.version,
        "patch": patch,
        "classification_correction": {
            "old": "flag 4 treated as standard LZHAM",
            "current": "flag 4 decoded as Arknights LZ4AK",
            "effect": "standard LZHAM status codes are not diagnostic for these Bundle blocks",
        },
        "match_terms": terms,
        "retained_path_ids": sorted(retained_path_ids),
        "bundles": bundles,
        "totals": {
            "input_count": len(bundles),
            "parsed_count": parsed,
            "object_count": len(all_objects),
            "matched_object_count": sum("matched_terms" in row for row in all_objects),
            "reference_count": sum(len(row["references"]) for row in all_objects),
        },
        "limits": [
            "A parsed TypeTree/reference is static serialized-data evidence, not a runtime call edge.",
            "A name match does not prove that an object is invoked by Ulpianus skill 2.",
            "No game, ADB, Ghidra, runtime observation, injection, or decryption was used.",
        ],
    }
    report_path = output / "ark_unpacker_probe.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8-sig")
    source_files = [
        Path(__file__).resolve(),
        Path(__file__).resolve().parent / "vendor" / "ark_unpacker_lz4ak.py",
        Path(__file__).resolve().parent / "vendor" / "LICENSE.ARK-UNPACKER.txt",
        objects_path,
        report_path,
    ]
    source_files.extend(output.rglob("typetrees/*.json"))
    source_files.extend(output.rglob("payloads/**/*.bytes"))
    write_manifest(output, source_files)
    print(json.dumps({"status": report["status"], **report["totals"]}, ensure_ascii=False))
    return 0 if report["status"] == "completed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
