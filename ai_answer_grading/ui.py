"""Reviewer UI: answer box on the question side, feedback panel on the
answer side, ease-button suggestion, optional auto-answer.

All aqt interaction lives here; grading logic is in grader.py.
"""

from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
import urllib.parse
from concurrent.futures import Future
from typing import Any, Optional

from aqt import mw
from aqt.qt import QTimer
from aqt.utils import showText, tooltip

from . import context_store, grader, media, pdf_render
from .grader import GradingError, GradingResult

log = logging.getLogger("ai_answer_grading")

PANEL_ID = "ai-grading-panel"
RATING_COLORS = {1: "#d43f3f", 2: "#d4933f", 3: "#3fa650", 4: "#3f7ad4"}


def _fmt(text: str) -> str:
    """Escape for HTML, then render leftover markdown bold/italic properly
    (models occasionally emit **bold** despite instructions)."""
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", escaped)
    return escaped.replace("\n", "<br>")


def _strip_html(text: str) -> str:
    """Plain text from rendered card HTML (works across Anki versions)."""
    try:
        from anki.utils import strip_html  # modern
        result = strip_html(text)
    except ImportError:
        try:
            from anki.utils import stripHTML  # legacy
            result = stripHTML(text)
        except ImportError:
            result = re.sub(r"<[^>]+>", " ", text)
    result = re.sub(r"\[sound:[^\]]+\]", "", result)
    return re.sub(r"[ \t]+", " ", result).strip()


class GradingSession:
    """Per-card grading state (one active grading at a time)."""

    def __init__(self) -> None:
        self.card_id: Optional[int] = None
        self.status: str = "idle"  # idle | pending | done | error
        self.result: Optional[GradingResult] = None
        self.error: str = ""
        self.io_used: bool = False  # graded via occlusion coordinates
        self.slides: list[tuple[str, str]] = []  # (label, data_uri) rendered slides
        self.slide_refs: list[str] = []  # text fallback when rendering unavailable

    def reset(self) -> None:
        self.card_id = None
        self.status = "idle"
        self.result = None
        self.error = ""
        self.io_used = False
        self.slides = []
        self.slide_refs = []


session = GradingSession()


def _config() -> dict[str, Any]:
    return mw.addonManager.getConfig(__name__.split(".")[0]) or {}


def _cache_dir() -> str:
    addon_id = __name__.split(".")[0]
    return os.path.join(mw.addonManager.addonsFolder(addon_id), "user_files", "context_cache")


# ---------------------------------------------------------------------------
# HTML injection (card_will_show filter)
# ---------------------------------------------------------------------------

def _deck_name_for_card(card: Any) -> str:
    try:
        return mw.col.decks.name(card.odid or card.did)
    except Exception:
        return ""


def _context_label_html(card: Any) -> str:
    """Small line showing which script document(s) back this deck, if any."""
    files = context_store.resolve_deck_files(
        _deck_name_for_card(card), _config().get("deck_context_map") or {}
    )
    names = ", ".join(os.path.basename(f) for f in files)
    if not names:
        return ""
    return (
        '<div style="margin-top:0.35em; font-size:0.8em; opacity:0.6;">'
        f"📄 Skript geladen: {html.escape(names)}</div>"
    )


def _answer_box_html(card: Any) -> str:
    auto_checked = "checked" if _config().get("auto_answer") else ""
    context_label = _context_label_html(card)
    return f"""
<div id="ai-answer-box" style="margin-top:1.5em; padding:0.8em; border-top:1px solid #8884;
     text-align:left; font-size:0.9em;">
  <textarea id="ai-answer-input" rows="4" placeholder="Deine Antwort eintippen … (leer lassen für normales Aufdecken)"
    style="width:100%; box-sizing:border-box; padding:0.5em; border:1px solid #8886;
           border-radius:6px; background:transparent; color:inherit; font:inherit;
           resize:vertical;"></textarea>
  <div style="margin-top:0.4em; display:flex; align-items:center; gap:1em; flex-wrap:wrap;">
    <button id="ai-grade-btn"
      style="padding:0.4em 1.1em; border:1px solid #8886; border-radius:6px;
             background:transparent; color:inherit; cursor:pointer;">
      Bewerten (Strg+Enter)</button>
    <label style="display:flex; align-items:center; gap:0.35em; cursor:pointer;
                  opacity:0.85; user-select:none;">
      <input type="checkbox" id="ai-auto-toggle" {auto_checked}>
      Auto-Modus <span style="opacity:0.6;">(Good/Easy automatisch übernehmen)</span>
    </label>
  </div>
  {context_label}
</div>
<script>
(function() {{
  var ta = document.getElementById('ai-answer-input');
  var btn = document.getElementById('ai-grade-btn');
  var auto = document.getElementById('ai-auto-toggle');
  if (!ta || !btn) {{ return; }}
  function send() {{
    if (btn.disabled) {{ return; }}
    var v = ta.value.trim();
    if (!v) {{ return; }}
    btn.disabled = true;
    btn.textContent = 'Bewerte…';
    ta.readOnly = true;
    pycmd('ai_grade:' + encodeURIComponent(v));
  }}
  btn.addEventListener('click', send);
  ta.addEventListener('keydown', function(e) {{
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {{ e.preventDefault(); send(); }}
  }});
  if (auto) {{
    auto.addEventListener('change', function() {{
      pycmd('ai_auto:' + (auto.checked ? '1' : '0'));
    }});
  }}
  setTimeout(function() {{ ta.focus(); }}, 100);
}})();
</script>
"""

PANEL_PENDING_HTML = (
    f'<div id="{PANEL_ID}" style="margin-top:1.5em; padding:0.9em; border:1px solid #8884;'
    ' border-radius:8px; text-align:left; font-size:0.9em;">'
    "⏳ KI-Bewertung läuft&nbsp;…</div>"
)


def on_card_will_show(text: str, card: Any, kind: str) -> str:
    """Append the answer box (question) or feedback panel (answer)."""
    if kind == "reviewQuestion":
        if session.card_id != card.id:
            session.reset()
        _maybe_warm_cache(card)
        return text + _answer_box_html(card)

    if kind == "reviewAnswer" and session.card_id == card.id and session.status != "idle":
        if session.status == "pending":
            return text + PANEL_PENDING_HTML
        return text + _panel_html()

    return text


def _panel_html() -> str:
    """Feedback panel for the current session result/error."""
    if session.status == "error":
        body = (
            "<b>⚠️ KI-Bewertung fehlgeschlagen</b><br>"
            f"{html.escape(session.error)}<br>"
            "<i>Bitte manuell bewerten — der normale Review-Ablauf ist nicht betroffen.</i>"
        )
        return (
            f'<div id="{PANEL_ID}" style="margin-top:1.5em; padding:0.9em;'
            ' border:1px solid #d4933f; border-radius:8px; text-align:left;'
            ' font-size:0.9em;">' + body + "</div>"
        )

    r = session.result
    if r is None:
        return f'<div id="{PANEL_ID}"></div>'

    color = RATING_COLORS.get(r.rating, "#888")

    def _points(title: str, symbol: str, items: list[str]) -> str:
        if not items:
            return ""
        lis = "".join(f"<li>{_fmt(i)}</li>" for i in items)
        return (
            f'<div style="margin-top:0.5em;"><b>{symbol} {title}</b>'
            f'<ul style="margin:0.2em 0 0 1.2em; padding:0;">{lis}</ul></div>'
        )

    auto = _config().get("auto_answer", False)
    if auto and r.rating >= 3:
        hint = "Auto: wird übernommen …"
    elif auto:
        hint = "Auto pausiert — Strg+Enter bestätigt, 1–4 überstimmt."
    else:
        hint = "Strg+Enter bestätigt, 1–4 überstimmt."

    return f"""
<div id="{PANEL_ID}" style="margin-top:1.5em; padding:0.9em; border:1px solid {color};
     border-radius:8px; text-align:left; font-size:0.9em;">
  <div style="display:flex; gap:1em; align-items:center; flex-wrap:wrap;">
    <span style="font-size:1.4em; font-weight:bold;">{r.score}<span style="font-size:0.6em;">/100</span></span>
    <span style="background:{color}; color:white; padding:0.15em 0.7em; border-radius:99px;
          font-weight:bold;">{r.rating} · {html.escape(r.rating_label)}</span>
    <span style="opacity:0.7; font-size:0.85em;">{hint}</span>
  </div>
  <div style="margin-top:0.6em;">{_fmt(r.feedback)}</div>
  {_points("Korrekt", "✅", r.correct_points)}
  {_points("Fehlt", "❓", r.missing_points)}
  {_points("Falsch", "❌", r.wrong_points)}
  {_explanation_html(r.explanation)}
  {_slides_html()}
  {_io_note_html()}
</div>"""


def _slides_html() -> str:
    """Rendered lecture slides (or a text reference as fallback)."""
    parts = []
    for label, data_uri in session.slides:
        parts.append(
            f'<div style="margin-top:0.7em;"><b>📑 {html.escape(label)}</b><br>'
            f'<img src="{data_uri}" style="max-width:100%; border:1px solid #8884;'
            ' border-radius:6px; margin-top:0.3em;"></div>'
        )
    if not parts and session.slide_refs:
        refs = ", ".join(html.escape(ref) for ref in session.slide_refs)
        parts.append(
            f'<div style="margin-top:0.6em; font-size:0.85em; opacity:0.75;">📑 Im Skript: {refs}</div>'
        )
    return "".join(parts)


def _io_note_html() -> str:
    if not session.io_used:
        return ""
    return (
        '<div style="margin-top:0.5em; font-size:0.8em; opacity:0.6;">'
        "ℹ️ IO-Karte: Bewertung koordinatenbasiert — bei dichten Beschriftungen "
        "ggf. ungenau.</div>"
    )


def _explanation_html(explanation: str) -> str:
    if not explanation.strip():
        return ""
    return (
        '<div style="margin-top:0.7em; padding:0.6em 0.8em; border-left:3px solid #3f7ad4;'
        ' background:#3f7ad414; border-radius:0 6px 6px 0;">'
        f"<b>💡 Erklärung</b><br>{_fmt(explanation)}</div>"
    )


# ---------------------------------------------------------------------------
# pycmd bridge
# ---------------------------------------------------------------------------

def on_js_message(handled: tuple[bool, Any], message: str, context: Any) -> tuple[bool, Any]:
    if message.startswith("ai_grade:"):
        answer = urllib.parse.unquote(message[len("ai_grade:"):]).strip()
        if answer:
            _start_grading(answer)
        return (True, None)

    if message.startswith("ai_auto:"):
        enabled = message[len("ai_auto:"):] == "1"
        _set_auto_answer(enabled)
        return (True, None)

    return handled


# Checkable menu action, set by hooks.register() — kept in sync with the
# in-card toggle so both surfaces always show the same state.
auto_action: Any = None


def _set_auto_answer(enabled: bool) -> None:
    """Persist the auto-mode toggle (from review UI or Tools menu)."""
    addon_id = __name__.split(".")[0]
    config = _config()
    if bool(config.get("auto_answer")) != enabled:
        config["auto_answer"] = enabled
        mw.addonManager.writeConfig(addon_id, config)
        tooltip("Auto-Modus " + ("aktiviert — Rating wird automatisch übernommen"
                                 if enabled else "deaktiviert"))
    if auto_action is not None and auto_action.isChecked() != enabled:
        auto_action.blockSignals(True)
        auto_action.setChecked(enabled)
        auto_action.blockSignals(False)


def _start_grading(answer: str) -> None:
    """Reveal the answer and grade in the background. Never blocks the UI."""
    reviewer = mw.reviewer
    card = reviewer.card if reviewer else None
    if card is None:
        return

    config = _config()
    session.reset()
    session.card_id = card.id
    session.status = "pending"

    question_html = card.question()
    answer_html = card.answer()
    front = _strip_html(question_html)
    back = _strip_html(answer_html)
    # card.answer() usually contains the rendered front too — that is fine
    # for the LLM, it sees question context plus the reference answer.

    images = _gather_images(question_html, answer_html, config)
    io_hint = _io_hint(card)
    session.io_used = io_hint is not None

    try:
        deck_name = mw.col.decks.name(card.odid or card.did)
    except Exception:
        deck_name = ""

    # Reveal the answer (private API — guarded, since there is no public way).
    if reviewer.state == "question":
        if hasattr(reviewer, "_showAnswer"):
            reviewer._showAnswer()
        else:
            log.warning("Reviewer._showAnswer not available in this Anki version")

    def task() -> GradingResult:
        script_text = context_store.get_context_for_deck(
            deck_name,
            config.get("deck_context_map") or {},
            _cache_dir(),
            int(config.get("max_context_chars") or 150000),
        )
        if config.get("debug_logging"):
            log.info(
                "Grading card %s (deck %r, provider %r, context %s chars, "
                "%d Bild(er), IO=%s)",
                card.id,
                deck_name,
                config.get("provider", "anthropic"),
                len(script_text) if script_text else 0,
                len(images),
                bool(io_hint),
            )
        return grader.grade_answer(
            config, front, back, answer, script_text, images=images, io_hint=io_hint
        )

    mw.taskman.run_in_background(task, lambda fut: _on_grading_done(fut, card.id))


# Decks whose script context was already pre-warmed this Anki session.
_warmed_decks: set = set()


def _maybe_warm_cache(card: Any) -> None:
    """On the first card of a mapped deck: extract the script (fills the PDF
    cache) and fire a minimal API call so the provider's prompt cache is warm
    before the first real grading. Fire-and-forget; failures only log."""
    config = _config()
    mapping = config.get("deck_context_map") or {}
    if not mapping:
        return
    deck_name = _deck_name_for_card(card)
    if not deck_name or deck_name in _warmed_decks:
        return
    if not context_store.resolve_deck_files(deck_name, mapping):
        return
    _warmed_decks.add(deck_name)

    def task() -> None:
        script_text = context_store.get_context_for_deck(
            deck_name, mapping, _cache_dir(), int(config.get("max_context_chars") or 150000)
        )
        if script_text:
            grader.warm_cache(config, script_text)

    def on_done(future: Future) -> None:
        try:
            future.result()
            log.info("Prompt-Cache für Deck %r vorgewärmt.", deck_name)
        except Exception as exc:
            # Warming is an optimization — never bother the user about it.
            log.warning("Cache-Vorwärmen für %r fehlgeschlagen: %s", deck_name, exc)

    mw.taskman.run_in_background(task, on_done)


def on_ctrl_enter() -> None:
    """Review-state shortcut: on the question, submit the typed answer; on the
    answer, confirm the suggested rating."""
    reviewer = mw.reviewer
    if not reviewer or not reviewer.card:
        return
    if reviewer.state == "question":
        # The Qt shortcut may swallow the key before the textarea's JS handler
        # sees it — click the button from here instead (send() double-guards).
        reviewer.web.eval(
            "(function(){var b=document.getElementById('ai-grade-btn');"
            "if(b && !b.disabled){b.click();}})();"
        )
    elif (
        reviewer.state == "answer"
        and session.card_id == reviewer.card.id
        and session.status == "done"
        and session.result is not None
    ):
        _auto_answer(reviewer.card.id, session.result.rating)


def _gather_images(
    question_html: str, answer_html: str, config: dict[str, Any]
) -> list[tuple[str, str]]:
    """Base64 images referenced on the card (question first, then answer)."""
    if not config.get("send_images", True):
        return []
    names = media.extract_image_filenames(question_html)
    for name in media.extract_image_filenames(answer_html):
        if name not in names:
            names.append(name)
    if not names:
        return []
    try:
        media_dir = mw.col.media.dir()
    except Exception:
        return []
    return media.load_images(names, media_dir, int(config.get("max_images") or 3))


def _io_hint(card: Any) -> Optional[str]:
    """Occlusion-region description for native Anki Image-Occlusion cards."""
    try:
        note = card.note()
        if "Occlusion" not in note.keys():
            return None
        shapes = media.parse_occlusion_field(note["Occlusion"], card.ord + 1)
        if not shapes:
            return None
        return media.occlusion_hint(shapes)
    except Exception:
        log.exception("Image-Occlusion detection failed")
        return None


def _on_grading_done(future: Future, card_id: int) -> None:
    """Main-thread callback: render feedback, mark/press the ease button."""
    if session.card_id != card_id:
        return  # user moved on — discard silently

    try:
        result = future.result()
        session.status = "done"
        session.result = result
        _prepare_slides(card_id, result)
    except GradingError as exc:
        session.status = "error"
        session.error = str(exc)
        log.warning("Grading failed: %s", exc)
    except Exception as exc:
        session.status = "error"
        session.error = f"Unerwarteter Fehler: {exc}"
        log.exception("Unexpected grading error")

    reviewer = mw.reviewer
    if not reviewer or not reviewer.card or reviewer.card.id != card_id:
        return
    if reviewer.state != "answer":
        return  # panel will be rendered by card_will_show once revealed

    _render_panel_into_webview()

    if session.status == "done" and session.result is not None:
        rating = session.result.rating
        _highlight_ease_button(rating)
        _maybe_schedule_auto_answer(card_id, rating)


def _prepare_slides(card_id: int, result: GradingResult) -> None:
    """Render the model-cited script slides to data URIs (main thread only).

    Falls back to text references when QtPdf is unavailable or a page
    cannot be rendered."""
    config = _config()
    if not config.get("show_source_slides", True) or not result.source_pages:
        return
    try:
        card = mw.col.get_card(card_id)
        deck_name = mw.col.decks.name(card.odid or card.did)
    except Exception:
        return
    files = context_store.resolve_deck_files(
        deck_name, config.get("deck_context_map") or {}
    )
    pdfs = [f for f in files if f.lower().endswith(".pdf")]
    if not pdfs:
        return
    slide_cache = os.path.join(
        mw.addonManager.addonsFolder(__name__.split(".")[0]), "user_files", "slide_cache"
    )
    for fname, page in result.source_pages[:2]:
        target = pdfs[0]
        if fname:
            for path in pdfs:
                if fname.lower() in os.path.basename(path).lower():
                    target = path
                    break
        label = f"Folie {page} — {os.path.basename(target)}"
        png_path = pdf_render.render_page_png(target, page, slide_cache)
        if png_path:
            try:
                with open(png_path, "rb") as f:
                    encoded = base64.b64encode(f.read()).decode("ascii")
                session.slides.append((label, "data:image/png;base64," + encoded))
                continue
            except OSError:
                pass
        session.slide_refs.append(label)


def _render_panel_into_webview() -> None:
    """Replace the pending panel with the final feedback (or append it)."""
    panel = _panel_html()
    js = f"""
(function() {{
  var el = document.getElementById({json.dumps(PANEL_ID)});
  var html = {json.dumps(panel)};
  if (el) {{ el.outerHTML = html; }}
  else {{
    var d = document.createElement('div');
    d.innerHTML = html;
    document.body.appendChild(d.firstElementChild);
  }}
}})();"""
    mw.reviewer.web.eval(js)


def _highlight_ease_button(rating: int) -> None:
    """Outline the suggested ease button. Retries, since the answer buttons
    may render slightly after us; runs in both the bottom bar and the main
    webview to cover different Anki versions/layouts."""
    color = RATING_COLORS.get(rating, "#888")
    js = f"""
(function() {{
  function mark() {{
    var hits = 0;
    var btns = document.querySelectorAll('button');
    for (var i = 0; i < btns.length; i++) {{
      var oc = (btns[i].getAttribute('onclick') || '') + ' ' + (btns[i].outerHTML || '');
      var de = btns[i].getAttribute('data-ease') || '';
      if (de === '{rating}' || oc.indexOf('ease{rating}') !== -1) {{
        btns[i].style.setProperty('outline', '3px solid {color}', 'important');
        btns[i].style.setProperty('outline-offset', '1px', 'important');
        btns[i].style.setProperty('box-shadow', '0 0 8px {color}', 'important');
        btns[i].title = 'KI-Vorschlag';
        hits++;
      }}
    }}
    return hits;
  }}
  var tries = 0;
  (function attempt() {{
    if (mark() === 0 && tries++ < 6) {{ setTimeout(attempt, 300); }}
  }})();
}})();"""
    try:
        mw.bottomWeb.eval(js)
    except Exception:
        log.exception("bottomWeb highlight failed")
    try:
        if mw.reviewer and mw.reviewer.web:
            mw.reviewer.web.eval(js)
    except Exception:
        log.exception("reviewer web highlight failed")


def _maybe_schedule_auto_answer(card_id: int, rating: int) -> None:
    """Auto-advance only on Good/Easy; Again/Hard always waits for the user,
    so wrong answers get read (and can be overridden) instead of flying by."""
    if not _config().get("auto_answer"):
        return
    if rating < 3:
        tooltip("Auto-Modus pausiert: Again/Hard bitte selbst bestätigen.")
        return
    delay = int(_config().get("auto_answer_delay_ms") or 2500)
    QTimer.singleShot(delay, lambda: _auto_answer(card_id, rating))


def _auto_answer(card_id: int, rating: int) -> None:
    """Press the suggested ease automatically (guarded private API)."""
    reviewer = mw.reviewer
    if (
        not reviewer
        or not reviewer.card
        or reviewer.card.id != card_id
        or reviewer.state != "answer"
        or session.card_id != card_id
    ):
        return
    if hasattr(reviewer, "_answerCard"):
        reviewer._answerCard(rating)
    else:
        log.warning("Reviewer._answerCard not available — rating not auto-applied")
        tooltip("Auto-Answer nicht verfügbar, bitte manuell bewerten.")


def on_reviewer_did_show_answer(card: Any) -> None:
    """If grading finished before the answer was revealed, decorate now."""
    if session.card_id == card.id and session.status == "done" and session.result:
        rating = session.result.rating
        _highlight_ease_button(rating)
        _maybe_schedule_auto_answer(card.id, rating)


# ---------------------------------------------------------------------------
# Menu: assign lecture scripts to a deck via file dialog
# ---------------------------------------------------------------------------

def assign_deck_context() -> None:
    """Pick a deck and its script files (PDF/TXT/MD) via dialogs — writes
    the result into deck_context_map, no manual config editing needed."""
    from aqt.qt import QFileDialog
    from aqt.utils import askUser, chooseList

    deck_names = sorted(d.name for d in mw.col.decks.all_names_and_ids())
    if not deck_names:
        tooltip("Keine Decks vorhanden.")
        return
    idx = chooseList(
        "Für welches Deck soll ein Vorlesungsskript hinterlegt werden?\n"
        "(gilt automatisch auch für alle Subdecks)",
        deck_names,
    )
    deck = deck_names[idx]

    addon_id = __name__.split(".")[0]
    config = _config()
    mapping = dict(config.get("deck_context_map") or {})
    existing = mapping.get(deck)

    files, _filter = QFileDialog.getOpenFileNames(
        mw,
        f"Skriptdateien für „{deck}“ wählen",
        os.path.expanduser("~"),
        "Skripte (*.pdf *.txt *.md);;Alle Dateien (*)",
    )

    if files:
        mapping[deck] = files
        config["deck_context_map"] = mapping
        mw.addonManager.writeConfig(addon_id, config)
        tooltip(f"{len(files)} Datei(en) für „{deck}“ hinterlegt.", period=3000)
    elif existing and askUser(
        f"Keine Dateien gewählt. Bestehende Zuordnung für „{deck}“ entfernen?\n\n"
        + "\n".join(existing if isinstance(existing, list) else [existing])
    ):
        del mapping[deck]
        config["deck_context_map"] = mapping
        mw.addonManager.writeConfig(addon_id, config)
        tooltip(f"Zuordnung für „{deck}“ entfernt.", period=3000)


# ---------------------------------------------------------------------------
# Debug: test grading call from the Tools menu
# ---------------------------------------------------------------------------

def run_debug_grading() -> None:
    """Fire a sample grading call against the API to verify config and key."""
    config = _config()
    front = "Was ist die Hauptstadt von Frankreich?"
    back = "Paris"
    answer = "Ich glaube Paris, bin aber nicht sicher."

    def task() -> GradingResult:
        return grader.grade_answer(config, front, back, answer, None)

    def on_done(future: Future) -> None:
        try:
            r = future.result()
            showText(
                "Testaufruf erfolgreich ✅\n\n"
                f"Provider: {config.get('provider', 'anthropic')}\n"
                f"Score: {r.score}/100\n"
                f"Rating: {r.rating} ({r.rating_label})\n"
                f"Feedback: {r.feedback}\n"
                f"Erklärung: {r.explanation}\n"
                f"Korrekt: {r.correct_points}\n"
                f"Fehlt: {r.missing_points}\n"
                f"Falsch: {r.wrong_points}",
                title="AI Answer Grading — Test",
            )
        except GradingError as exc:
            showText(f"Testaufruf fehlgeschlagen ❌\n\n{exc}", title="AI Answer Grading — Test")
        except Exception as exc:
            showText(
                f"Unerwarteter Fehler beim Testaufruf ❌\n\n{exc!r}",
                title="AI Answer Grading — Test",
            )

    tooltip("AI Answer Grading: Testaufruf läuft …")
    mw.taskman.run_in_background(task, on_done)
