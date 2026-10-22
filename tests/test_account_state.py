"""Account profiles share transcripts without sharing mutable credential links."""

import pytest

from claude_on_the_fly import accounts, codex_auth, codex_state, envfile, sandbox


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    base = tmp_path / "base"
    first = tmp_path / "first"
    second = tmp_path / "second"
    for home, credential in ((base, "base"), (first, "first"), (second, "second")):
        home.mkdir()
        (home / "auth.json").write_text(credential)
    (base / "config.toml").write_text('model = "base-model"\n')
    monkeypatch.setattr(
        envfile, "daemon_environment", lambda: {"CODEX_HOME": str(base)}
    )
    monkeypatch.setattr(codex_state, "HOMES_DIR", tmp_path / "homes")
    monkeypatch.setattr(sandbox, "scoped_sessions", lambda: False)
    return base, first, second


def test_account_homes_keep_credentials_separate_and_resume_history(profiles, tmp_path):
    base, first, second = profiles
    workspace = tmp_path / "workspace"
    with accounts.use(accounts.Account("first", "codex", first)):
        first_home = codex_state.ensure_home(workspace)
        rollout = first_home / "sessions" / "rollout-test-thread-1.jsonl"
        rollout.write_text("conversation marker")
    with accounts.use(accounts.Account("second", "codex", second)):
        second_home = codex_state.home_dir(workspace)
        assert not second_home.exists()
        assert codex_state.adopt_rollout(workspace, "thread-1")
        assert (
            second_home / "sessions" / rollout.name
        ).read_text() == "conversation marker"
    assert first_home != second_home
    assert (first_home / "auth.json").resolve() == first / "auth.json"
    assert (second_home / "auth.json").resolve() == second / "auth.json"
    assert (first_home / "config.toml").resolve() == base / "config.toml"
    assert (second_home / "config.toml").resolve() == base / "config.toml"
    assert (first_home / "sessions").resolve() == (second_home / "sessions").resolve()


def test_explicit_path_environment_bypasses_account_context(profiles):
    base, first, _ = profiles
    with accounts.use(accounts.Account("first", "codex", first)):
        assert envfile.codex_home() == first
        assert envfile.codex_home({"CODEX_HOME": str(base)}) == base
    with accounts.use(accounts.Account("first", "claude", first)):
        assert envfile.claude_config_dir() == first
        assert envfile.claude_config_dir({"CLAUDE_CONFIG_DIR": str(base)}) == base


def test_unjailed_account_env_removes_inherited_auth(profiles, monkeypatch):
    _, first, _ = profiles
    monkeypatch.setattr(sandbox, "enabled", lambda: False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "wrong-profile")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "wrong-profile")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "wrong-profile")
    with accounts.use(accounts.Account("native", "claude", None)):
        child = sandbox.agent_env()
        assert "CLAUDE_CONFIG_DIR" not in child
        assert "ANTHROPIC_API_KEY" not in child
        assert "ANTHROPIC_AUTH_TOKEN" not in child
    with accounts.use(accounts.Account("first", "claude", first)):
        assert sandbox.agent_env()["CLAUDE_CONFIG_DIR"] == str(first)


def test_unjailed_codex_account_gets_its_broker_route(profiles, monkeypatch):
    _, first, _ = profiles
    monkeypatch.setattr(sandbox, "enabled", lambda: False)
    monkeypatch.setenv("COTF_CHATGPT_BASE_URL", "http://wrong-account")
    monkeypatch.setattr(codex_auth, "published_url", lambda: "http://selected-account")
    with accounts.use(accounts.Account("first", "codex", first)):
        assert sandbox.agent_env()["COTF_CHATGPT_BASE_URL"] == "http://selected-account"
    with accounts.use(accounts.Account("first", "claude", first)):
        assert "COTF_CHATGPT_BASE_URL" not in sandbox.agent_env()


def test_overlay_cleanup_keeps_operator_credentials(profiles, tmp_path):
    _, first, _ = profiles
    workspace = tmp_path / "workspace"
    with accounts.use(accounts.Account("first", "codex", first)):
        home = codex_state.ensure_home(workspace)
    codex_state.remove_workspace(workspace)
    assert not home.exists()
    assert (first / "auth.json").read_text() == "first"


def test_account_profile_grants_refuse_extra_paths(profiles, monkeypatch):
    _, first, second = profiles
    monkeypatch.setattr(
        accounts,
        "all_accounts",
        lambda: {
            "first": accounts.Account("first", "claude", first),
            "second": accounts.Account("second", "codex", second),
        },
    )
    assert sandbox._extra_path_refusal(first) is not None
    assert sandbox._extra_path_refusal(second / "auth.json") is not None


def test_account_policy_denies_other_profiles_and_grants_only_selected_auth(
    profiles, monkeypatch, tmp_path
):
    _, first, second = profiles
    monkeypatch.setattr(
        accounts,
        "all_accounts",
        lambda: {
            "first": accounts.Account("first", "codex", first),
            "second": accounts.Account("second", "codex", second),
        },
    )
    monkeypatch.setattr(codex_auth, "published_url", lambda: None)
    workspace = tmp_path / "workspace"
    with accounts.use(accounts.Account("first", "codex", first)):
        home = codex_state.ensure_home(workspace)
        grants = {
            "opaque": [],
            "read_only": [second],
            "read_write": [first, second],
            "write_denied": [second / "auth.json"],
        }
        sandbox._protect_account_mounts(grants, workspace)
        assert second in grants["opaque"]
        assert second not in grants["read_only"]
        assert second not in grants["read_write"]
        assert second / "auth.json" not in grants["write_denied"]
        assert first / "auth.json" in grants["read_write"]
        rules = sandbox._account_seatbelt_rules(workspace)
        assert f'(deny file-read* file-write* (subpath "{second}"))' in rules
        assert f'(literal "{first / "auth.json"}")' in rules
        assert str((home / "sessions").resolve()) in rules


def test_custom_claude_profile_uses_its_own_keychain_service(profiles, monkeypatch):
    import hashlib
    import json
    import unicodedata

    from claude_on_the_fly import broker

    _, first, _ = profiles
    called = []

    def credential(service):
        called.append(service)
        return json.dumps({"claudeAiOauth": {"accessToken": "profile-token"}})

    monkeypatch.setattr(sandbox, "_platform", lambda: "darwin")
    monkeypatch.setattr(broker, "read_keychain", credential)
    with accounts.use(accounts.Account("first", "claude", first)):
        assert sandbox._claude_oauth_token() == "profile-token"
    suffix = hashlib.sha256(
        unicodedata.normalize("NFC", str(first)).encode("utf-8")
    ).hexdigest()[:8]
    assert called == [f"Claude Code-credentials-{suffix}"]
    called.clear()
    with accounts.use(accounts.Account("native", "claude", None)):
        assert sandbox._claude_oauth_token() == "profile-token"
    assert called == ["Claude Code-credentials"]


def test_account_auth_symlink_targets_stay_hidden(profiles, monkeypatch, tmp_path):
    _, first, second = profiles
    external = tmp_path / "external-auth.json"
    external.write_text("other-account")
    (second / "auth.json").unlink()
    (second / "auth.json").symlink_to(external)
    configured = {
        "first": accounts.Account("first", "codex", first),
        "second": accounts.Account("second", "codex", second),
    }
    monkeypatch.setattr(accounts, "all_accounts", lambda: configured)
    monkeypatch.setattr(codex_auth, "published_url", lambda: None)
    with accounts.use(configured["first"]):
        grants = {"opaque": [], "read_only": [], "read_write": []}
        sandbox._protect_account_mounts(grants, tmp_path / "workspace")
        assert external in grants["masked"]
        assert first / "auth.json" not in grants["masked"]
        assert sandbox._extra_path_refusal(external) is not None
        rules = sandbox._account_seatbelt_rules(tmp_path / "workspace")
        assert f'(deny file-read* file-write* (subpath "{external}"))' in rules


def test_brokered_selected_account_masks_its_auth(profiles, monkeypatch, tmp_path):
    _, first, _ = profiles
    selected = accounts.Account("first", "codex", first)
    monkeypatch.setattr(accounts, "all_accounts", lambda: {"first": selected})
    monkeypatch.setattr(codex_auth, "published_url", lambda: "http://broker")
    with accounts.use(selected):
        grants = {"opaque": [], "read_only": [], "read_write": []}
        sandbox._protect_account_mounts(grants, tmp_path / "workspace")
        assert first / "auth.json" in grants["masked"]
        assert first / "auth.json" not in grants["read_write"]
        assert (
            "(allow file-read* file-write* (literal"
            not in sandbox._account_seatbelt_rules(tmp_path / "workspace")
        )


def test_ollama_and_codex_accounts_do_not_read_claude_credentials(
    profiles, monkeypatch
):
    from claude_on_the_fly import broker

    _, first, _ = profiles

    def unexpected_read(service):
        pytest.fail(f"Unexpected keychain read: {service}")

    monkeypatch.setattr(broker, "read_keychain", unexpected_read)
    for selected in (
        accounts.Account("first", "codex", first),
        accounts.Account("ollama", "claude", first, mode="ollama"),
    ):
        with accounts.use(selected):
            assert sandbox._claude_credential_text() is None


def test_no_account_leaves_dynamic_policy_empty(tmp_path):
    with accounts.use(None):
        assert sandbox._account_denied_homes() == []
        assert sandbox._account_seatbelt_rules(tmp_path) == ""
        grants = {"opaque": [], "read_only": [], "read_write": []}
        sandbox._protect_account_mounts(grants, tmp_path)
        assert grants == {"opaque": [], "read_only": [], "read_write": []}


def test_mac_wrapper_applies_account_rules_after_base_profile(
    profiles, monkeypatch, tmp_path
):
    _, first, second = profiles
    selected = accounts.Account("first", "codex", first)
    monkeypatch.setattr(
        accounts,
        "all_accounts",
        lambda: {
            "first": selected,
            "second": accounts.Account("second", "codex", second),
        },
    )
    monkeypatch.setattr(sandbox, "mode", lambda: "jail")
    monkeypatch.setattr(sandbox, "_platform", lambda: "darwin")
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/sandbox-exec")
    monkeypatch.setattr(codex_auth, "published_url", lambda: None)
    with accounts.use(selected):
        codex_state.ensure_home(tmp_path / "workspace")
        wrapped = sandbox.wrap(["/bin/echo", "probe"], tmp_path / "workspace")
    assert "-f" not in wrapped
    profile = wrapped[wrapped.index("-p") + 1]
    assert profile.startswith('(import "')
    assert profile.index('(import "') < profile.index("(deny file-read* file-write*")
    assert str(second) in profile
    assert wrapped[-2:] == ["/bin/echo", "probe"]


def test_missing_selected_keychain_login_does_not_fall_back(profiles, monkeypatch):
    from claude_on_the_fly import broker

    _, first, _ = profiles
    calls = []

    def missing(service):
        calls.append(service)
        raise KeyError(service)

    monkeypatch.setattr(sandbox, "_platform", lambda: "darwin")
    monkeypatch.setattr(broker, "read_keychain", missing)
    with accounts.use(accounts.Account("first", "claude", first)):
        assert sandbox._claude_oauth_token() is None
    assert len(calls) == 1
    assert calls[0].startswith("Claude Code-credentials-")


def test_empty_command_shim_directory_preserves_search_path(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "shim_dir", lambda: tmp_path)
    child = {"PATH": "/bin", "COTF_CMD_ENDPOINT": "http://broker"}
    assert sandbox._with_shims_on_path(child)["PATH"] == "/bin"


def test_selected_account_without_broker_drops_inherited_route(profiles, monkeypatch):
    _, first, _ = profiles
    monkeypatch.setattr(sandbox, "enabled", lambda: False)
    monkeypatch.setenv("COTF_CHATGPT_BASE_URL", "http://wrong-account")
    monkeypatch.setattr(codex_auth, "published_url", lambda: None)
    with accounts.use(accounts.Account("first", "codex", first)):
        assert "COTF_CHATGPT_BASE_URL" not in sandbox.agent_env()


def test_changed_overlay_sessions_link_restores_workspace_history(profiles, tmp_path):
    _, first, _ = profiles
    workspace = tmp_path / "workspace"
    other = tmp_path / "other-history"
    other.mkdir()
    with accounts.use(accounts.Account("first", "codex", first)):
        home = codex_state.ensure_home(workspace)
        canonical = (home / "sessions").resolve()
        (home / "sessions").unlink()
        (home / "sessions").symlink_to(other)
        codex_state.ensure_home(workspace)
        assert (home / "sessions").resolve() == canonical


def test_real_overlay_sessions_directory_refuses_history_split(profiles, tmp_path):
    _, first, _ = profiles
    with accounts.use(accounts.Account("first", "codex", first)):
        home = codex_state.home_dir(tmp_path / "workspace")
        (home / "sessions").mkdir(parents=True)
        with pytest.raises(
            accounts.AccountError, match="must link to the workspace history"
        ):
            codex_state.ensure_home(tmp_path / "workspace")


@pytest.mark.parametrize("backend", ["claude", "codex"])
@pytest.mark.parametrize("brokered", [False, True])
def test_unnamed_base_credentials_stay_hidden(
    profiles, monkeypatch, tmp_path, backend, brokered
):
    base, first, _ = profiles
    selected = accounts.Account("first", backend, first)
    base_claude = tmp_path / "base-claude"
    base_claude.mkdir()
    claude_credential = base_claude / ".credentials.json"
    claude_credential.write_text("base-claude-credential")
    monkeypatch.setattr(
        envfile,
        "daemon_environment",
        lambda: {"CODEX_HOME": str(base), "CLAUDE_CONFIG_DIR": str(base_claude)},
    )
    monkeypatch.setattr(accounts, "all_accounts", lambda: {"first": selected})
    monkeypatch.setattr(
        codex_auth, "published_url", lambda: "http://broker" if brokered else None
    )
    with accounts.use(selected):
        grants = {
            "opaque": [],
            "read_only": [base, base / "auth.json"],
            "read_write": [base],
            "write_denied": [base / "auth.json"],
        }
        sandbox._protect_account_mounts(grants, tmp_path / "workspace")
        assert base in grants["opaque"]
        assert base not in grants["read_write"]
        assert base / "auth.json" not in grants["read_only"]
        assert base / "auth.json" not in grants["write_denied"]
        assert base / "auth.json" in grants["masked"]
        assert claude_credential in grants["masked"]
        assert base / "config.toml" in grants["read_only"]
        rules = sandbox._account_seatbelt_rules(tmp_path / "workspace")
        assert f'(deny file-read* file-write* (subpath "{base}"))' in rules
        assert f'(deny file-read* file-write* (subpath "{claude_credential}"))' in rules
        assert (
            f'(deny file-read* file-write* (subpath "{base / "auth.json"}"))' in rules
        )
        assert f'(allow file-read* (subpath "{base / "config.toml"}"))' in rules
        assert (
            f'(allow file-read* file-write* (literal "{base / "auth.json"}"))'
            not in rules
        )


def test_first_selected_account_adopts_legacy_daemon_rollout(profiles, tmp_path):
    base, first, _ = profiles
    legacy = (
        base / "sessions" / "2026" / "10" / "23" / "rollout-test-thread-legacy.jsonl"
    )
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy conversation marker")
    selected = accounts.Account("first", "codex", first)
    with accounts.use(selected):
        assert codex_state.adopt_rollout(tmp_path / "workspace", "thread-legacy")
        adopted = codex_state.home_dir(tmp_path / "workspace") / legacy.relative_to(
            base
        )
        assert adopted.read_text() == "legacy conversation marker"
    assert legacy.read_text() == "legacy conversation marker"
    assert not (first / "sessions").exists()


def test_account_rules_preserve_unicode_profile_paths(profiles, monkeypatch, tmp_path):
    _, first, _ = profiles
    other = tmp_path / "café"
    other.mkdir()
    configured = {
        "first": accounts.Account("first", "codex", first),
        "other": accounts.Account("other", "codex", other),
    }
    monkeypatch.setattr(accounts, "all_accounts", lambda: configured)
    monkeypatch.setattr(codex_auth, "published_url", lambda: None)
    with accounts.use(configured["first"]):
        rules = sandbox._account_seatbelt_rules(tmp_path / "workspace")
    assert f'(deny file-read* file-write* (subpath "{other}"))' in rules
    assert "\\u00e9" not in rules


@pytest.mark.parametrize("brokered", [False, True])
def test_implicit_default_turn_hides_named_credentials(
    profiles, monkeypatch, tmp_path, brokered
):
    base, first, _ = profiles
    external = tmp_path / "named-credential.json"
    external.write_text("named-account")
    (first / "auth.json").unlink()
    (first / "auth.json").symlink_to(external)
    configured = {"first": accounts.Account("first", "codex", first)}
    monkeypatch.setattr(accounts, "all_accounts", lambda: configured)
    monkeypatch.setattr(
        codex_auth, "published_url", lambda: "http://broker" if brokered else None
    )
    with accounts.use(None):
        grants = {"opaque": [], "read_only": [base, first], "read_write": [base, first]}
        sandbox._protect_account_mounts(grants, tmp_path / "workspace")
        assert first in grants["opaque"]
        assert first not in grants["read_only"]
        assert first not in grants["read_write"]
        assert base in grants["read_only"]
        assert base in grants["read_write"]
        assert external in grants["masked"]
        assert (base / "auth.json" in grants["masked"]) == brokered
        rules = sandbox._account_seatbelt_rules(tmp_path / "workspace")
        assert f'(deny file-read* file-write* (subpath "{first}"))' in rules
        assert f'(deny file-read* file-write* (subpath "{external}"))' in rules
        allow_auth = f'(allow file-read* file-write* (literal "{base / "auth.json"}"))'
        assert (allow_auth in rules) == (not brokered)


def test_redirected_claude_config_keeps_default_read_grant_without_accounts(
    tmp_path, monkeypatch
):
    from claude_on_the_fly import agent

    home = tmp_path / "home"
    redirected = tmp_path / "redirected-claude"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(redirected))
    monkeypatch.setattr(accounts, "all_accounts", lambda: {})
    monkeypatch.setattr(agent, "DATA_DIR", home / "daemon")
    monkeypatch.setattr(agent, "MEMORY_DIR", home / "daemon" / "memory")
    monkeypatch.setattr(
        envfile, "daemon_environment", lambda: {"CLAUDE_CONFIG_DIR": str(redirected)}
    )
    grants = sandbox._linux_grants(tmp_path / "workspace")
    assert home / ".claude" in grants["read_only"]
    assert redirected in grants["read_only"]


def test_named_account_does_not_restore_default_claude_read_grant(
    tmp_path, monkeypatch
):
    from claude_on_the_fly import agent

    home = tmp_path / "home"
    selected = accounts.Account("selected", "claude", home / "selected")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(accounts, "all_accounts", lambda: {"selected": selected})
    monkeypatch.setattr(agent, "DATA_DIR", home / "daemon")
    monkeypatch.setattr(agent, "MEMORY_DIR", home / "daemon" / "memory")
    monkeypatch.setattr(
        envfile,
        "daemon_environment",
        lambda: {"CLAUDE_CONFIG_DIR": str(home / "redirected")},
    )
    with accounts.use(selected):
        grants = sandbox._linux_grants(tmp_path / "workspace")
    assert home / ".claude" not in grants["read_only"]
    assert selected.source_home in grants["read_only"]
