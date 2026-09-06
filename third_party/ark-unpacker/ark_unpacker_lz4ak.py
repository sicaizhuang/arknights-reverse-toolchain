"""Arknights LZ4AK decompression adapter for UnityPy.

Adapted from Ark-Unpacker 5.1.0 at commit
8b4101f36bc9ccb283fff272928c3f0b23583980:
https://github.com/isHarryh/Ark-Unpacker/blob/v5.x/src/lz4ak/Block.py

Portions copyright (c) 2022-2026 Harry Huang and contributors.
Licensed under the BSD 3-Clause License. See LICENSE.ARK-UNPACKER.txt.
"""

from __future__ import annotations

from typing import Union

import lz4.block


ByteString = Union[bytes, bytearray, memoryview]


def _read_extra_length(data: ByteString, position: int, limit: int) -> tuple[int, int]:
    length = 0
    while position < limit:
        value = data[position]
        length += value
        position += 1
        if value != 0xFF:
            return length, position
    raise ValueError("Truncated LZ4AK extended length")


def decompress_lz4ak(compressed_data: ByteString, uncompressed_size: int) -> bytes:
    """Convert an Arknights LZ4AK block to ordinary LZ4 and decompress it."""
    if uncompressed_size < 0:
        raise ValueError("uncompressed_size must be non-negative")
    if not compressed_data and uncompressed_size:
        raise ValueError("Empty LZ4AK input for a non-empty output")

    source_position = 0
    output_position = 0
    fixed = bytearray(compressed_data)
    compressed_size = len(fixed)

    while source_position < compressed_size:
        token_position = source_position
        literal_length = fixed[token_position] & 0x0F
        match_length = (fixed[token_position] >> 4) & 0x0F
        fixed[token_position] = (literal_length << 4) | match_length
        source_position += 1

        if literal_length == 0x0F:
            extra, source_position = _read_extra_length(fixed, source_position, compressed_size)
            literal_length += extra
        if source_position + literal_length > compressed_size:
            raise ValueError("LZ4AK literal run exceeds the compressed block")
        source_position += literal_length
        output_position += literal_length
        if output_position >= uncompressed_size:
            break

        if source_position + 2 > compressed_size:
            raise ValueError("LZ4AK match offset is truncated")
        offset = (fixed[source_position] << 8) | fixed[source_position + 1]
        fixed[source_position] = offset & 0xFF
        fixed[source_position + 1] = (offset >> 8) & 0xFF
        source_position += 2

        if match_length == 0x0F:
            extra, source_position = _read_extra_length(fixed, source_position, compressed_size)
            match_length += extra
        output_position += match_length + 4

    result = lz4.block.decompress(fixed, uncompressed_size=uncompressed_size)
    if len(result) != uncompressed_size:
        raise ValueError(
            f"LZ4AK output length mismatch: expected {uncompressed_size}, got {len(result)}"
        )
    return result


def install_unitypy_patch() -> dict[str, str]:
    """Install the target-only flag-4 handler without modifying UnityPy files."""
    from UnityPy.enums.BundleFile import CompressionFlags
    from UnityPy.helpers import CompressionHelper

    previous = CompressionHelper.DECOMPRESSION_MAP.get(CompressionFlags.LZHAM)
    CompressionHelper.DECOMPRESSION_MAP[CompressionFlags.LZHAM] = decompress_lz4ak
    return {
        "flag": str(int(CompressionFlags.LZHAM)),
        "enum_name": CompressionFlags.LZHAM.name,
        "previous_handler": getattr(previous, "__name__", repr(previous)),
        "installed_handler": decompress_lz4ak.__name__,
    }
