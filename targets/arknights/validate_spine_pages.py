#!/usr/bin/env python3
"""Validate the irreversible part of a Spine atlas -> texture mapping.

This is deliberately a static gate.  It does not render a skeleton and it
never chooses a texture by filename similarity.  Every declared atlas page is
resolved, hashed, inspected for alpha, and checked against its region bounds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from PIL import Image


SIZE_RE = re.compile(r"^\s*(?:size|bounds):\s*(-?\d+)\s*,\s*(-?\d+)", re.I)
XY_RE = re.compile(r"^\s*xy:\s*(-?\d+)\s*,\s*(-?\d+)", re.I)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def is_page_header(lines: list[str], index: int) -> bool:
    """Recognise a page header without mistaking a region for a page."""
    line = lines[index]
    if not line.strip() or line[:1].isspace() or ":" in line:
        return False
    # A page header is immediately followed by its page metadata.  Region
    # names are also unindented, but their first metadata line is ``rotate``
    # or ``xy`` rather than ``size``.
    return bool(index + 1 < len(lines) and re.match(
        r"^\s*size:\s*\d+\s*,\s*\d+", lines[index + 1], re.I
    ))


def parse_atlas(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    headers = [index for index in range(len(lines)) if is_page_header(lines, index)]
    pages: list[dict[str, Any]] = []
    for page_index, header_index in enumerate(headers):
        end = headers[page_index + 1] if page_index + 1 < len(headers) else len(lines)
        block = lines[header_index:end]
        declared_size = None
        for line in block[1:8]:
            match = re.match(r"^\s*size:\s*(\d+)\s*,\s*(\d+)", line, re.I)
            if match:
                declared_size = [int(match.group(1)), int(match.group(2))]
                break

        regions: list[dict[str, Any]] = []
        region_start: int | None = None
        for local_index, line in enumerate(block[1:], start=1):
            if not line.strip() or line[:1].isspace() or ":" in line:
                continue
            if region_start is not None:
                region_lines = block[region_start:local_index]
                regions.append(parse_region(region_lines))
            region_start = local_index
        if region_start is not None:
            regions.append(parse_region(block[region_start:]))
        pages.append(
            {
                "declared_page": block[0].strip(),
                "declared_size": declared_size,
                "regions": [region for region in regions if region["name"]],
            }
        )
    return pages


def parse_region(lines: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {"name": lines[0].strip(), "xy": None, "size": None, "rotate": False}
    for line in lines[1:]:
        if line.lstrip().lower().startswith("rotate:"):
            value = line.split(":", 1)[1].strip().casefold()
            result["rotate"] = value not in {"false", "0"}
            continue
        match = XY_RE.match(line)
        if match:
            result["xy"] = [int(match.group(1)), int(match.group(2))]
            continue
        match = SIZE_RE.match(line)
        if match and line.lstrip().lower().startswith("size:"):
            result["size"] = [int(match.group(1)), int(match.group(2))]
    return result


def inspect_page(
    atlas_path: Path,
    page: dict[str, Any],
    page_root: Path,
    require_alpha: bool,
) -> dict[str, Any]:
    declared = Path(page["declared_page"])
    texture = declared if declared.is_absolute() else page_root / declared
    result: dict[str, Any] = {
        "declared_page": page["declared_page"],
        "resolved_path": str(texture),
        "exists": texture.is_file(),
        "sha256": None,
        "actual_size": None,
        "declared_size": page["declared_size"],
        "alpha": None,
        "region_bounds_ok": False,
        "errors": [],
    }
    if not texture.is_file():
        result["errors"].append("missing_page_texture")
        return result
    result["sha256"] = sha256(texture)
    try:
        with Image.open(texture) as image:
            rgba = image.convert("RGBA")
            width, height = rgba.size
            result["actual_size"] = [width, height]
            if page["declared_size"] and page["declared_size"] != [width, height]:
                result["errors"].append("declared_size_mismatch")
            alpha_histogram = rgba.getchannel("A").histogram()
            pixel_count = width * height
            zero = alpha_histogram[0]
            full = alpha_histogram[255]
            partial = pixel_count - zero - full
            near_black = sum(
                pixel[3] >= 250 and pixel[0] <= 8 and pixel[1] <= 8 and pixel[2] <= 8
                for pixel in rgba.get_flattened_data()
            )
            result["alpha"] = {
                "pixel_count": pixel_count,
                "zero": zero,
                "full": full,
                "partial": partial,
                "min": next(index for index, count in enumerate(alpha_histogram) if count),
                "max": next(index for index in range(255, -1, -1) if alpha_histogram[index]),
                "near_black_opaque": near_black,
                "near_black_opaque_ratio": near_black / pixel_count if pixel_count else 1.0,
            }
            if require_alpha and zero == 0 and partial == 0:
                result["errors"].append("alpha_contract_failed_all_opaque")
            if pixel_count and near_black / pixel_count >= 0.95:
                result["errors"].append("black_background_suspected")
            bounds_ok = True
            for region in page["regions"]:
                xy, size = region.get("xy"), region.get("size")
                if not xy or not size or xy[0] < 0 or xy[1] < 0 or size[0] < 0 or size[1] < 0:
                    bounds_ok = False
                    result["errors"].append(f"missing_region_geometry:{region['name']}")
                    continue
                stored_width, stored_height = (size[1], size[0]) if region.get("rotate") else size
                if xy[0] + stored_width > width or xy[1] + stored_height > height:
                    bounds_ok = False
                    result["errors"].append(f"region_out_of_bounds:{region['name']}")
            result["region_bounds_ok"] = bounds_ok
    except Exception as exc:  # PIL identifies malformed/unsupported PNGs here.
        result["errors"].append(f"image_decode_failed:{type(exc).__name__}")
    return result


def compare_geometry(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> dict[str, Any]:
    def flatten(pages: list[dict[str, Any]]) -> dict[str, tuple[Any, Any]]:
        return {
            region["name"]: (
                tuple(region.get("xy") or ()),
                tuple(region.get("size") or ()),
                bool(region.get("rotate")),
            )
            for page in pages
            for region in page["regions"]
        }

    lhs, rhs = flatten(left), flatten(right)
    names = sorted(set(lhs) | set(rhs))
    mismatches = [
        {"name": name, "left": lhs.get(name), "right": rhs.get(name)}
        for name in names
        if lhs.get(name) != rhs.get(name)
    ]
    return {"region_count_left": len(lhs), "region_count_right": len(rhs), "mismatches": mismatches}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--atlas", type=Path, required=True)
    parser.add_argument("--skel", type=Path)
    parser.add_argument("--page-root", type=Path)
    parser.add_argument("--compare-atlas", type=Path)
    parser.add_argument("--allow-opaque", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    atlas = args.atlas.resolve()
    page_root = (args.page_root or atlas.parent).resolve()
    pages = parse_atlas(atlas)
    report: dict[str, Any] = {
        "schema": "spine_page_validation.v1",
        "atlas": str(atlas),
        "atlas_sha256": sha256(atlas),
        "skel": str(args.skel.resolve()) if args.skel else None,
        "page_root": str(page_root),
        "require_alpha": not args.allow_opaque,
        "pages": [],
        "geometry_comparison": None,
        "errors": [],
    }
    if args.skel and not args.skel.is_file():
        report["errors"].append("missing_skeleton")
    if not pages:
        report["errors"].append("no_atlas_pages_detected")
    report["pages"] = [inspect_page(atlas, page, page_root, not args.allow_opaque) for page in pages]
    report["errors"].extend(
        error for page in report["pages"] for error in page["errors"]
    )
    if args.compare_atlas:
        compare_path = args.compare_atlas.resolve()
        if not compare_path.is_file():
            report["errors"].append("missing_compare_atlas")
        else:
            comparison = compare_geometry(pages, parse_atlas(compare_path))
            report["geometry_comparison"] = {
                "atlas": str(compare_path),
                **comparison,
            }
            if comparison["mismatches"]:
                report["errors"].append("region_geometry_mismatch")
    report["status"] = "passed" if not report["errors"] else "alpha_or_page_contract_failed"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    sys.exit(main())
