"""Markdown report rendering facade (re-exports rendering helpers from core)."""

from .core import (
    _format_reason_en,
    _translate_hint_zh,
    _translate_reason_zh,
    _translate_warning_zh,
    generate_report,
    render_markdown,
)

__all__ = [
    "_format_reason_en",
    "_translate_hint_zh",
    "_translate_reason_zh",
    "_translate_warning_zh",
    "generate_report",
    "render_markdown",
]
