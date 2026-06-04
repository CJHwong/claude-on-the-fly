"""Shared base for the secondary screens (logs / history / doctor).

They float as modal overlays over a dimmed dashboard rather than swapping it out
full-bleed, so the supervisor reads as one screen with panels you summon and
dismiss (lazygit / k9s feel) instead of a navigation tree.

A subclass wraps its content in a single `#overlay-box` container; the Footer
stays a screen-level sibling so it spans the dimmed band at the bottom. The
framing CSS below is keyed on the `OverlayScreen` type selector, which Textual
matches against every subclass in the MRO, so each screen inherits it for free.
"""

from __future__ import annotations

from textual.screen import ModalScreen


class OverlayScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    OverlayScreen {
        align: center middle;
        background: $background 60%;
    }
    OverlayScreen > #overlay-box {
        width: 92%;
        height: 90%;
        border: round $accent;
        background: $surface;
        padding: 0 1;
    }
    """
