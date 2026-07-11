"""Lecture-script context: deck mapping, PDF text extraction, disk caching.

Extracted text is cached as .txt in the addon's user_files folder; the cache
is invalidated when the source file's mtime or size changes. This module has
no aqt imports — the caller passes the cache directory in.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

log = logging.getLogger("ai_answer_grading")


def resolve_deck_files(deck_name: str, deck_context_map: dict[str, Any]) -> list[str]:
    """Find script files for a deck via prefix matching (subdecks included).

    The most specific (longest) matching prefix wins. A map value may be a
    single path string or a list of paths.
    """
    best_prefix: str | None = None
    for prefix in deck_context_map:
        if deck_name == prefix or deck_name.startswith(prefix + "::"):
            if best_prefix is None or len(prefix) > len(best_prefix):
                best_prefix = prefix
    if best_prefix is None:
        return []
    value = deck_context_map[best_prefix]
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(p) for p in value]
    return []


# Bump when the extraction format changes (e.g. page markers) so stale
# caches are re-extracted.
CACHE_FORMAT = 2


def _extract_pdf_text(path: str) -> str:
    """Extract PDF text with per-page markers so the model can cite slides."""
    from pypdf import PdfReader  # vendored in lib/, on sys.path

    reader = PdfReader(path)
    name = os.path.basename(path)
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # single broken page must not kill the run
            log.warning("PDF page extraction failed in %s: %s", path, exc)
            text = ""
        pages.append(f"[Seite {i} von {name}]\n{text}")
    return "\n".join(pages)


def _cache_paths(cache_dir: str, source_path: str) -> tuple[str, str]:
    digest = hashlib.sha256(source_path.encode("utf-8")).hexdigest()[:24]
    base = os.path.join(cache_dir, digest)
    return base + ".txt", base + ".meta.json"


def _get_file_text(source_path: str, cache_dir: str) -> str:
    """Return extracted text for one file, using/refreshing the disk cache."""
    if not os.path.isfile(source_path):
        log.warning("Context file not found, skipping: %s", source_path)
        return ""

    stat = os.stat(source_path)
    meta = {
        "mtime": stat.st_mtime,
        "size": stat.st_size,
        "source": source_path,
        "fmt": CACHE_FORMAT,
    }

    ext = os.path.splitext(source_path)[1].lower()
    if ext not in (".pdf",):
        # Plain text files are read directly — no cache needed.
        try:
            with open(source_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError as exc:
            log.warning("Cannot read context file %s: %s", source_path, exc)
            return ""

    os.makedirs(cache_dir, exist_ok=True)
    txt_path, meta_path = _cache_paths(cache_dir, source_path)

    if os.path.isfile(txt_path) and os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                cached_meta = json.load(f)
            if (
                cached_meta.get("mtime") == meta["mtime"]
                and cached_meta.get("size") == meta["size"]
                and cached_meta.get("fmt") == CACHE_FORMAT
            ):
                with open(txt_path, "r", encoding="utf-8") as f:
                    return f.read()
        except (OSError, json.JSONDecodeError):
            pass  # fall through to re-extraction

    log.info("Extracting PDF text: %s", source_path)
    try:
        text = _extract_pdf_text(source_path)
    except Exception as exc:
        log.warning("PDF extraction failed for %s: %s", source_path, exc)
        return ""

    try:
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(text)
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f)
    except OSError as exc:
        log.warning("Could not write context cache: %s", exc)

    return text


def get_context_for_deck(
    deck_name: str,
    deck_context_map: dict[str, Any],
    cache_dir: str,
    max_chars: int = 150000,
) -> str | None:
    """Combined script text for a deck, or None if no mapping exists."""
    files = resolve_deck_files(deck_name, deck_context_map)
    if not files:
        return None

    parts = []
    for path in files:
        text = _get_file_text(path, cache_dir)
        if text.strip():
            parts.append(f"=== {os.path.basename(path)} ===\n{text}")
    combined = "\n\n".join(parts)
    if not combined.strip():
        return None

    if len(combined) > max_chars:
        log.warning(
            "Skriptkontext für Deck '%s' überschreitet max_context_chars "
            "(%d > %d) und wird hart abgeschnitten.",
            deck_name,
            len(combined),
            max_chars,
        )
        combined = combined[:max_chars]
    return combined
