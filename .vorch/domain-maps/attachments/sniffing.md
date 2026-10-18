# Attachment Sniffing

MIME sniffing in `core/attachments/` (`AttachmentStore.store`, the public `sniff_media_type`): the signatures it recognizes, their false-positive guards, the allowlist and the canonical storage extensions. The domain-wide rule (bounded magic bytes only, the one OLE exception) stays in `attachments.md` -> Contracts.

## Signatures and conventions

- BMP sniffing additionally requires zero reserved fields, a recognized DIB header size, and a pixel offset after that header and within the supplied bytes; ordinary UTF-8 text starting with `BM` stays text (`test_attachments.py`).
- ASCII magic words also require their binary header fields, so text such as `ID3 tags` or `OggS notes` stays text for uploads and `read`: GIF needs a `GIF87a`/`GIF89a` header followed, after the optional global colour table, by an extension/image/trailer marker (`0x21`/`0x2C`/`0x3B`; at most 782 leading bytes, so 64 KiB probes and whole uploads agree), ID3 a version byte 2-4 and syncsafe size bytes, Ogg version 0 with valid page flags, FLAC a 34-byte STREAMINFO block header. Known simplification: text whose bytes happen to satisfy these header fields (e.g. `GIF89a` plus a marker character at the block offset) still classifies as that media type.
- Accepted raster originals are JPEG, PNG, GIF, WebP, BMP, TIFF, AVIF, and HEIC/HEIF. ISO-BMFF brands distinguish AVIF first, then HEIC/HEIF, before the video fallback.
- Signature matches: images, PDF, OOXML, Ogg/MP3/WAVE/FLAC/M4A, MP4/QuickTime/WebM/AVI; UTF-8-decodable input becomes `text/plain`; everything else `application/octet-stream` then allowlist-rejected. Known simplifications: Ogg always classifies audio, EBML always webm.
- Blobs always carry their type's canonical storage extension so filesystem consumers get typed paths even without source filenames, while meaningful original display suffixes survive.
- The allowlist covers images, any `text/*`, any `audio/*`/`video/*`, PDF, and Office formats; because sniffing only ever produces `text/plain`, the wildcard allowances are wider than reachable.

## OOXML

- OOXML sniffing admits only stored/DEFLATE `[Content_Types].xml` entries (the OPC ZIP methods) and rejects declared sizes over 1 MiB before opening the entry. The read remains capped independently of the declared size; other ZIP methods can allocate unbounded output internally even for a bounded `ZipExtFile.read(n)`. A streaming XML parser rejects DOCTYPE declarations and recognizes exact main-document content types on `Override` entries whose ZIP part exists; embedded Office package types on `Default` entries never determine the container's type. Malformed or ambiguous manifests, overflow, encrypted entries, unsupported compression and malformed compressed data stay unrecognized instead of escaping as unexpected exceptions. The upload limit bounds compressed bytes only (`test_attachments.py`).
