"""Lossless parser for the Relic Chunky v3 container used by CoH2 assets.

This module intentionally stops at the container boundary. It exposes nested
chunks and preserves all unknown fields and payload bytes. Geometry decoding
will be added only after the parser is verified against real user-owned files.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Iterator, Sequence


SIGNATURE = b"Relic Chunky"
EXPECTED_SIGNATURE2 = 1_706_509
EXPECTED_VERSION = 3
FILE_HEADER = struct.Struct("<12s6I")
CHUNK_HEADER = struct.Struct("<4s4sIIIiI")


class ChunkParseError(ValueError):
    """Raised when a file cannot be parsed without guessing."""


def _display(raw: bytes) -> str:
    return raw.rstrip(b"\x00").decode("ascii", errors="backslashreplace")


@dataclass(frozen=True, slots=True)
class Chunk:
    kind: bytes
    type: bytes
    version: int
    name: bytes
    unknown_signed: int
    unknown_unsigned: int
    payload: bytes
    children: tuple["Chunk", ...] | None
    offset: int

    @property
    def kind_text(self) -> str:
        return _display(self.kind)

    @property
    def type_text(self) -> str:
        return _display(self.type)

    @property
    def name_text(self) -> str:
        return self.name.rstrip(b"\x00").decode("utf-8", errors="backslashreplace")

    @property
    def tag(self) -> str:
        return f"{self.kind_text}/{self.type_text}"

    @property
    def data_size(self) -> int:
        return len(self.payload)

    def walk(self, depth: int = 0) -> Iterator[tuple[int, "Chunk"]]:
        yield depth, self
        if self.children is not None:
            for child in self.children:
                yield from child.walk(depth + 1)

    def to_bytes(self) -> bytes:
        if self.children is None:
            payload = self.payload
        else:
            payload = b"".join(child.to_bytes() for child in self.children)
        header = CHUNK_HEADER.pack(
            self.kind,
            self.type,
            self.version,
            len(payload),
            len(self.name),
            self.unknown_signed,
            self.unknown_unsigned,
        )
        return header + self.name + payload

    def as_dict(self, *, recursive: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "tag": self.tag,
            "kind": self.kind_text,
            "type": self.type_text,
            "version": self.version,
            "name": self.name_text,
            "offset": self.offset,
            "header_size": CHUNK_HEADER.size + len(self.name),
            "data_size": self.data_size,
            "unknown_signed": self.unknown_signed,
            "unknown_unsigned": self.unknown_unsigned,
        }
        if recursive and self.children is not None:
            result["children"] = [child.as_dict() for child in self.children]
        return result


@dataclass(frozen=True, slots=True)
class ChunkyFile:
    signature2: int
    version: int
    unknown1: int
    data_offset: int
    unknown2: int
    unknown3: int
    prefix_extra: bytes
    chunks: tuple[Chunk, ...]
    original_size: int

    def walk(self) -> Iterator[tuple[int, Chunk]]:
        for chunk in self.chunks:
            yield from chunk.walk(0)

    @property
    def chunk_count(self) -> int:
        return sum(1 for _ in self.walk())

    def to_bytes(self) -> bytes:
        expected_offset = FILE_HEADER.size + len(self.prefix_extra)
        if self.data_offset != expected_offset:
            raise ChunkParseError(
                "cannot rebuild: data_offset does not match preserved header prefix"
            )
        header = FILE_HEADER.pack(
            SIGNATURE,
            self.signature2,
            self.version,
            self.unknown1,
            self.data_offset,
            self.unknown2,
            self.unknown3,
        )
        return header + self.prefix_extra + b"".join(
            chunk.to_bytes() for chunk in self.chunks
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "format": "Relic Chunky",
            "signature2": self.signature2,
            "version": self.version,
            "data_offset": self.data_offset,
            "original_size": self.original_size,
            "chunk_count": self.chunk_count,
            "unknown_fields": [self.unknown1, self.unknown2, self.unknown3],
            "chunks": [chunk.as_dict() for chunk in self.chunks],
        }


def _parse_chunks(
    data: bytes, start: int, end: int, *, context: str
) -> tuple[Chunk, ...]:
    chunks: list[Chunk] = []
    cursor = start

    while cursor < end:
        remaining = end - cursor
        if remaining < CHUNK_HEADER.size:
            raise ChunkParseError(
                f"{context}: {remaining} trailing byte(s) at 0x{cursor:x}; "
                "not enough for a chunk header"
            )

        (
            kind,
            chunk_type,
            version,
            data_size,
            name_size,
            unknown_signed,
            unknown_unsigned,
        ) = CHUNK_HEADER.unpack_from(data, cursor)

        if kind not in (b"FOLD", b"DATA"):
            raise ChunkParseError(
                f"{context}: unsupported chunk kind {kind!r} at 0x{cursor:x}"
            )

        name_start = cursor + CHUNK_HEADER.size
        payload_start = name_start + name_size
        payload_end = payload_start + data_size
        if payload_start > end or payload_end > end:
            raise ChunkParseError(
                f"{context}: chunk {kind!r}/{chunk_type!r} at 0x{cursor:x} "
                f"claims {name_size} name byte(s) and {data_size} data byte(s) "
                f"beyond boundary 0x{end:x}"
            )

        name = data[name_start:payload_start]
        payload = data[payload_start:payload_end]
        children: tuple[Chunk, ...] | None
        if kind == b"FOLD":
            children = _parse_chunks(
                data,
                payload_start,
                payload_end,
                context=f"{context}/{_display(chunk_type)}v{version}",
            )
        else:
            children = None

        chunks.append(
            Chunk(
                kind=kind,
                type=chunk_type,
                version=version,
                name=name,
                unknown_signed=unknown_signed,
                unknown_unsigned=unknown_unsigned,
                payload=payload,
                children=children,
                offset=cursor,
            )
        )
        cursor = payload_end

    return tuple(chunks)


def parse_chunky(data: bytes) -> ChunkyFile:
    """Parse *data* as a Relic Chunky v3 file without discarding bytes."""

    if len(data) < FILE_HEADER.size:
        raise ChunkParseError(
            f"file is {len(data)} byte(s); header requires {FILE_HEADER.size}"
        )

    (
        signature,
        signature2,
        version,
        unknown1,
        data_offset,
        unknown2,
        unknown3,
    ) = FILE_HEADER.unpack_from(data, 0)

    if signature != SIGNATURE:
        raise ChunkParseError(
            f"bad signature {signature!r}; expected {SIGNATURE!r}"
        )
    if version != EXPECTED_VERSION:
        raise ChunkParseError(
            f"unsupported Relic Chunky version {version}; expected {EXPECTED_VERSION}"
        )
    if data_offset < FILE_HEADER.size or data_offset > len(data):
        raise ChunkParseError(
            f"invalid data offset 0x{data_offset:x} for {len(data)}-byte file"
        )

    prefix_extra = data[FILE_HEADER.size:data_offset]
    chunks = _parse_chunks(data, data_offset, len(data), context="root")
    return ChunkyFile(
        signature2=signature2,
        version=version,
        unknown1=unknown1,
        data_offset=data_offset,
        unknown2=unknown2,
        unknown3=unknown3,
        prefix_extra=prefix_extra,
        chunks=chunks,
        original_size=len(data),
    )


def first_difference(left: bytes, right: bytes) -> int | None:
    """Return the first differing byte offset, including a length mismatch."""

    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return index
    if len(left) != len(right):
        return min(len(left), len(right))
    return None


def tags(chunks: Sequence[Chunk]) -> set[str]:
    result: set[str] = set()
    for chunk in chunks:
        result.add(chunk.tag)
        if chunk.children is not None:
            result.update(tags(chunk.children))
    return result

