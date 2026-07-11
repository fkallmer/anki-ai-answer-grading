"""AI Answer Grading — actively quiz cards, grade typed answers with an LLM.

Entry point: puts the vendored lib/ on sys.path, then registers hooks.
"""

from __future__ import annotations

import logging
import os
import sys

_addon_dir = os.path.dirname(__file__)
_lib_dir = os.path.join(_addon_dir, "lib")
if _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)

log = logging.getLogger("ai_answer_grading")
if not log.handlers:
    log.addHandler(logging.StreamHandler())
log.setLevel(logging.INFO)

try:
    import aqt  # noqa: F401
    _in_anki = True
except ImportError:
    _in_anki = False  # standalone (e.g. tests) — skip UI wiring

if _in_anki:
    from . import hooks  # noqa: E402

    hooks.register()
