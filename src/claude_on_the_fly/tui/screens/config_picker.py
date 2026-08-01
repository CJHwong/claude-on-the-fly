"""Modal picker for the `g` key — choose which config file to edit.

Decoupled from panel focus on purpose: editing config is a rare, deliberate
action, so an explicit pick reads better than a key whose meaning shifts with
whatever happens to be focused. Returns the chosen target id (
"env" | "sandbox" | "cron") via dismiss(), or None on cancel.

The "sandbox" id outlived the file's rename to config.yaml. It is internal, the
dashboard resolves the real path through `settings.operator_settings()`, and
churning it through every caller and test to match a label buys nothing.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option


class ConfigPickerScreen(ModalScreen[str | None]):
    DEFAULT_CSS = """
    ConfigPickerScreen {
        align: center middle;
    }
    #config-picker {
        /* Sized to the longest label rather than left at a round number: at 60 the
           sandbox row was cut mid-word (54 chars of label into 50 of content).
           `width: auto` is not the fix -- it fills the available space, ballooning to
           102 columns on a wide terminal while still truncating on a narrow one.
           max-width keeps a small terminal shrinking rather than overflowing. */
        width: 58;
        max-width: 100%;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #config-picker-title {
        padding: 0 0 1 0;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="config-picker"):
            yield Static("[bold]Edit which config?[/bold]", id="config-picker-title")
            # Ordered the way a deployment is actually set up: credentials first,
            # because nothing runs without them; then what the agent is allowed to
            # reach and do; then the optional scheduled work. Alphabetical, or
            # newest-last, would both read as arbitrary to someone opening this for
            # the first time.
            yield OptionList(
                Option(".env            tokens and credentials", id="env"),
                # Names the top-level keys rather than describing them, which is both
                # shorter and more useful: it tells you what you will be looking at.
                Option("config.yaml     agent, models, policy, app", id="sandbox"),
                Option("cron.yaml       scheduled runs", id="cron"),
                id="config-picker-list",
            )

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_id)

    def action_cancel(self) -> None:
        self.dismiss(None)
