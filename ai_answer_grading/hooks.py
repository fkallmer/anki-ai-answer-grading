"""Hook registration — gui_hooks only, no monkey patching."""

from __future__ import annotations

from aqt import gui_hooks, mw
from aqt.qt import QAction, QMenu

from . import settings, ui


def _on_state_shortcuts(state: str, shortcuts: list) -> None:
    if state == "review":
        shortcuts.append(("Ctrl+Return", ui.on_ctrl_enter))
        shortcuts.append(("Ctrl+Enter", ui.on_ctrl_enter))  # numpad Enter
        shortcuts.append(("Ctrl+Shift+Return", ui.on_ctrl_shift_enter))
        shortcuts.append(("Ctrl+Shift+Enter", ui.on_ctrl_shift_enter))


def register() -> None:
    gui_hooks.card_will_show.append(ui.on_card_will_show)
    gui_hooks.webview_did_receive_js_message.append(ui.on_js_message)
    gui_hooks.reviewer_did_show_answer.append(ui.on_reviewer_did_show_answer)
    gui_hooks.state_shortcuts_will_change.append(_on_state_shortcuts)

    menu = QMenu("AI Answer Grading", mw)

    settings_action = QAction("Einstellungen…", mw)
    settings_action.triggered.connect(settings.show_settings)
    menu.addAction(settings_action)

    assign_action = QAction("Skript für Deck wählen…", mw)
    assign_action.triggered.connect(ui.assign_deck_context)
    menu.addAction(assign_action)

    auto_action = QAction("Auto-Modus (Rating automatisch übernehmen)", mw)
    auto_action.setCheckable(True)
    auto_action.setChecked(bool(ui._config().get("auto_answer")))
    auto_action.toggled.connect(ui._set_auto_answer)
    ui.auto_action = auto_action
    menu.addAction(auto_action)

    menu.addSeparator()

    test_action = QAction("Test-Bewertung ausführen", mw)
    test_action.triggered.connect(ui.run_debug_grading)
    menu.addAction(test_action)

    mw.form.menuTools.addMenu(menu)
