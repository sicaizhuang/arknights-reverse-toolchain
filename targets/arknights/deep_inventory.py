#!/usr/bin/env python3
"""Resumable object-level inventory for the retained Arknights UnityFS capture."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HEADER_INVENTORY = ROOT / "reports" / "arknights_comprehensive_20260810_164656" / "unity_bundle_inventory.jsonl"
CAPABILITY_MATRIX = ROOT / "reports" / "arknights_animestudio_delivery_20260810_215551" / "animestudio_capability_matrix.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def directory_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8-sig")


def prepare_isolated_tool(source_exe: Path, output: Path) -> tuple[Path, dict]:
    source_dir = source_exe.parent
    target_dir = output / "tool_env" / "AnimeStudio.CLI"
    if not target_dir.exists():
        shutil.copytree(source_dir, target_dir)
    target_exe = target_dir / source_exe.name
    config = target_dir / "AnimeStudio.CLI.dll.config"
    original_config_hash = sha256(source_dir / config.name)
    text = config.read_text(encoding="utf-8-sig")
    changed, count = re.subn(r'(key="minimalAssetMap"\s+value=")True(")', r"\1False\2", text, flags=re.I)
    if count != 1 and 'key="minimalAssetMap" value="False"' not in text:
        raise RuntimeError("Could not set minimalAssetMap=False in the isolated tool configuration")
    config.write_text(changed, encoding="utf-8")
    return target_exe, {
        "source_executable": str(source_exe),
        "source_executable_sha256": sha256(source_exe),
        "isolated_executable": str(target_exe),
        "isolated_executable_sha256": sha256(target_exe),
        "source_config_sha256": original_config_hash,
        "isolated_config": str(config),
        "isolated_config_sha256": sha256(config),
        "minimal_asset_map": False,
    }


def read_7bit_int(stream) -> int:
    value = 0
    shift = 0
    while True:
        byte = stream.read(1)
        if not byte:
            raise EOFError("Unexpected EOF in 7-bit integer")
        current = byte[0]
        value |= (current & 0x7F) << shift
        if not current & 0x80:
            return value
        shift += 7
        if shift > 35:
            raise ValueError("Invalid 7-bit integer")


def read_dotnet_string(stream) -> str:
    length = read_7bit_int(stream)
    return stream.read(length).decode("utf-8", errors="replace")


def parse_cab_map(path: Path) -> tuple[str, list[dict]]:
    entries = []
    with path.open("rb") as stream:
        base_folder = read_dotnet_string(stream)
        count = struct.unpack("<i", stream.read(4))[0]
        for _ in range(count):
            cab = read_dotnet_string(stream)
            rel = read_dotnet_string(stream)
            offset = struct.unpack("<q", stream.read(8))[0]
            dep_count = struct.unpack("<i", stream.read(4))[0]
            deps = [read_dotnet_string(stream) for _ in range(dep_count)]
            entries.append({"cab": cab, "path": rel.replace("\\", "/"), "offset": offset, "dependencies": deps})
    return base_folder, entries


def init_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS bundles (
          id INTEGER PRIMARY KEY,
          relative_path TEXT UNIQUE NOT NULL,
          source_path TEXT NOT NULL,
          extension TEXT NOT NULL,
          size INTEGER NOT NULL,
          sha256 TEXT NOT NULL,
          header_status TEXT,
          parse_status TEXT NOT NULL DEFAULT 'pending',
          failure_reason TEXT,
          asset_count INTEGER NOT NULL DEFAULT 0,
          object_error_count INTEGER NOT NULL DEFAULT 0,
          dependency_count INTEGER NOT NULL DEFAULT 0,
          batch_id INTEGER,
          raw_support TEXT NOT NULL DEFAULT 'tool_implemented_unverified',
          convert_support TEXT NOT NULL DEFAULT 'type_dependent'
        );
        CREATE TABLE IF NOT EXISTS objects (
          id INTEGER PRIMARY KEY,
          bundle_id INTEGER NOT NULL REFERENCES bundles(id),
          name TEXT,
          container TEXT,
          path_id TEXT,
          unity_type TEXT NOT NULL,
          object_hash TEXT,
          object_offset INTEGER,
          raw_support TEXT NOT NULL,
          convert_support TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_objects_bundle ON objects(bundle_id);
        CREATE INDEX IF NOT EXISTS idx_objects_type ON objects(unity_type);
        CREATE INDEX IF NOT EXISTS idx_objects_name ON objects(name);
        CREATE INDEX IF NOT EXISTS idx_objects_container ON objects(container);
        CREATE TABLE IF NOT EXISTS dependencies (
          bundle_id INTEGER NOT NULL REFERENCES bundles(id),
          cab_name TEXT NOT NULL,
          UNIQUE(bundle_id, cab_name)
        );
        CREATE TABLE IF NOT EXISTS object_errors (
          id INTEGER PRIMARY KEY,
          bundle_id INTEGER REFERENCES bundles(id),
          unity_type TEXT,
          path_id TEXT,
          message TEXT NOT NULL,
          batch_id INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS commands (
          batch_id INTEGER PRIMARY KEY,
          mode TEXT NOT NULL,
          input_count INTEGER NOT NULL,
          command_json TEXT NOT NULL,
          started_utc TEXT NOT NULL,
          duration_ms INTEGER NOT NULL,
          exit_code INTEGER,
          timed_out INTEGER NOT NULL,
          stdout_path TEXT NOT NULL,
          stderr_path TEXT NOT NULL,
          map_path TEXT,
          cab_map_path TEXT,
          status TEXT NOT NULL
        );
        """
    )
    return db


def capability_lookup() -> dict[str, dict]:
    matrix = load_json(CAPABILITY_MATRIX)
    return {str(row["type"]): row for row in matrix.get("types", [])}


def support_for_type(unity_type: str, capabilities: dict[str, dict]) -> tuple[str, str]:
    row = capabilities.get(unity_type)
    raw = "supported_current_sample" if unity_type == "AudioClip" else "tool_implemented_unverified"
    if not row:
        return raw, "unverified"
    status = row.get("status", "unverified")
    if status == "supported":
        return raw, "supported_current_sample"
    if status == "supported_with_limits":
        return raw, "failed_current_sample" if unity_type == "AudioClip" else "supported_with_limits"
    if status == "failed_current_sample":
        return raw, "failed_current_sample"
    return raw, "unverified"


def make_staging(stage: Path, records: list[dict]) -> dict[str, dict]:
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    by_staged = {}
    for record in records:
        relative = Path(record["relative_path"].removeprefix("Bundles/"))
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(record["source_path"], target)
        by_staged[str(target.resolve()).lower()] = record
    return by_staged


ERROR_RE = re.compile(
    r"Unable to load object\s+Assets\s+(?P<assets>[^\r\n]+)\s+Path\s+(?P<path>[^\r\n]+)\s+Type\s+(?P<type>[^\r\n]+)\s+PathID\s+(?P<pathid>[^\r\n]+)(?P<message>.*?)(?=\[Error\]|\Z)",
    re.I | re.S,
)


def run_batch(
    db: sqlite3.Connection,
    batch_id: int,
    records: list[dict],
    output: Path,
    tool: Path,
    timeout: int,
    capabilities: dict[str, dict],
    mode: str = "batch",
) -> tuple[set[str], bool]:
    batch_dir = output / "batches" / f"batch_{batch_id:06d}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    stage = output / "staging" / f"batch_{batch_id:06d}"
    staged_lookup = make_staging(stage, records)
    stdout_path = batch_dir / "stdout.txt"
    stderr_path = batch_dir / "stderr.txt"
    map_path = batch_dir / "asset_inventory.json"
    cab_path = batch_dir / "Maps" / "asset_inventory.bin"
    command = [
        str(tool), str(stage), str(batch_dir), "--game", "Arknights",
        "--map_op", "Both", "--map_type", "JSON", "--map_name", "asset_inventory",
    ]
    started = utc_now()
    before = time.monotonic()
    timed_out = False
    exit_code = None
    try:
        result = subprocess.run(command, cwd=batch_dir, capture_output=True, timeout=timeout, check=False)
        exit_code = result.returncode
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = (exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        stderr += f"\nTimed out after {timeout} seconds."
    duration_ms = int((time.monotonic() - before) * 1000)
    stdout_path.write_text(stdout, encoding="utf-8-sig")
    stderr_path.write_text(stderr, encoding="utf-8-sig")

    processed: set[str] = set()
    asset_rows: list[tuple] = []
    dependencies: dict[str, set[str]] = defaultdict(set)
    parse_error = None
    if map_path.exists():
        try:
            payload = load_json(map_path)
            for entry in payload.get("AssetEntries", []):
                source_key = str(Path(entry.get("Source", "")).resolve()).lower()
                record = staged_lookup.get(source_key)
                if not record:
                    continue
                relative_path = record["relative_path"]
                processed.add(relative_path)
                raw_support, convert_support = support_for_type(str(entry.get("Type", "Unknown")), capabilities)
                asset_rows.append((
                    relative_path, entry.get("Name"), entry.get("Container"), str(entry.get("PathID", "")),
                    str(entry.get("Type", "Unknown")), entry.get("Hash"), entry.get("Offset"), raw_support, convert_support,
                ))
        except Exception as exc:
            parse_error = f"AssetMap parse failed: {exc}"

    if cab_path.exists():
        try:
            _, cab_entries = parse_cab_map(cab_path)
            for entry in cab_entries:
                stage_file = (stage / Path(entry["path"])).resolve()
                record = staged_lookup.get(str(stage_file).lower())
                if record:
                    dependencies[record["relative_path"]].update(entry["dependencies"])
        except Exception as exc:
            parse_error = f"{parse_error}; CABMap parse failed: {exc}" if parse_error else f"CABMap parse failed: {exc}"

    command_failed = timed_out or exit_code != 0 or not map_path.exists() or "AssetMap was not build" in stdout or parse_error is not None
    status = "timeout" if timed_out else "failed" if command_failed else "completed"
    db.execute(
        "INSERT OR REPLACE INTO commands VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (batch_id, mode, len(records), json.dumps(command, ensure_ascii=False), started, duration_ms, exit_code,
         int(timed_out), str(stdout_path), str(stderr_path), str(map_path) if map_path.exists() else None,
         str(cab_path) if cab_path.exists() else None, status, ),
    )

    ids = {row[1]: row[0] for row in db.execute("SELECT id, relative_path FROM bundles")}
    counts = Counter(row[0] for row in asset_rows)
    object_errors = defaultdict(list)
    for match in ERROR_RE.finditer(stdout + "\n" + stderr):
        error_path = Path(match.group("path"))
        record = staged_lookup.get(str(error_path.resolve()).lower())
        relative = record["relative_path"] if record else None
        if relative:
            object_errors[relative].append(match)
            db.execute(
                "INSERT INTO object_errors(bundle_id,unity_type,path_id,message,batch_id) VALUES(?,?,?,?,?)",
                (ids[relative], match.group("type").strip(), match.group("pathid").strip(), match.group(0), batch_id),
            )

    for relative, name, container, path_id, unity_type, object_hash, offset, raw_support, convert_support in asset_rows:
        db.execute(
            "INSERT INTO objects(bundle_id,name,container,path_id,unity_type,object_hash,object_offset,raw_support,convert_support) VALUES(?,?,?,?,?,?,?,?,?)",
            (ids[relative], name, container, path_id, unity_type, object_hash, offset, raw_support, convert_support),
        )
    for relative, deps in dependencies.items():
        for dep in sorted(deps):
            db.execute("INSERT OR IGNORE INTO dependencies(bundle_id,cab_name) VALUES(?,?)", (ids[relative], dep))

    if not command_failed:
        for record in records:
            relative = record["relative_path"]
            if relative in processed:
                state = "parsed_with_object_errors" if object_errors[relative] else "parsed"
                db.execute(
                    "UPDATE bundles SET parse_status=?,failure_reason=?,asset_count=?,object_error_count=?,dependency_count=?,batch_id=? WHERE id=?",
                    (state, None, counts[relative], len(object_errors[relative]), len(dependencies[relative]), batch_id, ids[relative]),
                )
    db.commit()
    shutil.rmtree(stage)
    return processed, command_failed


def export_indexes(db: sqlite3.Connection, output: Path, metadata: dict) -> dict:
    objects_path = output / "unity_objects.jsonl"
    bundles_path = output / "bundle_deep_inventory.jsonl"
    with objects_path.open("w", encoding="utf-8") as stream:
        query = """SELECT b.relative_path,o.name,o.container,o.path_id,o.unity_type,o.object_hash,o.object_offset,o.raw_support,o.convert_support
                   FROM objects o JOIN bundles b ON b.id=o.bundle_id ORDER BY b.relative_path,o.id"""
        for row in db.execute(query):
            stream.write(json.dumps(dict(zip(("bundle","name","container","path_id","type","object_hash","offset","raw_support","convert_support"), row)), ensure_ascii=False) + "\n")
    with bundles_path.open("w", encoding="utf-8") as stream:
        query = """SELECT id,relative_path,source_path,extension,size,sha256,header_status,parse_status,failure_reason,asset_count,object_error_count,dependency_count,batch_id,raw_support,convert_support FROM bundles ORDER BY relative_path"""
        columns = ("id","relative_path","source_path","extension","size","sha256","header_status","parse_status","failure_reason","asset_count","object_error_count","dependency_count","batch_id","raw_support","convert_support")
        for row in db.execute(query):
            record = dict(zip(columns, row))
            record["type_counts"] = {key: value for key, value in db.execute("SELECT unity_type,COUNT(*) FROM objects WHERE bundle_id=? GROUP BY unity_type", (record["id"],))}
            record["dependencies"] = [x[0] for x in db.execute("SELECT cab_name FROM dependencies WHERE bundle_id=? ORDER BY cab_name", (record["id"],))]
            del record["id"]
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    status_counts = dict(db.execute("SELECT parse_status,COUNT(*) FROM bundles GROUP BY parse_status"))
    type_counts = dict(db.execute("SELECT unity_type,COUNT(*) FROM objects GROUP BY unity_type ORDER BY unity_type"))
    summary = {
        "schema_version": 1,
        "created_utc": utc_now(),
        "status": "completed" if set(status_counts) <= {"parsed", "parsed_with_object_errors"} else "partial",
        "coverage": {
            "unityfs_total": db.execute("SELECT COUNT(*) FROM bundles").fetchone()[0],
            "ab_count": db.execute("SELECT COUNT(*) FROM bundles WHERE extension='.ab'").fetchone()[0],
            "bin_count": db.execute("SELECT COUNT(*) FROM bundles WHERE extension='.bin'").fetchone()[0],
            "parse_status_counts": status_counts,
            "object_count": db.execute("SELECT COUNT(*) FROM objects").fetchone()[0],
            "object_error_count": db.execute("SELECT COUNT(*) FROM object_errors").fetchone()[0],
            "type_counts": type_counts,
        },
        "count_reconciliation": {
            "prior_unityfs_count": 11731,
            "prior_ab_inventory_count": 11668,
            "difference": 63,
            "explanation": "11,731 counts every UnityFS input: 11,668 .ab files plus 63 UnityFS .bin files under Bundles/anon.",
        },
        "inputs": metadata,
        "outputs": {
            "sqlite": str(output / "unity_deep_inventory.sqlite"),
            "bundles_jsonl": str(bundles_path),
            "objects_jsonl": str(objects_path),
        },
        "limitations": [
            "AssetMap inventories serialized object metadata and does not export bulk payloads.",
            "Dependency names come from AnimeStudio CABMap; unresolved names are retained without guessing a target Bundle.",
            "Raw/Convert support is evidence-qualified per type and does not imply every object instance can be exported.",
            "No runtime address or game behavior was verified.",
        ],
    }
    write_json(output / "unity_deep_inventory.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, default=ROOT / "targets" / "arknights" / "profile.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--batch-timeout", type=int, default=900)
    parser.add_argument("--file-timeout", type=int, default=60)
    parser.add_argument("--disk-limit-bytes", type=int, default=8 * 1024**3)
    parser.add_argument("--max-bundles", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    output = args.output.resolve()
    if output.exists() and not args.resume:
        raise SystemExit(f"Refusing existing output without --resume: {output}")
    output.mkdir(parents=True, exist_ok=True)
    profile = load_json(args.profile)
    if profile.get("profile_id") != "arknights":
        raise SystemExit("Only the Arknights profile is accepted")
    source_tool = Path(profile["assets_adapter"]["tool_path"])
    tool, tool_metadata = prepare_isolated_tool(source_tool, output)
    capabilities = capability_lookup()

    records = []
    with HEADER_INVENTORY.open("r", encoding="utf-8-sig") as stream:
        for line in stream:
            if line.strip():
                item = json.loads(line)
                item["extension"] = Path(item["source_path"]).suffix.lower()
                records.append(item)
    records.sort(key=lambda x: x["relative_path"].lower())
    if args.max_bundles:
        records = records[: args.max_bundles]

    db_path = output / "unity_deep_inventory.sqlite"
    db = init_db(db_path)
    for record in records:
        db.execute(
            "INSERT OR IGNORE INTO bundles(relative_path,source_path,extension,size,sha256,header_status) VALUES(?,?,?,?,?,?)",
            (record["relative_path"], record["source_path"], record["extension"], record["size"], record["sha256"], record.get("status")),
        )
    db.commit()

    pending_paths = {row[0] for row in db.execute("SELECT relative_path FROM bundles WHERE parse_status='pending'")}
    pending = [record for record in records if record["relative_path"] in pending_paths]
    next_batch = (db.execute("SELECT COALESCE(MAX(batch_id),0) FROM commands").fetchone()[0] or 0) + 1
    for start in range(0, len(pending), args.batch_size):
        if directory_size(output) >= args.disk_limit_bytes:
            db.execute("UPDATE bundles SET parse_status='blocked_disk_limit',failure_reason='Inventory output reached configured disk limit' WHERE parse_status='pending'")
            db.commit()
            break
        group = pending[start : start + args.batch_size]
        processed, failed = run_batch(db, next_batch, group, output, tool, args.batch_timeout, capabilities)
        next_batch += 1
        unresolved = group if failed else [row for row in group if row["relative_path"] not in processed]
        for record in unresolved:
            if directory_size(output) >= args.disk_limit_bytes:
                db.execute("UPDATE bundles SET parse_status='blocked_disk_limit',failure_reason='Inventory output reached configured disk limit' WHERE relative_path=?", (record["relative_path"],))
                db.commit()
                continue
            single_processed, single_failed = run_batch(db, next_batch, [record], output, tool, args.file_timeout, capabilities, mode="single_file_fallback")
            next_batch += 1
            if single_failed or record["relative_path"] not in single_processed:
                reason = "Single-file fallback timed out or produced no valid AssetMap entry"
                db.execute("UPDATE bundles SET parse_status='failed',failure_reason=?,batch_id=? WHERE relative_path=?", (reason, next_batch - 1, record["relative_path"]))
                db.commit()
        checkpoint = {
            "schema_version": 1,
            "updated_utc": utc_now(),
            "completed_or_terminal": db.execute("SELECT COUNT(*) FROM bundles WHERE parse_status<>'pending'").fetchone()[0],
            "total": len(records),
            "next_batch_id": next_batch,
            "resume_command": f'"{sys.executable}" "{Path(__file__).resolve()}" --output "{output}" --resume',
        }
        write_json(output / "checkpoint.json", checkpoint)

    metadata = {
        "profile": str(args.profile.resolve()),
        "profile_sha256": sha256(args.profile.resolve()),
        "header_inventory": str(HEADER_INVENTORY),
        "header_inventory_sha256": sha256(HEADER_INVENTORY),
        "capability_matrix": str(CAPABILITY_MATRIX),
        "capability_matrix_sha256": sha256(CAPABILITY_MATRIX),
        "tool": tool_metadata,
        "batch_size": args.batch_size,
        "batch_timeout_seconds": args.batch_timeout,
        "single_file_timeout_seconds": args.file_timeout,
        "disk_limit_bytes": args.disk_limit_bytes,
    }
    summary = export_indexes(db, output, metadata)
    db.close()
    return 0 if summary["status"] == "completed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
