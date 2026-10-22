"""Every cron entry the docs hand out loads with the daemon's own validator.

An operator copies these entries as they are. One that fails to load does not fail
alone: the daemon rejects the whole file and keeps running the previous one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from claude_on_the_fly.cron import load_config

EXAMPLES = Path(__file__).parent
REPO = EXAMPLES.parent.parent
RECIPES = REPO / "docs" / "how-to" / "cron-recipes.md"
CLONE = "~/claude-on-the-fly-src/"  # where the docs tell operators to clone the repo


def example_files() -> list[Path]:
    return sorted(EXAMPLES.glob("*/cron.yaml"))


def recipe_blocks() -> list[str]:
    return re.findall(r"```yaml\n(.*?)```", RECIPES.read_text(), re.S)


def load(entries: list[dict], tmp_path: Path) -> list:
    path = tmp_path / "cron.yaml"
    path.write_text(yaml.safe_dump({"entries": entries}))
    return load_config(path)


def missing_files(text: str) -> list[str]:
    """Repo paths the text names under the clone that do not exist."""
    refs = re.findall(re.escape(CLONE) + r"([\w./-]+)", text)
    return [ref for ref in refs if not (REPO / ref).exists()]


@pytest.mark.parametrize("path", example_files(), ids=lambda p: p.parent.name)
def test_example_entry_loads_and_names_real_files(path: Path, tmp_path: Path):
    entries = yaml.safe_load(path.read_text())
    assert [e.name for e in load(entries, tmp_path)] == [path.parent.name]
    assert missing_files(path.read_text()) == []


def test_every_example_ships_an_entry():
    with_entry = {p.parent for p in example_files()}
    assert {
        d
        for d in EXAMPLES.iterdir()
        if d.is_dir() and not d.name.startswith((".", "_"))
    } == with_entry


def test_recipe_page_entries_load(tmp_path: Path):
    entries = [e for block in recipe_blocks() for e in yaml.safe_load(block)]
    assert len(load(entries, tmp_path)) == len(entries) > 0


def test_docs_name_only_real_example_paths():
    pages = [RECIPES, REPO / "docs" / "how-to" / "reflect-on-skills.md"]
    assert {page.name: missing_files(page.read_text()) for page in pages} == {
        page.name: [] for page in pages
    }
