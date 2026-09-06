#!/usr/bin/env python3
"""Build one operator-scoped export directory from retained Arknights Bundles.

The pack is an aggregation layer over the existing AnimeStudio adapter.  It
does not infer runtime ownership from a filename: direct Bundles are exported
under a stable category, while shared libraries are recorded as candidates
until a serialized/config edge proves the exact object.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE = ROOT / "targets" / "arknights" / "profile.json"
EXACT_CLOSURE = ROOT / "targets" / "arknights" / "operator_resource_closure.py"
DIRECT_TYPES = [
    "Texture2D",
    "Sprite",
    "TextAsset",
    "AnimationClip",
    "AudioClip",
    "Mesh",
    "Material",
    "Shader",
    "MonoBehaviour",
    "Animator",
    "GameObject",
    "VideoClip",
    "Font",
]

def annotation_index() -> dict[str, str]:
    return {}


def classify_exported_asset(relative_path: str, unity_type: str) -> str:
    """Place one adapter output in a stable operator-facing resource class."""
    path = relative_path.replace("\\", "/")
    lower = path.casefold()
    kind = unity_type.casefold()
    suffix = Path(path).suffix.casefold()
    if "audio" in kind or suffix in {".ogg", ".wav", ".mp3", ".acb", ".awb"}:
        return "audio"
    if "/arts/characters/" in lower or "illust_" in lower or "/spritepack/" in lower:
        return "portrait"
    if "/arts/ui/" in lower or lower.startswith(("sprite/", "texture2d/")):
        return "shared_ui"
    if "/battle/[pack]common/" in lower:
        return "shared_battle_common"
    if "/battle/prefabs/skins/character/" in lower and suffix == ".json":
        return "spine"
    if suffix in {".atlas", ".skel", ".reanim"} or "skeletondata" in lower:
        return "spine"
    if kind in {"animationclip", "animation", "animator"} or "animation" in lower:
        return "animation"
    if kind in {"material", "shader", "mesh", "meshfilter", "meshrenderer"} or suffix in {".shader", ".mat", ".obj"}:
        return "render_dependencies"
    if suffix in {".bytes", ".bin", ".txt"} and ("gamedata" in lower or "config" in lower):
        return "combat_config"
    if "gamedata/" in lower or "/config/" in lower:
        return "combat_config"
    if "/avg/" in lower or lower.startswith("avg/") or "/story/" in lower:
        return "story"
    if "particle" in kind or "/effects/" in lower:
        return "effects"
    if suffix == ".png" and "/battle/" in lower:
        return "battle_textures"
    if kind in {"monobehaviour", "gameobject", "transform", "recttransform", "boxcollider2d", "sprite", "texture2d"}:
        return "serialized_objects"
    return "other"


def lookup_annotation(annotations: dict[str, str], key: str) -> str | None:
    """Use exact or longest parent annotation from the annotation JSON."""
    normalized = key.replace("\\", "/")
    if normalized in annotations:
        return annotations[normalized]
    parents = [
        (prefix, value)
        for prefix, value in annotations.items()
        if normalized.startswith(prefix.rstrip("/") + "/")
    ]
    return max(parents, key=lambda item: len(item[0]))[1] if parents else None


def build_catalog(output: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Create one stable, deduplicated view over all direct Bundle exports."""
    catalog_root = output / "catalog"
    catalog_root.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    seen_hashes: dict[str, Path] = {}
    for record in records:
        report_path = record.get("report")
        if not report_path or not Path(report_path).is_file():
            continue
        try:
            report = load_json(Path(report_path))
        except (OSError, json.JSONDecodeError):
            continue
        files = report.get("output", {}).get("files", []) if isinstance(report, dict) else []
        is_config_probe = Path(report_path).name == "ark_unpacker_probe.json"
        if is_config_probe:
            export_root = Path(str(record.get("output", "")))
            files = []
            if export_root.is_dir():
                for source in export_root.rglob("*"):
                    if not source.is_file() or source.name in {"sha256_manifest.json", "KEY_SHA256SUMS.txt"}:
                        continue
                    relative = source.relative_to(export_root).as_posix()
                    if relative.startswith("payloads/"):
                        unity_type = "TextAsset"
                    elif relative.startswith("typetrees/"):
                        unity_type = "configuration_typetree"
                    elif relative.startswith("raw/"):
                        unity_type = "configuration_bundle_raw"
                    elif source.name == "objects.jsonl":
                        unity_type = "configuration_object_index"
                    else:
                        unity_type = "configuration_probe_report"
                    files.append(
                        {
                            "output_path": str(source),
                            "relative_path": relative,
                            "valid": True,
                            "size": source.stat().st_size,
                            "sha256": sha256(source),
                            "unity_type": unity_type,
                            "source_attribution": "inventory_container_edge_config_export",
                        }
                    )
        for item in files:
            if not isinstance(item, dict) or not item.get("valid"):
                continue
            source = Path(str(item.get("output_path", "")))
            if not source.is_file():
                continue
            relative = str(item.get("relative_path", source.name)).replace("\\", "/")
            unity_type = str(item.get("unity_type", "unknown"))
            resource_class = "combat_config" if is_config_probe else classify_exported_asset(relative, unity_type)
            file_hash = str(item.get("sha256") or sha256(source)).upper()
            canonical_name = safe_name(relative)
            destination = catalog_root / resource_class / canonical_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                # Same relative names can occur in separate Bundle exports.
                stem = destination.stem
                destination = destination.with_name(f"{stem}__{file_hash[:12]}{destination.suffix}")
            link_mode = "hardlink"
            try:
                os.link(source, destination)
            except OSError:
                shutil.copy2(source, destination)
                link_mode = "copy"
            first = seen_hashes.get(file_hash)
            if first is None:
                seen_hashes[file_hash] = destination
            rows.append(
                {
                    "catalog_path": str(destination),
                    "resource_class": resource_class,
                    "relative_path": relative,
                    "unity_type": unity_type,
                    "size": destination.stat().st_size,
                    "sha256": file_hash,
                    "source_bundle": item.get("source_bundle") or record.get("source_path"),
                    "source_bundle_sha256": record.get("source_sha256"),
                    "source_attribution": item.get("source_attribution", "direct_operator_bundle"),
                    "link_mode": link_mode,
                    "same_content_as": str(first) if first else None,
                    "evidence": "inventory_container_edge_config_export" if is_config_probe else "direct_operator_bundle_export",
                }
            )
    counts = Counter(row["resource_class"] for row in rows)
    index = {
        "schema_version": 1,
        "method": "direct_bundle_export_catalog",
        "deduplication": "same SHA-256 is represented by hardlinked files; logical entries remain separate",
        "file_count": len(rows),
        "unique_sha256_count": len(seen_hashes),
        "class_counts": dict(sorted(counts.items())),
        "files": rows,
    }
    index_path = output / "catalog_index.json"
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8-sig")
    (catalog_root / "README.md").write_text(
        "\n".join(
            [
                "# Operator resource catalog",
                "",
                "This directory is a stable view over all direct operator Bundle exports.",
                "Each file keeps its original relative path in `catalog_index.json`; files with the same SHA-256 are hardlinked when possible.",
                "",
                "Classes: `portrait`, `spine`, `animation`, `battle_textures`, `render_dependencies`, `audio`, `combat_config`, `effects`, `serialized_objects`, `story`, `other`.",
            ]
        ),
        encoding="utf-8-sig",
    )
    return index


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def safe_name(value: str) -> str:
    value = value.replace("/", "_").replace("\\", "_")
    value = re.sub(r"[^0-9A-Za-z._-]+", "_", value)
    return value.strip("._") or "bundle"


def category(relative: str) -> str:
    lower = relative.casefold()
    if "/audio/" in f"/{lower}" or lower.startswith("audio/"):
        return "voice"
    if "/avg/" in f"/{lower}" or lower.startswith("avg/"):
        return "story"
    if lower.startswith("skinpack/"):
        return "skin"
    if lower.startswith("chararts/") or lower.startswith("charpack/"):
        return "battle_spine"
    if lower.startswith("spritepack/"):
        return "portrait"
    if lower.startswith("config/"):
        return "combat_config"
    return "operator_direct"


def relative_to_bundle(path: Path, bundle_root: Path) -> str:
    return path.resolve().relative_to(bundle_root.resolve()).as_posix()


def read_bundle_inventory(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def inventory_operator_bundles(inventory_root: Path | None, token: str) -> dict[str, dict[str, Any]]:
    """Find source Bundles whose serialized object container names mention the exact operator."""
    if inventory_root is None:
        return {}
    bundle_rows = read_bundle_inventory(inventory_root / "bundle_deep_inventory.jsonl")
    by_relative = {}
    for row in bundle_rows:
        relative = str(row.get("relative_path", "")).replace("\\", "/")
        if relative.casefold().startswith("bundles/"):
            relative = relative.split("/", 1)[1]
        if relative:
            by_relative[relative] = row
    matches: dict[str, dict[str, Any]] = {}
    for path in sorted(inventory_root.glob("batches/batch_*/asset_inventory.json")):
        try:
            payload = load_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        entries = payload.get("AssetEntries", []) if isinstance(payload, dict) else []
        selected = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            haystack = " ".join(str(entry.get(key, "")) for key in ("Name", "Container", "Source")).casefold()
            if token in haystack:
                source_text = str(entry.get("Source", "")).replace("\\", "/")
                staged_match = re.search(r"/staging/batch_\d+/(.+)$", source_text, flags=re.IGNORECASE)
                staged_relative = staged_match.group(1) if staged_match else ""
                bundle = by_relative.get(staged_relative)
                if bundle:
                    selected.append((entry, bundle))
        if not selected:
            continue
        # A batch contains many Bundles, so group matches by their exact staged path.
        grouped: dict[str, list[dict[str, Any]]] = {}
        bundle_by_key: dict[str, dict[str, Any]] = {}
        for entry, bundle in selected:
            relative = str(bundle.get("relative_path", "")).replace("\\", "/")
            if relative.casefold().startswith("bundles/"):
                relative = relative.split("/", 1)[1]
            grouped.setdefault(relative, []).append(entry)
            bundle_by_key[relative] = bundle
        for relative, selected_entries in grouped.items():
            bundle = bundle_by_key[relative]
            source = Path(str(bundle.get("source_path", ""))).resolve()
            row = matches.setdefault(
                relative,
                {
                    "source_path": str(source),
                    "relative_path": relative,
                    "source_sha256": bundle.get("sha256"),
                    "batch_id": bundle.get("batch_id"),
                    "selection": "inventory_container_edge",
                    "inventory_match_count": 0,
                    "inventory_match_samples": [],
                    "inventory_types": set(),
                },
            )
            row["inventory_match_count"] += len(selected_entries)
            row["inventory_types"].update(str(item.get("Type", "unknown")) for item in selected_entries)
            for item in selected_entries:
                if len(row["inventory_match_samples"]) >= 8:
                    break
                row["inventory_match_samples"].append(
                    {
                        "name": item.get("Name"),
                        "container": item.get("Container"),
                        "type": item.get("Type"),
                        "path_id": item.get("PathID", item.get("PathId")),
                    }
                )
    for row in matches.values():
        row["inventory_types"] = sorted(row["inventory_types"])
        if any("gamedata/" in str(sample.get("container", "")).casefold() for sample in row["inventory_match_samples"]):
            row["category"] = "combat_config"
    return matches


def find_inventory_root(profile: dict[str, Any]) -> Path | None:
    configured = profile.get("assets_adapter", {}).get("dependency_map_sources", {}).get("phase1_inventory_root")
    if configured:
        candidate = Path(str(configured))
        if (candidate / "bundle_deep_inventory.jsonl").is_file():
            return candidate
    for candidate in sorted((ROOT / "reports").glob("arknights_unity_deep_inventory_*"), reverse=True):
        if (candidate / "bundle_deep_inventory.jsonl").is_file():
            return candidate
    return None


def shared_candidates(inventory_root: Path | None, token: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "projectile_library": [],
        "effect_library": [],
        "operator_named_objects": [],
        "limitations": [
            "Shared candidates are serialized inventory evidence, not proven runtime ownership.",
            "A projectile/effect is included as exact only after a config or PPtr edge resolves its object.",
        ],
    }
    if inventory_root is None:
        return result

    bundle_rows = read_bundle_inventory(inventory_root / "bundle_deep_inventory.jsonl")
    for row in bundle_rows:
        relative = str(row.get("relative_path", "")).replace("\\", "/")
        lower = relative.casefold()
        if lower.endswith("[uc]projectiles.ab") or "/projectiles/" in lower:
            result["projectile_library"].append(
                {
                    "relative_path": relative,
                    "source_path": row.get("source_path"),
                    "size": row.get("size"),
                    "sha256": row.get("sha256"),
                    "asset_count": row.get("asset_count"),
                    "type_counts": row.get("type_counts"),
                    "evidence": "bundle_inventory",
                }
            )
        if "/battle/prefabs/effects/" in lower or lower.startswith("battle/prefabs/effects/"):
            result["effect_library"].append(
                {
                    "relative_path": relative,
                    "source_path": row.get("source_path"),
                    "size": row.get("size"),
                    "sha256": row.get("sha256"),
                    "asset_count": row.get("asset_count"),
                    "type_counts": row.get("type_counts"),
                    "evidence": "bundle_inventory",
                }
            )

    # Object inventory is split into batches.  Keep only records useful for
    # this operator, and retain the original source/container fields.
    for path in sorted(inventory_root.glob("batches/batch_*/asset_inventory.json")):
        try:
            payload = load_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        entries = payload.get("AssetEntries", []) if isinstance(payload, dict) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            haystack = " ".join(str(entry.get(key, "")) for key in ("Name", "Container", "Source")).casefold()
            if token not in haystack:
                continue
            result["operator_named_objects"].append(
                {
                    "name": entry.get("Name"),
                    "container": entry.get("Container"),
                    "source": entry.get("Source"),
                    "type": entry.get("Type"),
                    "path_id": entry.get("PathID", entry.get("PathId")),
                    "batch_inventory": str(path),
                    "evidence": "asset_inventory_name_or_path_match",
                }
            )
    for key in ("projectile_library", "effect_library"):
        result[key] = sorted(result[key], key=lambda item: str(item.get("relative_path", "")).casefold())
    result["operator_named_objects"] = sorted(
        result["operator_named_objects"],
        key=lambda item: (str(item.get("type", "")), str(item.get("container", "")).casefold(), str(item.get("path_id", ""))),
    )
    return result


def run_export(bundle: Path, destination: Path, profile: Path, timeout: int, operator_token: str) -> dict[str, Any]:
    # The existing adapter creates its output directory and intentionally
    # refuses an already-existing path.  Create only the category parent.
    destination.parent.mkdir(parents=True, exist_ok=True)
    powershell = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    if not powershell.is_file():
        powershell = Path("powershell.exe")
    if bundle.suffix.casefold() != ".ab":
        script = ROOT / "targets" / "arknights" / "ark_unpacker_probe.py"
        command = [
            str(ROOT / "tools" / "unitypy" / "Scripts" / "python.exe"),
            str(script),
            "--input",
            str(bundle),
            "--output",
            str(destination),
            "--match",
            operator_token,
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
            return {
                "exit_code": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "timed_out": False,
                "command": command,
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "exit_code": None,
                "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
                "stderr": (exc.stderr or "") if isinstance(exc.stderr, str) else "",
                "timed_out": True,
                "command": command,
            }
    script = ROOT / "targets" / "arknights" / "run_animestudio_assets.ps1"
    # Import the stock utility module explicitly.  The Codex runner can expose
    # a reduced PowerShell module path to child processes, which otherwise
    # hides Get-FileHash used by the existing adapter.
    def quote(value: Path | str) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    command_text = (
        "Import-Module Microsoft.PowerShell.Utility -Force; "
        f"& {quote(script)} -ProfilePath {quote(profile)} "
        f"-InputBundle {quote(bundle)} -Output {quote(destination)} "
        f"-TypesCsv {quote(','.join(DIRECT_TYPES))} -GroupAssets ByContainer"
    )
    command = [str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command_text]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        return {
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "timed_out": False,
            "command": command,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": None,
            "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "") if isinstance(exc.stderr, str) else "",
            "timed_out": True,
            "command": command,
        }


def run_exact_closure(
    operator_id: str,
    output: Path,
    profile_path: Path,
    profile: dict[str, Any],
    timeout: int,
) -> dict[str, Any]:
    python = Path(str(profile.get("lz4ak_adapter", {}).get("python", sys.executable)))
    command = [
        str(python),
        str(EXACT_CLOSURE),
        "--operator-id",
        operator_id,
        "--profile",
        str(profile_path),
        "--output",
        str(output),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    report_path = output / "operator_resource_closure.json"
    report = load_json(report_path) if report_path.is_file() else None
    return {
        "status": report.get("status", "failed") if isinstance(report, dict) else "failed",
        "report": str(report_path) if report_path.is_file() else None,
        "manifest": str(output / "sha256_manifest.json") if (output / "sha256_manifest.json").is_file() else None,
        "command": command,
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "summary": {
            "source_bundle_count": len(report.get("source_bundles", [])) if isinstance(report, dict) else 0,
            "object_count": len(report.get("object_graph", {}).get("nodes", [])) if isinstance(report, dict) else 0,
            "resolution_count": len(report.get("resolutions", [])) if isinstance(report, dict) else 0,
            "validation": report.get("validation") if isinstance(report, dict) else None,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operator-id", required=True)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--disk-limit-bytes", type=int, default=2 * 1024 * 1024 * 1024)
    parser.add_argument("--manifest-only", action="store_true")
    parser.add_argument("--skip-exact-closure", action="store_true")
    args = parser.parse_args()

    operator_id = args.operator_id.strip()
    token = operator_id.casefold()
    if not re.fullmatch(r"char_[0-9]+_[a-z0-9_#-]+", token):
        raise SystemExit("operator-id must look like char_501_durin")
    profile_path = args.profile.resolve()
    profile = load_json(profile_path)
    if profile.get("profile_id") != "arknights":
        raise SystemExit("operator pack accepts only the retained Android Arknights profile")
    capture = Path(str(profile["storage"]["capture"])).resolve()
    bundle_root = (capture / "android" / "data" / "com.hypergryph.arknights" / "files" / "Bundles").resolve()
    if not bundle_root.is_dir():
        raise SystemExit(f"retained Bundle root missing: {bundle_root}")
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"refusing existing output: {output}")
    output.mkdir(parents=True)
    logs = output / "logs"
    resources = output / "resources"
    logs.mkdir()
    resources.mkdir()

    bundles = []
    bundle_paths = {}
    for path in sorted((item for item in bundle_root.rglob("*") if item.is_file()), key=lambda item: item.as_posix().casefold()):
        relative = relative_to_bundle(path, bundle_root)
        bundle_paths[relative] = path
        if token in relative.casefold():
            bundles.append((path, relative, category(relative)))

    inventory_root = find_inventory_root(profile)
    inventory_matches = inventory_operator_bundles(inventory_root, token)
    known_relatives = {relative for _, relative, _ in bundles}
    for relative, match in sorted(inventory_matches.items(), key=lambda item: item[0].casefold()):
        source = bundle_paths.get(relative, Path(str(match["source_path"])))
        if not source.is_file() or relative in known_relatives:
            continue
        bundles.append((source, relative, str(match.get("category") or category(relative))))
        known_relatives.add(relative)

    records: list[dict[str, Any]] = []
    total_output_bytes = 0
    for index, (bundle, relative, kind) in enumerate(bundles, start=1):
        inventory_match = inventory_matches.get(relative)
        entry = {
            "relative_path": relative,
            "source_path": str(bundle),
            "source_sha256": sha256(bundle),
            "category": kind,
            "selection": "path_match" if inventory_match is None else "path_match_and_inventory_edge",
            "inventory_match_count": inventory_match.get("inventory_match_count", 0) if inventory_match else 0,
            "inventory_match_samples": inventory_match.get("inventory_match_samples", []) if inventory_match else [],
            "inventory_types": inventory_match.get("inventory_types", []) if inventory_match else [],
            "status": "not_started",
            "output": None,
            "command": None,
        }
        if not args.manifest_only:
            destination = resources / kind / f"{index:03d}_{safe_name(relative)}"
            result = run_export(bundle, destination, profile_path, args.timeout_seconds, token)
            stdout_path = logs / f"bundle_{index:03d}.stdout.txt"
            stderr_path = logs / f"bundle_{index:03d}.stderr.txt"
            stdout_path.write_text(result["stdout"], encoding="utf-8")
            stderr_path.write_text(result["stderr"], encoding="utf-8")
            report_path = destination / "asset_analysis.json"
            if not report_path.is_file():
                report_path = destination / "ark_unpacker_probe.json"
            report = load_json(report_path) if report_path.is_file() else None
            files = report.get("output", {}).get("files", []) if isinstance(report, dict) else []
            if not files and report_path.name == "ark_unpacker_probe.json":
                files = [
                    {"output_path": str(path), "relative_path": str(path.relative_to(destination)), "valid": True, "size": path.stat().st_size, "sha256": sha256(path), "unity_type": "configuration_evidence"}
                    for path in destination.rglob("*")
                    if path.is_file() and path.name not in {"sha256_manifest.json", "KEY_SHA256SUMS.txt"}
                ]
            total_output_bytes += sum(int(item.get("size", 0)) for item in files if isinstance(item, dict))
            raw_export_path = None
            if bundle.suffix.casefold() != ".ab" and bundle.is_file():
                raw_export_path = destination / "raw" / bundle.name
                raw_export_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(bundle, raw_export_path)
                except OSError:
                    shutil.copy2(bundle, raw_export_path)
                total_output_bytes += raw_export_path.stat().st_size
            entry.update(
                {
                    "status": report.get("status", "failed") if isinstance(report, dict) else "failed",
                    "output": str(destination),
                    "report": str(report_path) if report_path.is_file() else None,
                    "exported_file_count": len(files) + (1 if raw_export_path else 0),
                    "raw_export_path": str(raw_export_path) if raw_export_path else None,
                    "raw_source_sha256": sha256(bundle) if raw_export_path else None,
                    "command": {
                        "command": result["command"],
                        "exit_code": result["exit_code"],
                        "timed_out": result["timed_out"],
                        "stdout": str(stdout_path),
                        "stderr": str(stderr_path),
                    },
                }
            )
            if total_output_bytes > args.disk_limit_bytes:
                entry["status"] = "stopped_disk_limit"
                records.append(entry)
                break
        else:
            entry["status"] = "indexed_only"
        records.append(entry)

    annotations = annotation_index()
    for record in records:
        annotation_key = "Bundles/" + str(record["relative_path"]).replace("\\", "/")
        record["annotation"] = lookup_annotation(annotations, annotation_key)

    shared = shared_candidates(inventory_root, token)
    evidence_root = output / "evidence"
    evidence_root.mkdir(parents=True, exist_ok=True)
    (evidence_root / "shared_projectile_candidates.json").write_text(
        json.dumps(shared.get("projectile_library", []), ensure_ascii=False, indent=2),
        encoding="utf-8-sig",
    )
    (evidence_root / "shared_effect_candidates.json").write_text(
        json.dumps(shared.get("effect_library", []), ensure_ascii=False, indent=2),
        encoding="utf-8-sig",
    )
    (evidence_root / "operator_named_objects.json").write_text(
        json.dumps(shared.get("operator_named_objects", []), ensure_ascii=False, indent=2),
        encoding="utf-8-sig",
    )
    exact = {
        "status": "not_run_manifest_only" if args.manifest_only else "skipped_by_request",
        "report": None,
        "manifest": None,
        "summary": None,
    }
    if not args.manifest_only and not args.skip_exact_closure:
        exact_root = output / "exact"
        try:
            exact = run_exact_closure(
                operator_id,
                exact_root,
                profile_path,
                profile,
                max(args.timeout_seconds * 4, 600),
            )
        except subprocess.TimeoutExpired as exc:
            exact = {
                "status": "failed_timeout",
                "report": None,
                "manifest": None,
                "command": exc.cmd,
                "exit_code": None,
                "stdout": exc.stdout or "",
                "stderr": exc.stderr or "",
                "summary": None,
            }
        (logs / "exact_closure.stdout.txt").write_text(str(exact.get("stdout", "")), encoding="utf-8")
        (logs / "exact_closure.stderr.txt").write_text(str(exact.get("stderr", "")), encoding="utf-8")
    catalog = build_catalog(output, records) if not args.manifest_only else {
        "schema_version": 1,
        "method": "direct_bundle_export_catalog",
        "status": "not_built_manifest_only",
        "file_count": 0,
        "unique_sha256_count": 0,
        "class_counts": {},
        "files": [],
    }
    direct_counts = Counter(item["category"] for item in records)
    passed = sum(1 for item in records if item["status"] in {"completed", "exported", "partial", "indexed_only"})
    failed = len(records) - passed
    direct_partial = any(item["status"] == "partial" for item in records)
    if not records:
        status = "blocked_no_direct_operator_bundle"
    elif args.manifest_only:
        status = "indexed_direct_operator_resources"
    elif failed or direct_partial:
        status = "partial_direct_operator_export_partial_shared"
    else:
        status = "completed_direct_operator_export_partial_shared"
    if not args.manifest_only and not args.skip_exact_closure:
        if exact["status"] == "completed_exact_static_closure":
            status = (
                "completed_direct_and_exact_static_closure"
                if status == "completed_direct_operator_export_partial_shared"
                else "partial_direct_export_completed_exact_static_closure"
            )
        else:
            status = "partial_operator_pack_exact_closure_incomplete"

    report = {
        "schema_version": 1,
        "created_utc": utc_now(),
        "status": status,
        "operator_id": operator_id,
        "profile": str(profile_path),
        "bundle_root": str(bundle_root),
        "manifest_only": args.manifest_only,
        "direct_bundle_count": len(records),
        "direct_bundle_category_counts": dict(sorted(direct_counts.items())),
        "direct_bundle_passed_count": passed,
        "direct_bundle_failed_count": failed,
        "direct_bundles": records,
        "catalog": {
            "index": str(output / "catalog_index.json") if not args.manifest_only else None,
            "file_count": catalog["file_count"],
            "unique_sha256_count": catalog["unique_sha256_count"],
            "class_counts": catalog["class_counts"],
            "status": catalog.get("status", "built"),
        },
        "shared_candidates": shared,
        "exact": exact,
        "evidence": {
            "shared_projectiles": str(evidence_root / "shared_projectile_candidates.json"),
            "shared_effects": str(evidence_root / "shared_effect_candidates.json"),
            "operator_named_objects": str(evidence_root / "operator_named_objects.json"),
        },
        "inventory_root": str(inventory_root) if inventory_root else None,
        "limitations": [
            "Direct bundles are grouped and exported with the existing AnimeStudio adapter.",
            "The catalog is a deduplicated operator-facing view; original per-Bundle exports remain under resources/.",
            "Shared projectile/effect libraries are indexed as candidates unless an exact config/PPtr edge resolves ownership.",
            "Resources proven by current config fields and serialized object/PPtr edges are under exact/.",
            "This pack does not claim runtime invocation, complete game logic, or original source code.",
            "A partial bundle export remains partial even when other categories pass.",
        ],
    }
    report_path = output / "operator_pack.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8-sig")
    readme = output / "README.md"
    readme.write_text(
        "\n".join(
            [
                f"# Operator pack: {operator_id}",
                "",
                f"Status: `{status}`",
                "",
                "All direct operator-named Bundles are retained below `resources/` and exposed in one type-oriented `catalog/`.",
                "Use `catalog_index.json` as the single machine-readable entry point; every row includes source Bundle, source SHA-256, Unity type, class and evidence.",
                "Shared projectile/effect libraries are under `evidence/`; they are not silently promoted to operator-owned resources.",
                "Current config and serialized-reference closures are under `exact/`; each promoted root records its field, PathID, Bundle and recursive CAB dependencies.",
                "",
                "Direct Bundle categories: `battle_spine`, `skin`, `voice`, `portrait`, `story`, `combat_config`, `operator_direct`.",
                "Catalog classes: `portrait`, `spine`, `animation`, `battle_textures`, `render_dependencies`, `audio`, `combat_config`, `effects`, `serialized_objects`, `story`, `other`.",
            ]
        ),
        encoding="utf-8-sig",
    )
    manifest_paths = [
        report_path,
        readme,
        Path(__file__).resolve(),
        output / "catalog_index.json",
        output / "catalog" / "README.md",
        evidence_root / "shared_projectile_candidates.json",
        evidence_root / "shared_effect_candidates.json",
        evidence_root / "operator_named_objects.json",
        Path(str(exact.get("report"))) if exact.get("report") else output / "exact" / "operator_resource_closure.json",
        Path(str(exact.get("manifest"))) if exact.get("manifest") else output / "exact" / "sha256_manifest.json",
    ]
    for item in records:
        source = Path(item["source_path"])
        if source.is_file():
            manifest_paths.append(source)
    for item in records:
        report_file = item.get("report")
        if report_file and Path(report_file).is_file():
            manifest_paths.append(Path(report_file))
    manifest = {
        "schema_version": 1,
        "files": [
            {"path": str(path), "size": path.stat().st_size, "sha256": sha256(path)}
            for path in dict.fromkeys(path.resolve() for path in manifest_paths if path.is_file())
        ],
    }
    manifest_path = output / "sha256_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8-sig")
    key_lines = [f"{row['sha256']}  {row['path']}" for row in manifest["files"]]
    (output / "KEY_SHA256SUMS.txt").write_text("\n".join(key_lines) + "\n", encoding="utf-8-sig")
    print(json.dumps({"status": status, "direct_bundle_count": len(records), "categories": dict(direct_counts)}, ensure_ascii=False))
    return 0 if status in {
        "completed_direct_operator_export_partial_shared",
        "completed_direct_and_exact_static_closure",
        "partial_direct_export_completed_exact_static_closure",
        "indexed_direct_operator_resources",
    } else 3


if __name__ == "__main__":
    raise SystemExit(main())
