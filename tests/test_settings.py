"""Tests for the unified sandbox policy file."""

from __future__ import annotations

import pytest

from claude_on_the_fly import settings

# --- the bundled template ---


def test_bundled_settings_ship_in_the_package():
    """It sits beside the seatbelt profiles, so it must survive a wheel build the
    same way they do."""
    assert settings.BUNDLED_SETTINGS.is_file()
    assert settings.BUNDLED_SETTINGS.parent.name == "claude_on_the_fly"


def test_bundled_template_carries_both_sections():
    for name in settings.SECTIONS:
        assert settings.bundled(name), name


def test_bundled_template_is_commented_enough_to_edit():
    """The whole point of seeding it is that an operator opens something that
    explains itself; a bare data file would not."""
    text = settings.BUNDLED_SETTINGS.read_text()
    comments = [line for line in text.splitlines() if line.strip().startswith("#")]
    assert len(comments) > 40


def test_a_broken_bundled_file_is_not_swallowed(monkeypatch, tmp_path):
    """Falling back would ship a build whose entire policy is quietly empty."""
    broken = tmp_path / "sandbox.yaml"
    broken.write_text("egress: 3\n")
    monkeypatch.setattr(settings, "BUNDLED_SETTINGS", broken)
    with pytest.raises(ValueError, match="must be a mapping"):
        settings.bundled("egress")


# --- parsing ---


def test_an_empty_file_means_bundled_defaults_not_an_error(operator_settings):
    """Commenting every line out is a legitimate way to say "defaults, please"."""
    operator_settings.write_text("# nothing enabled\n")
    assert settings.operator("egress") == {}


def test_a_missing_section_is_empty_not_an_error(operator_settings):
    operator_settings.write_text("commands:\n  tools: []\n")
    assert settings.operator("egress") == {}


@pytest.mark.parametrize(
    ("document", "fragment"),
    [
        ("- a\n- b\n", "top level must be a mapping"),
        ("egress: [unclosed\n", "not valid YAML"),
    ],
)
def test_read_document_names_what_is_wrong(tmp_path, document, fragment):
    path = tmp_path / "sandbox.yaml"
    path.write_text(document)
    with pytest.raises(ValueError, match=fragment):
        settings.read_document(path)


def test_a_section_that_is_not_a_mapping_is_named(operator_settings, caplog):
    operator_settings.write_text("egress: 3\n")
    with caplog.at_level("ERROR", logger="claude_on_the_fly.settings"):
        assert settings.operator("egress") == {}
    assert "`egress:` must be a mapping" in caplog.text


def test_an_unusable_file_says_nothing_you_added_is_active(operator_settings, caplog):
    """Distinct from a bad section on purpose: the reader needs to know whether the
    rest of their file still loaded."""
    operator_settings.write_text("egress: [unclosed\n")
    with caplog.at_level("ERROR", logger="claude_on_the_fly.settings"):
        assert settings.operator("commands") == {}
    assert "ignoring all of" in caplog.text
    assert "nothing you added there is active" in caplog.text


def test_a_bad_section_says_the_others_still_load(operator_settings, caplog):
    operator_settings.write_text("egress: 3\ncommands:\n  tools: []\n")
    with caplog.at_level("ERROR", logger="claude_on_the_fly.settings"):
        settings.operator("egress")
    assert "Other sections still load" in caplog.text
    # And they do.
    assert settings.operator("commands") == {"tools": []}


def test_an_unreadable_file_falls_back_rather_than_raising(operator_settings, caplog):
    """An OSError here (a directory where a file should be, a permission problem)
    must not take the daemon down with it."""
    operator_settings.mkdir()
    with caplog.at_level("ERROR", logger="claude_on_the_fly.settings"):
        assert settings.operator("egress") == {}


def test_no_operator_file_is_silent(operator_settings, caplog):
    with caplog.at_level("ERROR", logger="claude_on_the_fly.settings"):
        assert settings.operator("egress") == {}
    assert caplog.text == ""


# --- seeding ---


def test_seeding_writes_the_commented_template(operator_settings):
    assert settings.seed_operator_settings() == operator_settings
    assert operator_settings.read_text() == settings.BUNDLED_SETTINGS.read_text()


def test_seeding_creates_the_directory(tmp_path, monkeypatch):
    """First run on a fresh machine has no ~/.claude-on-the-fly at all."""
    data = tmp_path / "never-existed"
    monkeypatch.setattr("claude_on_the_fly.agent.DATA_DIR", data)
    assert settings.seed_operator_settings() == data / "sandbox.yaml"
    assert (data / "sandbox.yaml").is_file()


def test_seeding_never_overwrites_the_operators_own_file(operator_settings):
    operator_settings.write_text("egress:\n  allow: [mine.example]\n")
    assert settings.seed_operator_settings() is None
    assert "mine.example" in operator_settings.read_text()


def test_a_failed_seed_is_a_warning_not_a_crash(tmp_path, monkeypatch, caplog):
    """Every loader falls back to the bundled defaults, so a daemon that cannot
    seed still runs the vetted policy."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr("claude_on_the_fly.agent.DATA_DIR", data)

    def refuse(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(settings.shutil, "copyfile", refuse)
    with caplog.at_level("WARNING", logger="claude_on_the_fly.settings"):
        assert settings.seed_operator_settings() is None
    assert "could not seed" in caplog.text


# --- startup check ---


def test_startup_check_seeds_and_names_the_path(operator_settings, caplog):
    with caplog.at_level("INFO", logger="claude_on_the_fly.settings"):
        settings.check_operator_settings()
    assert operator_settings.is_file()
    assert str(operator_settings) in caplog.text


def test_startup_check_names_a_misspelled_section(operator_settings, caplog):
    """YAML accepts `egres:` happily, and it would otherwise do nothing at all
    with no diagnostic anywhere."""
    operator_settings.write_text("egres:\n  allow: [pypi.org]\n")
    with caplog.at_level("ERROR", logger="claude_on_the_fly.settings"):
        settings.check_operator_settings()
    assert "unrecognised top-level key(s) ['egres']" in caplog.text
    assert "which do nothing" in caplog.text


def test_startup_check_reports_a_bad_section_type(operator_settings, caplog):
    operator_settings.write_text("commands: 3\n")
    with caplog.at_level("ERROR", logger="claude_on_the_fly.settings"):
        settings.check_operator_settings()
    assert "`commands:` must be a mapping" in caplog.text


def test_startup_check_reports_an_unparseable_file(operator_settings, caplog):
    operator_settings.write_text("egress: [unclosed\n")
    with caplog.at_level("ERROR", logger="claude_on_the_fly.settings"):
        settings.check_operator_settings()
    assert "is unusable" in caplog.text


def test_startup_check_is_quiet_when_the_file_is_fine(operator_settings, caplog):
    with caplog.at_level("ERROR", logger="claude_on_the_fly.settings"):
        settings.check_operator_settings()
    assert caplog.text == ""


def test_startup_check_returns_when_seeding_failed(tmp_path, monkeypatch, caplog):
    """No file to validate is not an error: the bundled defaults are in effect."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr("claude_on_the_fly.agent.DATA_DIR", data)
    monkeypatch.setattr(
        settings.shutil,
        "copyfile",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no")),
    )
    with caplog.at_level("ERROR", logger="claude_on_the_fly.settings"):
        settings.check_operator_settings()
    assert caplog.text == ""


# --- where it lives ---


def test_the_operator_file_is_outside_the_agents_write_scope():
    """This file decides what runs outside the sandbox with real credentials and
    which hosts skip the operator prompt, so the agent must not be able to edit it."""
    from claude_on_the_fly import sandbox

    path = settings.operator_settings()
    assert path.name == "sandbox.yaml"
    assert path.parent.name == ".claude-on-the-fly"
    # Same directory that holds the shims, which is read/exec but not writable.
    assert path.parent == sandbox.shim_dir().parent
