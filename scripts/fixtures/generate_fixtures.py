#!/usr/bin/env python3
"""Create deterministic, non-target fixtures for static-toolchain health checks.

The fixtures intentionally contain no game/client data.  They exercise file
identification and bounded parser paths; they are not deployable Android apps
or production Unity/IL2CPP binaries.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import struct
import zlib
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def uleb128(value: int) -> bytes:
    result = bytearray()
    while True:
        current = value & 0x7F
        value >>= 7
        result.append(current | (0x80 if value else 0))
        if not value:
            return bytes(result)


def axml_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return uleb128(len(value)) + uleb128(len(encoded)) + encoded + b"\0"


def build_android_manifest() -> bytes:
    """Build a minimal binary AndroidManifest.xml with package metadata."""
    strings = ["manifest", "package", "org.example.staticfixture"]
    string_data = b"".join(axml_string(item) for item in strings)
    offsets = []
    cursor = 0
    for item in strings:
        offsets.append(cursor)
        cursor += len(axml_string(item))
    padding = (-len(string_data)) % 4
    string_data += b"\0" * padding
    pool_size = 28 + 4 * len(strings) + len(string_data)
    string_pool = struct.pack(
        "<HHIIIIII",
        0x0001,
        28,
        pool_size,
        len(strings),
        0,
        0x00000100,
        28 + 4 * len(strings),
        0,
    ) + struct.pack("<" + "I" * len(offsets), *offsets) + string_data
    node = struct.pack("<HHIII", 0x0102, 36, 56, 1, 0xFFFFFFFF)
    attr_ext = struct.pack("<IIHHHHH", 0xFFFFFFFF, 0, 20, 20, 1, 0, 0) + struct.pack("<H", 0)
    attribute = struct.pack("<IIIHBBI", 0xFFFFFFFF, 1, 2, 8, 0, 3, 2)
    start = node + attr_ext + attribute
    end = struct.pack("<HHIIIII", 0x0103, 24, 24, 1, 0xFFFFFFFF, 0xFFFFFFFF, 0)
    document = string_pool + start + end
    return struct.pack("<HHI", 0x0003, 8, 8 + len(document)) + document


def build_empty_dex() -> bytes:
    """A structurally valid empty DEX used only to let generic tools open an APK."""
    map_items = struct.pack("<I", 2)
    map_items += struct.pack("<HHII", 0x0000, 0, 1, 0)
    map_items += struct.pack("<HHII", 0x1000, 0, 1, 0x70)
    file_size = 0x70 + len(map_items)
    header = bytearray(0x70)
    header[0:8] = b"dex\n035\0"
    struct.pack_into("<I", header, 0x20, file_size)
    struct.pack_into("<I", header, 0x24, 0x70)
    struct.pack_into("<I", header, 0x28, 0x12345678)
    struct.pack_into("<I", header, 0x34, 0x70)
    struct.pack_into("<I", header, 0x68, len(map_items))
    struct.pack_into("<I", header, 0x6C, 0x70)
    payload = bytes(header) + map_items
    signature = hashlib.sha1(payload[0x20:]).digest()
    payload = payload[:12] + signature + payload[32:]
    checksum = zlib.adler32(payload[12:]) & 0xFFFFFFFF
    return payload[:8] + struct.pack("<I", checksum) + payload[12:]


def build_fixture_apk(path: Path) -> None:
    """Write a known-good synthetic APK generated from the minimal manifest.

    The bytes are a 766-byte unsigned APK containing only an AAPT2-compiled
    manifest for `org.example.staticfixture` and an empty DEX.  Embedding the
    final fixture avoids making a health test depend on Apktool to manufacture
    its own input.
    """
    encoded = (
        "UEsDBAoAAAgAAMwtCl0ideA5iAEAAIgBAAATAAAAQW5kcm9pZE1hbmlmZXN0LnhtbAMACACIAQAAAQAcAPgAAAAFAAAAAAAAAAAAAAAwAAAAAAAAAAAAAAASAAAAagAAAH4AAAC0AAAABwBhAG4AZAByAG8AaQBkAAAAKgBoAHQAdABwADoALwAvAHMAYwBoAGUAbQBhAHMALgBhAG4AZAByAG8AaQBkAC4AYwBvAG0ALwBhAHAAawAvAHIAZQBzAC8AYQBuAGQAcgBvAGkAZAAAAAgAbQBhAG4AaQBmAGUAcwB0AAAAGQBvAHIAZwAuAGUAeABhAG0AcABsAGUALgBzAHQAYQB0AGkAYwBmAGkAeAB0AHUAcgBlAAAABwBwAGEAYwBrAGEAZwBlAAAAAACAAQgACAAAAAABEAAYAAAAAAAAAP////8AAAAAAQAAAAIBEAA4AAAAAgAAAP//////////AgAAABQAFAABAAAAAAAAAP////8EAAAAAwAAAAgAAAMDAAAAAwEQABgAAAACAAAA//////////8CAAAAAQEQABgAAAAAAAAA/////wAAAAABAAAAUEsDBAoAAAgAAMwtCl2GbdeLjAAAAIwAAAALAAAAY2xhc3Nlcy5kZXhkZXgKMDM1AL4LcNkdnD+Icw0O1sqjd9RSBGXnMi02WowAAABwAAAAeFY0EgAAAAAAAAAAcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABwAAABwAAAAAgAAAAAAAAABAAAAAAAAAAAQAAABAAAAcAAAAFBLAQIKAAoAAAgAAMwtCl0ideA5iAEAAIgBAAATAAAAAAAAAAAAAAAAAAAAAABBbmRyb2lkTWFuaWZlc3QueG1sUEsBAgoACgAACAAAzC0KXYZt14uMAAAAjAAAAAsAAAAAAAAAAAAAAAAAuQEAAGNsYXNzZXMuZGV4UEsFBgAAAAACAAIAegAAAG4CAAAAAA=="
    )
    path.write_bytes(base64.b64decode(encoded))


def build_unityfs(path: Path) -> None:
    """A zero-entry UnityFS v6 bundle with an uncompressed block-info table."""
    signature = b"UnityFS\0"
    version = struct.pack(">I", 6)
    unity_version = b"2019.4.0f1\0"
    revision = b"2019.4.0f1\0"
    block_info = b"\0" * 16 + struct.pack(">I", 1)
    block_info += struct.pack(">IIH", 0, 0, 0)
    block_info += struct.pack(">I", 0)
    header_without_size = signature + version + unity_version + revision
    full_size = len(header_without_size) + 8 + 12 + len(block_info)
    header = header_without_size + struct.pack(">QIII", full_size, len(block_info), len(block_info), 0)
    path.write_bytes(header + block_info)


def build_metadata(path: Path) -> None:
    """Write a header-only, standard IL2CPP metadata signature fixture."""
    data = bytearray(256)
    struct.pack_into("<II", data, 0, 0xFAB11BAF, 29)
    for offset in range(8, 256, 8):
        struct.pack_into("<II", data, offset, 256, 0)
    path.write_bytes(data)


def build_arm64_elf(path: Path) -> None:
    """Write a tiny ARM64 ET_EXEC ELF containing one RET instruction."""
    code_offset = 0x1000
    code = b"\xC0\x03\x5F\xD6"
    size = code_offset + len(code)
    ident = b"\x7fELF\x02\x01\x01\x00" + b"\0" * 8
    header = struct.pack(
        "<16sHHIQQQIHHHHHH",
        ident,
        2,
        183,
        1,
        0x401000,
        64,
        0,
        0,
        64,
        56,
        1,
        0,
        0,
        0,
    )
    program = struct.pack("<IIQQQQQQ", 1, 5, 0, 0x400000, 0x400000, size, size, 0x1000)
    payload = bytearray(size)
    payload[: len(header)] = header
    payload[64 : 64 + len(program)] = program
    payload[code_offset:] = code
    path.write_bytes(payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=FIXTURES)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    files = {
        root / "apk" / "minimal-static-fixture.apk": build_fixture_apk,
        root / "unity" / "empty-unityfs-v6.bundle": build_unityfs,
        root / "il2cpp" / "standard-metadata-header-v29.dat": build_metadata,
        root / "native" / "minimal-arm64-ret.elf": build_arm64_elf,
    }
    for path, builder in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if args.force or not path.exists():
            builder(path)
    manifest = {
        "schema_version": 1,
        "purpose": "non-target deterministic static-analysis fixtures",
        "files": [
            {
                "path": str(path.relative_to(root)).replace("\\", "/"),
                "size": path.stat().st_size,
                "sha256": sha256(path),
                "scope": "format identification and bounded parser health only",
            }
            for path in sorted(files)
        ],
    }
    (root / "fixture_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
