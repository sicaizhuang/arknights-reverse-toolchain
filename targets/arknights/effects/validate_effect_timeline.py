#!/usr/bin/env python3
"""Offline acceptance checks for one formal effect timeline output."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def check(name: str, passed: bool, observed, tests: list[dict]) -> None:
    tests.append({"name": name, "passed": bool(passed), "observed": observed})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeline-output", required=True, type=Path)
    parser.add_argument("--baseline-output", required=True, type=Path)
    parser.add_argument("--single-output", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    root = args.timeline_output.resolve()
    baseline_root = args.baseline_output.resolve()
    single_root = args.single_output.resolve()
    report = json.loads((root / "effects_timeline_report.json").read_text(encoding="utf-8-sig"))
    render = json.loads((root / "effect_render.json").read_text(encoding="utf-8-sig"))
    post = json.loads((root / "preview" / "timeline_postprocess.json").read_text(encoding="utf-8-sig"))
    baseline_render = json.loads((baseline_root / "effect_render.json").read_text(encoding="utf-8-sig"))
    single = json.loads((single_root / "effects_adapter_report.json").read_text(encoding="utf-8-sig"))
    tests: list[dict] = []

    check("formal_timeline_status", report.get("status") == "passed_timeline", report.get("status"), tests)
    check("unity_timeline_status", render.get("status") == "passed_timeline_frames", render.get("status"), tests)
    trail_count = int(render.get("trail_renderer_count", 0) or 0)
    trail_motion = {
        "trail_renderer_count": trail_count,
        "external_projectile_motion": render.get("external_projectile_motion"),
        "max_trail_position_count": render.get("max_trail_position_count"),
        "max_trail_path_length": render.get("max_trail_path_length"),
    }
    check(
        "trail_motion_fixture",
        trail_count == 0 or (
            render.get("external_projectile_motion") is True
            and int(render.get("max_trail_position_count", 0) or 0) >= 2
            and float(render.get("max_trail_path_length", 0.0) or 0.0) > 0.0
        ),
        trail_motion,
        tests,
    )
    check("deterministic_shader_time_driven", render.get("drive_shader_time") is True, render.get("drive_shader_time"), tests)
    check("dependency_closure_complete", report.get("unresolved_cab_count") == 0 and render.get("failed_bundle_count") == 0, {"resolved": report.get("resolved_cab_count"), "unresolved": report.get("unresolved_cab_count"), "failed": render.get("failed_bundle_count")}, tests)
    check("vulkan_shader_gate", render.get("graphics_device") == "Vulkan" and render.get("unsupported_non_null_shader_count") == 0, {"graphics": render.get("graphics_device"), "unsupported": render.get("unsupported_non_null_shader_count")}, tests)
    gates = report.get("authenticity_gates", {})
    check("no_reconstructed_content", gates.get("replacement_shader_used") is False and gates.get("generated_texture_used") is False and gates.get("synthetic_particle_used") is False, gates, tests)
    check("documented_render_controls", gates.get("dependency_first_main_last") is True and gates.get("prewarmed_bounds") is True and gates.get("fixed_camera_z") == -50 and gates.get("alpha_recovery") == "max_rgb", gates, tests)

    frames = render.get("frames", [])
    frame_failures = []
    for frame in frames:
        path = Path(frame["path"])
        if not path.is_file():
            frame_failures.append(f"missing:{path}")
            continue
        if sha256(path) != frame["sha256"]:
            frame_failures.append(f"hash:{path}")
            continue
        with Image.open(path) as image:
            if image.size != (512, 512) or image.mode != "RGBA":
                frame_failures.append(f"contract:{path}:{image.size}:{image.mode}")
    check("lossless_frame_contract", not frame_failures and len(frames) > 1, {"frames": len(frames), "failures": frame_failures}, tests)

    baseline_hashes = [item["sha256"] for item in baseline_render.get("frames", [])]
    current_hashes = [item["sha256"] for item in frames]
    check("deterministic_frame_hashes", current_hashes == baseline_hashes and len(current_hashes) > 1, {"current": len(current_hashes), "baseline": len(baseline_hashes), "mismatches": sum(a != b for a, b in zip(current_hashes, baseline_hashes)) if len(current_hashes) == len(baseline_hashes) else None}, tests)

    gif_path = Path(post["gif"]["path"])
    with Image.open(gif_path) as gif:
        durations = []
        for index in range(gif.n_frames):
            gif.seek(index)
            durations.append(int(gif.info.get("duration", 0)))
        gif_fps = 1000 * gif.n_frames / sum(durations)
        gif_fact = {"size": gif.size, "frames": gif.n_frames, "durations_ms": durations, "average_fps": gif_fps, "loop": gif.info.get("loop")}
        gif_passed = gif.size == (256, 256) and gif.n_frames == post["retained_frame_count"] and abs(gif_fps - render["fps"]) < 0.001 and gif.info.get("loop") == 0
    check("animated_gif_contract", gif_passed, gif_fact, tests)

    contact_path = Path(post["contact_sheet"]["path"])
    with Image.open(contact_path) as contact:
        contact_fact = {"size": contact.size, "mode": contact.mode, "sha256": sha256(contact_path)}
        contact_passed = contact.width > 512 and contact.height > 180 and contact.mode == "RGBA"
    check("contact_sheet_contract", contact_passed, contact_fact, tests)
    crop = post.get("uniform_preview_crop_bounds")
    check("uniform_preview_crop", isinstance(crop, list) and len(crop) == 4 and crop[2] > crop[0] and crop[3] > crop[1], crop, tests)

    manifest_failures = []
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
    check("timeline_hash_manifest", not manifest_failures, manifest_failures, tests)
    check("single_frame_regression", single.get("status") == "passed_single_frame" and single.get("render_status") == "passed_single_frame", {"status": single.get("status"), "render": single.get("render_status")}, tests)

    passed = sum(item["passed"] for item in tests)
    result = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "passed" if passed == len(tests) else "failed",
        "timeline_output": str(root),
        "baseline_output": str(baseline_root),
        "single_output": str(single_root),
        "test_count": len(tests),
        "passed": passed,
        "failed": len(tests) - passed,
        "tests": tests,
    }
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "timeline_validation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": result["status"], "passed": passed, "failed": len(tests) - passed, "output": str(args.output)}, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
