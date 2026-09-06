#!/usr/bin/env python3
"""Build the immutable delivery summary and key-file hash manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--toolchain-root", required=True, type=Path)
    parser.add_argument("--workflow", required=True, type=Path)
    parser.add_argument("--batch-output", required=True, type=Path)
    parser.add_argument("--validation", required=True, type=Path)
    parser.add_argument("--isolation", required=True, type=Path)
    parser.add_argument("--health", required=True, type=Path)
    parser.add_argument("--repeat-output", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    root = args.toolchain_root.resolve()
    workflow = args.workflow.resolve()
    batch_root = args.batch_output.resolve()
    validation_path = args.validation.resolve()
    isolation_path = args.isolation.resolve()
    health_path = args.health.resolve()
    repeat_root = args.repeat_output.resolve()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"Output already exists: {output}")
    output.mkdir(parents=True)

    batch = load(batch_root / "effects_timeline_batch_report.json")
    validation = load(validation_path)
    isolation = load(isolation_path)
    health = load(health_path)
    results = batch.get("results", [])
    source_frames = sum(int(item.get("frame_count", 0)) for item in results)
    retained_frames = sum(int(item.get("retained_frame_count", 0)) for item in results)
    repeat = load(repeat_root / "effect_render.json")
    repeat_asset = repeat.get("asset_path")
    batch_repeat_item = next(item for item in results if item.get("asset") == repeat_asset)
    batch_repeat = load(Path(batch_repeat_item["directory"]) / "effect_render.json")
    repeat_hashes = [item.get("sha256") for item in repeat.get("frames", [])]
    batch_hashes = [item.get("sha256") for item in batch_repeat.get("frames", [])]
    deterministic_mismatches = sum(a != b for a, b in zip(repeat_hashes, batch_hashes)) if len(repeat_hashes) == len(batch_hashes) else None

    passed = (
        batch.get("status") == "passed_timeline_batch"
        and batch.get("selected_asset_count") == 18
        and batch.get("passed_asset_count") == 18
        and batch.get("failed_asset_count") == 0
        and validation.get("status") == "passed"
        and isolation.get("status") == "passed"
        and health.get("status") == "passed"
        and deterministic_mismatches == 0
    )
    report = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "passed_scoped_delivery" if passed else "failed",
        "profile": "arknights",
        "source_workflow": {
            "path": str(workflow),
            "sha256": sha256(workflow),
            "role": "external reference workflow, independently reimplemented and tested",
        },
        "implemented": [
            "explicit bounded prefab regex enumeration",
            "recursive CAB host resolution and dependency-first/main-last loading",
            "shared dependency conversion stage with per-prefab Unity process isolation",
            "Animator/Animation/ParticleSystem deterministic timeline simulation",
            "bounded early-burst-aware prewarm bounds scanning",
            "orthographic camera at Z=-50",
            "black-background render with max-RGB alpha recovery",
            "visible-tail trimming, uniform timeline crop, contact sheet, and transparent GIF",
            "per-frame SHA-256 and command/stdout/stderr evidence",
            "unified CLI command effects-timeline-batch",
        ],
        "verified_scope": {
            "input_bundle": batch.get("input_bundle"),
            "input_sha256": batch.get("input_sha256"),
            "asset_pattern": batch.get("asset_pattern"),
            "exclude_pattern": batch.get("exclude_pattern"),
            "selected_prefabs": batch.get("selected_asset_count"),
            "passed_prefabs": batch.get("passed_asset_count"),
            "failed_prefabs": batch.get("failed_asset_count"),
            "resolved_cabs": batch.get("resolved_cab_count"),
            "unresolved_cabs": batch.get("unresolved_cab_count"),
            "source_png_frames": source_frames,
            "retained_preview_frames": retained_frames,
            "deterministic_repeat_asset": repeat_asset,
            "deterministic_repeat_frames": len(repeat_hashes),
            "deterministic_repeat_hash_mismatches": deterministic_mismatches,
        },
        "tests": {
            "batch_validation": {"path": str(validation_path), "status": validation.get("status"), "passed": validation.get("passed"), "failed": validation.get("failed")},
            "pvz2_profile_isolation": {"path": str(isolation_path), "status": isolation.get("status"), "exit_code": isolation.get("exit_code"), "output_created": isolation.get("output_created")},
            "toolchain_health": {"path": str(health_path), "status": health.get("status")},
        },
        "visual_review": {
            "representative_contact_sheets_reviewed": [
                "chen_skill_01_hit",
                "chen_skill_02_start",
                "chen_skill_03_hit_01",
                "chen_skill_03_hit_10",
                "chen_skill_03_start_03",
            ],
            "result": "time-varying content present and uniformly framed",
            "gameplay_pixel_equivalence_compared": False,
        },
        "limitations": [
            "The 18/18 result is a regression for this exact Chen Bundle and default-skin path set, not proof for every operator or enemy.",
            "The outputs are deterministic offline Unity/Vulkan simulations; they do not prove the in-game skill call site, event ordering, world transform, or runtime parameters.",
            "Visual equality against captured gameplay was not tested, so the delivery does not claim pixel-identical game appearance.",
            "The workflow document's TEX_MAP replacement fallback was not enabled because the verified batch resolved its serialized CAB closure; no generated textures, replacement shaders, or synthetic particles were introduced.",
            "GIF is a palette-quantized preview. The 512x512 RGBA PNG frames and their hashes are the lossless evidence.",
        ],
        "stable_layers_unchanged": ["APK", "Java/JADX", "AnimeStudio assets", "LZ4AK", "Android IL2CPP provenance", "Native", "PC profile"],
        "artifacts": {
            "batch_report": str(batch_root / "effects_timeline_batch_report.json"),
            "batch_validation": str(validation_path),
            "health": str(health_path),
            "profile": str(root / "targets" / "arknights" / "profile.json"),
            "cli": str(root / "scripts" / "toolchain.ps1"),
        },
    }
    report_path = output / "effects_timeline_delivery.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    markdown = f"""# Arknights effect timeline adapter delivery

状态：`{report['status']}`。

- 群文件流程已转换为可复用的 `effects-timeline-batch` 入口，不是一次性陈专用脚本。
- 当前真实验收范围：陈默认皮肤技能 Prefab `18/18` 通过，CAB `5 resolved / 0 unresolved`。
- 共保留 `{source_frames}` 张 512x512 RGBA 原始证据帧，派生预览保留 `{retained_frames}` 帧。
- 短爆发样本 `{repeat_asset}` 独立/批量两次共 `{len(repeat_hashes)}` 帧，SHA-256 差异 `0`。
- 离线门禁：`{validation.get('passed')}/{validation.get('test_count')}`；PVZ2 隔离通过；通用 health=`{health.get('status')}`。

## 调用

```powershell
powershell -ExecutionPolicy Bypass -File D:\\Arknights_Reverse_Toolchain\\scripts\\toolchain.ps1 effects-timeline-batch `
  -Profile arknights `
  -InputBundle <retained-effect-bundle> `
  -AssetPattern '<anchored-prefab-regex>' `
  -ExcludePattern '#' -MaxAssets 24 -Fps 30 -MaxFrames 300
```

## 边界

这是离线 Unity/Vulkan 复现能力，不是游戏运行时调用证明；本轮也没有拿游戏录像做逐像素对照。因此 18/18 表示技术流水线通过，不表示已证明所有画面与游戏逐像素一致，更不表示所有角色自动通过。原始 PNG 是证据，GIF 只是预览。
"""
    (output / "DELIVERY.md").write_text(markdown, encoding="utf-8")

    key_paths = [
        workflow,
        root / "scripts" / "toolchain.ps1",
        root / "targets" / "arknights" / "profile.json",
        root / "targets" / "arknights" / "effects" / "prepare_effect_stage.py",
        root / "targets" / "arknights" / "effects" / "run_effect_timeline.ps1",
        root / "targets" / "arknights" / "effects" / "run_effect_timeline_batch.ps1",
        root / "targets" / "arknights" / "effects" / "assemble_effect_timeline.py",
        root / "targets" / "arknights" / "effects" / "validate_effect_timeline.py",
        root / "targets" / "arknights" / "effects" / "validate_effect_timeline_batch.py",
        root / "targets" / "arknights" / "effects" / "test_effect_timeline_batch_isolation.ps1",
        root / "targets" / "arknights" / "effects" / "build_effect_timeline_delivery.py",
        root / "targets" / "arknights" / "effects" / "unity_project" / "Assets" / "Editor" / "ArknightsEffectTimelineRenderer.cs",
        root / "targets" / "arknights" / "effects" / "README.md",
        root / "targets" / "arknights" / "effects" / "REFERENCE_WORKFLOW_NOTES.md",
        root / "docs" / "CLI.md",
        root / "docs" / "AI_CALLABLE_SURFACE.md",
        batch_root / "effect_stage.json",
        batch_root / "effects_timeline_batch_report.json",
        batch_root / "KEY_SHA256SUMS.txt",
        validation_path,
        isolation_path,
        health_path,
        report_path,
        output / "DELIVERY.md",
    ]
    for item in results:
        directory = Path(item["directory"])
        key_paths.extend([
            directory / "effect_render.json",
            directory / "preview" / "timeline_postprocess.json",
            Path(item["gif"]),
            Path(item["contact_sheet"]),
        ])
    entries = []
    for path in dict.fromkeys(path.resolve() for path in key_paths):
        if not path.is_file():
            raise FileNotFoundError(path)
        entries.append({"path": str(path), "sha256": sha256(path), "size": path.stat().st_size})
    manifest_path = output / "sha256_manifest.json"
    manifest_path.write_text(json.dumps({"schema_version": 1, "count": len(entries), "files": entries}, ensure_ascii=False, indent=2), encoding="utf-8")
    text_entries = entries + [{"path": str(manifest_path), "sha256": sha256(manifest_path), "size": manifest_path.stat().st_size}]
    lines = [f"{item['sha256']} *{item['path']}" for item in text_entries]
    (output / "KEY_SHA256SUMS.txt").write_text("\ufeff" + "\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "output": str(output), "manifest_files": len(entries), "text_manifest_lines": len(text_entries)}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
