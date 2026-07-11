"""First-run setup wizard: pick a provider, get a (free) key, test, done.

Shown once when no credentials are configured; reachable later via
Tools → AI Answer Grading → Einrichtung starten…
"""

from __future__ import annotations

import os
from concurrent.futures import Future
from typing import Any

from aqt import mw
from aqt.qt import (
    QButtonGroup,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)
from aqt.utils import tooltip

from . import grader

# (id, title, subtitle, key_url)
CHOICES = [
    (
        "gemini",
        "Google Gemini — kostenlos, empfohlen zum Start",
        "Gratis-Kontingent, reicht locker für tägliches Lernen. Du brauchst nur ein Google-Konto.",
        "https://aistudio.google.com/apikey",
    ),
    (
        "anthropic",
        "Anthropic Claude — beste Qualität (kostenpflichtig)",
        "Präziseste Bewertungen und Erklärungen; wenige Cent pro Lernsession.",
        "https://console.anthropic.com/settings/keys",
    ),
    (
        "ollama",
        "Ollama — lokal auf deinem Rechner (kein Key, privat)",
        "Kostenlos und offline; erfordert installiertes Ollama und ein geladenes Modell.",
        "https://ollama.com/download",
    ),
    (
        "other",
        "Anderer Dienst (AWS Bedrock, OpenRouter, Groq, LM Studio …)",
        "Öffnet die vollständigen Einstellungen.",
        "",
    ),
]

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
GEMINI_MODEL = "gemini-2.5-flash"
OLLAMA_BASE_URL = "http://localhost:11434/v1"


def _addon_id() -> str:
    return __name__.split(".")[0]


def _has_any_credentials(config: dict[str, Any]) -> bool:
    return bool(
        (config.get("api_key") or "").strip()
        or (config.get("bedrock_api_key") or "").strip()
        or (config.get("aws_access_key_id") or "").strip()
        or (config.get("openai_api_key") or "").strip()
        or (config.get("openai_base_url") or "").strip()
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
        or os.environ.get("AWS_ACCESS_KEY_ID")
    )


class SetupWizard(QDialog):
    def __init__(self) -> None:
        super().__init__(mw)
        self.setWindowTitle("AI Answer Grading — Einrichtung")
        self.setMinimumWidth(560)
        self.config: dict[str, Any] = mw.addonManager.getConfig(_addon_id()) or {}
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "<b>Willkommen!</b> Dieses Addon bewertet deine getippten Antworten mit "
            "einem KI-Modell.<br>Wähle, worüber die Bewertung laufen soll — "
            "du kannst das später jederzeit ändern."
        ))

        self.group = QButtonGroup(self)
        for i, (_id, title, subtitle, _url) in enumerate(CHOICES):
            radio = QRadioButton(title)
            radio.setChecked(i == 0)
            self.group.addButton(radio, i)
            layout.addWidget(radio)
            sub = QLabel(f'<span style="color:gray; font-size:11px;">{subtitle}</span>')
            sub.setContentsMargins(24, 0, 0, 6)
            layout.addWidget(sub)
        self.group.idToggled.connect(lambda *_: self._update_fields())

        self.key_link = QLabel("")
        self.key_link.setOpenExternalLinks(True)
        layout.addWidget(self.key_link)

        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.key_input)

        self.model_input = QLineEdit()
        self.model_input.setPlaceholderText("Ollama-Modell, z. B. qwen2.5:32b")
        layout.addWidget(self.model_input)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        self.test_btn = QPushButton("Verbindung testen && speichern")
        self.test_btn.clicked.connect(self._save_and_test)
        buttons.addWidget(self.test_btn)
        later_btn = QPushButton("Später einrichten")
        later_btn.clicked.connect(self._later)
        buttons.addWidget(later_btn)
        buttons.addStretch()
        layout.addLayout(buttons)

        self._update_fields()

    def _choice(self) -> str:
        return CHOICES[max(0, self.group.checkedId())][0]

    def _update_fields(self) -> None:
        choice = self._choice()
        url = next(u for cid, _t, _s, u in CHOICES if cid == choice)
        self.key_link.setText(
            f'→ <a href="{url}">Hier kostenlosen API-Key holen</a>' if choice == "gemini"
            else f'→ <a href="{url}">API-Key erstellen</a>' if choice == "anthropic"
            else f'→ <a href="{url}">Ollama herunterladen</a>' if choice == "ollama"
            else ""
        )
        self.key_input.setVisible(choice in ("gemini", "anthropic"))
        self.key_input.setPlaceholderText(
            "AIza… (Gemini-API-Key hier einfügen)" if choice == "gemini"
            else "sk-ant-… (Anthropic-API-Key hier einfügen)"
        )
        self.model_input.setVisible(choice == "ollama")
        self.test_btn.setText(
            "Einstellungen öffnen" if choice == "other" else "Verbindung testen && speichern"
        )
        self.adjustSize()

    def _later(self) -> None:
        self._mark_completed()
        tooltip("Einrichtung jederzeit über Extras → AI Answer Grading möglich.", period=4000)
        self.reject()

    def _mark_completed(self) -> None:
        self.config["setup_completed"] = True
        mw.addonManager.writeConfig(_addon_id(), self.config)

    def _save_and_test(self) -> None:
        choice = self._choice()
        c = self.config

        if choice == "other":
            self._mark_completed()
            self.accept()
            from . import settings
            settings.show_settings()
            return

        if choice == "gemini":
            key = self.key_input.text().strip()
            if not key:
                self.status.setText("⚠️ Bitte zuerst den Gemini-API-Key einfügen (Link oben).")
                return
            c.update(provider="openai", openai_base_url=GEMINI_BASE_URL,
                     openai_api_key=key, openai_model=GEMINI_MODEL)
        elif choice == "anthropic":
            key = self.key_input.text().strip()
            if not key:
                self.status.setText("⚠️ Bitte zuerst den Anthropic-API-Key einfügen (Link oben).")
                return
            c.update(provider="anthropic", api_key=key)
        elif choice == "ollama":
            model = self.model_input.text().strip()
            if not model:
                self.status.setText("⚠️ Bitte den Namen des geladenen Ollama-Modells angeben.")
                return
            c.update(provider="openai", openai_base_url=OLLAMA_BASE_URL,
                     openai_api_key="", openai_model=model)

        mw.addonManager.writeConfig(_addon_id(), c)
        self.test_btn.setEnabled(False)
        self.status.setText("⏳ Teste Verbindung …")

        def task() -> grader.GradingResult:
            return grader.grade_answer(
                c, "Was ist die Hauptstadt von Frankreich?", "Paris", "Paris", None
            )

        def on_done(future: Future) -> None:
            self.test_btn.setEnabled(True)
            try:
                result = future.result()
                self._mark_completed()
                self.status.setText(
                    f"✅ Verbindung steht! (Testbewertung: {result.score}/100, "
                    f"{result.rating_label}) — viel Erfolg beim Lernen!"
                )
                tooltip("AI Answer Grading ist eingerichtet.", period=4000)
                self.accept()
            except grader.GradingError as exc:
                self.status.setText(f"❌ {exc}")
            except Exception as exc:
                self.status.setText(f"❌ Unerwarteter Fehler: {exc!r}")

        mw.taskman.run_in_background(task, on_done)


def show_wizard() -> None:
    SetupWizard().exec()


def maybe_show_wizard() -> None:
    """Auto-show once on startup when nothing is configured yet."""
    config = mw.addonManager.getConfig(_addon_id()) or {}
    if config.get("setup_completed"):
        return
    if _has_any_credentials(config):
        config["setup_completed"] = True
        mw.addonManager.writeConfig(_addon_id(), config)
        return
    show_wizard()
