"""Modal shown before an upgrade — the command, what it interrupts, confirm.

The confirmation is the point of the screen. An upgrade stops every daemon, so
the operator has to see what is in flight before agreeing, not afterwards in a
log line. Chat turns are journaled before they run and replayed on the next
start, so what the modal reports for them is a delay, not a loss.
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
            for line in self._step_lines():
                yield Static(line)
            yield Static(Text(f"from: {self._plan.source}", style="dim"))
            yield Static(self._pending_text(), id="upgrade-pending")
            yield Static(Text(self._cost_text(), style="dim"))
            with Horizontal(id="upgrade-buttons"):
                yield Button("Upgrade [y]", id="confirm", variant="primary")
                yield Button("Cancel [n]", id="cancel")

    def _step_lines(self) -> list[Text]:
        """The commands in the order they run, each marked with what it costs.

        An operator deciding whether to upgrade now needs to see which part
        interrupts anyone. The prepare step runs with the daemons up, so only
        the second one is a cost to whoever is waiting on an answer.
        """
        if not self._plan.prepare:
            return [Text(self._plan.command, style="bold")]
        return [
            Text(f"1. {self._plan.prepare}", style="bold"),
            Text("   daemons keep running", style="dim"),
            Text(f"2. {self._plan.command}", style="bold"),
            Text("   daemons stop for this one", style="dim"),
        ]

    def _cost_text(self) -> str:
        if self._plan.prepare:
            return (
                "Step 1 runs live. Then the daemons stop, step 2 runs, and they "
                "start again. This TUI relaunches itself on the new code."
            )
        return (
            "Daemons stop, the command runs, then they start again. "
            "This TUI relaunches itself on the new code."
        )

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
