"""Modal shown after $EDITOR exits — diff summary + restart prompt."""

from __future__ import annotations

from rich.table import Table
from rich.text import Text

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from claude_on_the_fly.tui import supervisor
from claude_on_the_fly.tui.env_editor import EnvDiff, affected_daemons


def _redact(value: str) -> str:
    """Tokens shouldn't appear in plaintext on the diff screen."""
    if len(value) <= 6:
        return value
    return f"{value[:3]}…{value[-3:]} ({len(value)} chars)"


def _diff_table(diff: EnvDiff) -> Table:
    table = Table(title="env diff", show_header=True, header_style="bold")
    table.add_column("change")
    table.add_column("key")
    table.add_column("value")
    for k, v in sorted(diff.added.items()):
        table.add_row(Text("added", style="green"), k, _redact(v))
    for k, v in sorted(diff.removed.items()):
        table.add_row(Text("removed", style="red"), k, _redact(v))
    for k, (old, new) in sorted(diff.changed.items()):
        table.add_row(
            Text("changed", style="yellow"),
            k,
            f"{_redact(old)} → {_redact(new)}",
        )
    return table


class EnvDiffScreen(ModalScreen[None]):
    """Show env diff + offer to restart the affected daemons."""

    BINDINGS = [
        ("escape", "dismiss", "Cancel"),
        ("y", "restart", "Restart"),
        ("n", "dismiss", "Cancel"),
    ]

    def __init__(self, diff: EnvDiff) -> None:
        super().__init__()
        self._diff = diff
        self._affected = sorted(affected_daemons(diff))

    def compose(self) -> ComposeResult:
        with Vertical(id="env-diff-modal"):
            yield Static(_diff_table(self._diff))
            yield Static(self._affected_line(), id="env-diff-affected")
            with Horizontal(id="env-diff-buttons"):
                if self._affected:
                    yield Button("Restart [y]", id="restart", variant="primary")
                yield Button("Cancel [n]", id="cancel")

    def _affected_line(self) -> Text:
        if not self._affected:
            return Text(
                "No declared daemon env vars changed — nothing to restart.",
                style="dim",
            )
        running = [d for d in self._affected if supervisor.is_running(d)]
        if not running:
            return Text(
                f"Affected daemons: {', '.join(self._affected)} (none running).",
                style="dim",
            )
        return Text(f"Would restart: {', '.join(running)}", style="bold")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "restart":
            self.action_restart()
        else:
            self.dismiss(None)

    def action_restart(self) -> None:
        running = [d for d in self._affected if supervisor.is_running(d)]
        if not running:
            self.app.notify("No running daemons to restart.", severity="information")
            self.dismiss(None)
            return
        for d in running:
            try:
                pid = supervisor.restart(d)
                self.app.notify(f"restarted {d} (pid {pid})", severity="information")
            except supervisor.PreflightFailed as exc:
                self.app.notify(f"{d}: preflight failed ({exc})", severity="error")
            except supervisor.SpawnTimeout as exc:
                self.app.notify(f"{d}: spawn timeout — {exc}", severity="error")
            except Exception as exc:
                self.app.notify(f"{d}: restart failed: {exc}", severity="error")
        self.dismiss(None)
