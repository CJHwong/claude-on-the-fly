"""Tests for `$model` / `/model`: the grammar, the catalogues, and the wording.

Every test drives `parse` directly with a hand-built `AgentProfile`, so nothing
here needs a daemon, a frontend, or a real CLI. The catalogues are written into
`tmp_path` in the shape the real ones use, because the shape is the contract this
module reads.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from claude_on_the_fly import model_command as mc
from claude_on_the_fly.agent import AgentProfile


def _profile(
    backend: str = "claude",
    mode: str = "native",
    model: str = "sonnet",
    effort: str = "",
) -> AgentProfile:
    return AgentProfile(backend=backend, mode=mode, model=model, effort=effort)


@pytest.fixture(autouse=True)
def no_real_home(tmp_path, monkeypatch):
    """Keep every test off the developer's own `~/.claude`.

    `_catalogue_roots` falls back to the CLI's default config directory, so a
    machine that has fetched a real catalogue would answer for a test that meant
    to prove the fallback path. The temporary home holds nothing.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")


@pytest.fixture
def claude_catalogue(tmp_path, monkeypatch):
    """A claude config dir holding one catalogue, in the CLI's own shape."""
    config = tmp_path / "claude-config"
    _write_claude_catalogue(config)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    return config


def _write_claude_catalogue(config: Path, models: list[dict] | None = None) -> Path:
    directory = config / "cache" / "model-catalog"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "surface-cc.json"
    body = {
        "version": 2,
        "catalog": {
            "surface": "cc",
            "config": {
                "id": "cc",
                "models": models
                if models is not None
                else [
                    {
                        "id": "claude-opus-5",
                        "short_name": "Opus",
                        "thinking": {
                            "type": "effort",
                            "effort_options": [
                                {"id": level}
                                for level in ("low", "medium", "high", "xhigh", "max")
                            ],
                        },
                    },
                    {
                        "id": "claude-haiku-4-5-20251001",
                        "short_name": "Haiku",
                        "thinking": {"type": "none"},
                    },
                ],
            },
        },
    }
    path.write_text(json.dumps(body))
    return path


@pytest.fixture
def codex_cache(tmp_path, monkeypatch):
    """A codex home holding the fetched catalogue."""
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-5.5",
                        "supported_reasoning_levels": [
                            {"effort": "low"},
                            {"effort": "high"},
                            {"effort": "xhigh"},
                        ],
                    },
                    {
                        "slug": "gpt-6-astra",
                        "supported_reasoning_levels": [
                            {"effort": "low"},
                            {"effort": "max"},
                        ],
                    },
                ]
            }
        )
    )
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def _ollama(stdout: str, returncode: int = 0, stderr: str = ""):
    """A `subprocess.run` stand-in that answers `ollama list`."""

    def run(argv, **kwargs):
        assert argv == ["ollama", "list"]
        return subprocess.CompletedProcess(
            args=argv, returncode=returncode, stdout=stdout, stderr=stderr
        )

    return run


OLLAMA_LIST = "NAME            ID      SIZE\nqwen3:8b        111     4GB\n"


# --------------------------------------------------------------------------
# Grammar
# --------------------------------------------------------------------------


def test_parse_with_no_words_is_a_report():
    assert isinstance(mc.parse([], _profile()), mc.Report)


def test_parse_refuses_more_than_two_words():
    result = mc.parse(["opus", "high", "please"], _profile())

    assert isinstance(result, mc.Refusal)
    assert "Too many words" in result.text


def test_parse_sets_the_model_and_leaves_the_effort_alone(claude_catalogue):
    result = mc.parse(["opus"], _profile(effort="high"))

    assert result == mc.Change({"model": "opus"})


def test_parse_sets_the_model_and_the_effort(claude_catalogue):
    result = mc.parse(["opus", "high"], _profile())

    assert result == mc.Change({"model": "opus", "effort": "high"})


def test_parse_with_effort_default_clears_the_effort(claude_catalogue):
    result = mc.parse(["opus", "default"], _profile(effort="high"))

    assert result == mc.Change({"model": "opus", "effort": None})


def test_parse_with_a_bare_default_clears_both_fields(claude_catalogue):
    assert mc.parse(["default"], _profile(effort="high")) == mc.Change(
        {"model": None, "effort": None}
    )


def test_parse_with_a_bare_default_clears_a_pinned_effort(tmp_path, monkeypatch):
    """The case the first version got wrong: it cleared the model and left the
    effort pinned, so `default` did not put the conversation back on config."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty-config"))

    assert mc.parse(["default"], _profile(model="opus", effort="high")) == mc.Change(
        {"model": None, "effort": None}
    )


def test_parse_with_default_model_keeps_the_effort_the_person_asked_for(
    claude_catalogue,
):
    result = mc.parse(["default", "low"], _profile())

    assert result == mc.Change({"model": None, "effort": "low"})


def test_parse_refuses_an_unknown_model_and_names_the_options(claude_catalogue):
    result = mc.parse(["sonet"], _profile())

    assert isinstance(result, mc.Refusal)
    assert "`sonet`" in result.text
    assert "opus" in result.text
    assert "haiku" in result.text


def test_parse_lists_a_near_miss_one_per_line_in_backticks(claude_catalogue):
    """The format exists for the case that produced it: `gpt-5.6-astra` against
    a catalogue holding `gpt-6-astra` differs by one character, mid-name, among
    seven neighbours. A comma-separated run of prose hides that."""
    result = mc.parse(["sonet"], _profile())

    assert isinstance(result, mc.Refusal)
    assert result.text.startswith("I do not know the model `sonet`. Try:\n")
    assert "\n- `opus`" in result.text
    assert ", " not in result.text


def test_parse_refuses_an_unknown_effort_and_names_the_levels(claude_catalogue):
    result = mc.parse(["opus", "hihg"], _profile())

    assert isinstance(result, mc.Refusal)
    assert "`hihg`" in result.text
    assert "xhigh" in result.text


def test_parse_applies_both_fields_on_pty():
    """pty used to refuse the effort and drop the model with it, so a two-word
    command changed nothing and said so in a message about effort only."""
    result = mc.parse(["opus", "high"], _profile(mode="pty"))

    assert isinstance(result, mc.Change)
    assert result.values == {"model": "opus", "effort": "high"}


def test_parse_allows_a_model_change_on_pty():
    result = mc.parse(["claude-opus-5"], _profile(mode="pty"))

    assert isinstance(result, mc.Change)
    assert result.values == {"model": "claude-opus-5"}


# --------------------------------------------------------------------------
# Which catalogue answers
# --------------------------------------------------------------------------


def test_parse_refuses_a_typo_that_looks_like_a_real_id(claude_catalogue):
    """A readable catalogue is the authority, so the shape check must not be a
    second way in. Applied to a readable catalogue it accepted `gpt-5.6-lunna`
    and another vendor's `claude-opus-5` on a codex conversation, silently."""
    result = mc.parse(["claude-opus-6"], _profile())

    assert isinstance(result, mc.Refusal)
    assert "claude-opus-6" in result.text


def test_parse_refuses_another_vendors_model_when_it_is_not_listed(codex_cache):
    profile = _profile(backend="codex", model="gpt-5.5")

    result = mc.parse(["claude-opus-5"], profile)

    assert isinstance(result, mc.Refusal)
    assert "gpt-5.5" in result.text


def test_parse_takes_a_catalogue_id_that_also_has_an_alias(claude_catalogue):
    """Both spellings are valid, and listing only the derived one made the
    catalogue refuse its own ids."""
    assert mc.parse(["claude-opus-5"], _profile()) == mc.Change(
        {"model": "claude-opus-5"}
    )
    assert mc.parse(["opus"], _profile()) == mc.Change({"model": "opus"})


def test_describe_lists_both_spellings(claude_catalogue):
    text = mc.describe(_profile(), pinned=False)

    assert "claude-opus-5" in text and "opus" in text


def test_parse_takes_a_catalogue_id(claude_catalogue):
    assert mc.parse(["claude-opus-5"], _profile()) == mc.Change(
        {"model": "claude-opus-5"}
    )


def test_parse_takes_an_effort_the_catalogue_gives_that_model(claude_catalogue):
    assert mc.parse(["opus", "max"], _profile()) == mc.Change(
        {"model": "opus", "effort": "max"}
    )


def test_parse_refuses_effort_on_a_model_the_catalogue_says_takes_none(
    claude_catalogue,
):
    result = mc.parse(["haiku", "high"], _profile())

    assert isinstance(result, mc.Refusal)
    assert "`high`" in result.text


def test_parse_accepts_a_full_id_without_a_catalogue(tmp_path, monkeypatch):
    """A model released after a catalogue was written is not blocked by it."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty-config"))

    result = mc.parse(["claude-opus-6"], _profile())

    assert isinstance(result, mc.Change)
    assert result.values == {"model": "claude-opus-6"}


def test_parse_accepts_an_alias_without_a_catalogue(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty-config"))

    assert mc.parse(["opus"], _profile()) == mc.Change({"model": "opus"})


def test_parse_refuses_a_typo_without_a_catalogue(tmp_path, monkeypatch):
    """The alias list is the fallback, and it still catches a misspelling."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty-config"))

    result = mc.parse(["opus4"], _profile())

    assert isinstance(result, mc.Refusal)
    assert "haiku" in result.text


def test_catalogue_prefers_the_resolved_config_dir_over_the_default(
    tmp_path, monkeypatch
):
    default = tmp_path / "home" / ".claude"
    _write_claude_catalogue(default, models=[{"id": "claude-from-default"}])
    resolved = tmp_path / "resolved"
    _write_claude_catalogue(resolved, models=[{"id": "claude-from-resolved"}])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(resolved))

    catalogue = mc._claude_catalogue()

    assert catalogue.models == ["claude-from-resolved"]


def test_catalogue_falls_back_to_the_default_config_dir(tmp_path, monkeypatch):
    """A daemon pointed elsewhere still uses the catalogue the CLI fetched."""
    _write_claude_catalogue(
        tmp_path / "home" / ".claude", models=[{"id": "claude-from-default"}]
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "resolved-missing"))

    assert mc._claude_catalogue().models == ["claude-from-default"]


def test_catalogue_skips_a_file_that_is_not_the_expected_shape(tmp_path, monkeypatch):
    config = tmp_path / "config"
    directory = config / "cache" / "model-catalog"
    directory.mkdir(parents=True)
    (directory / "a-unreadable.json").write_text("{ not json")
    (directory / "b-shapeless.json").write_text(json.dumps({"catalog": {"config": {}}}))
    # Sorted order matters: the shapeless file is read before the last one.
    (directory / "c-empty.json").write_text(
        json.dumps({"catalog": {"config": {"models": [{"short_name": "NoId"}]}}})
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))

    catalogue = mc._claude_catalogue()

    assert catalogue.models is None


def test_catalogue_ignores_an_entry_with_no_id(tmp_path, monkeypatch):
    config = tmp_path / "config"
    _write_claude_catalogue(
        config,
        models=[
            {"short_name": "Nameless"},
            {"id": "claude-sonnet-5", "short_name": ""},
        ],
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))

    catalogue = mc._claude_catalogue()

    # No short name means no alias, so the id is listed on its own.
    assert catalogue.models == ["claude-sonnet-5"]
    assert catalogue.aliases == {}


def test_effort_from_thinking_tolerates_a_missing_block():
    """None is the entry staying quiet, which falls back to the backend's set.
    Empty is a real answer: this model takes no effort."""
    assert mc._effort_from_thinking(None) is None
    assert mc._effort_from_thinking({"type": "none"}) == []
    assert mc._effort_from_thinking({"effort_options": ["not-a-mapping"]}) is None
    assert mc._effort_from_thinking({"effort_options": []}) is None
    assert mc._effort_from_thinking({"effort_options": "low"}) is None


# --------------------------------------------------------------------------
# ollama mode
# --------------------------------------------------------------------------


def test_ollama_model_from_the_list_is_accepted(monkeypatch):
    monkeypatch.setattr(mc.shutil, "which", lambda _: "/usr/bin/ollama")
    monkeypatch.setattr(mc.subprocess, "run", _ollama(OLLAMA_LIST))
    profile = _profile(mode="ollama", model="qwen3:8b")

    assert mc.parse(["qwen3:8b"], profile) == mc.Change({"model": "qwen3:8b"})


def test_ollama_refuses_an_unknown_model_and_names_the_list(monkeypatch):
    monkeypatch.setattr(mc.shutil, "which", lambda _: "/usr/bin/ollama")
    monkeypatch.setattr(mc.subprocess, "run", _ollama(OLLAMA_LIST))
    profile = _profile(mode="ollama", model="qwen3:8b")

    result = mc.parse(["qwen3:99b"], profile)

    assert isinstance(result, mc.Refusal)
    assert "qwen3:8b" in result.text


def test_ollama_takes_a_cloud_model_on_faith(monkeypatch):
    """A remote model can be served without appearing in `ollama list`."""
    monkeypatch.setattr(mc.shutil, "which", lambda _: "/usr/bin/ollama")
    monkeypatch.setattr(mc.subprocess, "run", _ollama(OLLAMA_LIST))
    profile = _profile(mode="ollama", model="qwen3:8b")

    assert mc.parse(["glm-5.3-flash:cloud"], profile) == mc.Change(
        {"model": "glm-5.3-flash:cloud"}
    )


def test_ollama_without_the_cli_refuses_and_says_it_has_no_list(monkeypatch):
    monkeypatch.setattr(mc.shutil, "which", lambda _: None)
    profile = _profile(mode="ollama", model="qwen3:8b")

    result = mc.parse(["qwen3:99b"], profile)

    assert isinstance(result, mc.Refusal)
    assert "could not read a list" in result.text


def test_ollama_treats_a_failed_list_as_no_list(monkeypatch):
    monkeypatch.setattr(mc.shutil, "which", lambda _: "/usr/bin/ollama")
    monkeypatch.setattr(
        mc.subprocess, "run", _ollama("", returncode=1, stderr="daemon down")
    )

    assert mc._ollama_models() is None


def test_ollama_treats_an_unrunnable_list_as_no_list(monkeypatch):
    def explode(argv, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(mc.shutil, "which", lambda _: "/usr/bin/ollama")
    monkeypatch.setattr(mc.subprocess, "run", explode)

    assert mc._ollama_models() is None


# --------------------------------------------------------------------------
# codex mode
# --------------------------------------------------------------------------


def test_codex_slug_from_the_cache_is_accepted(codex_cache):
    profile = _profile(backend="codex", model="gpt-5.5")

    assert mc.parse(["gpt-6-astra"], profile) == mc.Change({"model": "gpt-6-astra"})


def test_codex_refuses_a_level_that_model_does_not_have(codex_cache):
    """`max` is real on one codex model and not on another."""
    profile = _profile(backend="codex", model="gpt-5.5")

    result = mc.parse(["gpt-5.5", "max"], profile)

    assert isinstance(result, mc.Refusal)
    assert "xhigh" in result.text


def test_codex_accepts_a_level_that_model_has(codex_cache):
    profile = _profile(backend="codex", model="gpt-5.5")

    assert mc.parse(["gpt-5.5", "xhigh"], profile) == mc.Change(
        {"model": "gpt-5.5", "effort": "xhigh"}
    )


def test_codex_falls_back_to_the_backend_set_when_the_cache_stays_quiet(
    tmp_path, monkeypatch
):
    """A slug the cache lists but does not annotate still gets its levels.

    `deepseek-v4.1-flash:cloud` arrives through codex's `model.json` with
    `supported_reasoning_levels: []`, which is codex saying nothing, not a model
    that takes no effort. Reading it as the latter refused every level it was
    offered, with a message claiming no list could be read while the list was
    right there.
    """
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {"slug": "gpt-quiet", "supported_reasoning_levels": []},
                ]
            }
        )
    )
    monkeypatch.setenv("CODEX_HOME", str(home))
    profile = _profile(backend="codex", model="gpt-quiet")

    assert mc.parse(["gpt-quiet", "max"], profile) == mc.Change(
        {"model": "gpt-quiet", "effort": "max"}
    )


def test_codex_cache_wins_over_the_last_chosen_model(tmp_path, monkeypatch):
    """`model.json` holds whatever codex was last pointed at, which after a turn
    the ollama launcher served is an ollama tag. Merging the two files offered
    that tag among codex's own slugs, and a listed name is an accepted one, so
    it would have reached codex as `-m glm-5.3-flash:cloud`."""
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "model.json").write_text(
        json.dumps({"models": [{"slug": "glm-5.3-flash:cloud"}]})
    )
    (home / "models_cache.json").write_text(
        json.dumps({"models": [{"slug": "gpt-5.6-sol"}]})
    )
    monkeypatch.setenv("CODEX_HOME", str(home))

    assert mc._codex_catalogue().models == ["gpt-5.6-sol"]
    refusal = mc.parse(["glm-5.3-flash:cloud"], _profile(backend="codex"))
    assert isinstance(refusal, mc.Refusal)


def test_codex_falls_back_to_the_last_chosen_model_with_no_cache(tmp_path, monkeypatch):
    """Why `model.json` is read at all: codex that has never fetched a
    catalogue still names the model it is on, and one name beats none."""
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "model.json").write_text(json.dumps({"models": [{"slug": "gpt-pinned"}]}))
    monkeypatch.setenv("CODEX_HOME", str(home))

    assert mc._codex_catalogue().models == ["gpt-pinned"]


def test_codex_falls_back_past_a_cache_that_names_nothing(tmp_path, monkeypatch):
    """An unreadable or empty cache is not an answer, so the fallback still
    runs rather than leaving the catalogue blank."""
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "models_cache.json").write_text(json.dumps({"models": []}))
    (home / "model.json").write_text(json.dumps({"models": [{"slug": "gpt-pinned"}]}))
    monkeypatch.setenv("CODEX_HOME", str(home))

    assert mc._codex_catalogue().models == ["gpt-pinned"]


def test_codex_tolerates_a_cache_with_nothing_usable_in_it(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    "not-a-mapping",
                    {"no_slug": True},
                    {"slug": "gpt-6-astra", "supported_reasoning_levels": ["bare"]},
                ]
            }
        )
    )
    monkeypatch.setenv("CODEX_HOME", str(home))

    catalogue = mc._codex_catalogue()

    assert catalogue.models == ["gpt-6-astra"]
    # No entry: the cache listed no levels for it, so the backend's set answers.
    assert catalogue.effort == {}


def test_codex_without_a_cache_accepts_a_full_id(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))

    result = mc.parse(["gpt-6-astra"], _profile(backend="codex"))

    assert isinstance(result, mc.Change)
    assert result.values == {"model": "gpt-6-astra"}


def test_codex_without_a_cache_refuses_a_plain_word(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))

    result = mc.parse(["astra"], _profile(backend="codex"))

    assert isinstance(result, mc.Refusal)
    assert "could not read a list" in result.text


# --------------------------------------------------------------------------
# Report and confirmation
# --------------------------------------------------------------------------


def test_describe_lists_the_state_and_what_it_could_be(claude_catalogue):
    text = mc.describe(_profile(model="opus", effort="high"), pinned=False)

    assert "Model: opus." in text
    assert "Effort: high." in text
    assert "Backend: claude, native." in text
    assert "opus" in text and "haiku" in text
    assert "xhigh" in text


def test_describe_marks_a_pinned_conversation(claude_catalogue):
    assert "(pinned)" in mc.describe(_profile(), pinned=True)


def test_describe_names_the_cli_default_when_no_model_is_configured(
    claude_catalogue,
):
    text = mc.describe(_profile(model=""), pinned=False)

    assert "(the CLI default)" in text
    # No model means no catalogue entry, so the backend's own set answers.
    assert "Effort for this model: `high`, `low`, `max`, `medium`, `xhigh`." in text


def test_describe_reports_effort_on_pty(claude_catalogue):
    text = mc.describe(_profile(mode="pty", effort="high"), pinned=False)

    assert "Effort: high." in text
    assert "Effort for" in text


def test_describe_on_ollama_without_the_cli_lists_no_models(monkeypatch):
    monkeypatch.setattr(mc.shutil, "which", lambda _: None)
    text = mc.describe(_profile(mode="ollama", model="qwen3:8b"), pinned=False)

    assert "Models:" not in text
    assert "Effort for `qwen3:8b`: `high`, `low`, `max`, `medium`, `xhigh`." in text


def test_ack_names_both_fields(claude_catalogue):
    text = mc.ack(_profile(model="opus", effort="high"))

    assert text == "Model: opus. Effort: high."


def test_ack_names_the_cli_default_and_an_unset_effort(claude_catalogue):
    text = mc.ack(_profile(model="", effort=""))

    assert text == "Model: (the CLI default). Effort: unset."


def test_ack_appends_the_note(claude_catalogue):
    text = mc.ack(_profile(model="opus"), note=" Unverified.")

    assert text.endswith("Unverified.")


# --------------------------------------------------------------------------
# The unverified note
# --------------------------------------------------------------------------


def test_a_full_id_without_a_catalogue_is_accepted_but_marked_unverified(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty-config"))

    result = mc.parse(["claude-opus-6"], _profile())

    assert isinstance(result, mc.Change)
    assert "unverified" in result.note


def test_an_alias_without_a_catalogue_needs_no_note(tmp_path, monkeypatch):
    """An alias is checked against the list this module carries itself."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty-config"))

    assert mc.parse(["opus"], _profile()).note == ""


def test_a_catalogue_entry_needs_no_note(claude_catalogue):
    assert mc.parse(["claude-opus-5"], _profile()).note == ""


def test_clearing_the_model_needs_no_note(claude_catalogue):
    assert mc.parse(["default"], _profile()).note == ""


def test_a_cloud_model_without_a_list_is_marked_unverified(monkeypatch):
    monkeypatch.setattr(mc.shutil, "which", lambda _: None)
    profile = _profile(mode="ollama", model="qwen3:8b")

    result = mc.parse(["glm-5.3-flash:cloud"], profile)

    assert isinstance(result, mc.Change)
    assert "unverified" in result.note


def test_a_listed_ollama_model_needs_no_note(monkeypatch):
    monkeypatch.setattr(mc.shutil, "which", lambda _: "/usr/bin/ollama")
    monkeypatch.setattr(mc.subprocess, "run", _ollama(OLLAMA_LIST))
    profile = _profile(mode="ollama", model="qwen3:8b")

    assert mc.parse(["qwen3:8b"], profile).note == ""


# --------------------------------------------------------------------------
# Drift
# --------------------------------------------------------------------------


def test_effort_levels_match_the_backends():
    """The sets here are literals, so a backend edit must fail this test."""
    from claude_on_the_fly.backends import claude, codex

    assert mc.CLAUDE_EFFORT_LEVELS == claude._CLAUDE_EFFORT_LEVELS
    assert mc.CODEX_EFFORT_LEVELS == codex._CODEX_EFFORT_LEVELS
