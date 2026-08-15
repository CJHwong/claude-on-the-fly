"""Tests for the unified operator settings file."""

from __future__ import annotations

import pytest

from claude_on_the_fly import settings

# --- the bundled template ---


def test_bundled_settings_ship_in_the_package():
    """It sits beside the seatbelt profiles, so it must survive a wheel build the
    same way they do."""
    assert settings.BUNDLED_SETTINGS.is_file()
    assert settings.BUNDLED_SETTINGS.parent.name == "claude_on_the_fly"


def test_bundled_template_carries_the_sections_that_ship_defaults():
    for name in settings.DEFAULTED_SECTIONS:
        assert settings.bundled(name), name


def test_the_other_sections_are_present_but_all_commented():
    """`sandbox:` and `agent:` are documentation, not defaults: the values live in the
    code that reads them, so an absent key means "whatever this build does" rather
    than "whatever the template happened to say"."""
    document = settings.read_document(settings.BUNDLED_SETTINGS)
    for name in settings.SECTIONS:
        assert name in document, name
        if name not in settings.DEFAULTED_SECTIONS:
            assert document[name] is None, name


def test_every_migrated_field_is_documented_in_the_template():
    """A field reachable by `settings.get` but absent from the template is one an
    operator can only find by reading the source."""
    text = settings.BUNDLED_SETTINGS.read_text()
    for path in settings.FIELDS:
        leaf = path.split(".")[-1]
        assert f"{leaf}:" in text, path


def test_bundled_template_is_commented_enough_to_edit():
    """The whole point of seeding it is that an operator opens something that
    explains itself; a bare data file would not."""
    text = settings.BUNDLED_SETTINGS.read_text()
    comments = [line for line in text.splitlines() if line.strip().startswith("#")]
    assert len(comments) > 40


def test_a_broken_bundled_file_is_not_swallowed(monkeypatch, tmp_path):
    """Falling back would ship a build whose entire policy is quietly empty."""
    broken = tmp_path / "config.yaml"
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
    path = tmp_path / "config.yaml"
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


def test_a_yaml_error_names_the_location_and_problem(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("slack:\n  allowed_senders: [*]\n")
    with pytest.raises(ValueError) as caught:
        settings.read_document(path)
    message = str(caught.value)
    assert "line 2, column" in message
    assert "alias" in message


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
    assert settings.seed_operator_settings() == data / "config.yaml"
    assert (data / "config.yaml").is_file()


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


# --- re-reading ---


def test_an_edit_takes_effect_without_a_restart(operator_settings):
    """The property the whole file's usability rests on: saving is enough."""
    operator_settings.write_text("egress:\n  allow: [first.example]\n")
    assert settings.operator("egress") == {"allow": ["first.example"]}
    operator_settings.write_text("egress:\n  allow: [second.example]\n")
    assert settings.operator("egress") == {"allow": ["second.example"]}


def test_an_unchanged_file_is_parsed_once(operator_settings, monkeypatch):
    """The loaders run on every session, CONNECT, and tool call."""
    operator_settings.write_text("egress:\n  allow: [a.example]\n")
    parses = []
    real = settings.read_document
    monkeypatch.setattr(
        settings,
        "read_document",
        lambda path: (parses.append(path), real(path))[1],
    )
    for _ in range(5):
        settings.operator("egress")
    assert parses == [operator_settings]


def test_a_caller_cannot_mutate_the_cached_document(operator_settings):
    """Sections come out by reference, and one mutated in place would rewrite policy
    for every later reader with nothing in the file to explain it."""
    operator_settings.write_text("egress:\n  allow: [a.example]\n")
    settings.operator("egress")["allow"].append("evil.example")
    assert settings.operator("egress") == {"allow": ["a.example"]}


def test_a_broken_file_is_re_read_so_the_fix_lands(operator_settings, caplog):
    """Caching the failure would mean the operator's correction did nothing until
    they restarted, which is the opposite of what they just learned from the log."""
    operator_settings.write_text("egress: [unclosed\n")
    with caplog.at_level("ERROR", logger="claude_on_the_fly.settings"):
        assert settings.operator("egress") == {}
    operator_settings.write_text("egress:\n  allow: [fixed.example]\n")
    assert settings.operator("egress") == {"allow": ["fixed.example"]}


# --- settings that moved out of .env ---


def test_a_yaml_value_is_read_where_the_env_var_was(operator_settings):
    operator_settings.write_text("sandbox:\n  mode: jail\n")
    assert settings.get("COTF_SANDBOX", "off") == "jail"


def test_an_absent_key_leaves_the_readers_own_default(operator_settings):
    """Defaults stay in the code that reads them, so they do not depend on whether an
    operator's file happens to carry a key."""
    operator_settings.write_text("sandbox: {}\n")
    assert settings.get("COTF_SANDBOX", "off") == "off"
    assert settings.get("AGENT_BACKEND", "claude") == "claude"


def test_a_nested_backend_block_flattens(operator_settings):
    operator_settings.write_text(
        "agent:\n  backend: codex\n  codex:\n    mode: ollama\n    model: o3\n"
        "  ollama:\n    effort: xhigh\n"
    )
    assert settings.get("AGENT_BACKEND", "claude") == "codex"
    assert settings.get("CODEX_MODE", "native") == "ollama"
    assert settings.get("CODEX_MODEL") == "o3"
    assert settings.get("OLLAMA_EFFORT") == "xhigh"


def test_interim_progress_reads_from_its_own_section(operator_settings):
    """Every section here names a module, and mid-turn progress has one of its own
    (`interim.py`); it is neither a backend knob nor platform rendering, so it is
    not under `agent:` and not under `slack:`. The section carries the prefix, so
    the leaf is just `progress`."""
    operator_settings.write_text("interim:\n  progress: true\n")
    assert settings.get("COTF_INTERIM_PROGRESS") == "1"


def test_interim_pacing_reads_from_its_own_section(operator_settings):
    """The warm-up and the gap sit beside the toggle they pace: one module reads all
    three, so one section holds all three."""
    operator_settings.write_text(
        "interim:\n  warmup_seconds: 60\n  min_gap_seconds: 90.5\n"
    )
    assert settings.get("COTF_INTERIM_WARMUP_SECONDS") == "60"
    assert settings.get("COTF_INTERIM_MIN_GAP_SECONDS") == "90.5"


def test_a_yaml_list_joins_the_way_its_env_var_did(operator_settings):
    """Paths were colon-joined like PATH; sender ids comma-joined. A single separator
    for all of them would corrupt one of the two."""
    operator_settings.write_text("sandbox:\n  extra_paths:\n    - /a\n    - /b\n")
    assert settings.get("COTF_SANDBOX_EXTRA_PATHS") == "/a:/b"


def test_a_yaml_boolean_becomes_the_truthy_string(operator_settings):
    """The readers test membership in a truthy set. `False` stringifies to "False",
    which is neither truthy nor obviously not."""
    operator_settings.write_text("sandbox:\n  broker_only_loopback: true\n")
    assert settings.get("COTF_SANDBOX_BROKER_ONLY_LOOPBACK") == "1"
    operator_settings.write_text("sandbox:\n  broker_only_loopback: false\n")
    assert settings.get("COTF_SANDBOX_BROKER_ONLY_LOOPBACK") == "0"


def test_a_path_through_a_scalar_is_absent_not_an_error(operator_settings):
    """`claude: native` where a block belongs is a plausible typo, and digging into a
    string has to stop rather than raise. The section-level version of the same
    mistake is caught earlier by `_section`; this is the nested one, which is not.
    """
    operator_settings.write_text("agent:\n  claude: native\n")
    assert settings.get("CLAUDE_MODE", "native") == "native"


def test_a_number_is_stringified(operator_settings):
    operator_settings.write_text("agent:\n  claude:\n    model: 5\n")
    assert settings.get("CLAUDE_MODEL") == "5"


def test_absent_and_empty_stay_distinct(operator_settings):
    """Some settings read a blank value as a deliberate off rather than as unset."""
    operator_settings.write_text("agent:\n  claude:\n    model: ''\n")
    assert settings.get("CLAUDE_MODEL", "sonnet") == ""
    operator_settings.write_text("agent:\n  claude: {}\n")
    assert settings.get("CLAUDE_MODEL", "sonnet") == "sonnet"


@pytest.mark.parametrize(
    ("document", "fragment"),
    [
        ("sandbox:\n  mode: [jail, env]\n", "expected a single value"),
        ("sandbox:\n  mode:\n    nested: jail\n", "expected a value"),
    ],
)
def test_an_unflattenable_field_is_named_and_dropped(
    operator_settings, caplog, document, fragment
):
    """Failing the daemon over one malformed knob would take a deployment down for a
    typo in a field it may not even use."""
    operator_settings.write_text(document)
    with caplog.at_level("ERROR", logger="claude_on_the_fly.settings"):
        assert settings.get("COTF_SANDBOX", "off") == "off"
    assert fragment in caplog.text
    assert "sandbox.mode" in caplog.text


# --- .env backward compatibility ---


def test_a_legacy_env_var_still_wins_over_the_file(operator_settings, monkeypatch):
    """The whole backward-compatibility story. File-wins would have switched off the
    jail of any deployment that set COTF_SANDBOX in .env and never edited the yaml --
    silently, on the setting where silence costs the most."""
    operator_settings.write_text('sandbox:\n  mode: "off"\n')
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    assert settings.get("COTF_SANDBOX", "off") == "jail"


def test_a_legacy_env_var_is_named_once(operator_settings, monkeypatch, caplog):
    """Once per variable per process: the point is to say where the setting moved,
    and repeating it on every read would bury the rest of the log."""
    monkeypatch.setattr(settings, "_LEGACY_WARNED", set())
    monkeypatch.setenv("AGENT_BACKEND", "codex")
    with caplog.at_level("WARNING", logger="claude_on_the_fly.settings"):
        for _ in range(4):
            settings.get("AGENT_BACKEND", "claude")
    assert caplog.text.count("AGENT_BACKEND is set in the environment") == 1
    assert "`agent.backend:`" in caplog.text


def test_an_env_var_wins_from_a_passed_in_mapping(operator_settings):
    """The TUI doctor merges ~/.claude-on-the-fly/.env over os.environ to model what a
    supervised child receives; reading os.environ instead would show a verdict the
    daemon does not share."""
    operator_settings.write_text('sandbox:\n  mode: "off"\n')
    assert settings.resolved({"COTF_SANDBOX": "jail"})["COTF_SANDBOX"] == "jail"


def test_environment_carries_unmigrated_keys_through(operator_settings):
    """checks.py takes one mapping and looks up tokens out of it too."""
    operator_settings.write_text("sandbox:\n  mode: jail\n")
    env = settings.environment({"SLACK_APP_TOKEN": "xapp-1"})
    assert env["SLACK_APP_TOKEN"] == "xapp-1"
    assert env["COTF_SANDBOX"] == "jail"


# --- the environment must not reach a traceback ---


def test_no_frame_holds_the_process_environment(operator_settings):
    """A traceback renderer that prints frame locals prints every local it finds.

    `resolved` is on the path of every setting read, so a frame there holding
    `os.environ` writes the operator's whole environment into any crash report that
    passes through it. Observed for real: a missing bundled template raised out of a TUI
    log refresh and the rendered traceback carried three unrelated API keys.

    Asserted structurally, on the compiled function, because the leak is a property of
    the frame rather than of any behaviour a call can reveal -- and because it has to
    stay true for exceptions nobody has thought of yet.
    """
    for func in (settings.resolved, settings.environment):
        names = func.__code__.co_varnames
        assert "source" not in names, f"{func.__name__} binds the environment"


def test_the_environment_is_only_touched_where_nothing_can_raise():
    """The two helpers that do hold it are one expression each, so there is no window
    between binding and returning in which anything can fail."""
    import dis

    for func in (settings._from_environment, settings._environ_snapshot):
        # No exception handling, no loops, no calls that can fail mid-frame.
        ops = {instruction.opname for instruction in dis.get_instructions(func)}
        assert not {"SETUP_FINALLY", "FOR_ITER", "SEND"} & ops, func.__name__


# --- a broken install ---


def test_a_missing_bundled_template_is_loud_but_not_fatal(
    monkeypatch, caplog, tmp_path
):
    """Every setting read goes through `bundled`, so an absent file used to surface as a
    FileNotFoundError from whichever caller happened to ask first -- observed as a hard
    crash three frames inside a TUI log refresh. Empty leaves a degraded but coherent
    posture and says so on every read."""
    monkeypatch.setattr(settings, "BUNDLED_SETTINGS", tmp_path / "gone.yaml")
    monkeypatch.setattr(settings, "_DOCUMENTS", {})
    with caplog.at_level("ERROR", logger="claude_on_the_fly.settings"):
        assert settings.bundled("egress") == {}
        assert settings.get("COTF_SANDBOX", "off") == "off"
    assert "broken install, not a config mistake" in caplog.text


def test_a_malformed_bundled_template_still_raises(monkeypatch, tmp_path):
    """Distinct from missing: a file that parses to the wrong shape is a packaging bug
    that must not ship, and an empty policy would hide it."""
    broken = tmp_path / "config.yaml"
    broken.write_text("egress: 3\n")
    monkeypatch.setattr(settings, "BUNDLED_SETTINGS", broken)
    monkeypatch.setattr(settings, "_DOCUMENTS", {})
    with pytest.raises(ValueError, match="must be a mapping"):
        settings.bundled("egress")


# --- restart-required fields ---


def test_check_reload_is_quiet_before_a_baseline_exists(operator_settings):
    """Nothing has been read once at startup yet, so nothing can be stale."""
    operator_settings.write_text("permissions:\n  mode: ask\n")
    assert settings.check_reload() == ()


def test_check_reload_is_quiet_when_nothing_changed(operator_settings):
    operator_settings.write_text("permissions:\n  mode: ask\n")
    settings.check_operator_settings()
    assert settings.check_reload() == ()


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        ("permissions:\n  mode: ask\n", ("permissions.mode",)),
        ("commands:\n  tools: []\n", ("commands",)),
    ],
)
def test_check_reload_names_the_field_that_needs_a_restart(
    operator_settings, edit, expected
):
    operator_settings.write_text("egress:\n  allow: [a.example]\n")
    settings.check_operator_settings()
    operator_settings.write_text(edit)
    assert settings.check_reload() == expected


def test_startup_value_keeps_security_mode_pinned_until_restart(operator_settings):
    operator_settings.write_text(
        'sandbox:\n  mode: "off"\npermissions:\n  mode: "off"\n'
    )
    settings.check_operator_settings()

    operator_settings.write_text("sandbox:\n  mode: jail\npermissions:\n  mode: ask\n")

    assert settings.startup_value("sandbox.mode") == "off"
    assert settings.startup_value("permissions.mode") == "off"
    assert settings.check_reload() == ("sandbox.mode", "permissions.mode")


def test_worker_construction_settings_are_restart_required():
    assert {
        "jobs.concurrency",
        "jobs.poll_interval_s",
        "jobs.timeout",
    } <= set(settings.RESTART_REQUIRED)


def test_alert_target_settings_are_restart_required():
    """The alert sinks are constructed once, at startup, in both the worker and
    the cron producer — an edit cannot reach a live daemon."""
    assert {
        "slack.alert_target",
        "telegram.alert_target",
    } <= set(settings.RESTART_REQUIRED)


def test_check_reload_ignores_a_field_that_is_re_read(operator_settings):
    """ttl_seconds and the allowlist land on their own. Reporting them would train
    the operator to ignore the notice."""
    operator_settings.write_text("permissions:\n  ttl_seconds: 60\n")
    settings.check_operator_settings()
    operator_settings.write_text(
        "permissions:\n  ttl_seconds: 120\negress:\n  allow: [b.example]\n"
    )
    assert settings.check_reload() == ()


def test_reordering_a_section_is_not_a_change(operator_settings):
    """A YAML mapping's order is not meaningful, and a fingerprint that said
    otherwise would report a restart for reformatting."""
    operator_settings.write_text('permissions:\n  mode: "off"\n  ttl_seconds: 60\n')
    settings.check_operator_settings()
    operator_settings.write_text('permissions:\n  ttl_seconds: 60\n  mode: "off"\n')
    assert settings.check_reload() == ()


# --- where it lives ---


def test_the_operator_file_is_outside_the_agents_write_scope():
    """This file decides what runs outside the sandbox with real credentials and
    which hosts skip the operator prompt, so the agent must not be able to edit it."""
    from claude_on_the_fly import sandbox

    path = settings.operator_settings()
    assert path.name == "config.yaml"
    assert path.parent.name == ".claude-on-the-fly"
    # Same directory that holds the shims, which is read/exec but not writable.
    assert path.parent == sandbox.shim_dir().parent
