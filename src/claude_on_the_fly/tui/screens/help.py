"""Help overlay: the full keymap, including keys hidden from the slim footer.

The footer only carries the handful of keys reached for constantly; everything
else lives here. Built from a (key, label, description) list the caller derives
from its own BINDINGS, so the overlay can never drift from what actually works.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static


class HelpScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    HelpScreen > #help-box {
        width: 64;
        height: auto;
        border: round $accent;
        padding: 1 2;
        background: $surface;
    }
    HelpScreen > #help-box > Static {
        width: 100%;
        height: auto;
    }
    #help-title {
        text-style: bold;
        padding: 0 0 1 0;
    }
    #help-footer {
        padding: 1 0 0 0;
        color: $text-muted;
    }
    """

    BINDINGS: ClassVar = [
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.pop_screen", "Back"),
        ("question_mark", "app.pop_screen", "Back"),
    ]

    def __init__(
        self,
        rows: list[tuple[str, str, str]],
        *,
        title: str = "claude-on-the-fly keys",
        footer: str = "esc / q / ? to close",
    ) -> None:
        super().__init__()
        self._rows = rows
        self._title = title
        self._footer = footer

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Static(self._title, id="help-title")
            yield Static(self._keymap_markup(), id="help-keys", markup=True)
            yield Static(self._footer, id="help-footer")

    def _keymap_markup(self) -> str:
        # Right-align the key column so the labels line up, then a dim
        # description. A rich Table mis-measures inside an auto-width container
        # and collapses the box, so this is plain aligned markup.
        key_w = max((len(key) for key, _, _ in self._rows), default=1)
        label_w = max((len(label) for _, label, _ in self._rows), default=1)
        lines = []
        for key, label, description in self._rows:
            line = f"[bold cyan]{key:>{key_w}}[/bold cyan]  {label:<{label_w}}"
            if description:
                line += f"  [dim]{description}[/dim]"
            lines.append(line)
        return "\n".join(lines)
