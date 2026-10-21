"""PySide6 desktop GUI for PhonePilot (PRD 18). Entry point: `phonepilot.gui.app.run_gui`."""

from __future__ import annotations

__all__ = ["run_gui"]


def run_gui(*args, **kwargs):  # type: ignore[no-untyped-def]
    from phonepilot.gui.app import run_gui as _run

    return _run(*args, **kwargs)
