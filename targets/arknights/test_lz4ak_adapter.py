#!/usr/bin/env python3
"""Focused unit tests for the target-only LZ4AK adapter."""

from __future__ import annotations

import unittest

import lz4.block

from vendor.ark_unpacker_lz4ak import decompress_lz4ak


def to_lz4ak(ordinary: bytes, output_size: int) -> bytes:
    data = bytearray(ordinary)
    position = 0
    produced = 0
    while position < len(data):
        token_position = position
        literal_length = data[token_position] >> 4
        match_length = data[token_position] & 0x0F
        data[token_position] = (match_length << 4) | literal_length
        position += 1
        if literal_length == 0x0F:
            while position < len(data):
                value = data[position]
                literal_length += value
                position += 1
                if value != 0xFF:
                    break
        position += literal_length
        produced += literal_length
        if produced >= output_size:
            break
        data[position], data[position + 1] = data[position + 1], data[position]
        position += 2
        if match_length == 0x0F:
            while position < len(data):
                value = data[position]
                match_length += value
                position += 1
                if value != 0xFF:
                    break
        produced += match_length + 4
    return bytes(data)


class Lz4AkTests(unittest.TestCase):
    def test_round_trip_repetitive_data(self) -> None:
        source = (b"Arknights-LZ4AK-" * 2000) + bytes(range(256))
        ordinary = lz4.block.compress(source, store_size=False)
        self.assertEqual(decompress_lz4ak(to_lz4ak(ordinary, len(source)), len(source)), source)

    def test_round_trip_literal_tail(self) -> None:
        source = bytes(range(251)) * 7
        ordinary = lz4.block.compress(source, store_size=False, mode="high_compression")
        self.assertEqual(decompress_lz4ak(to_lz4ak(ordinary, len(source)), len(source)), source)

    def test_truncated_input_is_rejected(self) -> None:
        with self.assertRaises((ValueError, lz4.block.LZ4BlockError)):
            decompress_lz4ak(b"\xFF", 4096)


if __name__ == "__main__":
    unittest.main(verbosity=2)
