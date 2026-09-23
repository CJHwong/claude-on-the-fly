"""claude_on_the_fly.audit: replaying past tool calls against a config.

Every number this script prints decides an allowlist line, so the parser is tested
on the shapes that broke earlier hand-written versions: heredoc bodies read as
commands, `VAR=/path/tool` counted as a call, and newlines lost to shlex.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_on_the_fly import audit as audit_module
from claude_on_the_fly import commands


@pytest.fixture
def audit():
    return audit_module


# --- splitting a Bash call into commands ---


def test_a_heredoc_body_is_not_a_command(audit):
    script = "python3 - <<'EOF'\nSELECT * FROM t\nf.write(x)\nEOF\ngh pr list"
    assert [c.argv for c in audit.commands_in(script)] == [
        ["python3", "-"],
        ["gh", "pr", "list"],
    ]


def test_a_multiline_quoted_argument_is_one_word(audit):
    """Split at every newline, the body of `python3 -c "..."` came back as rows
    like `= str`, `))` and `= /home/.../gws`."""
    code = "\nimport json\nname = str(1)\nGWS = '/home/u/.local/bin/gws'\nprint(len(name))\n"
    script = f'python3 -c "{code}" && gh pr list'
    assert [c.argv for c in audit.commands_in(script)] == [
        ["python3", "-c", code],
        ["gh", "pr", "list"],
    ]


def test_a_single_quoted_body_and_an_escaped_quote_stay_in_one_word(audit):
    script = 'python3 -c \'\nx = "a\\"b"\nprint((x))\n\'\necho \\"done'
    assert [c.argv for c in audit.commands_in(script)] == [
        ["python3", "-c", '\nx = "a\\"b"\nprint((x))\n'],
        ["echo", '"done'],
    ]


def test_an_apostrophe_in_a_comment_does_not_open_a_quote(audit):
    script = "# don't split here\ngh pr list  # isn't a quote\necho done"
    assert [c.argv for c in audit.commands_in(script)] == [
        ["gh", "pr", "list"],
        ["echo", "done"],
    ]


def test_an_assignment_is_not_a_call(audit):
    """`basename("SLACKER=/path/slacker.sh")` is `slacker.sh`, which is how an
    earlier replay counted 121 assignments as invocations."""
    calls = audit.commands_in('SLACKER="$HOME/bin/slacker.sh"\nFOO=1 gh pr view 3')
    assert [c.argv for c in calls] == [["gh", "pr", "view", "3"]]


def test_operators_split_commands_and_redirects_are_kept(audit):
    calls = audit.commands_in("cd /x && gh pr list | head -3 > out.txt; echo hi >> log")
    assert [c.argv for c in calls] == [
        ["cd", "/x"],
        ["gh", "pr", "list"],
        ["head", "-3"],
        ["echo", "hi"],
    ]
    assert calls[2].writes == ["out.txt"]
    assert calls[3].writes == ["log"]


def test_wrappers_are_looked_through(audit):
    calls = audit.commands_in("env A=1 timeout 30 gh run list")
    assert [c.argv for c in calls] == [["gh", "run", "list"]]


def test_a_continued_line_is_one_command(audit):
    calls = audit.commands_in("gh pr list \\\n  --limit 3")
    assert [c.argv for c in calls] == [["gh", "pr", "list", "--limit", "3"]]


def test_unparseable_text_is_skipped_not_fatal(audit):
    assert audit.commands_in('echo "unterminated') == []


# --- which invocations reach the broker ---


def test_a_bare_name_reaches_the_shim_and_a_path_bypasses_it(audit):
    names = {"slacker.sh"}
    bare = audit.commands_in("slacker.sh whois @me")[0]
    path = audit.commands_in("~/.local/bin/slacker.sh whois @me")[0]
    assert audit.invocation(bare, names, {}) == ("slacker.sh", "bare")
    assert audit.invocation(path, names, {}) == ("slacker.sh", "path")


def test_a_variable_holding_a_tool_path_is_a_bypass(audit):
    script = 'SLACKER="${SLACKER_SH:-/skills/slacker.sh}"\n"$SLACKER" send @me hi'
    calls = audit.commands_in(script)
    variables = audit.assignments_in(script)
    assert audit.invocation(calls[0], {"slacker.sh"}, variables) == (
        "slacker.sh",
        "variable",
    )


def test_an_unrelated_command_is_not_an_invocation(audit):
    call = audit.commands_in("ls -la")[0]
    assert audit.invocation(call, {"gh"}, {}) is None


# --- the broker's verdict ---


def _tool(**fields):
    return commands._tool_from_entry({"name": "gh", **fields})


def test_the_verdict_uses_the_brokers_own_checks(audit, tmp_path):
    tool = _tool(allow=["pr list", "pr view"], readback=["auth token"])
    assert audit.verdict(tool, ["pr", "list"], tmp_path) == "allowed"
    assert audit.verdict(tool, ["pr", "create"], tmp_path) == "not allowlisted"
    assert audit.verdict(tool, ["auth", "token"], tmp_path) == "not allowlisted"


def test_a_leading_flag_is_named(audit, tmp_path):
    tool = _tool(allow=["pr list"])
    assert audit.verdict(tool, ["-R", "o/r", "pr", "list"], tmp_path) == (
        "flag before subcommand"
    )


def test_a_path_outside_the_workspace_is_refused(audit, tmp_path):
    tool = _tool(allow=["pr view"])
    assert audit.verdict(tool, ["pr", "view", "/etc/passwd"], tmp_path) == (
        "path argument"
    )


# --- file access against the jail's grants ---


def _grants(tmp_path):
    home = tmp_path / "home"
    return {
        "opaque": [home],
        "read_only": [home / ".claude"],
        "read_write": [home / "ws", home / "memory"],
        "write_denied": [home / "ws" / ".git" / "hooks"],
        "masked": [home / ".claude" / ".credentials.json"],
    }


def test_writes_are_judged_against_the_write_grants(audit, tmp_path):
    grants = _grants(tmp_path)
    home = tmp_path / "home"
    assert audit.writable(home / "ws" / "a.txt", grants)
    assert not audit.writable(home / "ws" / ".git" / "hooks" / "pre-commit", grants)
    assert not audit.writable(home / ".config" / "systemd" / "x.service", grants)


def test_reads_are_judged_against_the_read_grants(audit, tmp_path):
    grants = _grants(tmp_path)
    home = tmp_path / "home"
    assert audit.readable(home / ".claude" / "settings.json", grants)
    assert not audit.readable(home / ".claude" / ".credentials.json", grants)
    assert not audit.readable(home / "notes" / "todo.md", grants)
    # Outside every opaque tree the root is readable.
    assert audit.readable(Path("/usr/share/dict/words"), grants)


def test_a_blocked_path_is_grouped_by_its_directory(audit, tmp_path):
    home = tmp_path / "home"
    assert audit.group(home / ".config" / "systemd" / "user" / "a.service", home) == (
        "~/.config/systemd"
    )
    assert audit.group(Path("/etc/hosts"), home) == "/etc/hosts"


# --- one transcript, end to end ---


def _line(cwd, name, tool_input):
    return json.dumps(
        {
            "type": "assistant",
            "cwd": str(cwd),
            "message": {
                "content": [{"type": "tool_use", "name": name, "input": tool_input}]
            },
        }
    )


def test_a_transcript_is_tallied(audit, tmp_path):
    ws = tmp_path / "home" / "ws"
    ws.mkdir(parents=True)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        "\n".join(
            [
                _line(ws, "Bash", {"command": "gh pr list; gh pr create"}),
                _line(ws, "Bash", {"command": "/usr/bin/gh pr list"}),
                _line(ws, "Write", {"file_path": str(tmp_path / "home" / "x.md")}),
                _line(ws, "Read", {"file_path": str(ws / "a.py")}),
                "not json",
            ]
        )
    )
    tools = {"gh": _tool(allow=["pr list"])}
    tally = audit.Tally()
    audit.replay(
        transcript, tools, lambda _cwd: _grants(tmp_path), tally, home=tmp_path / "home"
    )
    assert tally.calls["gh"] == {"bare": 2, "path": 1}
    assert tally.refused["gh"] == {"not allowlisted: pr create": 1}
    assert tally.blocked_writes == {"~/x.md": 1}
    assert tally.blocked_reads == {}


def test_the_report_names_every_refusal(audit):
    tally = audit.Tally()
    tally.calls["gh"] = {"bare": 3, "path": 1}
    tally.refused["gh"] = {"not allowlisted: pr create": 2}
    tally.blocked_writes = {"~/.config/systemd": 4}
    report = audit.render(tally, transcripts=7)
    assert "| gh | 3 | 1 | 0 | 2 |" in report
    assert "| gh | not allowlisted: pr create | 2 |" in report
    assert "| ~/.config/systemd | 4 |" in report
    assert "7 transcripts" in report


def test_an_fd_before_a_redirect_is_not_an_argument(audit):
    call = audit.commands_in("gh pr list 2> err.txt")[0]
    assert call.argv == ["gh", "pr", "list"]
    assert call.writes == ["err.txt"]


def test_a_variable_never_assigned_is_not_an_invocation(audit):
    call = audit.commands_in('"$UNSET" send')[0]
    assert audit.invocation(call, {"slacker.sh"}, {}) is None


def test_a_read_only_prefix_asked_to_write_is_named(audit, tmp_path):
    tool = _tool(allow_read_only=["api"])
    assert audit.verdict(tool, ["api", "-XPOST", "repos/o/r"], tmp_path) == (
        "write on read-only"
    )


def test_a_readback_inside_the_allowlist_is_named(audit, tmp_path):
    tool = _tool(allow=["auth"], readback=["auth token"])
    assert audit.verdict(tool, ["auth", "token"], tmp_path) == "credential readback"


def test_every_file_tool_and_redirect_is_judged(audit, tmp_path):
    home = tmp_path / "home"
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        "\n".join(
            [
                # No cwd: resolved against home.
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Bash",
                                    "input": {
                                        "command": "echo x > ~/.bashrc 2>/dev/null"
                                    },
                                }
                            ]
                        },
                    }
                ),
                json.dumps({"type": "user", "message": {"content": "hi"}}),
                _line(
                    home / "ws", "Edit", {"file_path": "~/.config/systemd/u/a.service"}
                ),
                _line(
                    home / "ws",
                    "NotebookEdit",
                    {"notebook_path": str(home / "ws/n.ipynb")},
                ),
                _line(home / "ws", "Read", {"file_path": str(home / "notes/todo.md")}),
                _line(home / "ws", "Read", {}),
            ]
        )
    )
    tally = audit.Tally()
    audit.replay(transcript, {}, lambda _cwd: _grants(tmp_path), tally, home=home)
    assert tally.blocked_writes == {"~/.bashrc": 1, "~/.config/systemd": 1}
    assert tally.blocked_reads == {"~/notes/todo.md": 1}


def test_an_empty_section_says_so(audit):
    assert "None." in audit.render(audit.Tally(), transcripts=0)


def test_main_judges_a_config_as_if_jailed(audit, tmp_path, monkeypatch, capsys):
    """The config says `env`; the audit still judges the jail's grants, because
    the question it answers is what switching would break."""
    # setenv, not delenv: only setenv records "absent" and undoes what main() writes.
    monkeypatch.setenv("COTF_DATA_DIR", "unset")
    monkeypatch.setenv("COTF_SANDBOX", "env")
    home = tmp_path / "home"
    ws = home / "ws"
    ws.mkdir(parents=True)
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.yaml").write_text(
        "sandbox:\n  mode: env\ncommands:\n  tools:\n    - name: gh\n"
        "      allow: [pr list]\n"
    )
    transcripts = tmp_path / "projects"
    transcripts.mkdir()
    (transcripts / "a.jsonl").write_text(
        _line(ws, "Bash", {"command": "gh pr list; gh repo delete x"})
    )
    seen: dict = {}

    def grants(workspace):
        seen["mode"] = __import__("os").environ["COTF_SANDBOX"]
        return {"read_write": [workspace]}

    from claude_on_the_fly import sandbox

    monkeypatch.setattr(sandbox, "_linux_grants", grants)
    assert (
        audit.main(["--data-dir", str(data), "--home", str(home), str(transcripts)])
        == 0
    )
    report = capsys.readouterr().out
    assert seen["mode"] == "jail"
    assert "1 transcripts" in report
    assert "| gh | not allowlisted: repo delete | 1 |" in report


def test_main_reads_every_transcript_under_home_by_default(
    audit, tmp_path, monkeypatch, capsys
):
    # setenv, not delenv: only setenv records "absent" and undoes what main() writes.
    monkeypatch.setenv("COTF_DATA_DIR", "unset")
    monkeypatch.setenv("COTF_SANDBOX", "env")
    home = tmp_path / "home"
    project = home / ".claude" / "projects" / "p"
    project.mkdir(parents=True)
    (project / "one.jsonl").write_text("")
    (project / "two.jsonl").write_text("")
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.yaml").write_text("")
    from claude_on_the_fly import sandbox

    monkeypatch.setattr(sandbox, "_linux_grants", lambda _ws: {})
    audit.main(["--data-dir", str(data), "--home", str(home)])
    assert "2 transcripts" in capsys.readouterr().out


def test_a_path_through_a_link_is_judged_where_it_lands(audit, tmp_path):
    """The kernel resolves the link before the jail sees the write, so
    `~/.claude-on-the-fly/memory/x` is judged at the repo it points into."""
    home = tmp_path / "home"
    soul = home / "soul" / "memory"
    soul.mkdir(parents=True)
    (home / "data").mkdir()
    (home / "data" / "memory").symlink_to(soul)
    grants = {"opaque": [home], "read_write": [home / "soul"]}
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        _line(home, "Write", {"file_path": str(home / "data" / "memory" / "x.md")})
    )
    tally = audit.Tally()
    audit.replay(transcript, {}, lambda _cwd: grants, tally, home=home)
    assert tally.blocked_writes == {}
