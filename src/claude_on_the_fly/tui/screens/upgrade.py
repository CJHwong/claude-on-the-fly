"""Modal shown before an upgrade — the command, what it interrupts, confirm.

The confirmation is the point of the screen. An upgrade stops every daemon, and
the chat turns it stops are not recoverable: the operator has to see how many
before agreeing, not afterwards in a log line.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from claude_on_the_fly.tui.supervisor import PendingWork
from claude_on_the_fly.upgrade import Plan


class UpgradeScreen(ModalScreen[bool]):
    """Confirm an upgrade. Dismisses True to go ahead, False to leave it alone."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("y", "confirm", "Upgrade"),
        ("n", "cancel", "Cancel"),
    ]

    def __init__(self, plan: Plan, pending: list[PendingWork]) -> None:
        super().__init__()
        self._plan = plan
        self._pending = pending

    def compose(self) -> ComposeResult:
        with Vertical(id="upgrade-modal"):
            yield Static(Text("Upgrade", style="bold"))
            yield Static(Text(self._plan.command, style="bold"))
            yield Static(Text(f"from: {self._plan.source}", style="dim"))
            yield Static(self._pending_text(), id="upgrade-pending")
            yield Static(
                Text(
                    "Daemons stop, the command runs, then they start again. "
                    "This TUI relaunches itself on the new code.",
                    style="dim",
                )
            )
            with Horizontal(id="upgrade-buttons"):
                yield Button("Upgrade [y]", id="confirm", variant="primary")
                yield Button("Cancel [n]", id="cancel")

    def _pending_text(self) -> Text:
        if not self._pending:
            return Text("Nothing is in flight — this costs nobody an answer.")
        lines = [item.describe() for item in self._pending]
        at_risk = sum(item.at_risk for item in self._pending)
        body = Text("\n".join(lines))
        if at_risk:
            body.append(
                f"\n{at_risk} of those are lost for good and need resending.",
                style="bold red",
            )
        return body

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)
