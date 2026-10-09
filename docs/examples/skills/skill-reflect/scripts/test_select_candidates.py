# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest"]
# ///
"""Fixture tests for select_candidates.py: a tiny thread index, rollouts and cron logs."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "select_candidates", Path(__file__).with_name("select_candidates.py")
)
sel = importlib.util.module_from_spec(SPEC)
sys.modules["select_candidates"] = sel
SPEC.loader.exec_module(sel)

UPSTREAM_SKILL = (
    "/Users/x/.codex/plugins/cache/team-market/jira-kit/2.30.0/skills/fetch/SKILL.md"
)
SOUL = Path("/Users/x/Soul")
LOCAL_SKILL = f"{SOUL}/skills/agent-ops/skills/check-ai-usage/SKILL.md"
CONFIG = sel.Config(SOUL, ("team-market",), ("twg", ".system/"))


def user(text: str, who: str = "U1") -> dict:
    body = f'[from-id: {who}] [display: "p"] {text}\n\n<cotf-outbox>System instruction</cotf-outbox>'
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": body}],
        },
    }


def injected(text: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def agent(text: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        },
    }


def call(call_id: str, cmd: str) -> dict:
    js = "const r=await tools.exec_command({cmd:" + json.dumps(cmd) + "});"
    return {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call",
            "call_id": call_id,
            "name": "exec",
            "input": js,
        },
    }


def result(call_id: str, code: int) -> dict:
    text = f'Script completed\nOutput:\n{{"exit_code":{code}}}'
    return {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call_output",
            "call_id": call_id,
            "output": [{"type": "input_text", "text": text}],
        },
    }


@pytest.fixture
def world(tmp_path: Path) -> dict:
    codex, cotf = tmp_path / "codex", tmp_path / "cotf"
    claude, soul = tmp_path / "claude", tmp_path / "Soul"
    (cotf / "logs").mkdir(parents=True)
    codex.mkdir()
    (cotf / "skill-reflect.yaml").write_text(
        f"soul_root: {soul}\n"
        "upstream_marketplaces: [team-market]\n"
        "third_party_skills: [twg, .system/]\n"
    )
    ws = cotf / "workspaces"
    con = sqlite3.connect(codex / "state_5.sqlite")
    con.execute(
        "create table threads (id text, rollout_path text, cwd text, updated_at int, thread_source text)"
    )
    con.commit()
    return {
        "codex": codex,
        "claude": claude,
        "soul": soul,
        "cotf": cotf,
        "ws": ws,
        "con": con,
        "tmp": tmp_path,
    }


def add_thread(
    world: dict, thread_id: str, cwd: Path, records: list[dict], source: str = "user"
) -> None:
    rollout = world["tmp"] / f"{thread_id}.jsonl"
    rollout.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    world["con"].execute(
        "insert into threads values (?,?,?,?,?)",
        (thread_id, str(rollout), str(cwd), int(time.time()), source),
    )
    world["con"].commit()


def run_select(world: dict, *extra: str) -> dict:
    out = world["tmp"] / "out" / "candidates.json"
    args = sel.parse_args(
        [
            "--out",
            str(out),
            "--codex-home",
            str(world["codex"]),
            "--claude-home",
            str(world["claude"]),
            "--cotf-home",
            str(world["cotf"]),
            *extra,
        ]
    )
    return sel.run(args)


def test_correction_after_reply_scores_and_excerpts(world):
    add_thread(
        world,
        "t1",
        world["ws"] / "slack/dm/U1",
        [
            injected("<environment_context>ignore me</environment_context>"),
            user("summarize the ticket"),
            call("c1", f"cat {UPSTREAM_SKILL}"),
            result("c1", 0),
            agent("Here is a long summary."),
            user("too long, just give me the status"),
        ],
    )
    out = run_select(world)
    cand = out["candidates"][0]
    assert cand["where"] == "dm/U1"
    assert cand["signals"] == {"correction": 1}
    assert cand["skills_read"] == [
        {"skill": "jira-kit:fetch", "owner": "upstream", "reads": 1}
    ]
    assert "[user U1] too long" in cand["excerpts"][0]["text"]
    assert "environment_context" not in cand["excerpts"][0]["text"]


def test_first_message_is_not_a_correction(world):
    add_thread(
        world,
        "t1",
        world["ws"] / "slack/dm/U1",
        [user("no rush, can you check the deploy?")],
    )
    out = run_select(world)
    assert out["candidates"] == []
    assert out["counts"]["sessions_scanned"] == 1


def test_explicit_ask_outranks_correction(world):
    add_thread(
        world,
        "t1",
        world["ws"] / "slack/dm/U1",
        [user("hi"), agent("ok"), user("wrong file")],
    )
    add_thread(
        world,
        "t2",
        world["ws"] / "slack/channel/C1",
        [user("hi"), agent("ok"), user("remember this for next time")],
    )
    out = run_select(world)
    assert [c["thread_id"] for c in out["candidates"]] == ["t2", "t1"]


def test_recovered_error_pairs_failure_with_later_success(world):
    add_thread(
        world,
        "t1",
        world["ws"] / "cron/standup-flash",
        [
            call("a", "gh api repos/x --jq .y"),
            result("a", 1),
            call("b", "ls"),
            result("b", 0),
            call("c", "gh api repos/x --paginate"),
            result("c", 0),
        ],
    )
    cand = run_select(world)["candidates"][0]
    assert cand["kind"] == "cron" and cand["where"] == "standup-flash"
    assert cand["signals"] == {"recovered_error": 1}
    text = cand["excerpts"][0]["text"]
    assert (
        "[tool exit=1] gh api repos/x --jq .y" in text
        and "[tool exit=0] gh api repos/x --paginate" in text
    )


def test_non_cotf_and_subagent_threads_are_skipped(world):
    add_thread(
        world,
        "t1",
        world["tmp"] / "elsewhere",
        [user("hi"), agent("ok"), user("wrong")],
    )
    add_thread(
        world,
        "t2",
        world["ws"] / "slack/dm/U1",
        [user("hi"), agent("ok"), user("wrong")],
        source="subagent",
    )
    assert run_select(world)["counts"]["sessions_scanned"] == 0


def test_skill_edit_is_counted_separately_from_reads(world):
    patch = {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call",
            "call_id": "p",
            "name": "exec",
            "input": f"apply_patch *** Update File: {world['soul']}/skills/agent-ops/skills/check-ai-usage/SKILL.md",
        },
    }
    add_thread(
        world,
        "t1",
        world["ws"] / "slack/dm/U1",
        [user("hi"), agent("ok"), user("update the skill"), patch],
    )
    cand = run_select(world)["candidates"][0]
    assert cand["skills_edited"] == [
        {"skill": "agent-ops:check-ai-usage", "owner": "local", "edits": 1}
    ]
    assert cand["skills_read"] == []


def write_skill(root: Path, rel: str) -> Path:
    path = root / rel / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nname: x\n---\n")
    return path


def test_usage_report_lists_unused_owned_skills_only(world):
    used = write_skill(world["soul"], "skills/agent-ops/skills/check-ai-usage")
    write_skill(world["soul"], "skills/agent-ops/skills/unused-one")
    write_skill(world["soul"], "skills/twg")
    write_skill(world["soul"], "skills/.trash/agent-ops/skills/gone")
    write_skill(
        world["codex"], "plugins/cache/team-market/jira-kit/2.30.0/skills/fetch"
    )
    write_skill(world["claude"], "plugins/cache/other-market/tool/1.0.0/skills/thing")
    add_thread(
        world,
        "t1",
        world["ws"] / "slack/dm/U1",
        [call("c", f"sed -n 1,40p {used}"), result("c", 0)],
    )
    usage = run_select(world)["skill_usage"]
    assert usage["sessions_reading"] == {"agent-ops:check-ai-usage": 1}
    # twg and the other marketplace are third-party; the trashed skill is not on disk.
    assert usage["unused_in_window"] == ["agent-ops:unused-one", "jira-kit:fetch"]
    assert usage["inventory"]["tool:thing"] == "third-party"


def test_cron_recovery_from_job_logs(world):
    def local(ago: float) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - ago))

    log = world["cotf"] / "logs" / "cron-standup-flash-My_Host-2026-10-06.log"
    old = local(10 * 86400)  # outside the 7-day window: ignored
    failed_at, done_at = local(3600), local(1800)
    log.write_text(
        f"--- {old} reply (FAILED) ---\nancient\n--- {failed_at} reply (FAILED) ---\nboom: 403\n"
        f"--- {done_at} reply (done) ---\nok\n"
    )
    (world["cotf"] / "logs" / "cron-My_Host-2026-10-06.log").write_text(
        f"--- {local(60)} reply (FAILED) ---\nx\n"
    )
    found = run_select(world)["cron_recoveries"]
    assert [f["job"] for f in found] == ["standup-flash"]
    assert found[0]["failures"] == 1 and found[0]["recovered_at"] == done_at
    assert "boom: 403" in found[0]["failure_excerpt"]


def test_state_cursor_overrides_days(world, tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"last_run_at": "2100-01-01T00:00:00+00:00"}))
    add_thread(
        world,
        "t1",
        world["ws"] / "slack/dm/U1",
        [user("hi"), agent("ok"), user("wrong")],
    )
    assert run_select(world, "--state", str(state))["counts"]["sessions_scanned"] == 0


def test_main_writes_file(world):
    out = world["tmp"] / "o" / "c.json"
    assert (
        sel.main(
            [
                "--out",
                str(out),
                "--codex-home",
                str(world["codex"]),
                "--claude-home",
                str(world["claude"]),
                "--cotf-home",
                str(world["cotf"]),
            ]
        )
        == 0
    )
    assert json.loads(out.read_text())["counts"]["sessions_scanned"] == 0


@pytest.mark.parametrize(
    "path,name,owner",
    [
        (UPSTREAM_SKILL, "jira-kit:fetch", "upstream"),
        (
            f"{SOUL}/workspace/team-market/plugins/pm-kit/skills/intake-request/SKILL.md",
            "pm-kit:intake-request",
            "upstream",
        ),
        (
            "/Users/x/.claude/plugins/cache/other/tool/1.0.0/skills/thing/SKILL.md",
            "tool:thing",
            "third-party",
        ),
        (f"{SOUL}/skills/slacker-sh/SKILL.md", "slacker-sh", "local"),
        (f"{SOUL}/skills/twg-jira/SKILL.md", "twg-jira", "third-party"),
        (LOCAL_SKILL, "agent-ops:check-ai-usage", "local"),
        ("/Users/x/elsewhere/skills/foo/SKILL.md", "elsewhere:foo", "unknown"),
    ],
)
def test_skill_names_match_the_codex_index(path, name, owner):
    assert (sel.skill_name(path, CONFIG), sel.owner_of(path, CONFIG)) == (name, owner)


def test_owner_follows_a_symlinked_skills_dir(tmp_path):
    soul = tmp_path / "Soul"
    skill = write_skill(soul, "skills/notes")
    link = tmp_path / "dot-claude" / "skills"
    link.parent.mkdir()
    link.symlink_to(soul / "skills")
    config = sel.Config(soul)
    assert sel.owner_of(str(link / "notes" / "SKILL.md"), config) == "local"
    assert sel.owner_of(str(skill), sel.Config()) == "unknown"


def test_missing_config_stops_with_a_hint(world):
    (world["cotf"] / "skill-reflect.yaml").unlink()
    with pytest.raises(SystemExit, match=r"skill-reflect\.yaml not found"):
        run_select(world)


@pytest.mark.parametrize(
    "text,hit",
    [
        ("No, use the other channel", True),
        ("that's wrong, it was Tuesday", True),
        ("不要再發到頻道", True),
        ("no rush", False),
        ("Demo video: No", False),
        ("還是用上週的格式", False),
        ("check it again tomorrow", False),
    ],
)
def test_correction_phrases(text, hit):
    assert bool(sel.CORRECTION.search(text)) is hit


def test_pick_caps_per_conversation_and_reserves_cron(world):
    for i in range(4):
        add_thread(
            world,
            f"dm{i}",
            world["ws"] / "slack/dm/U1",
            [user("hi"), agent("ok"), user("that's wrong")],
        )
    add_thread(
        world,
        "job",
        world["ws"] / "cron/standup",
        [call("a", "gh api x"), result("a", 1), call("b", "gh api y"), result("b", 0)],
    )
    picked = run_select(world, "--top", "2")["candidates"]
    assert sorted(c["thread_id"] for c in picked) == ["dm0", "job"]


def test_producer_items_count_as_one_job(world):
    for key in ("a", "b", "c"):
        add_thread(
            world,
            f"j{key}",
            world["ws"] / f"cron/sys-followup_{key}",
            [
                call("x", "gh api x"),
                result("x", 1),
                call("y", "gh api y"),
                result("y", 0),
            ],
        )
    picked = run_select(world)["candidates"]
    assert len(picked) == 2 and {c["where"] for c in picked} == {"sys-followup"}


@pytest.mark.parametrize(
    "rel,where",
    [
        ("slack/dm/U1", ("slack", "dm/U1")),
        ("slack/team-mazu-1779867296", ("slack", "team-mazu")),
        ("slack/dm-Encore_Alert-1789192495-174579", ("slack", "dm-Encore_Alert")),
        ("cron/standup_ACE-1", ("cron", "standup")),
        ("schedule/standup", ("cron", "standup")),
        ("jobs/__runs/abc", ("cron", "__runs")),
        ("slack", None),
        ("other/x", None),
    ],
)
def test_every_workspace_layout_is_classified(rel, where):
    assert sel.classify_cwd(f"/w/{rel}", Path("/w")) == where


def old_call(call_id: str, cmd: str) -> dict:
    args = json.dumps({"cmd": cmd, "workdir": "/w"})
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "call_id": call_id,
            "name": "exec_command",
            "arguments": args,
        },
    }


def old_result(call_id: str, code: int) -> dict:
    text = f"Chunk ID: x\nWall time: 0.1 seconds\nProcess exited with code {code}\nOutput:\nok"
    return {
        "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": call_id, "output": text},
    }


def test_old_session_reads_name_tag_and_exit_text(world):
    persona = "You are an assistant.\n\n## Memory\nRead notes.\n\n---\n[from: some.one] summarize the ticket"
    add_thread(
        world,
        "old",
        world["ws"] / "slack/dm-someone-1780390524",
        [
            injected("# AGENTS.md instructions for /w\n\nBe brief."),
            injected(persona),
            old_call("a", "gh api x"),
            old_result("a", 1),
            old_call("b", "gh api y"),
            old_result("b", 0),
            agent("Here is a long summary."),
            injected("[from: some.one] that's wrong, it was Tuesday"),
        ],
    )
    cand = run_select(world)["candidates"][0]
    assert cand["where"] == "dm-someone"
    assert cand["signals"] == {"correction": 1, "recovered_error": 1}
    assert cand["speakers"] == ["some.one"]
    assert "You are an assistant" not in cand["excerpts"][0]["text"]


def test_glob_paths_are_not_skills(world):
    add_thread(
        world,
        "t1",
        world["ws"] / "slack/dm/U1",
        [
            call(
                "c",
                "cat ~/Soul/skills/{a,b}/SKILL.md ~/Soul/skills/*/SKILL.md",
            ),
            result("c", 0),
        ],
    )
    assert run_select(world)["skill_usage"]["sessions_reading"] == {}


def claude_record(role: str, content, cwd: Path, sidechain: bool = False) -> dict:
    return {
        "type": role,
        "cwd": str(cwd),
        "sessionId": "s",
        "isSidechain": sidechain,
        "message": {"role": role, "content": content},
    }


def add_claude_session(world: dict, name: str, cwd: Path, records: list) -> Path:
    folder = world["claude"] / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.jsonl"
    path.write_text(
        "\n".join(json.dumps(claude_record(*r, cwd=cwd)) for r in records)
        + "\nnot json\n"
    )
    return path


def tool_use(use_id: str, name: str, **args) -> dict:
    return {"type": "tool_use", "id": use_id, "name": name, "input": args}


def tool_result(use_id: str, text: str, error: bool = False) -> dict:
    return {
        "type": "tool_result",
        "tool_use_id": use_id,
        "content": text,
        "is_error": error,
    }


def test_claude_session_is_read_like_a_codex_one(world):
    write_skill(world["soul"], "skills/media/skills/agent-vision")
    local = f"{world['soul']}/skills/notes/SKILL.md"
    cwd = world["ws"] / "slack/dm/U9"
    add_claude_session(
        world,
        "s1",
        cwd,
        [
            ("user", '[from-id: U9] [display: "p"] fetch the board'),
            ("assistant", [tool_use("a", "Skill", skill="media:agent-vision")]),
            ("user", [tool_result("a", "Launching skill")]),
            ("assistant", [tool_use("b", "Read", file_path=local)]),
            ("assistant", [tool_use("c", "Edit", file_path=local)]),
            ("assistant", [tool_use("d", "Bash", command="gh api x --jq .y")]),
            ("user", [tool_result("d", "Exit code 1\nboom", error=True)]),
            ("assistant", [tool_use("e", "Bash", command="gh api x")]),
            ("user", [tool_result("e", "ok")]),
            ("assistant", [{"type": "text", "text": "Here it is."}]),
            (
                "user",
                [
                    {
                        "type": "text",
                        "text": "[from-id: U9] that's wrong, use the other board",
                    }
                ],
            ),
        ],
    )
    out = run_select(world)
    assert out["counts"]["sessions_by_backend"] == {"claude": 1}
    cand = out["candidates"][0]
    assert (cand["backend"], cand["where"], cand["thread_id"]) == (
        "claude",
        "dm/U9",
        "s1",
    )
    assert cand["signals"] == {"correction": 1, "recovered_error": 1}
    assert cand["skills_read"] == [
        {"skill": "media:agent-vision", "owner": "local", "reads": 1},
        {"skill": "notes", "owner": "local", "reads": 1},
    ]
    assert cand["skills_edited"] == [{"skill": "notes", "owner": "local", "edits": 1}]
    text = "\n".join(e["text"] for e in cand["excerpts"])
    assert "[tool exit=1] gh api x --jq .y" in text and "[agent] Here it is." in text


def test_claude_sidechains_other_dirs_and_old_files_are_skipped(world):
    cwd = world["ws"] / "slack/dm/U9"
    sidechain = add_claude_session(world, "side", cwd, [])
    sidechain.write_text(
        json.dumps(
            claude_record("user", "[from-id: U9] that's wrong", cwd, sidechain=True)
        )
        + "\n"
    )
    add_claude_session(world, "elsewhere", world["tmp"] / "elsewhere", [("user", "hi")])
    old = add_claude_session(world, "old", cwd, [("user", "[from-id: U9] hi")])
    os.utime(old, (0, 0))
    nested = old.parent / "s1" / "subagents"
    nested.mkdir(parents=True)
    (nested / "agent.jsonl").write_text("{}\n")
    out = run_select(world)
    assert (
        out["counts"]["sessions_scanned"] == 1
    )  # only "side", and it carries no signal
    assert out["candidates"] == []


def test_claude_session_without_a_cwd_is_skipped(world):
    folder = add_claude_session(world, "x", world["ws"] / "slack/dm/U9", []).parent
    (folder / "x.jsonl").write_text('{"type": "summary"}\n')
    assert run_select(world)["counts"]["sessions_scanned"] == 0


@pytest.mark.parametrize(
    "block,code",
    [
        (tool_result("a", "Exit code 2\nno such file", error=True), 2),
        (tool_result("a", "permission denied", error=True), 1),
        (tool_result("a", "fine"), 0),
        ({"tool_use_id": "a", "content": [{"type": "text", "text": "Exit code 7"}]}, 7),
    ],
)
def test_claude_exit_codes(block, code):
    assert sel.claude_exit_code(block) == code


def test_malformed_records_are_skipped(world):
    rollout_noise = [
        {"type": "session_meta", "payload": {}},
        {"type": "response_item", "payload": {"type": "message", "role": "developer"}},
        {
            "type": "response_item",
            "payload": {"type": "function_call", "arguments": "{"},
        },
        {
            "type": "response_item",
            "payload": {"type": "function_call", "arguments": "{}"},
        },
        {"type": "response_item", "payload": {"type": "web_search_call"}},
        {  # `\'` is a JS escape JSON does not know
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "j",
                "input": 'tools.exec_command({cmd:"echo it\\\'s"})',
            },
        },
        result("j", 1),
        call("k", "gh api x"),
        result("k", 0),
    ]
    add_thread(world, "t1", world["ws"] / "cron/job", rollout_noise)
    rollout = world["tmp"] / "t1.jsonl"
    rollout.write_text("not json\n" + rollout.read_text())
    cwd = world["ws"] / "slack/dm/U9"
    path = add_claude_session(world, "c1", cwd, [("user", "[from-id: U9] hi")])
    path.write_text('[]\n{"type": "summary"}\n' + path.read_text())
    out = run_select(world)
    assert out["counts"]["sessions_scanned"] == 2
    assert out["counts"]["signals"] == {}


def test_long_sessions_stop_at_the_excerpt_budget(world):
    records = [user("hi"), agent("ok")]
    for i in range(30):
        records += [agent("x" * 300), user(f"that's wrong {i}"), *[agent("y")] * 9]
    add_thread(world, "t1", world["ws"] / "slack/dm/U1", records)
    cand = run_select(world)["candidates"][0]
    assert sum(len(e["text"]) for e in cand["excerpts"]) <= sel.EXCERPT_CHARS
    assert len(cand["excerpts"]) < 30


def test_job_that_never_failed_is_not_a_recovery(world):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 60))
    log = world["cotf"] / "logs" / "cron-ok-job-My_Host-2026-10-06.log"
    log.write_text(f"--- {stamp} reply (done) ---\nok\n")
    assert run_select(world)["cron_recoveries"] == []


def test_unreadable_transcript_is_counted(world):
    add_thread(world, "t1", world["ws"] / "slack/dm/U1", [user("hi")])
    (world["tmp"] / "t1.jsonl").unlink()
    assert run_select(world)["counts"]["unreadable_transcripts"] == 1


def test_no_codex_index_still_reads_claude(world):
    (world["codex"] / "state_5.sqlite").unlink()
    add_claude_session(
        world, "c1", world["ws"] / "slack/dm/U9", [("user", "[from-id: U9] hi")]
    )
    assert run_select(world)["counts"]["sessions_by_backend"] == {"claude": 1}


def test_short_paths_and_foreign_cwds():
    assert sel.skill_name("/a/SKILL.md", CONFIG) == "a"
    assert sel.classify_cwd("/elsewhere/slack/dm/U1", Path("/w")) is None


def test_bare_skill_names_resolve_to_the_inventory():
    inventory = {"content:read-rss-feed": "local", "a:dup": "local", "b:dup": "local"}
    assert sel.inventory_name("read-rss-feed", inventory) == "content:read-rss-feed"
    assert (
        sel.inventory_name("content:read-rss-feed", inventory)
        == "content:read-rss-feed"
    )
    assert sel.inventory_name("dup", inventory) == "dup"  # ambiguous: left as called
