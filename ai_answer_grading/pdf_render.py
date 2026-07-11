"""Render PDF pages to PNG via Qt's QtPdf module (bundled with Anki's Qt6).

QtPdf availability varies across Anki builds/platforms — every entry point
degrades to None so callers can fall back to a text-only slide reference.
"""

from __future__ import annotations

import hashlib
import logging
import os

log = logging.getLogger("ai_answer_grading")

RENDER_MAX_WIDTH = 900  # px; plenty for a readable slide in the panel


def qtpdf_available() -> bool:
    try:
        from PyQt6.QtPdf import QPdfDocument  # noqa: F401
        return True
    except Exception:
        return False


def render_page_png(
    pdf_path: str, page_1based: int, cache_dir: str, max_width: int = RENDER_MAX_WIDTH
) -> str | None:
    """Render one PDF page to a cached PNG; return its path or None.

    Must be called on the main (GUI) thread — QtPdf is not thread-safe.
    """
    if not os.path.isfile(pdf_path) or page_1based < 1:
        return None
    try:
        from PyQt6.QtCore import QSize
        from PyQt6.QtPdf import QPdfDocument
    except Exception:
        log.info("QtPdf nicht verfügbar — Folienbilder werden übersprungen.")
        return None

    try:
        os.makedirs(cache_dir, exist_ok=True)
        stat = os.stat(pdf_path)
        key = hashlib.sha256(
            f"{pdf_path}|{page_1based}|{stat.st_mtime}|{stat.st_size}|{max_width}".encode()
        ).hexdigest()[:24]
        out_path = os.path.join(cache_dir, f"slide_{key}.png")
        if os.path.isfile(out_path):
            return out_path

        doc = QPdfDocument()
        if doc.load(pdf_path) != QPdfDocument.Error.None_:
            log.warning("QtPdf konnte %s nicht laden.", pdf_path)
            return None
        index = page_1based - 1
        if index >= doc.pageCount():
            log.info("Seite %d existiert nicht in %s.", page_1based, pdf_path)
            return None
        point_size = doc.pagePointSize(index)
        if point_size.width() <= 0:
            return None
        scale = max_width / point_size.width()
        image = doc.render(
            index, QSize(int(point_size.width() * scale), int(point_size.height() * scale))
        )
        if image.isNull() or not image.save(out_path, "PNG"):
            return None
        return out_path
    except Exception:
        log.exception("Folien-Rendering fehlgeschlagen (%s Seite %d)", pdf_path, page_1based)
        return None
