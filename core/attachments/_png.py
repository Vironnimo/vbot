"""Repack validated static PNGs without decoding or changing their image data."""

import struct
import zlib

_BLOCK_BYTES = 1024 * 1024


def recompress_png(data: bytes, *, max_decoded_bytes: int) -> bytes:
    """Keep every filtered scanline and non-IDAT byte; return only smaller files.

    Animation and signed PNGs stay untouched. Streaming and an independent raw
    byte budget also bound work on excess compressed data a decoder may ignore.
    The caller has already validated the image and its pixel budget.
    """
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return data
    start = end = 0
    offset = 8
    while offset + 12 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4 : offset + 8]
        chunk_end = offset + length + 12
        if chunk_end > len(data) or kind in {b"acTL", b"dSIG"}:
            return data
        if kind == b"IDAT":
            if end and offset != end:
                return data
            start = start or offset
            end = chunk_end
        offset = chunk_end
        if kind == b"IEND":
            break
    if not end:
        return data

    max_encoded_bytes = end - start - 12
    inflater = zlib.decompressobj()
    compressor = zlib.compressobj(level=6)
    encoded = bytearray()
    raw = bytearray()
    decoded_bytes = 0
    try:
        view = memoryview(data)
        chunk_start = start
        while chunk_start < end:
            chunk_end = chunk_start + struct.unpack_from(">I", data, chunk_start)[0] + 12
            for position in range(chunk_start + 8, chunk_end - 4, _BLOCK_BYTES):
                pending = view[position : min(position + _BLOCK_BYTES, chunk_end - 4)]
                while True:
                    capacity = _BLOCK_BYTES - len(raw)
                    block = inflater.decompress(pending, capacity)
                    decoded_bytes += len(block)
                    if decoded_bytes > max_decoded_bytes or inflater.unused_data:
                        return data
                    raw.extend(block)
                    # Fixed input blocks make the encoding independent of the
                    # source IDAT boundaries and stable on repeated preparation.
                    if len(raw) == _BLOCK_BYTES:
                        encoded.extend(compressor.compress(raw))
                        raw.clear()
                        if len(encoded) >= max_encoded_bytes:
                            return data
                    pending = memoryview(inflater.unconsumed_tail)
                    if not pending and len(block) < capacity:
                        break
            chunk_start = chunk_end
        if not inflater.eof:
            return data
        encoded.extend(compressor.compress(raw))
        encoded.extend(compressor.flush())
    except zlib.error:
        return data
    if len(encoded) >= max_encoded_bytes:
        return data
    checksum = zlib.crc32(encoded, zlib.crc32(b"IDAT"))
    return (
        data[:start]
        + struct.pack(">I", len(encoded))
        + b"IDAT"
        + encoded
        + struct.pack(">I", checksum)
        + data[end:]
    )
