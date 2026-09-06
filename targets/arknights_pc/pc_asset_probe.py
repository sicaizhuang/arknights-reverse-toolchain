from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value.replace("\\", "/")).strip("._") or "bundle"


def parse_asset_count(path: Path) -> int | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        entries = payload.get("AssetEntries")
        return len(entries) if isinstance(entries, list) else None
    except (OSError, json.JSONDecodeError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded Windows PC Arknights Bundle probe")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tool", type=Path, required=True)
    parser.add_argument(
        "--bundle",
        action="append",
        required=True,
        help=".ab or anon .bin Bundle path relative to --source-root",
    )
    parser.add_argument("--game", default="Arknights")
    parser.add_argument("--types", nargs="+", default=[
        "GameObject", "Animator", "AnimationClip", "MonoBehaviour", "Material", "Mesh",
        "Texture2D", "Sprite", "TextAsset", "ParticleSystem", "ParticleSystemRenderer",
    ])
    parser.add_argument("--containers", default="")
    parser.add_argument("--names", default="")
    parser.add_argument("--group-assets", default="ByContainer")
    parser.add_argument("--export-type", default="JSON")
    parser.add_argument("--map-op", default="None")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()

    root = args.source_root.resolve()
    tool = args.tool.resolve()
    output = args.output.resolve()
    if not root.is_dir():
        raise RuntimeError(f"PC source root not found: {root}")
    if not tool.is_file():
        raise RuntimeError(f"AnimeStudio CLI not found: {tool}")
    if output.exists():
        raise RuntimeError(f"Refusing existing output: {output}")
    output.mkdir(parents=True)

    records: list[dict[str, object]] = []
    for relative in args.bundle:
        source = (root / relative).resolve()
        try:
            source.relative_to(root)
        except ValueError as exc:
            raise RuntimeError(f"Bundle escapes PC source root: {relative}") from exc
        if not source.is_file() or source.suffix.lower() not in {".ab", ".bin"}:
            raise RuntimeError(f"PC Bundle not found or not .ab/.bin: {source}")
        bundle_out = output / "bundles" / safe_name(relative)
        bundle_out.mkdir(parents=True)
        stdout_path = bundle_out / "animestudio.stdout.txt"
        stderr_path = bundle_out / "animestudio.stderr.txt"
        command = [
            str(tool), str(source), str(bundle_out), "--game", args.game,
            "--map_op", args.map_op, "--export_type", args.export_type,
            "--group_assets", args.group_assets, "--types", *args.types,
        ]
        if args.map_op and args.map_op.lower() != "none":
            command += ["--map_type", "JSON"]
        if args.containers:
            command += ["--containers", args.containers]
        if args.names:
            command += ["--names", args.names]
        started = datetime.now(timezone.utc)
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=args.timeout_seconds)
            exit_code = result.returncode
            launch_error = None
        except subprocess.TimeoutExpired as exc:
            result = None
            exit_code = None
            launch_error = f"timeout_after_{args.timeout_seconds}s"
            stdout_text = str(exc.stdout or "")
            stderr_text = str(exc.stderr or "")
        else:
            stdout_text = result.stdout
            stderr_text = result.stderr
        stdout_path.write_text(stdout_text, encoding="utf-8")
        stderr_path.write_text(stderr_text, encoding="utf-8")
        map_candidates = [bundle_out / "asset_inventory.json", bundle_out / "assets_map.json"]
        map_path = next((candidate for candidate in map_candidates if candidate.is_file()), None)
        exported_files = [
            path for path in bundle_out.rglob("*")
            if path.is_file() and path not in {stdout_path, stderr_path, *map_candidates}
        ]
        type_counts = Counter(
            match.group(1)
            for line in stdout_text.splitlines()
            if (match := re.search(r"Exporting ([^:]+):", line))
        )
        records.append({
            "relative_path": relative.replace("\\", "/"),
            "source_path": str(source),
            "bytes": source.stat().st_size,
            "sha256": sha256(source),
            "status": "parsed" if exit_code == 0 and (map_path is not None or args.map_op == "None") else "failed",
            "exit_code": exit_code,
            "launch_error": launch_error,
            "asset_map": str(map_path) if map_path is not None else None,
            "asset_count": parse_asset_count(map_path) if map_path is not None else None,
            "export_file_count": len(exported_files),
            "export_type_counts": dict(sorted(type_counts.items())),
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
            "started_utc": started.isoformat(),
        })

    failed = [item for item in records if item["status"] != "parsed"]
    report = {
        "schema_version": 1,
        "status": "passed_pc_asset_probe" if not failed else "partial_pc_asset_probe",
        "platform": "windows-x64",
        "source_root": str(root),
        "tool": str(tool),
        "bundle_count": len(records),
        "records": records,
        "policy": {
            "pc_only": True,
            "android_substitution": False,
            "full_directory_scan": False,
            "runtime_execution": False,
        },
    }
    (output / "pc_asset_probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "output": str(output), "bundle_count": len(records), "failed": len(failed)}))
    return 0 if not failed else 3


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
