#!/usr/bin/env python3
"""Prepare an isolated, dependency-complete Arknights effect render stage."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import shutil
import struct
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import UnityPy


CAB_PATTERN = re.compile(r"CAB-[0-9a-fA-F]{32}")


def parse_vector3(value: str) -> dict[str, float]:
    """Parse an explicit x,y,z point for the offline projectile motion fixture."""
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 3:
        raise ValueError("motion points must use x,y,z")
    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise ValueError("motion points must use numeric x,y,z") from exc
    return {"x": numbers[0], "y": numbers[1], "z": numbers[2]}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


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


def parse_cab_map(path: Path) -> list[dict[str, Any]]:
    with path.open("rb") as stream:
        read_string(stream)  # historical staging base; never trusted as a source path
        count = struct.unpack("<i", stream.read(4))[0]
        entries = []
        for _ in range(count):
            cab = read_string(stream)
            relative = read_string(stream).replace("\\", "/").lstrip("/")
            offset = struct.unpack("<q", stream.read(8))[0]
            dependency_count = struct.unpack("<i", stream.read(4))[0]
            dependencies = [read_string(stream) for _ in range(dependency_count)]
            entries.append({"cab": cab, "relative": relative, "offset": offset, "dependencies": dependencies})
        if stream.read(1):
            raise ValueError(f"unexpected trailing CAB map bytes: {path}")
    return entries


def contained(path: Path, roots: Iterable[Path]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def walk_pptrs(value: Any):
    if isinstance(value, dict):
        if "m_FileID" in value and "m_PathID" in value:
            try:
                yield int(value["m_FileID"]), int(value["m_PathID"])
            except (TypeError, ValueError):
                pass
        for child in value.values():
            yield from walk_pptrs(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from walk_pptrs(child)


def serialized_files(environment):
    for top_name, top in environment.files.items():
        if hasattr(top, "objects") and hasattr(top, "externals"):
            yield str(top_name), top
        nested = getattr(top, "files", None)
        if isinstance(nested, dict):
            for name, child in nested.items():
                if hasattr(child, "objects") and hasattr(child, "externals"):
                    yield str(name), child


def external_cab(external) -> str | None:
    text = " ".join((str(getattr(external, "name", "")), str(getattr(external, "path", ""))))
    match = CAB_PATTERN.search(text)
    return match.group(0) if match else None


def inspect_converted_bundle(path: Path) -> dict[str, Any]:
    environment = UnityPy.load(str(path))
    host_cabs: set[str] = set()
    required: collections.Counter[str] = collections.Counter()
    ignored_external_paths: set[str] = set()
    types: collections.Counter[str] = collections.Counter()
    read_errors: list[dict[str, Any]] = []

    for serialized_name, serialized in serialized_files(environment):
        match = CAB_PATTERN.search(serialized_name)
        if match:
            host_cabs.add(match.group(0))
        externals = list(getattr(serialized, "externals", None) or [])
        objects = getattr(serialized, "objects", {})
        readers = objects.values() if isinstance(objects, dict) else objects
        for reader in readers:
            object_type = reader.type.name
            types[object_type] += 1
            if object_type == "AssetBundle":
                continue
            try:
                tree = reader.read_typetree()
            except Exception as exception:  # object remains indexed; failed tree is evidence
                if len(read_errors) < 100:
                    read_errors.append({"path_id": reader.path_id, "type": object_type, "error": repr(exception)})
                continue
            for file_id, _ in walk_pptrs(tree):
                if file_id <= 0 or file_id > len(externals):
                    continue
                external = externals[file_id - 1]
                cab = external_cab(external)
                if cab:
                    required[cab] += 1
                else:
                    ignored_external_paths.add(str(getattr(external, "path", "")))

    return {
        "path": str(path),
        "sha256": sha256(path),
        "bytes": path.stat().st_size,
        "host_cabs": sorted(host_cabs),
        "required_cabs": [{"cab": cab, "reference_count": count} for cab, count in sorted(required.items())],
        "ignored_external_paths": sorted(item for item in ignored_external_paths if item),
        "container_count": len(environment.container),
        "container_assets": sorted(str(item) for item in environment.container.keys()),
        "object_count": sum(types.values()),
        "types": dict(sorted(types.items())),
        "typetree_error_count": len(read_errors),
        "typetree_errors": read_errors,
    }


class StageBuilder:
    def __init__(self, profile: dict[str, Any], output: Path):
        self.profile = profile
        self.config = profile["effects_adapter"]
        self.output = output
        self.work = output / "work"
        self.stage = output / "stage"
        self.logs = output / "logs"
        self.converter = Path(self.config["converter"])
        self.allowed_roots = [Path(item).resolve() for item in self.config["allowed_roots"]]
        self.inventory_root = Path(self.config["cab_inventory_root"])
        self.max_candidates = int(self.config.get("max_fallback_candidates", 256))
        self.max_dependencies = int(self.config.get("max_dependencies", 128))
        self.max_depth = int(self.config.get("max_dependency_depth", 4))
        self.commands: list[dict[str, Any]] = []
        self.converted_by_source: dict[str, tuple[Path, dict[str, Any]]] = {}
        self.host_sources: dict[str, list[Path]] = collections.defaultdict(list)
        self.scanned_fallbacks: dict[str, list[str]] = {}
        self.fallback_candidates = self._fallback_candidates()
        self._load_existing_maps()

    def _source_for_relative(self, relative: str) -> list[Path]:
        variants = {relative, relative.removeprefix("Bundles/")}
        result = []
        for root in self.allowed_roots:
            for variant in variants:
                candidate = (root / Path(variant)).resolve()
                if candidate.is_file() and candidate not in result:
                    result.append(candidate)
        return result

    def _load_existing_maps(self) -> None:
        for map_path in sorted(self.inventory_root.glob("batches/batch_*/Maps/asset_inventory.bin")):
            for entry in parse_cab_map(map_path):
                for source in self._source_for_relative(entry["relative"]):
                    if source not in self.host_sources[entry["cab"]]:
                        self.host_sources[entry["cab"]].append(source)

    def _fallback_candidates(self) -> list[Path]:
        result: list[Path] = []
        for raw in self.config.get("fallback_search_roots", []):
            base = Path(raw)
            if base.is_file():
                result.append(base.resolve())
            elif base.is_dir():
                result.extend(item.resolve() for item in base.rglob("*") if item.is_file() and item.suffix.lower() in {".ab", ".bin"})
        unique = sorted(set(result), key=lambda item: str(item).lower())
        return unique[: self.max_candidates]

    def convert(self, source: Path, role: str) -> tuple[Path, dict[str, Any]]:
        key = str(source.resolve()).lower()
        if key in self.converted_by_source:
            return self.converted_by_source[key]
        digest = sha256(source)
        directory = self.stage / ("main" if role == "main" else "dependencies") / digest[:16]
        directory.mkdir(parents=True, exist_ok=True)
        command = [str(self.converter), "-i", str(source), "-o", str(directory)]
        started = datetime.now(timezone.utc)
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        record = {
            "command": command,
            "started_utc": started.isoformat(),
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        self.commands.append(record)
        if completed.returncode != 0:
            raise RuntimeError(f"ArkAbConverter failed for {source}: exit {completed.returncode}: {completed.stderr}")
        outputs = sorted(directory.glob("*.ab"))
        if len(outputs) != 1:
            raise RuntimeError(f"Expected one converted output for {source}, observed {len(outputs)}")
        converted = outputs[0]
        info = inspect_converted_bundle(converted)
        info.update({"source_path": str(source), "source_sha256": digest, "role": role})
        self.converted_by_source[key] = (converted, info)
        for cab in info["host_cabs"]:
            if source not in self.host_sources[cab]:
                self.host_sources[cab].append(source)
        return converted, info

    def resolve(self, cab: str) -> tuple[Path, dict[str, Any]] | None:
        for source in list(self.host_sources.get(cab, [])):
            if not source.is_file() or not contained(source, self.allowed_roots):
                continue
            converted, info = self.convert(source, "dependency")
            if cab in info["host_cabs"]:
                return converted, info

        for source in self.fallback_candidates:
            key = str(source).lower()
            known = self.scanned_fallbacks.get(key)
            if known is not None and cab not in known:
                continue
            converted, info = self.convert(source, "dependency")
            self.scanned_fallbacks[key] = list(info["host_cabs"])
            if cab in info["host_cabs"]:
                return converted, info
        return None

    def build(
        self,
        input_bundle: Path,
        asset: str | None,
        sample_time: float,
        asset_pattern: str | None = None,
        exclude_pattern: str | None = None,
        mode: str = "single-frame",
        duration: float = 0.0,
        fps: int = 30,
        max_frames: int = 300,
        minimum_frames: int = 30,
        empty_tail_frames: int = 15,
        prewarm_ratio: float = 0.6,
        motion_start: dict[str, float] | None = None,
        motion_end: dict[str, float] | None = None,
        motion_source: str | None = None,
    ) -> dict[str, Any]:
        if not input_bundle.is_file():
            raise FileNotFoundError(input_bundle)
        if not contained(input_bundle, self.allowed_roots):
            raise ValueError("Input Bundle is outside the profile's retained read-only roots")
        if not self.converter.is_file():
            raise FileNotFoundError(self.converter)

        main_converted, main_info = self.convert(input_bundle, "main")
        container_assets = main_info["container_assets"]
        if asset_pattern:
            selector = re.compile(asset_pattern)
            rejector = re.compile(exclude_pattern) if exclude_pattern else None
            selected_assets = [
                item for item in container_assets
                if selector.search(item) and (rejector is None or not rejector.search(item))
            ]
            if not selected_assets:
                raise ValueError(f"No effect assets matched the requested pattern: {asset_pattern}")
            asset = selected_assets[0]
        else:
            if not asset or asset not in container_assets:
                raise ValueError(f"Requested effect asset is absent from the main Bundle: {asset}")
            selected_assets = [asset]

        queue = collections.deque(
            (item["cab"], 1, str(input_bundle)) for item in main_info["required_cabs"]
        )
        resolved: dict[str, dict[str, Any]] = {}
        unresolved: list[dict[str, Any]] = []
        dependency_infos: dict[str, dict[str, Any]] = {}

        while queue:
            cab, depth, parent = queue.popleft()
            if cab in resolved or any(item["cab"] == cab for item in unresolved):
                continue
            if depth > self.max_depth:
                unresolved.append({"cab": cab, "reason": "max_dependency_depth", "parent": parent})
                continue
            if len(resolved) >= self.max_dependencies:
                unresolved.append({"cab": cab, "reason": "max_dependencies", "parent": parent})
                continue
            match = self.resolve(cab)
            if match is None:
                unresolved.append({"cab": cab, "reason": "host_not_found", "parent": parent})
                continue
            converted, info = match
            resolved[cab] = {
                "cab": cab,
                "source_path": info["source_path"],
                "source_sha256": info["source_sha256"],
                "converted_path": str(converted),
                "converted_sha256": info["sha256"],
                "depth": depth,
            }
            dependency_infos[info["source_path"]] = info
            for child in info["required_cabs"]:
                if child["cab"] not in resolved:
                    queue.append((child["cab"], depth + 1, info["source_path"]))

        dependency_paths = sorted({item["converted_path"] for item in resolved.values()}, key=str.lower)
        external_motion = motion_start is not None or motion_end is not None
        if external_motion and (motion_start is None or motion_end is None):
            raise ValueError("motion-start and motion-end must be provided together")
        if external_motion and not motion_source:
            raise ValueError("external projectile motion requires --motion-source evidence")
        image_path = self.output / "effect_frame.png"
        render_report = self.output / "effect_render.json"
        frames_directory = self.output / "frames"
        unity_input = {
            "main_bundle": str(main_converted),
            "dependency_bundles": dependency_paths,
            "asset_path": asset,
            "asset_paths": selected_assets,
            "sample_time": sample_time,
            "output_image": str(image_path),
            "output_report": str(render_report),
            "mode": mode,
            "duration_seconds": duration,
            "fps": fps,
            "max_frames": max_frames,
            "minimum_frames": minimum_frames,
            "empty_tail_frames": empty_tail_frames,
            "prewarm_ratio": prewarm_ratio,
            "frames_directory": str(frames_directory),
            "external_projectile_motion": external_motion,
            "motion_start": motion_start or {"x": 0.0, "y": 0.0, "z": 0.0},
            "motion_end": motion_end or {"x": 0.0, "y": 0.0, "z": 0.0},
            "motion_source": motion_source,
        }
        unity_input_path = self.stage / "unity_input.json"
        unity_input_path.write_text(json.dumps(unity_input, ensure_ascii=False, indent=2), encoding="utf-8")

        status = "passed" if not unresolved else "blocked_missing_dependencies"
        report = {
            "schema_version": 1,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "adapter": "arknights_effect_stage",
            "input_bundle": str(input_bundle),
            "input_sha256": sha256(input_bundle),
            "requested_asset": asset,
            "requested_asset_pattern": asset_pattern,
            "excluded_asset_pattern": exclude_pattern,
            "selected_assets": selected_assets,
            "selected_asset_count": len(selected_assets),
            "sample_time_seconds": sample_time,
            "render_mode": mode,
            "timeline_request": {
                "duration_seconds": duration,
                "fps": fps,
                "max_frames": max_frames,
                "minimum_frames": minimum_frames,
                "empty_tail_frames": empty_tail_frames,
                "prewarm_ratio": prewarm_ratio,
                "frames_directory": str(frames_directory),
                "external_projectile_motion": external_motion,
                "motion_start": motion_start,
                "motion_end": motion_end,
                "motion_source": motion_source,
            },
            "main": main_info,
            "resolved_cabs": sorted(resolved.values(), key=lambda item: item["cab"]),
            "resolved_cab_count": len(resolved),
            "dependency_bundle_count": len(dependency_paths),
            "unresolved": unresolved,
            "unresolved_count": len(unresolved),
            "fallback_candidate_count": len(self.fallback_candidates),
            "fallback_scanned_count": len(self.scanned_fallbacks),
            "converter": str(self.converter),
            "converter_sha256": sha256(self.converter),
            "converter_commands": self.commands,
            "unity_input": str(unity_input_path),
            "render_report": str(render_report),
            "output_image": str(image_path),
            "limits": [
                "Only serialized runtime-object PPtrs expand dependency closure; AssetBundle manifest-only references do not.",
                (
                    "Timeline mode is a deterministic offline simulation and does not prove the in-game runtime call site."
                    if mode == "timeline"
                    else "The single-frame renderer exports one deterministic sample frame, not a complete effect timeline."
                ),
            ],
        }
        (self.output / "effect_stage.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--input-bundle", required=True, type=Path)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--asset")
    selection.add_argument("--asset-pattern")
    parser.add_argument("--exclude-pattern")
    parser.add_argument("--sample-time", required=True, type=float)
    parser.add_argument("--mode", choices=("single-frame", "timeline"), default="single-frame")
    parser.add_argument("--duration", type=float, default=0.0)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--max-frames", type=int, default=300)
    parser.add_argument("--minimum-frames", type=int, default=30)
    parser.add_argument("--empty-tail-frames", type=int, default=15)
    parser.add_argument("--prewarm-ratio", type=float, default=0.6)
    parser.add_argument("--motion-start", type=parse_vector3, help="Optional evidenced projectile start point x,y,z")
    parser.add_argument("--motion-end", type=parse_vector3, help="Optional evidenced projectile end point x,y,z")
    parser.add_argument("--motion-source", help="Evidence identifier/path for the external projectile motion")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    profile = json.loads(args.profile.read_text(encoding="utf-8-sig"))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    try:
        if args.fps < 1 or args.fps > 120:
            raise ValueError("fps must be between 1 and 120")
        if args.max_frames < 1 or args.max_frames > 3600:
            raise ValueError("max-frames must be between 1 and 3600")
        if args.minimum_frames < 1 or args.minimum_frames > args.max_frames:
            raise ValueError("minimum-frames must be between 1 and max-frames")
        if args.empty_tail_frames < 1 or args.empty_tail_frames > args.max_frames:
            raise ValueError("empty-tail-frames must be between 1 and max-frames")
        if args.duration < 0 or args.duration > 120:
            raise ValueError("duration must be between 0 and 120 seconds")
        if args.prewarm_ratio < 0 or args.prewarm_ratio > 1:
            raise ValueError("prewarm-ratio must be between 0 and 1")
        if (args.motion_start is None) != (args.motion_end is None):
            raise ValueError("motion-start and motion-end must be provided together")
        if (args.motion_start is not None or args.motion_end is not None) and not args.motion_source:
            raise ValueError("motion-source is required when an external motion fixture is requested")
        report = StageBuilder(profile, output).build(
            args.input_bundle.resolve(),
            args.asset,
            args.sample_time,
            args.asset_pattern,
            args.exclude_pattern,
            args.mode,
            args.duration,
            args.fps,
            args.max_frames,
            args.minimum_frames,
            args.empty_tail_frames,
            args.prewarm_ratio,
            args.motion_start,
            args.motion_end,
            args.motion_source,
        )
        print(json.dumps({"status": report["status"], "output": str(output), "resolved_cabs": report["resolved_cab_count"], "unresolved": report["unresolved_count"]}, ensure_ascii=False))
        return 0 if report["status"] == "passed" else 4
    except Exception as exception:
        failure = {
            "schema_version": 1,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "status": "failed",
            "error": repr(exception),
        }
        (output / "effect_stage.json").write_text(json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(failure, ensure_ascii=False), file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
