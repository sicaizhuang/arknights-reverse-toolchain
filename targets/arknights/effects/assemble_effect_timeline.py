#!/usr/bin/env python3
"""Validate, trim, and assemble deterministic Arknights effect frames."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def transparent_gif_frame(
    source: Image.Image,
    crop_bounds: tuple[int, int, int, int],
    size: int = 256,
    alpha_min: int = 32,
) -> Image.Image:
    rgba = source.convert("RGBA").crop(crop_bounds)
    rgba.thumbnail((size, size), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(rgba, ((size - rgba.width) // 2, (size - rgba.height) // 2))
    rgb = canvas.convert("RGB")
    indexed = rgb.quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG)
    palette = indexed.getpalette()[: 255 * 3]
    palette.extend([0, 0, 0])
    palette.extend([0] * (768 - len(palette)))
    indexed.putpalette(palette)
    data = bytearray(indexed.tobytes())
    alpha = canvas.getchannel("A").tobytes()
    for index, value in enumerate(alpha):
        if value < alpha_min:
            data[index] = 255
    indexed.frombytes(bytes(data))
    indexed.info["transparency"] = 255
    indexed.info["disposal"] = 2
    return indexed


def make_contact_sheet(
    frames: list[dict],
    output: Path,
    crop_bounds: tuple[int, int, int, int],
    maximum: int = 24,
) -> dict:
    if len(frames) <= maximum:
        selected = frames
    else:
        indexes = sorted({round(index * (len(frames) - 1) / (maximum - 1)) for index in range(maximum)})
        selected = [frames[index] for index in indexes]
    columns = 6
    tile = 180
    label = 22
    rows = (len(selected) + columns - 1) // columns
    sheet = Image.new("RGBA", (columns * tile, rows * (tile + label)), (12, 12, 12, 255))
    draw = ImageDraw.Draw(sheet)
    for position, frame in enumerate(selected):
        image = Image.open(frame["path"]).convert("RGBA").crop(crop_bounds)
        image.thumbnail((tile - 8, tile - 8), Image.Resampling.LANCZOS)
        x = position % columns * tile + (tile - image.width) // 2
        y0 = position // columns * (tile + label)
        y = y0 + (tile - image.height) // 2
        sheet.alpha_composite(image, (x, y))
        draw.rectangle((position % columns * tile, y0, position % columns * tile + tile - 1, y0 + tile - 1), outline=(80, 80, 80, 255))
        draw.text((position % columns * tile + 5, y0 + tile + 2), f"f{frame['index']:04d}  {frame['time_seconds']:.3f}s", fill=(235, 235, 235, 255))
    sheet.save(output)
    return {"path": str(output), "sha256": sha256(output), "sampled_frames": [item["index"] for item in selected]}


def union_alpha_bounds(frames: list[dict], padding_ratio: float = 0.12) -> tuple[int, int, int, int]:
    union: tuple[int, int, int, int] | None = None
    canvas_size: tuple[int, int] | None = None
    for frame in frames:
        with Image.open(frame["path"]) as image:
            rgba = image.convert("RGBA")
            canvas_size = rgba.size
            current = rgba.getchannel("A").getbbox()
        if current is None:
            continue
        if union is None:
            union = current
        else:
            union = (
                min(union[0], current[0]),
                min(union[1], current[1]),
                max(union[2], current[2]),
                max(union[3], current[3]),
            )
    if union is None or canvas_size is None:
        raise ValueError("retained frames have no alpha bounds")
    width = union[2] - union[0]
    height = union[3] - union[1]
    padding = max(4, round(max(width, height) * padding_ratio))
    return (
        max(0, union[0] - padding),
        max(0, union[1] - padding),
        min(canvas_size[0], union[2] + padding),
        min(canvas_size[1], union[3] + padding),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--render-report", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--minimum-visible-pixels", type=int, default=300)
    parser.add_argument("--trim-pad", type=int, default=2)
    args = parser.parse_args()

    report = json.loads(args.render_report.read_text(encoding="utf-8-sig"))
    if report.get("status") != "passed_timeline_frames":
        raise ValueError(f"timeline renderer did not pass: {report.get('status')}")
    frames = report.get("frames", [])
    if not frames:
        raise ValueError("timeline renderer returned no frames")

    verified = []
    for frame in frames:
        path = Path(frame["path"])
        if not path.is_file():
            raise FileNotFoundError(path)
        digest = sha256(path)
        if digest != frame["sha256"]:
            raise ValueError(f"frame hash mismatch: {path}")
        with Image.open(path) as image:
            if image.size != (512, 512) or image.mode != "RGBA":
                raise ValueError(f"invalid frame contract: {path}: {image.size}/{image.mode}")
        verified.append({**frame, "path": str(path), "sha256": digest})

    active = [index for index, frame in enumerate(verified) if frame["visible_pixels"] >= args.minimum_visible_pixels]
    if not active:
        active = [index for index, frame in enumerate(verified) if frame["visible_pixels"] > 0]
    if not active:
        raise ValueError("no non-empty frame survived validation")
    first = max(0, active[0] - args.trim_pad)
    last = min(len(verified) - 1, active[-1] + args.trim_pad)
    retained = verified[first : last + 1]
    crop_bounds = union_alpha_bounds(retained)

    args.output.mkdir(parents=True, exist_ok=True)
    contact_path = args.output / "contact_sheet.png"
    contact = make_contact_sheet(retained, contact_path, crop_bounds)
    gif_path = args.output / "effect_timeline.gif"
    gif_frames = [transparent_gif_frame(Image.open(item["path"]), crop_bounds) for item in retained]
    fps = int(report["fps"])
    cumulative_ticks = [round((index + 1) * 100 / fps) for index in range(len(gif_frames))]
    previous_ticks = 0
    gif_durations_ms = []
    for ticks in cumulative_ticks:
        current_ticks = max(previous_ticks + 1, ticks)
        gif_durations_ms.append((current_ticks - previous_ticks) * 10)
        previous_ticks = current_ticks
    gif_frames[0].save(
        gif_path,
        save_all=True,
        append_images=gif_frames[1:],
        duration=gif_durations_ms,
        loop=0,
        transparency=255,
        disposal=2,
        optimize=False,
    )

    output = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "passed_timeline",
        "render_report": str(args.render_report),
        "source_frame_count": len(verified),
        "retained_frame_count": len(retained),
        "trimmed_leading_frames": first,
        "trimmed_trailing_frames": len(verified) - last - 1,
        "first_retained_index": retained[0]["index"],
        "last_retained_index": retained[-1]["index"],
        "fps": fps,
        "gif_frame_durations_ms": gif_durations_ms,
        "gif_average_fps": 1000 * len(gif_durations_ms) / sum(gif_durations_ms),
        "minimum_visible_pixels": args.minimum_visible_pixels,
        "trim_pad": args.trim_pad,
        "uniform_preview_crop_bounds": list(crop_bounds),
        "contact_sheet": contact,
        "gif": {"path": str(gif_path), "sha256": sha256(gif_path), "bytes": gif_path.stat().st_size},
        "frames": retained,
        "authenticity_limit": "Post-processing only trims, scales, quantizes, and derives alpha from already rendered authentic Unity/Vulkan frames; it does not add effect content.",
    }
    output_path = args.output / "timeline_postprocess.json"
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": output["status"], "frames": len(retained), "gif": str(gif_path), "contact_sheet": str(contact_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
