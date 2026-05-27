"""Config preview screen — shows the resolved (merged) symphony config.

Pressing `g` on the Dashboard opens this. From here:
  Enter → open $EDITOR on the local symphony.yaml, then re-render + validate
  Esc   → back to the Dashboard

The body is the effective config (remote + local merged), so the operator
sees exactly what the daemon will use before deciding to edit.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

from claude_on_the_fly.agent import DATA_DIR
from claude_on_the_fly.tui import env_editor

CONFIG_PATH = DATA_DIR / "symphony.yaml"


class ConfigPreviewScreen(Screen):
    # NOTE: do not name a helper `_render` — that shadows Textual's internal
    # Widget._render() and breaks the screen's own rendering.
    DEFAULT_CSS = """
    ConfigPreviewScreen VerticalScroll { padding: 1 2; }
    #config-preview-status { padding: 1 0 0 0; }
    """

    BINDINGS = [
        ("enter", "edit", "Edit"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.pop_screen", "Back"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            yield Static(id="config-preview")
            yield Static(id="config-preview-status")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_view()

    def _refresh_view(self) -> None:
        from claude_on_the_fly.symphony.config import (
            dump_effective_config,
            load_config,
        )

        body = self.query_one("#config-preview", Static)
        status = self.query_one("#config-preview-status", Static)
        try:
            body.update(dump_effective_config(load_config(CONFIG_PATH)))
            status.update("[dim]Enter to edit · Esc to go back[/dim]")
        except FileNotFoundError:
            body.update(
                "[dim](no symphony.yaml yet — Enter to create one from a "
                "commented template)[/dim]"
            )
            status.update("[dim]Enter to edit · Esc to go back[/dim]")
        except Exception as exc:
            body.update(f"[red]config does not load:[/red]\n{exc}")
            status.update("[red]Enter to edit and fix · Esc to go back[/red]")

    def action_edit(self) -> None:
        from claude_on_the_fly.symphony.config import EXAMPLE_YAML, load_config

        with self.app.suspend():
            env_editor.open_in_editor(CONFIG_PATH, seed=EXAMPLE_YAML)
        # Re-render so the screen reflects the edit, and surface validation.
        self._refresh_view()
        try:
            load_config(CONFIG_PATH).validate()
            self.notify("symphony.yaml ok")
        except Exception as exc:
            self.notify(f"symphony.yaml invalid: {exc}", severity="error")
