"""File types from a mimetype or from the bytes, shared by the tool and resource handlers."""

import codecs
from typing import Optional, Tuple

# Mimetypes that carry textual payloads — returned inline as ``text`` in a
# ``resources/read`` content entry instead of a base64 ``blob``. Everything
# else (images, audio, PDFs, archives, ...) is returned as a blob. Types
# ending in ``+json``/``+xml`` (application/ld+json, image/svg+xml, ...) are
# matched by suffix in ``is_text_mimetype`` and need no entry here.
_TEXT_MIMETYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/javascript",
        "application/ecmascript",
        "application/csv",
        "application/yaml",
        "application/x-yaml",
        "application/x-sh",
        "application/sql",
        "application/graphql",
        "text/uri-list",
    }
)

# Magic-number prefixes for common binary formats — a client-side stand-in
# for Odoo's guess_mimetype, used only when no backing ir.attachment carries
# an explicit mimetype.
_MAGIC_SIGNATURES: Tuple[Tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),
)


# SVG is XML, so it has no magic number: the root <svg> element may be
# preceded by a UTF-8 BOM, an <?xml?> declaration, a DOCTYPE or comments.
# It earns this extra check because Odoo renders every default user and
# partner avatar as SVG, making it the most common binary field served over
# MCP — without it those all degrade to an opaque octet-stream download.
_SVG_SNIFF_WINDOW = 1024
_SVG_PROLOGUE_PREFIXES = (b"<?xml", b"<!doctype svg", b"<!--")


def _looks_like_svg(raw: bytes) -> bool:
    """Whether `raw` opens an SVG document.

    Only the head of the payload is scanned, so a large XML file cannot turn
    this into a full-buffer search. The first tag must be `<svg` itself or a
    prologue that legitimately precedes it, and any HTML marker (`<html` or
    `<!doctype html`) in the head disqualifies it — a leading comment would
    otherwise defer `<!doctype html` past the prefix check, so an inline
    `<svg>` in a web page is not mistaken for one.
    """
    head = raw[:_SVG_SNIFF_WINDOW]
    if head.startswith(codecs.BOM_UTF8):
        head = head[len(codecs.BOM_UTF8) :]
    head = head.lstrip().lower()
    if head.startswith(b"<svg"):
        return True
    if head.startswith(_SVG_PROLOGUE_PREFIXES):
        return b"<svg" in head and b"<html" not in head and b"<!doctype html" not in head
    return False


def guess_mimetype(raw: bytes) -> str:
    """Best-effort mimetype from magic bytes; octet-stream when unknown."""
    for signature, mimetype in _MAGIC_SIGNATURES:
        if raw.startswith(signature):
            return mimetype
    if _looks_like_svg(raw):
        return "image/svg+xml"
    return "application/octet-stream"


def is_text_mimetype(mimetype: str) -> bool:
    """Whether ``mimetype`` denotes textual (inline-able) content."""
    base = (mimetype or "").split(";", 1)[0].strip().lower()
    if not base:
        return False
    if base.startswith("text/"):
        return True
    if base in _TEXT_MIMETYPES:
        return True
    return base.endswith("+json") or base.endswith("+xml")


# Image types a model API takes as an image block (the Claude API accepts only
# these four); any other image is returned as a link
_INLINE_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


def inline_image_type(raw: bytes) -> Optional[str]:
    """The mimetype of ``raw`` when it can go inline as an image block, else None.

    Taken from the bytes, not from the stored mimetype: Odoo derives that from
    the file name, so a JPEG named photo.png is declared image/png.
    """
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    mimetype = guess_mimetype(raw)
    return mimetype if mimetype in _INLINE_IMAGE_TYPES else None


def is_inline_image_type(mimetype: str) -> bool:
    """Whether a declared ``mimetype`` names an image type that can go inline."""
    return (mimetype or "").split(";", 1)[0].strip().lower() in _INLINE_IMAGE_TYPES
