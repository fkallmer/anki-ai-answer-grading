"""Card media helpers: image extraction for the API, Image-Occlusion parsing.

aqt-free so it can be tested standalone; the caller passes the media dir in.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import urllib.parse

log = logging.getLogger("ai_answer_grading")

IMG_SRC_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)

# Claude accepts jpeg/png/gif/webp; svg & co. are skipped.
MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

MAX_IMAGE_BYTES = 4_500_000  # API limit ~5MB per image; keep headroom
DEFAULT_MAX_IMAGES = 3


def extract_image_filenames(html_text: str) -> list[str]:
    """Media filenames referenced by <img> tags, in order, de-duplicated."""
    seen: list[str] = []
    for src in IMG_SRC_RE.findall(html_text or ""):
        if src.startswith(("http://", "https://", "data:")):
            continue  # remote/inline images are not in the media folder
        name = urllib.parse.unquote(src)
        if name not in seen:
            seen.append(name)
    return seen


def guess_media_type(filename: str) -> str | None:
    return MEDIA_TYPES.get(os.path.splitext(filename)[1].lower())


def load_images(
    filenames: list[str],
    media_dir: str,
    max_images: int = DEFAULT_MAX_IMAGES,
) -> list[tuple[str, str]]:
    """Load media images as (media_type, base64) tuples, capped and size-guarded."""
    images: list[tuple[str, str]] = []
    for name in filenames:
        if len(images) >= max_images:
            log.warning("Mehr als %d Bilder auf der Karte — Rest wird ignoriert.", max_images)
            break
        media_type = guess_media_type(name)
        if not media_type:
            log.info("Bildformat nicht unterstützt, übersprungen: %s", name)
            continue
        path = os.path.join(media_dir, name)
        if not os.path.isfile(path):
            log.warning("Bilddatei nicht gefunden: %s", path)
            continue
        if os.path.getsize(path) > MAX_IMAGE_BYTES:
            log.warning("Bild zu groß (>4.5MB), übersprungen: %s", name)
            continue
        try:
            with open(path, "rb") as f:
                images.append((media_type, base64.b64encode(f.read()).decode("ascii")))
        except OSError as exc:
            log.warning("Bild konnte nicht gelesen werden (%s): %s", name, exc)
    return images


# ---------------------------------------------------------------------------
# Image Occlusion (native Anki note type)
# ---------------------------------------------------------------------------

OCCLUSION_RE = re.compile(r"\{\{c(\d+)::image-occlusion:([^}]+)\}\}")


def parse_occlusion_field(field_text: str, cloze_index: int) -> list[dict]:
    """Parse the native-IO 'Occlusion' field; return shapes for one cloze.

    Entries look like
    {{c1::image-occlusion:rect:left=.2077:top=.4025:width=.1226:height=.0705:oi=1}}
    Values are fractions of the image size (newer Anki) — kept as floats.
    """
    shapes: list[dict] = []
    for match in OCCLUSION_RE.finditer(field_text or ""):
        if int(match.group(1)) != cloze_index:
            continue
        parts = match.group(2).split(":")
        shape: dict = {"shape": parts[0]}
        for part in parts[1:]:
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            if key == "points":
                try:
                    shape["points"] = [
                        (float(x), float(y))
                        for x, y in (pt.split(",") for pt in value.split() if "," in pt)
                    ]
                except ValueError:
                    pass
            else:
                try:
                    shape[key] = float(value)
                except ValueError:
                    shape[key] = value
        shapes.append(shape)
    return shapes


def _bounding_box(shape: dict) -> tuple[float, float, float, float] | None:
    """(left, top, width, height) as fractions, or None if not derivable."""
    if "points" in shape and shape["points"]:
        xs = [p[0] for p in shape["points"]]
        ys = [p[1] for p in shape["points"]]
        return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
    try:
        return (
            float(shape["left"]),
            float(shape["top"]),
            float(shape["width"]),
            float(shape["height"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def occlusion_hint(shapes: list[dict]) -> str | None:
    """Human/model-readable German description of the queried image region."""
    descriptions = []
    for shape in shapes:
        box = _bounding_box(shape)
        if box is None:
            continue
        left, top, width, height = box
        if max(left, top, width, height) > 1.5:
            # Old absolute-pixel format — pass through as pixels.
            descriptions.append(
                f"{shape.get('shape', 'Bereich')} bei ca. x={left:.0f}px, y={top:.0f}px, "
                f"Breite {width:.0f}px, Höhe {height:.0f}px"
            )
        else:
            cx, cy = left + width / 2, top + height / 2
            descriptions.append(
                f"{shape.get('shape', 'Bereich')} mit Zentrum bei ca. {cx * 100:.0f}% von links, "
                f"{cy * 100:.0f}% von oben (Breite {width * 100:.0f}%, Höhe {height * 100:.0f}% des Bildes)"
            )
    if not descriptions:
        return None
    return "; ".join(descriptions)
