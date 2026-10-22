"""Tests for mail_poll.py, with a fake `gws` executable on PATH."""

from __future__ import annotations

import importlib.util
import json
import stat
import sys
import textwrap
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "mail_poll", Path(__file__).with_name("mail_poll.py")
)
poll = importlib.util.module_from_spec(SPEC)
sys.modules["mail_poll"] = poll
SPEC.loader.exec_module(poll)

# Answers `gws gmail users messages list|get|modify` from a JSON mailbox file. It
# prints a log line before the JSON, as the real CLI sometimes does.
FAKE_GWS = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    db_path = os.environ["FAKE_GWS_DB"]
    db = json.load(open(db_path))
    verb = sys.argv[4]
    params = json.loads(sys.argv[sys.argv.index("--params") + 1])
    if db.get("fail") == verb or (db.get("fail_id") and db["fail_id"] == params.get("id")):
        print("boom", file=sys.stderr)
        sys.exit(2)
    print("Using keyring backend")
    if verb == "list":
        ids = [m for m in db["messages"] if m in db["unread"]][: params["maxResults"]]
        print(json.dumps({"messages": [{"id": i} for i in ids]} if ids else {}))
    elif verb == "get":
        m = db["messages"][params["id"]]
        headers = [{"name": k, "value": v} for k, v in m.items() if k != "snippet"]
        print(json.dumps({"payload": {"headers": headers}, "snippet": m.get("snippet", "")}))
    elif verb == "modify":
        db["unread"].remove(params["id"])
        json.dump(db, open(db_path, "w"))
        print("{}")
    """
)


@pytest.fixture
def mailbox(tmp_path: Path, monkeypatch) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "gws"
    fake.write_text(FAKE_GWS)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    db = {
        "messages": {
            "m1": {
                "From": "Boss <Boss@Example.com>",
                "Subject": "Invoice",
                "Date": "d1",
                "snippet": "pay",
            },
            "m2": {"From": "spam@elsewhere.test", "Subject": "Win", "Date": "d2"},
            "m3": {"From": "boss@example.com", "Subject": "", "Date": "d3"},
        },
        "unread": ["m1", "m2", "m3"],
    }
    path = tmp_path / "db.json"
    path.write_text(json.dumps(db))
    monkeypatch.setenv("FAKE_GWS_DB", str(path))
    return {"path": path, "state": tmp_path / "state" / "mail.json"}


def db(mailbox: dict) -> dict:
    return json.loads(mailbox["path"].read_text())


def set_db(mailbox: dict, **changes) -> None:
    mailbox["path"].write_text(json.dumps({**db(mailbox), **changes}))


def run(mailbox: dict, *extra: str) -> int:
    return poll.main(
        ["--senders", "boss@example.com", "--state", str(mailbox["state"]), *extra]
    )


def printed(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_poll_prints_allowed_mail_once_and_marks_it_read(mailbox, capsys):
    assert run(mailbox) == 0
    items = printed(capsys)
    assert [i["key"] for i in items] == ["m1", "m3"]
    assert items[0] == {
        "key": "m1",
        "from": "boss@example.com",
        "subject": "Invoice",
        "date": "d1",
        "snippet": "pay",
    }
    assert items[1]["subject"] == "(no subject)"
    assert db(mailbox)["unread"] == ["m2"]  # the stranger's mail is left alone
    assert run(mailbox) == 0 and printed(capsys) == []


def test_state_stops_a_repeat_even_when_mail_is_unread_again(mailbox, capsys):
    run(mailbox)
    capsys.readouterr()
    set_db(mailbox, unread=["m1", "m2", "m3"])
    assert run(mailbox) == 0 and printed(capsys) == []


def test_dry_run_marks_nothing_and_saves_nothing(mailbox, capsys):
    assert run(mailbox, "--dry-run") == 0
    assert [i["key"] for i in printed(capsys)] == ["m1", "m3"]
    assert db(mailbox)["unread"] == ["m1", "m2", "m3"]
    assert not mailbox["state"].exists()


def test_seed_records_unread_mail_without_printing(mailbox, capsys):
    assert run(mailbox, "--seed") == 0
    assert capsys.readouterr().out == ""
    assert run(mailbox) == 0 and printed(capsys) == []


def test_a_failing_message_is_skipped_and_retried(mailbox, capsys):
    set_db(mailbox, fail_id="m1")
    assert run(mailbox) == 0
    out = capsys.readouterr()
    assert [json.loads(line)["key"] for line in out.out.splitlines()] == ["m3"]
    assert "skipped m1, retrying next poll" in out.err
    set_db(mailbox, fail_id=None)
    assert run(mailbox) == 0 and [i["key"] for i in printed(capsys)] == ["m1"]


def test_a_failing_listing_exits_1(mailbox, capsys):
    set_db(mailbox, fail="list")
    assert run(mailbox) == 1
    assert "mail_poll:" in capsys.readouterr().err


def test_an_unsaved_state_still_prints(mailbox, capsys, monkeypatch):
    def refuse(*_):
        raise OSError("disk full")

    monkeypatch.setattr(poll, "save_state", refuse)
    assert run(mailbox) == 0
    out = capsys.readouterr()
    assert len(out.out.splitlines()) == 2 and "could not save" in out.err


def test_state_keeps_only_the_newest_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(poll, "KEEP_IDS", 2)
    path = tmp_path / "s.json"
    poll.save_state(path, {"a": "2026-01-01", "b": "2026-01-03", "c": "2026-01-02"})
    assert json.loads(path.read_text())["processed"] == {
        "c": "2026-01-02",
        "b": "2026-01-03",
    }


def test_missing_gws_and_bad_output(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(RuntimeError, match="gws not found"):
        poll.gws("users")
    fake = tmp_path / "gws"
    fake.write_text("#!/bin/sh\necho no json here\n")
    fake.chmod(0o755)
    with pytest.raises(RuntimeError, match="printed no JSON"):
        poll.gws("users")


@pytest.mark.parametrize(
    "address,hit",
    [
        ("boss@example.com", True),
        ("anyone@corp.test", True),
        ("x@sub.corp.test", False),
        ("x@corp.test.evil", False),
        ("corp.test", False),
        ("", False),
    ],
)
def test_allowed_takes_addresses_and_domains(address, hit):
    assert poll.allowed(address, {"boss@example.com", "@corp.test"}) is hit


def test_a_domain_lets_every_address_there_through(mailbox, capsys):
    assert (
        poll.main(["--senders", "@example.com", "--state", str(mailbox["state"])]) == 0
    )
    assert [i["key"] for i in printed(capsys)] == ["m1", "m3"]
