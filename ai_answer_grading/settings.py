"""Graphical settings dialog — edits the addon config without JSON editing.

Covers provider choice, credentials, model, behavior and image options.
deck_context_map is managed by the separate file-picker flow (ui.assign_deck_context).
"""

from __future__ import annotations

from typing import Any

from aqt import mw
from aqt.qt import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from aqt.utils import tooltip

from . import grader

ANTHROPIC_MODELS = [
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-haiku-4-5",
]

BEDROCK_MODELS = [
    "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
]


def _addon_id() -> str:
    return __name__.split(".")[0]


class SettingsDialog(QDialog):
    """All common options; writes straight into the addon config."""

    def __init__(self) -> None:
        super().__init__(mw)
        self.setWindowTitle("AI Answer Grading — Einstellungen")
        self.setMinimumWidth(520)
        self.config: dict[str, Any] = mw.addonManager.getConfig(_addon_id()) or {}
        self._build()
        self._load()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        tabs = QTabWidget()
        outer.addWidget(tabs)

        # --- Tab 1: general settings ---
        general = QWidget()
        layout = QVBoxLayout(general)
        tabs.addTab(general, "Allgemein")

        # Provider
        provider_box = QGroupBox("Provider")
        provider_form = QFormLayout(provider_box)
        self.provider = QComboBox()
        self.provider.addItems(["anthropic", "bedrock"])
        self.provider.currentTextChanged.connect(self._toggle_provider_fields)
        provider_form.addRow("Provider:", self.provider)
        layout.addWidget(provider_box)

        # Anthropic
        self.anthropic_box = QGroupBox("Anthropic API")
        anthropic_form = QFormLayout(self.anthropic_box)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("sk-ant-… (leer = Umgebungsvariable ANTHROPIC_API_KEY)")
        anthropic_form.addRow("API-Key:", self.api_key)
        self.model = QComboBox()
        self.model.setEditable(True)  # free text for future model ids
        self.model.addItems(ANTHROPIC_MODELS)
        anthropic_form.addRow("Modell:", self.model)
        layout.addWidget(self.anthropic_box)

        # Bedrock
        self.bedrock_box = QGroupBox("AWS Bedrock")
        bedrock_form = QFormLayout(self.bedrock_box)
        self.bedrock_api_key = QLineEdit()
        self.bedrock_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.bedrock_api_key.setPlaceholderText("ABSK… (Bedrock-API-Schlüssel aus der AWS-Konsole)")
        bedrock_form.addRow("Bedrock-API-Key:", self.bedrock_api_key)
        self.aws_region = QLineEdit()
        self.aws_region.setPlaceholderText("z. B. eu-central-1")
        bedrock_form.addRow("Region:", self.aws_region)
        self.bedrock_model = QComboBox()
        self.bedrock_model.setEditable(True)
        self.bedrock_model.addItems(BEDROCK_MODELS)
        bedrock_form.addRow("Modell/Profil-ID:", self.bedrock_model)
        layout.addWidget(self.bedrock_box)

        # Behavior
        behavior_box = QGroupBox("Verhalten")
        behavior_form = QFormLayout(behavior_box)
        self.auto_answer = QCheckBox("Rating bei Good/Easy automatisch übernehmen")
        behavior_form.addRow(self.auto_answer)
        self.auto_delay = QSpinBox()
        self.auto_delay.setRange(500, 15000)
        self.auto_delay.setSingleStep(500)
        self.auto_delay.setSuffix(" ms")
        behavior_form.addRow("Auto-Verzögerung:", self.auto_delay)
        self.send_images = QCheckBox("Bilder der Karte mitschicken (mehr Tokens)")
        behavior_form.addRow(self.send_images)
        self.feedback_language = QLineEdit()
        behavior_form.addRow("Feedback-Sprache:", self.feedback_language)
        layout.addWidget(behavior_box)

        # --- Tab 2: grading prompt ---
        prompt_tab = QWidget()
        prompt_layout = QVBoxLayout(prompt_tab)
        tabs.addTab(prompt_tab, "Bewertungs-Prompt")

        prompt_layout.addWidget(QLabel(
            "Hier passt du die Bewertungsregeln an (Persona, Strenge, Rating-Kriterien).\n"
            "Das JSON-Ausgabeformat hängt das Addon immer automatisch an — es kann\n"
            "nicht kaputtgehen. Der Platzhalter {language} wird durch die "
            "Feedback-Sprache ersetzt."
        ))
        self.custom_prompt = QPlainTextEdit()
        self.custom_prompt.setMinimumHeight(280)
        prompt_layout.addWidget(self.custom_prompt)

        reset_row = QHBoxLayout()
        reset_btn = QPushButton("Auf Standard zurücksetzen")
        reset_btn.clicked.connect(
            lambda: self.custom_prompt.setPlainText(grader.DEFAULT_GRADING_RULES)
        )
        reset_row.addWidget(reset_btn)
        reset_row.addStretch()
        prompt_layout.addLayout(reset_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _toggle_provider_fields(self, provider: str) -> None:
        self.anthropic_box.setVisible(provider == "anthropic")
        self.bedrock_box.setVisible(provider == "bedrock")
        self.adjustSize()

    def _load(self) -> None:
        c = self.config
        self.provider.setCurrentText(c.get("provider") or "anthropic")
        self.api_key.setText(c.get("api_key") or "")
        self.model.setCurrentText(c.get("model") or "claude-sonnet-4-6")
        self.bedrock_api_key.setText(c.get("bedrock_api_key") or "")
        self.aws_region.setText(c.get("aws_region") or "eu-central-1")
        self.bedrock_model.setCurrentText(c.get("bedrock_model") or BEDROCK_MODELS[0])
        self.auto_answer.setChecked(bool(c.get("auto_answer")))
        self.auto_delay.setValue(int(c.get("auto_answer_delay_ms") or 2500))
        self.send_images.setChecked(bool(c.get("send_images", True)))
        self.feedback_language.setText(c.get("feedback_language") or "Deutsch")
        self.custom_prompt.setPlainText(
            (c.get("custom_prompt") or "").strip() or grader.DEFAULT_GRADING_RULES
        )
        self._toggle_provider_fields(self.provider.currentText())

    def _save(self) -> None:
        c = self.config
        c["provider"] = self.provider.currentText()
        c["api_key"] = self.api_key.text().strip()
        c["model"] = self.model.currentText().strip()
        c["bedrock_api_key"] = self.bedrock_api_key.text().strip()
        c["aws_region"] = self.aws_region.text().strip()
        c["bedrock_model"] = self.bedrock_model.currentText().strip()
        c["auto_answer"] = self.auto_answer.isChecked()
        c["auto_answer_delay_ms"] = self.auto_delay.value()
        c["send_images"] = self.send_images.isChecked()
        c["feedback_language"] = self.feedback_language.text().strip() or "Deutsch"
        prompt_text = self.custom_prompt.toPlainText().strip()
        # Store "" when unchanged from default, so future addon updates to the
        # default prompt reach users who never customized it.
        c["custom_prompt"] = "" if prompt_text == grader.DEFAULT_GRADING_RULES else prompt_text
        mw.addonManager.writeConfig(_addon_id(), c)

        # Keep the Tools-menu auto-mode checkmark in sync.
        from . import ui
        if ui.auto_action is not None:
            ui.auto_action.blockSignals(True)
            ui.auto_action.setChecked(c["auto_answer"])
            ui.auto_action.blockSignals(False)

        tooltip("Einstellungen gespeichert.")
        self.accept()


def show_settings() -> None:
    SettingsDialog().exec()
