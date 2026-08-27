import gzip
import logging
from typing import Optional

logger = logging.getLogger(__name__)

GZIP_MAGIC = b"\x1f\x8b"
_GZIP_ENCODINGS = ("gzip", "x-gzip")
_HINT_SUFFIXES = (".gz", ".gzip", ".tgz")


class HTTPBodyError(Exception):
    pass


def content_encoding_is_gzip(headers: object) -> bool:
    """True if Content-Encoding header indicates gzip (handles io objects without .get too)."""
    value = None
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter("Content-Encoding")
    else:
        getheader = getattr(headers, "getheader", None)
        if callable(getheader):
            value = getheader("Content-Encoding")
    if not value:
        return False
    parts = [p.split(";")[0].strip().lower() for p in str(value).split(",")]
    return any(p in _GZIP_ENCODINGS for p in parts)


def name_hints_gzip(name: Optional[str]) -> bool:
    lowered = (name or "").lower()
    return any(lowered.endswith(suffix) for suffix in _HINT_SUFFIXES)


def decode_http_body(
    data: bytes,
    headers: object = None,
    name: Optional[str] = None,
    context: str = "",
) -> bytes:
    """Decode a fetched HTTP body when it is gzip-encoded.

    Handles server responses where gzip may come from the Content-Encoding
    header, the file magic bytes, or merely the URL/file name (which can be
    wrong), never raising for mislabelled-but-plain payloads.
    """
    if len(data) >= 2 and data[:2] == GZIP_MAGIC:
        pass  # magic wins below
    elif content_encoding_is_gzip(headers):
        pass
    elif name_hints_gzip(name):
        pass
    else:
        return data

    label = context or name or "body"
    try:
        decoded = gzip.decompress(data)
    except (OSError, EOFError) as exc:
        if data[:2] == GZIP_MAGIC:
            logger.warning("Failed to decompress gzip body %s: %s", label, exc)
            raise HTTPBodyError(str(exc)) from exc
        logger.info(
            "%s was expected to be gzip but is plain data (%s); using as-is",
            label,
            exc,
        )
        return data
    logger.info("Decompressed gzip body from %s", label)
    return decoded
