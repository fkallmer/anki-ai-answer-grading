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
keine Aufzählungszeichen, keine Überschriften). Innerhalb der Strings KEINE
geraden doppelten Anführungszeichen verwenden — nutze „deutsche" oder 'einfache'
Anführungszeichen, damit das JSON gültig bleibt."""


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


EXPLAIN_MODE_INSTRUCTION = """DER LERNENDE WEISS DIE ANTWORT NICHT und möchte direkt die Erklärung.
Setze "score": 0 und "rating": 1. Lasse correct_points und wrong_points leer.
"missing_points": die 2-4 Kernpunkte der Musterantwort als Stichpunkte.
"explanation": besonders klar und etwas ausführlicher als sonst (4-8 Sätze) —
eine gute Tutor-Erklärung zum Neulernen des Inhalts, mit dem wichtigsten
Zusammenhang. "feedback": ein einziger kurzer, sachlicher Satz."""


def build_user_message(
    front: str,
    back: str,
    answer: str,
    io_hint: str | None = None,
    has_images: bool = False,
    explain_mode: bool = False,
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
    if explain_mode:
        closing = (
            EXPLAIN_MODE_INSTRUCTION + "\n\nNur das JSON-Objekt ausgeben."
        )
    else:
        closing = f"""ANTWORT DES LERNENDEN:
{answer}

Bewerte die Antwort des Lernenden jetzt gemäß den Regeln. Nur das JSON-Objekt ausgeben."""
    return f"""KARTENVORDERSEITE (Frage):
{front}
{image_note}{io_note}
KARTENRÜCKSEITE (Musterantwort / Bewertungsmaßstab):
{back}

{closing}"""


def build_user_content(
    front: str,
    back: str,
    answer: str,
    images: list[tuple[str, str]] | None = None,
    io_hint: str | None = None,
    explain_mode: bool = False,
) -> str | list[dict[str, Any]]:
    """User content: plain string, or image blocks + text block when images exist."""
    text = build_user_message(
        front, back, answer, io_hint, has_images=bool(images), explain_mode=explain_mode
    )
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


def _extract_text_openai(response_json: dict[str, Any]) -> str:
    """Text from an OpenAI-compatible chat.completions response."""
    try:
        content = response_json["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise GradingError(f"Unerwartetes Antwortformat vom Provider: {exc}") from exc
    if isinstance(content, list):  # some servers return content parts
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return content or ""


def _post_openai(
    config: dict[str, Any],
    system_blocks: list[dict[str, Any]],
    user_content: str | list[dict[str, Any]],
    max_tokens: int = MAX_OUTPUT_TOKENS,
) -> dict[str, Any]:
    """OpenAI-compatible chat.completions (Gemini, OpenRouter, Groq, Ollama…)."""
    import requests

    base_url = (config.get("openai_base_url") or "").strip().rstrip("/")
    model = (config.get("openai_model") or "").strip()
    url = base_url + "/chat/completions"

    # cache_control is Anthropic-specific — system blocks become one string.
    system_text = "\n\n".join(block["text"] for block in system_blocks)

    if isinstance(user_content, str):
        oa_content: str | list[dict[str, Any]] = user_content
    else:
        oa_content = [
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{b['source']['media_type']};base64,{b['source']['data']}"
                },
            }
            if b.get("type") == "image"
            else {"type": "text", "text": b.get("text", "")}
            for b in user_content
        ]

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": oa_content},
        ],
    }
    headers = {"content-type": "application/json"}
    key = (config.get("openai_api_key") or "").strip() or os.environ.get("OPENAI_API_KEY", "")
    if key:  # local servers like Ollama need no key
        headers["authorization"] = f"Bearer {key}"
    timeout = float(config.get("request_timeout_s") or 60)

    response = requests.post(url, json=payload, headers=headers, timeout=timeout)
    if response.status_code == 429:
        retry_after = response.headers.get("retry-after")
        delay = min(float(retry_after) if retry_after else 2.0, 15.0)
        log.warning("Rate limit (429), retrying once after %.1fs", delay)
        time.sleep(delay)
        response = requests.post(url, json=payload, headers=headers, timeout=timeout)

    if response.status_code != 200:
        _raise_http_error(response.status_code, response.text)
    return response.json()


CLI_SEARCH_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/bin"),
)


def _find_cli_binary(config: dict[str, Any]) -> str:
    """Resolve the CLI binary path. Anki (GUI app) has a minimal PATH on
    macOS, so we search common install locations explicitly."""
    import shutil

    configured = (config.get("cli_path") or "").strip()
    if configured:
        if os.path.isfile(configured):
            return configured
        raise GradingError(f"CLI nicht gefunden unter: {configured}")

    name = "claude" if (config.get("cli_type") or "claude") == "claude" else "gemini"
    found = shutil.which(name)
    if found:
        return found
    for directory in CLI_SEARCH_DIRS:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate):
            return candidate
    raise GradingError(
        f"'{name}' CLI nicht gefunden. Bitte installieren (und ggf. den vollen "
        "Pfad in den Einstellungen unter 'CLI-Pfad' eintragen)."
    )


def _run_cli(
    config: dict[str, Any],
    system_blocks: list[dict[str, Any]],
    user_content: str | list[dict[str, Any]],
) -> str:
    """Grade via a local CLI (Claude Code CLI or Gemini CLI) — no API key.

    Claude CLI bills against the user's Claude subscription; Gemini CLI has a
    free tier. Prompt goes in via stdin (no arg-length limits); images are
    not supported on this provider.
    """
    import subprocess

    binary = _find_cli_binary(config)
    cli_type = (config.get("cli_type") or "claude").strip().lower()
    model = (config.get("cli_model") or "").strip()

    system_text = "\n\n".join(block["text"] for block in system_blocks)
    if isinstance(user_content, list):
        log.info("CLI-Provider: Bilder werden nicht unterstützt und übersprungen.")
        user_text = "\n".join(
            b.get("text", "") for b in user_content if b.get("type") == "text"
        )
    else:
        user_text = user_content
    prompt = system_text + "\n\n" + user_text

    if cli_type == "gemini":
        cmd = [binary]
        if model:
            cmd += ["-m", model]
    else:
        cmd = [binary, "-p", "--output-format", "text"]
        if model:
            cmd += ["--model", model]

    # CLI startup + inference is slower than a raw API call.
    timeout = max(float(config.get("request_timeout_s") or 60), 120.0)
    try:
        proc = subprocess.run(
            cmd,
            input=prompt.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GradingError(f"CLI-Zeitüberschreitung nach {timeout:.0f}s.") from exc
    except OSError as exc:
        raise GradingError(f"CLI konnte nicht gestartet werden: {exc}") from exc

    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise GradingError(
            f"CLI-Fehler (Exit {proc.returncode}): {(stderr or stdout)[:300]}"
        )
    if not stdout:
        raise GradingError("CLI lieferte keine Ausgabe.")
    return stdout


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
    elif provider == "openai":
        if not (config.get("openai_base_url") or "").strip():
            raise GradingError(
                "OpenAI-kompatibler Provider: 'openai_base_url' muss gesetzt sein "
                "(z. B. http://localhost:11434/v1 für Ollama)."
            )
        if not (config.get("openai_model") or "").strip():
            raise GradingError("OpenAI-kompatibler Provider: 'openai_model' muss gesetzt sein.")
        # API key is optional — local servers (Ollama, LM Studio) need none.
    elif provider == "cli":
        cli_type = (config.get("cli_type") or "claude").strip().lower()
        if cli_type not in ("claude", "gemini"):
            raise GradingError(f"Unbekannter cli_type: {cli_type!r} (claude oder gemini).")
        _find_cli_binary(config)  # raises with install hint if missing
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
    if provider in ("openai", "cli"):
        return  # no Anthropic-style prompt caching to warm on these providers
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
    explain_mode: bool = False,
) -> GradingResult:
    """Grade an answer. Blocking — call from a background thread.

    `images`: (media_type, base64) tuples sent as image blocks before the text.
    `io_hint`: description of the queried Image-Occlusion region, if any.
    `explain_mode`: user gave up — request a thorough explanation instead of
    grading; the model returns score 0 / rating 1 (Again).
    Performs one retry on JSON parse failure with a stricter instruction,
    then raises GradingError.
    """
    provider = _validate_credentials(config)

    if provider != "cli":  # CLI runs as subprocess, no HTTP library needed
        try:
            import requests
        except ImportError as exc:  # pragma: no cover — always bundled in Anki
            raise GradingError("Die 'requests'-Library ist nicht verfügbar.") from exc

    language = config.get("feedback_language") or "Deutsch"
    system_blocks = build_system_blocks(
        language, script_text, custom_rules=config.get("custom_prompt")
    )
    user_content = build_user_content(front, back, answer, images, io_hint, explain_mode)

    def _call(content: str | list[dict[str, Any]]) -> str:
        if provider == "cli":
            return _run_cli(config, system_blocks, content)
        try:
            if provider == "bedrock":
                return _extract_text(_post_bedrock(config, system_blocks, content))
            if provider == "openai":
                return _extract_text_openai(_post_openai(config, system_blocks, content))
            return _extract_text(_post_anthropic(config, system_blocks, content))
        except requests.exceptions.Timeout as exc:
            raise GradingError("Zeitüberschreitung beim API-Call.") from exc
        except requests.exceptions.RequestException as exc:
            raise GradingError(f"Netzwerkfehler: {exc}") from exc

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
