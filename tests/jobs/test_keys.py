"""safe_segment: one filename component out of an arbitrary producer key."""

from __future__ import annotations

from claude_on_the_fly.jobs.keys import (
    MAX_SEGMENT,
    filename_glob,
    job_id_from_filename,
    queue_filename,
    safe_segment,
    split_key,
)


def test_tracker_shaped_keys_stay_readable() -> None:
    """These are the keys that actually occur, so they should survive legibly
    rather than turning into hashes nobody can grep for."""
    assert safe_segment("ACE-1234") == "ACE-1234"
    assert safe_segment("jira/ACE-1234") == "jira_ACE-1234"
    assert safe_segment("owner/repo#42") == "owner_repo_42"


def test_dots_are_stripped() -> None:
    """Load-bearing, not cosmetic: `done/` tells a job file from a result file by
    suffix, so a key carrying dots could mint an id whose `<id>.json` is
    indistinguishable from some other id's `<id>.result.json`."""
    assert "." not in safe_segment("v1.2.3")
    assert safe_segment("a.result.json") == "a_result_json"


def test_path_traversal_cannot_escape_the_segment() -> None:
    segment = safe_segment("../../etc/passwd")
    assert "/" not in segment
    assert ".." not in segment


def test_long_keys_are_truncated_deterministically() -> None:
    key = "x" * 500
    first = safe_segment(key)
    assert first == safe_segment(key), "dedup and resume both need this stable"
    assert len(first) <= MAX_SEGMENT


def test_keys_sharing_a_prefix_do_not_collide_after_truncation() -> None:
    """Truncation alone would map every key with the same long prefix onto one
    segment, which would silently merge two tickets into one dedup slot."""
    a = safe_segment("y" * 400 + "-alpha")
    b = safe_segment("y" * 400 + "-beta")
    assert a != b


def test_empty_key_stays_empty() -> None:
    assert safe_segment("") == ""


# --- queue filenames -------------------------------------------------------


def test_sanitizing_never_produces_a_double_underscore() -> None:
    """Load-bearing: `__` is the separator `queue_filename` joins on, so a
    sanitized part containing one would make the name ambiguous."""
    for value in ("a/_b", "a//b", "a___b", "x?!/y", "_", "___", "a/./b"):
        assert "__" not in safe_segment(value), value


def test_unkeyed_jobs_keep_the_bare_filename() -> None:
    assert queue_filename("100-abc", None) == "100-abc.json"


def test_keyed_filename_carries_entry_and_item() -> None:
    assert queue_filename("100-abc", "jira/ACE-1234") == "100-abc__jira__ACE-1234.json"


def test_key_with_no_slash_is_all_entry() -> None:
    """A plain scheduled prompt has no item — the entry IS the unit of work."""
    assert split_key("digest") == ("digest", "")
    assert queue_filename("100-abc", "digest") == "100-abc__digest__.json"


def test_filename_splits_back_into_three_parts() -> None:
    """Whatever hyphens and single underscores the entry and item contain."""
    name = queue_filename("100-abc", "my_entry/owner/repo#42")
    parts = name[: -len(".json")].split("__")
    assert parts == ["100-abc", "my_entry", "owner_repo_42"]


def test_job_id_recovered_from_either_filename_shape() -> None:
    assert job_id_from_filename("100-abc.json") == "100-abc"
    assert job_id_from_filename("100-abc__jira__ACE-1.json") == "100-abc"
    assert job_id_from_filename("100-abc__jira__ACE-1") == "100-abc"


def test_entry_glob_does_not_match_a_similarly_named_entry() -> None:
    """The reason unsafe runs collapse: without it, `jira_extra` sanitized to
    `jira__extra` and an entry glob for `jira` would have swept it up."""
    import fnmatch

    jira = queue_filename("1-a", "jira/ACE-1")
    jira_extra = queue_filename("1-b", "jira_extra/ACE-1")

    assert fnmatch.fnmatch(jira, filename_glob("jira"))
    assert not fnmatch.fnmatch(jira_extra, filename_glob("jira"))
    assert fnmatch.fnmatch(jira_extra, filename_glob("jira_extra"))


def test_item_glob_matches_only_that_item() -> None:
    import fnmatch

    one = queue_filename("1-a", "jira/ACE-1")
    two = queue_filename("1-b", "jira/ACE-2")

    assert fnmatch.fnmatch(one, filename_glob("jira", "ACE-1"))
    assert not fnmatch.fnmatch(two, filename_glob("jira", "ACE-1"))
