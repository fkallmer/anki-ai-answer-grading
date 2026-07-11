"""LLM grading client: prompt construction, API calls, defensive parsing.

This module is intentionally free of any aqt/anki imports so it can be
tested outside of Anki. Supported providers:

- "anthropic": Anthropic Messages API via raw HTTP (Anki bundles requests).
- "bedrock":   AWS Bedrock InvokeModel with hand-rolled SigV4 signing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from . import aws_sigv4

log = logging.getLogger("ai_answer_grading")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
BEDROCK_ANTHROPIC_VERSION = "bedrock-2023-05-31"
MAX_OUTPUT_TOKENS = 1500

RATING_LABELS = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}


class GradingError(Exception):
    """Raised when grading fails; message is safe to show to the user."""


@dataclass
class GradingResult:
    score: int
    rating: int
    correct_points: list[str] = field(default_factory=list)
    missing_points: list[str] = field(default_factory=list)
    wrong_points: list[str] = field(default_factory=list)
    explanation: str = ""
    feedback: str = ""
    # (filename, 1-based page) of lecture-script slides the grading refers to;
    # filename may be "" when the model gave only a page number.
    source_pages: list[tuple[str, int]] = field(default_factory=list)

    @property
    def rating_label(self) -> str:
        return RATING_LABELS.get(self.rating, "?")


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

# Editable part: persona, grading standard, rating mapping. Users may replace
# this via config/settings; "{language}" is substituted at build time.
DEFAULT_GRADING_RULES = """Du bist ein strenger, fairer Prüfer für Karteikarten-Abfragen (Spaced Repetition).
Du bewertest die Antwort eines Lernenden inhaltlich gegen die Rückseite einer Karteikarte.

BEWERTUNGSMASSSTAB:
- Maßstab ist ausschließlich die Kartenrückseite. Falls ein Vorlesungsskript als Kontext
  mitgegeben wird, dient es NUR zur Einordnung von Synonymen, alternativen Formulierungen
  und Notation — es stellt KEINE zusätzlichen Anforderungen an den Lernenden.
- Rechtschreibung und Grammatik sind egal, es zählt der Inhalt.

RATING-MAPPING (konservativ):
- 4 (Easy): vollständig korrekt, präzise Fachbegriffe, keine Lücken
- 3 (Good): inhaltlich korrekt, kleine Lücken oder unpräzise Formulierungen
- 2 (Hard): Kernidee erkennbar, aber wesentliche Teile fehlen oder sind unsauber
- 1 (Again): Kernaussage falsch oder fehlt

WICHTIG: Wähle im Zweifel das NIEDRIGERE Rating. Wohlwollen verfälscht das
Spaced-Repetition-Scheduling und schadet dem Lernenden langfristig."""

# Fixed part: the JSON contract the addon parses. Always appended — a custom
# prompt must never be able to break the output format.
OUTPUT_FORMAT_RULES = """AUSGABEFORMAT:
Antworte AUSSCHLIESSLICH mit einem einzigen JSON-Objekt, ohne Markdown-Fences,
ohne Text davor oder danach, exakt in dieser Struktur:
{"score": <int 0-100>, "rating": <1|2|3|4>, "correct_points": ["..."], "missing_points": ["..."], "wrong_points": ["..."], "explanation": "<Erklärung>", "feedback": "<1-2 Sätze auf {language}>", "source_pages": ["<dateiname>:<seitenzahl>"]}

- "correct_points" / "missing_points" / "wrong_points": kurze Stichpunkte
  (je max. 8 Wörter), KEINE ganzen Sätze. Nur echte inhaltliche Punkte —
  nichts Offensichtliches: Bei einer leeren oder "weiß nicht"-Antwort bleiben
  correct_points und wrong_points leer.
- "explanation": Die richtige Antwort kompakt erklärt, wie ein Tutor nach
  einer falschen Antwort: Was ist richtig, und warum? Bei Rating 1-2: 2-5
  Sätze, nur der Kern und der wichtigste Zusammenhang — kein Aufsatz. Bei
  Rating 3: 1-2 Sätze. Bei Rating 4: leerer String. Auf {language}.
- "feedback": 1-2 kurze Sätze auf {language}, direkt und in Du-Form. KEINE
  Wiederholung der Punktelisten oder der Erklärung, KEINE Floskeln und keine
  allgemeinen Lernratschläge ("schau es dir nochmal in Ruhe an" o. Ä.).

- "source_pages": NUR wenn ein Vorlesungsskript mitgegeben wurde und die
  relevante Information dort steht: die 1-2 wichtigsten Fundstellen im Format
  "dateiname.pdf:Seitenzahl" (die Seitenzahl steht in den [Seite N von …]-
  Markern des Skripts). Sonst leere Liste.

WICHTIG: feedback, Punktelisten und explanation dürfen sich inhaltlich NICHT
wiederholen — jede Information erscheint genau einmal, am passendsten Ort.
Alle Strings sind reiner Fließtext OHNE Markdown (kein **fett**, kein *kursiv*,
keine Aufzählungszeichen, keine Überschriften)."""


def build_rules_prompt(language: str, custom_rules: str | None = None) -> str:
    """Grading-rules system block: custom or default rules + fixed JSON format.

    Uses str.replace (not str.format) so braces in user text stay intact.
    Kept byte-identical across calls for prompt caching.
    """
    rules = (custom_rules or "").strip() or DEFAULT_GRADING_RULES
    return (rules + "\n\n" + OUTPUT_FORMAT_RULES).replace("{language}", language)


def build_system_blocks(
    language: str,
    script_text: str | None,
    use_cache_control: bool = True,
    custom_rules: str | None = None,
) -> list[dict[str, Any]]:
    """System blocks: [rules] [+ optional script block, cached ephemeral]."""
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": build_rules_prompt(language, custom_rules)}
    ]
    if script_text:
        script_block: dict[str, Any] = {
            "type": "text",
            "text": (
                "VORLESUNGSSKRIPT (nur zur Einordnung von Synonymen, Formulierungen "
                "und Notation — kein zusätzlicher Bewertungsmaßstab):\n\n" + script_text
            ),
        }
        if use_cache_control:
            script_block["cache_control"] = {"type": "ephemeral"}
        blocks.append(script_block)
    return blocks


def build_user_message(
    front: str, back: str, answer: str, io_hint: str | None = None, has_images: bool = False
) -> str:
    image_note = (
        "\nHINWEIS: Die Bilder der Karte sind diesem Prompt beigefügt. Beziehe sie "
        "in die Bewertung ein.\n"
        if has_images
        else ""
    )
    io_note = (
        f"""
IMAGE-OCCLUSION-KARTE:
Dies ist eine Image-Occlusion-Karte. Abgefragt wird der Inhalt des folgenden
Bildbereichs (im beigefügten Bild ist er NICHT maskiert — du siehst das Original):
{io_hint}
Bewertungsmaßstab ist, was im Originalbild an dieser Stelle steht/zu sehen ist
(plus ggf. der Rückseitentext). Bewerte, ob die Antwort des Lernenden genau
diesen Bereich korrekt benennt bzw. beschreibt.
"""
        if io_hint
        else ""
    )
    return f"""KARTENVORDERSEITE (Frage):
{front}
{image_note}{io_note}
KARTENRÜCKSEITE (Musterantwort / Bewertungsmaßstab):
{back}

ANTWORT DES LERNENDEN:
{answer}

Bewerte die Antwort des Lernenden jetzt gemäß den Regeln. Nur das JSON-Objekt ausgeben."""


def build_user_content(
    front: str,
    back: str,
    answer: str,
    images: list[tuple[str, str]] | None = None,
    io_hint: str | None = None,
) -> str | list[dict[str, Any]]:
    """User content: plain string, or image blocks + text block when images exist."""
    text = build_user_message(front, back, answer, io_hint, has_images=bool(images))
    if not images:
        return text
    blocks: list[dict[str, Any]] = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
        for media_type, data in images
    ]
    blocks.append({"type": "text", "text": text})
    return blocks


# ---------------------------------------------------------------------------
# Defensive JSON parsing
# ---------------------------------------------------------------------------

def parse_grading_json(text: str) -> GradingResult:
    """Parse the model output defensively (strip fences, locate the object).

    Raises GradingError if no usable JSON object can be extracted.
    """
    if not text or not text.strip():
        raise GradingError("Leere Antwort vom Modell.")

    cleaned = text.strip()
    # Strip markdown fences like ```json ... ```
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        if first_newline != -1:
            cleaned = cleaned[first_newline + 1 :]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
        cleaned = cleaned.strip()

    # Locate the outermost JSON object.
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise GradingError("Modellantwort enthält kein JSON-Objekt.")
    cleaned = cleaned[start : end + 1]

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise GradingError(f"Modellantwort ist kein gültiges JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise GradingError("Modellantwort ist kein JSON-Objekt.")

    try:
        score = int(data["score"])
        rating = int(data["rating"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GradingError(f"JSON unvollständig oder falsch typisiert: {exc}") from exc

    score = max(0, min(100, score))
    if rating not in (1, 2, 3, 4):
        raise GradingError(f"Ungültiges Rating: {rating}")

    def _str_list(key: str) -> list[str]:
        value = data.get(key, [])
        if isinstance(value, str):
            return [value] if value else []
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        return []

    def _str(key: str) -> str:
        value = data.get(key, "")
        return value if isinstance(value, str) else str(value)

    def _parse_source_pages(value: Any) -> list[tuple[str, int]]:
        """Accept ints, "file.pdf:12", "Seite 7", "skript.pdf S. 4" — max 4."""
        if isinstance(value, (int, str)):
            value = [value]
        if not isinstance(value, list):
            return []
        out: list[tuple[str, int]] = []
        for item in value[:4]:
            if isinstance(item, int):
                if item > 0:
                    out.append(("", item))
            elif isinstance(item, str):
                match = re.search(
                    r"^(?:(.+?)[\s:|,]+)??(?:S(?:eite)?\.?\s*)?(\d+)\s*$", item.strip()
                )
                if match:
                    page = int(match.group(2))
                    if page > 0:
                        out.append(((match.group(1) or "").strip(), page))
        return out

    return GradingResult(
        score=score,
        rating=rating,
        correct_points=_str_list("correct_points"),
        missing_points=_str_list("missing_points"),
        wrong_points=_str_list("wrong_points"),
        explanation=_str("explanation"),
        feedback=_str("feedback"),
        source_pages=_parse_source_pages(data.get("source_pages")),
    )


# ---------------------------------------------------------------------------
# HTTP calls
# ---------------------------------------------------------------------------

def _extract_text(response_json: dict[str, Any]) -> str:
    """Join all text blocks of a Messages-API-shaped response."""
    if response_json.get("stop_reason") == "refusal":
        raise GradingError("Das Modell hat die Anfrage abgelehnt (refusal).")
    parts = [
        block.get("text", "")
        for block in response_json.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "".join(parts)


def _resolve_anthropic_key(config: dict[str, Any]) -> str:
    key = (config.get("api_key") or "").strip() or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise GradingError(
            "Kein API-Key konfiguriert. Trage 'api_key' in der Addon-Config ein "
            "oder setze die Umgebungsvariable ANTHROPIC_API_KEY."
        )
    return key


def _post_anthropic(
    config: dict[str, Any],
    system_blocks: list[dict[str, Any]],
    user_content: str | list[dict[str, Any]],
    max_tokens: int = MAX_OUTPUT_TOKENS,
) -> dict[str, Any]:
    import requests

    payload = {
        "model": config.get("model") or "claude-sonnet-4-6",
        "max_tokens": max_tokens,
        "system": system_blocks,
        "messages": [{"role": "user", "content": user_content}],
    }
    headers = {
        "content-type": "application/json",
        "x-api-key": _resolve_anthropic_key(config),
        "anthropic-version": ANTHROPIC_VERSION,
    }
    timeout = float(config.get("request_timeout_s") or 60)

    response = requests.post(ANTHROPIC_URL, json=payload, headers=headers, timeout=timeout)
    if response.status_code == 429:
        retry_after = response.headers.get("retry-after")
        delay = min(float(retry_after) if retry_after else 2.0, 15.0)
        log.warning("Rate limit (429), retrying once after %.1fs", delay)
        time.sleep(delay)
        response = requests.post(ANTHROPIC_URL, json=payload, headers=headers, timeout=timeout)

    if response.status_code != 200:
        _raise_http_error(response.status_code, response.text)
    return response.json()


def _resolve_bedrock_bearer(config: dict[str, Any]) -> str:
    """Long-term Bedrock API key (Bearer token), config or env fallback."""
    return (config.get("bedrock_api_key") or "").strip() or os.environ.get(
        "AWS_BEARER_TOKEN_BEDROCK", ""
    )


def _resolve_aws_credentials(config: dict[str, Any]) -> tuple[str, str, str]:
    access = (config.get("aws_access_key_id") or "").strip() or os.environ.get(
        "AWS_ACCESS_KEY_ID", ""
    )
    secret = (config.get("aws_secret_access_key") or "").strip() or os.environ.get(
        "AWS_SECRET_ACCESS_KEY", ""
    )
    token = (config.get("aws_session_token") or "").strip() or os.environ.get(
        "AWS_SESSION_TOKEN", ""
    )
    if not access or not secret:
        raise GradingError(
            "Keine AWS-Credentials konfiguriert. Trage 'aws_access_key_id' und "
            "'aws_secret_access_key' in der Addon-Config ein oder setze die "
            "Umgebungsvariablen AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY."
        )
    return access, secret, token


def _post_bedrock(
    config: dict[str, Any],
    system_blocks: list[dict[str, Any]],
    user_content: str | list[dict[str, Any]],
    max_tokens: int = MAX_OUTPUT_TOKENS,
) -> dict[str, Any]:
    import requests

    region = (config.get("aws_region") or "").strip()
    model_id = (config.get("bedrock_model") or "").strip()
    if not region or not model_id:
        raise GradingError("Bedrock: 'aws_region' und 'bedrock_model' müssen gesetzt sein.")

    bearer = _resolve_bedrock_bearer(config)
    if not bearer:
        access, secret, token = _resolve_aws_credentials(config)

    url = (
        f"https://bedrock-runtime.{region}.amazonaws.com/model/"
        f"{urllib.parse.quote(model_id, safe='')}/invoke"
    )
    payload = {
        "anthropic_version": BEDROCK_ANTHROPIC_VERSION,
        "max_tokens": max_tokens,
        "system": system_blocks,
        "messages": [{"role": "user", "content": user_content}],
    }
    timeout = float(config.get("request_timeout_s") or 60)

    def _send(body_payload: dict[str, Any]) -> "requests.Response":
        body = json.dumps(body_payload).encode("utf-8")
        if bearer:
            # Long-term Bedrock API key: plain Bearer auth, no SigV4 needed.
            headers = {
                "content-type": "application/json",
                "accept": "application/json",
                "authorization": f"Bearer {bearer}",
            }
        else:
            headers = aws_sigv4.sign_request(
                method="POST",
                url=url,
                region=region,
                service="bedrock",
                access_key=access,
                secret_key=secret,
                session_token=token,
                body=body,
                extra_headers={"content-type": "application/json", "accept": "application/json"},
            )
        return requests.post(url, data=body, headers=headers, timeout=timeout)

    response = _send(payload)

    # Some Bedrock model IDs reject cache_control — retry once without it.
    if response.status_code == 400 and "cache_control" in response.text:
        log.warning("Bedrock rejected cache_control, retrying without prompt caching")
        stripped = [
            {k: v for k, v in block.items() if k != "cache_control"} for block in system_blocks
        ]
        response = _send({**payload, "system": stripped})

    if response.status_code == 429:
        log.warning("Bedrock throttled (429), retrying once after 2s")
        time.sleep(2.0)
        response = _send(payload)

    if response.status_code != 200:
        _raise_http_error(response.status_code, response.text)
    return response.json()


def _raise_http_error(status: int, body: str) -> None:
    detail = ""
    try:
        parsed = json.loads(body)
        detail = (
            parsed.get("error", {}).get("message", "")
            if isinstance(parsed.get("error"), dict)
            else parsed.get("message", "")
        ) or ""
    except (json.JSONDecodeError, AttributeError):
        detail = body[:200]
    raise GradingError(f"API-Fehler (HTTP {status}): {detail}")


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def _validate_credentials(config: dict[str, Any]) -> str:
    """Check provider + credentials up front (no network); return provider."""
    provider = (config.get("provider") or "anthropic").strip().lower()
    if provider == "anthropic":
        _resolve_anthropic_key(config)
    elif provider == "bedrock":
        if not _resolve_bedrock_bearer(config):
            _resolve_aws_credentials(config)
    else:
        raise GradingError(f"Unbekannter Provider: {provider!r}")
    return provider


def warm_cache(config: dict[str, Any], script_text: str) -> None:
    """Pre-warm the provider's prompt cache for a deck's script context.

    Sends a minimal request with the exact system blocks a real grading call
    would use, so the first real grading of the session hits a warm cache.
    Blocking — call from a background thread; raises GradingError on failure.
    """
    provider = _validate_credentials(config)
    language = config.get("feedback_language") or "Deutsch"
    system_blocks = build_system_blocks(
        language, script_text, custom_rules=config.get("custom_prompt")
    )
    warm_msg = "Warmup. Antworte nur mit: ok"
    if provider == "bedrock":
        _post_bedrock(config, system_blocks, warm_msg, max_tokens=1)
    else:
        _post_anthropic(config, system_blocks, warm_msg, max_tokens=1)


def grade_answer(
    config: dict[str, Any],
    front: str,
    back: str,
    answer: str,
    script_text: str | None = None,
    images: list[tuple[str, str]] | None = None,
    io_hint: str | None = None,
) -> GradingResult:
    """Grade an answer. Blocking — call from a background thread.

    `images`: (media_type, base64) tuples sent as image blocks before the text.
    `io_hint`: description of the queried Image-Occlusion region, if any.
    Performs one retry on JSON parse failure with a stricter instruction,
    then raises GradingError.
    """
    provider = _validate_credentials(config)

    try:
        import requests
    except ImportError as exc:  # pragma: no cover — always bundled in Anki
        raise GradingError("Die 'requests'-Library ist nicht verfügbar.") from exc

    language = config.get("feedback_language") or "Deutsch"
    system_blocks = build_system_blocks(
        language, script_text, custom_rules=config.get("custom_prompt")
    )
    user_content = build_user_content(front, back, answer, images, io_hint)

    def _call(content: str | list[dict[str, Any]]) -> str:
        try:
            if provider == "bedrock":
                response_json = _post_bedrock(config, system_blocks, content)
            else:
                response_json = _post_anthropic(config, system_blocks, content)
        except requests.exceptions.Timeout as exc:
            raise GradingError("Zeitüberschreitung beim API-Call.") from exc
        except requests.exceptions.RequestException as exc:
            raise GradingError(f"Netzwerkfehler: {exc}") from exc
        return _extract_text(response_json)

    text = _call(user_content)
    try:
        return parse_grading_json(text)
    except GradingError as first_error:
        log.warning("Parse failed (%s), retrying once with stricter instruction", first_error)
        strict_note = (
            "\n\nWICHTIG: Deine letzte Ausgabe war kein gültiges JSON. Gib jetzt "
            "AUSSCHLIESSLICH das JSON-Objekt aus, ohne jeglichen weiteren Text."
        )
        if isinstance(user_content, str):
            retry_content: str | list[dict[str, Any]] = user_content + strict_note
        else:
            retry_content = user_content + [{"type": "text", "text": strict_note}]
        return parse_grading_json(_call(retry_content))
