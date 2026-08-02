"""Experimental Company of Heroes 2 RGM research tools."""

from .chunky import Chunk, ChunkParseError, ChunkyFile, parse_chunky

__all__ = ["Chunk", "ChunkParseError", "ChunkyFile", "parse_chunky"]
__version__ = "0.7.0"
