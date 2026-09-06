#!/usr/bin/env python3
"""Offline acceptance checks for a formal Arknights effect timeline batch."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image


EXPECTED_CHEN = [
    "dyn/battle/prefabs/effects/chen_skill_01_hit.prefab",
    "dyn/battle/prefabs/effects/chen_skill_01_start.prefab",
    "dyn/battle/prefabs/effects/chen_skill_02_buff.prefab",
    "dyn/battle/prefabs/effects/chen_skill_02_hit.prefab",
    "dyn/battle/prefabs/effects/chen_skill_02_start.prefab",
    *[f"dyn/battle/prefabs/effects/chen_skill_03_hit_{index:02d}.prefab" for index in range(1, 11)],
    "dyn/battle/prefabs/effects/chen_skill_03_start.prefab",
    "dyn/battle/prefabs/effects/chen_skill_03_start_02.prefab",
    "dyn/battle/prefabs/effects/chen_skill_03_start_03.prefab",
]


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def add(tests: list[dict], name: str, passed: bool, observed) -> None:
    tests.append({"name": name, "passed": bool(passed), "observed": observed})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-output", required=True, type=Path)
    parser.add_argument("--repeat-output", required=True, type=Path)
    parser.add_argument("--repeat-asset", default=EXPECTED_CHEN[5])
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    root = args.batch_output.resolve()
    repeat_root = args.repeat_output.resolve()
    report = load(root / "effects_timeline_batch_report.json")
    stage = load(root / "effect_stage.json")
    results = report.get("results", [])
    tests: list[dict] = []

    add(tests, "formal_batch_status", report.get("status") == "passed_timeline_batch", report.get("status"))
    selected = [item.get("asset") for item in results]
    add(tests, "exact_chen_skill_prefab_set", sorted(selected) == sorted(EXPECTED_CHEN), selected)
    add(tests, "batch_counts", report.get("selected_asset_count") == 18 and report.get("passed_asset_count") == 18 and report.get("failed_asset_count") == 0, {key: report.get(key) for key in ("selected_asset_count", "passed_asset_count", "failed_asset_count")})
    add(tests, "dependency_closure", report.get("resolved_cab_count") == 5 and report.get("unresolved_cab_count") == 0 and stage.get("unresolved_count") == 0, {"resolved": report.get("resolved_cab_count"), "unresolved": report.get("unresolved_cab_count")})
    add(tests, "shared_stage_contract", stage.get("status") == "passed" and stage.get("selected_asset_count") == 18 and stage.get("render_mode") == "timeline", {"status": stage.get("status"), "selected": stage.get("selected_asset_count"), "mode": stage.get("render_mode")})

    render_failures: list[str] = []
    frame_failures: list[str] = []
    preview_failures: list[str] = []
    command_failures: list[str] = []
    totals = {"source_frames": 0, "retained_frames": 0, "particles": 0, "renderers": 0}
    for item in results:
        asset = item["asset"]
        directory = Path(item["directory"])
        render_path = directory / "effect_render.json"
        post_path = directory / "preview" / "timeline_postprocess.json"
        if not render_path.is_file() or not post_path.is_file():
            render_failures.append(f"missing-report:{asset}")
            continue
        render = load(render_path)
        post = load(post_path)
        if item.get("status") != "passed_timeline" or render.get("status") != "passed_timeline_frames" or post.get("status") != "passed_timeline":
            render_failures.append(f"status:{asset}:{item.get('status')}:{render.get('status')}:{post.get('status')}")
        if render.get("graphics_device") != "Vulkan" or render.get("unsupported_non_null_shader_count") != 0 or render.get("failed_bundle_count") != 0:
            render_failures.append(f"graphics-or-dependency:{asset}")
        if render.get("drive_shader_time") is not True:
            render_failures.append(f"shader-time-not-driven:{asset}")
        if render.get("particle_system_count", 0) < 1 or render.get("renderer_count", 0) < 1:
            render_failures.append(f"empty-components:{asset}")
        frames = render.get("frames", [])
        totals["source_frames"] += len(frames)
        totals["retained_frames"] += post.get("retained_frame_count", 0)
        totals["particles"] += render.get("particle_system_count", 0)
        totals["renderers"] += render.get("renderer_count", 0)
        for frame in frames:
            path = Path(frame["path"])
            if not path.is_file() or sha256(path) != frame.get("sha256"):
                frame_failures.append(f"missing-or-hash:{asset}:{path.name}")
                continue
            with Image.open(path) as image:
                if image.size != (512, 512) or image.mode != "RGBA":
                    frame_failures.append(f"contract:{asset}:{path.name}:{image.size}:{image.mode}")
        gif_path = Path(post["gif"]["path"])
        contact_path = Path(post["contact_sheet"]["path"])
        if not gif_path.is_file() or not contact_path.is_file():
            preview_failures.append(f"missing:{asset}")
        else:
            with Image.open(gif_path) as gif:
                durations = []
                for index in range(gif.n_frames):
                    gif.seek(index)
                    durations.append(int(gif.info.get("duration", 0)))
                average_fps = 1000 * gif.n_frames / sum(durations)
                retained_count = post.get("retained_frame_count", 0)
                # Pillow/GIF may coalesce consecutive identical images and add their
                # delay to a neighboring stored frame. Validate preserved total time,
                # not a one-to-one encoded-frame count.
                target_total_ms = 1000 * retained_count / render.get("fps", 1)
                duration_is_nearest_gif_tick = abs(sum(durations) - target_total_ms) <= 5.000001
                if gif.size != (256, 256) or gif.n_frames < 1 or gif.n_frames > retained_count or not duration_is_nearest_gif_tick or gif.info.get("loop") != 0:
                    preview_failures.append(f"gif:{asset}")
            with Image.open(contact_path) as contact:
                if contact.mode != "RGBA" or contact.width <= 512 or contact.height <= 180:
                    preview_failures.append(f"contact:{asset}")
        for command_name in ("unity.command.json", "postprocess.command.json"):
            command_path = directory / "logs" / command_name
            if not command_path.is_file() or load(command_path).get("exit_code") != 0:
                command_failures.append(f"{asset}:{command_name}")

    add(tests, "all_asset_render_gates", not render_failures, {"failures": render_failures, **totals})
    add(tests, "all_lossless_frame_hashes", not frame_failures and totals["source_frames"] > 18, {"failures": frame_failures, "count": totals["source_frames"]})
    add(tests, "all_gif_and_contact_contracts", not preview_failures, preview_failures)
    add(tests, "all_per_asset_commands", not command_failures, command_failures)

    repeat = load(repeat_root / "effect_render.json")
    batch_item = next((item for item in results if item.get("asset") == args.repeat_asset), None)
    batch_render = load(Path(batch_item["directory"]) / "effect_render.json") if batch_item else {}
    repeat_hashes = [item.get("sha256") for item in repeat.get("frames", [])]
    batch_hashes = [item.get("sha256") for item in batch_render.get("frames", [])]
    add(tests, "deterministic_single_vs_batch", repeat_hashes == batch_hashes and len(batch_hashes) > 1, {"asset": args.repeat_asset, "single": len(repeat_hashes), "batch": len(batch_hashes), "mismatches": sum(a != b for a, b in zip(repeat_hashes, batch_hashes)) if len(repeat_hashes) == len(batch_hashes) else None})

    manifest_failures: list[str] = []
    for line in (root / "KEY_SHA256SUMS.txt").read_text(encoding="utf-8-sig").splitlines():
        match = re.fullmatch(r"([0-9A-Fa-f]{64}) \*(.+)", line)
        if not match:
            manifest_failures.append(f"malformed:{line}")
            continue
        path = root / match.group(2)
        if not path.is_file():
            manifest_failures.append(f"missing:{match.group(2)}")
        elif sha256(path) != match.group(1).upper():
            manifest_failures.append(f"mismatch:{match.group(2)}")
    add(tests, "batch_hash_manifest", not manifest_failures, manifest_failures)
    add(tests, "authentic_scope_not_runtime_claim", report.get("scope") == "authentic_gameobject_prefab_deterministic_offline_timeline_batch" and all("#" not in asset for asset in selected), {"scope": report.get("scope"), "skin_variants": [asset for asset in selected if "#" in asset]})

    passed = sum(item["passed"] for item in tests)
    result = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "passed" if passed == len(tests) else "failed",
        "batch_output": str(root),
        "repeat_output": str(repeat_root),
        "test_count": len(tests),
        "passed": passed,
        "failed": len(tests) - passed,
        "tests": tests,
    }
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "timeline_batch_validation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": result["status"], "passed": passed, "failed": len(tests) - passed, "output": str(args.output)}, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
