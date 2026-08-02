from __future__ import annotations

import struct
import unittest

from coh2_rgm_lab.chunky import (
    CHUNK_HEADER,
    EXPECTED_SIGNATURE2,
    FILE_HEADER,
    SIGNATURE,
    ChunkParseError,
    parse_chunky,
)


def make_chunk(
    kind: bytes,
    chunk_type: bytes,
    payload: bytes,
    *,
    version: int = 1,
    name: bytes = b"",
    unknown_signed: int = -1,
    unknown_unsigned: int = 0,
) -> bytes:
    return CHUNK_HEADER.pack(
        kind,
        chunk_type,
        version,
        len(payload),
        len(name),
        unknown_signed,
        unknown_unsigned,
    ) + name + payload


def make_file(chunks: bytes, *, prefix: bytes = b"") -> bytes:
    data_offset = FILE_HEADER.size + len(prefix)
    return FILE_HEADER.pack(
        SIGNATURE,
        EXPECTED_SIGNATURE2,
        3,
        1,
        data_offset,
        0,
        1,
    ) + prefix + chunks


class ChunkyTests(unittest.TestCase):
    def test_nested_file_roundtrips_byte_for_byte(self) -> None:
        leaf = make_chunk(
            b"DATA", b"INFO", b"payload", version=4, name=b"mesh\x00"
        )
        folder = make_chunk(b"FOLD", b"MODL", leaf, version=1, name=b"root")
        raw = make_file(folder, prefix=b"ABCD")

        parsed = parse_chunky(raw)

        self.assertEqual(parsed.to_bytes(), raw)
        self.assertEqual(parsed.chunk_count, 2)
        self.assertEqual(parsed.chunks[0].tag, "FOLD/MODL")
        self.assertIsNotNone(parsed.chunks[0].children)
        assert parsed.chunks[0].children is not None
        self.assertEqual(parsed.chunks[0].children[0].payload, b"payload")

    def test_rejects_wrong_signature(self) -> None:
        raw = bytearray(make_file(b""))
        raw[:12] = b"Not Chunky!!"
        with self.assertRaisesRegex(ChunkParseError, "bad signature"):
            parse_chunky(bytes(raw))

    def test_rejects_chunk_crossing_parent_boundary(self) -> None:
        malformed_child = CHUNK_HEADER.pack(
            b"DATA", b"INFO", 1, 1000, 0, 0, 0
        )
        raw = make_file(make_chunk(b"FOLD", b"MODL", malformed_child))
        with self.assertRaisesRegex(ChunkParseError, "beyond boundary"):
            parse_chunky(raw)

    def test_rejects_unknown_chunk_kind(self) -> None:
        raw = make_file(make_chunk(b"NOPE", b"TEST", b""))
        with self.assertRaisesRegex(ChunkParseError, "unsupported chunk kind"):
            parse_chunky(raw)


if __name__ == "__main__":
    unittest.main()
