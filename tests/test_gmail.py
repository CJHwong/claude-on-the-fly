"""Tests for claude_on_the_fly.gmail module."""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch


from claude_on_the_fly.agent import Response
from claude_on_the_fly.gmail import (
    GmailFrontend,
    _extract_header,
    _extract_plain_body,
    _parse_sender,
    _strip_quoted,
    _thread_key,
)


# ---------------------------------------------------------------------------
# _thread_key
# ---------------------------------------------------------------------------


class TestThreadKey:
    def test_deterministic(self):
        assert _thread_key("abc123") == _thread_key("abc123")

    def test_different_ids_differ(self):
        assert _thread_key("thread_a") != _thread_key("thread_b")

    def test_returns_int(self):
        assert isinstance(_thread_key("xyz"), int)


# ---------------------------------------------------------------------------
# _extract_header
# ---------------------------------------------------------------------------


class TestExtractHeader:
    def test_finds_header_by_name(self):
        headers = [{"name": "Subject", "value": "Hello"}]
        assert _extract_header(headers, "Subject") == "Hello"

    def test_case_insensitive(self):
        headers = [{"name": "FROM", "value": "alice@x.com"}]
        assert _extract_header(headers, "from") == "alice@x.com"

    def test_not_found_returns_empty(self):
        headers = [{"name": "Subject", "value": "Hi"}]
        assert _extract_header(headers, "X-Custom") == ""

    def test_empty_headers_returns_empty(self):
        assert _extract_header([], "Subject") == ""

    def test_missing_value_returns_empty(self):
        headers = [{"name": "Subject"}]
        assert _extract_header(headers, "Subject") == ""


# ---------------------------------------------------------------------------
# _parse_sender
# ---------------------------------------------------------------------------


class TestParseSender:
    def test_name_and_email(self):
        assert _parse_sender("Alice <alice@x.com>") == ("Alice", "alice@x.com")

    def test_quoted_name(self):
        assert _parse_sender('"Alice Bob" <alice@x.com>') == (
            "Alice Bob",
            "alice@x.com",
        )

    def test_empty_name_falls_back_to_email(self):
        assert _parse_sender("<alice@x.com>") == ("alice@x.com", "alice@x.com")

    def test_no_angle_brackets(self):
        assert _parse_sender("alice@x.com") == ("alice@x.com", "alice@x.com")


# ---------------------------------------------------------------------------
# _strip_quoted
# ---------------------------------------------------------------------------


class TestStripQuoted:
    def test_strips_english_on_wrote(self):
        text = "Hey there\n\nOn Mon, Jan 1, 2024, Alice wrote:\n> old text"
        assert _strip_quoted(text) == "Hey there"

    def test_strips_localized_date_colon_pattern(self):
        # Regex requires at least one char before \d{4} (e.g. weekday or punctuation)
        text = "Reply here\n\n於 2024年1月1日 Alice：\n> quoted stuff\n> more"
        assert _strip_quoted(text) == "Reply here"

    def test_strips_trailing_quoted_blocks(self):
        text = "My reply\n\n> some quote\n> another quote"
        assert _strip_quoted(text) == "My reply"

    def test_no_quotes_returns_full_text(self):
        text = "Just a normal message with no quotes"
        assert _strip_quoted(text) == "Just a normal message with no quotes"

    def test_empty_lines_between_content_and_quotes(self):
        text = "Content\n\n\n> quoted\n> stuff"
        assert _strip_quoted(text) == "Content"

    def test_empty_string(self):
        assert _strip_quoted("") == ""


# ---------------------------------------------------------------------------
# _extract_plain_body
# ---------------------------------------------------------------------------


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


class TestExtractPlainBody:
    def test_top_level_text_plain(self):
        payload = {"mimeType": "text/plain", "body": {"data": _b64("hello")}}
        assert _extract_plain_body(payload) == "hello"

    def test_from_parts(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("plain part")}},
                {"mimeType": "text/html", "body": {"data": _b64("<b>html</b>")}},
            ],
        }
        assert _extract_plain_body(payload) == "plain part"

    def test_recursive_nested_parts(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"data": _b64("nested")},
                        },
                    ],
                },
            ],
        }
        assert _extract_plain_body(payload) == "nested"

    def test_no_text_plain_returns_empty(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html", "body": {"data": _b64("<b>hi</b>")}},
            ],
        }
        assert _extract_plain_body(payload) == ""

    def test_missing_body_data(self):
        payload = {"mimeType": "text/plain", "body": {}}
        assert _extract_plain_body(payload) == ""

    def test_missing_body_key(self):
        payload = {"mimeType": "text/plain"}
        assert _extract_plain_body(payload) == ""


# ---------------------------------------------------------------------------
# GmailFrontend properties
# ---------------------------------------------------------------------------


class TestGmailFrontendProperties:
    def _make_frontend(self) -> GmailFrontend:
        return GmailFrontend(gcp_project="proj", allowed_senders={"a@x.com"})

    def test_workspace_name(self):
        fe = self._make_frontend()
        chat_id = _thread_key("t1")
        fe._sender_emails[chat_id] = "alice@example.com"
        result = fe.workspace_name(chat_id)
        assert result.startswith("gmail/alice-")
        assert hex(chat_id)[-8:] in result

    def test_workspace_name_unknown_sender(self):
        fe = self._make_frontend()
        assert fe.workspace_name(999).startswith("gmail/unknown-")

    def test_sender_name_cached(self):
        fe = self._make_frontend()
        fe._sender_names_map[42] = "Bob"
        assert fe.sender_name(42) == "Bob"

    def test_sender_name_unknown(self):
        fe = self._make_frontend()
        assert fe.sender_name(42) == "unknown"

    def test_channel_context(self):
        fe = self._make_frontend()
        fe._subjects[1] = "Re: Hello"
        fe._sender_emails[1] = "alice@x.com"
        ctx = fe.channel_context(1)
        assert 'subject="Re: Hello"' in ctx
        assert "from=alice@x.com" in ctx


# ---------------------------------------------------------------------------
# _handle_message
# ---------------------------------------------------------------------------


def _make_msg(
    *,
    from_addr: str = "Alice <alice@x.com>",
    subject: str = "Hello",
    body_text: str = "email body",
    labels: list[str] | None = None,
    thread_id: str = "t1",
    msg_id: str = "m1",
    extra_headers: list[dict] | None = None,
) -> dict:
    headers = [
        {"name": "From", "value": from_addr},
        {"name": "Subject", "value": subject},
    ]
    if extra_headers:
        headers.extend(extra_headers)
    return {
        "id": msg_id,
        "threadId": thread_id,
        "labelIds": labels or ["INBOX", "UNREAD"],
        "payload": {
            "mimeType": "text/plain",
            "headers": headers,
            "body": {"data": _b64(body_text)},
        },
    }


class TestHandleMessage:
    def _make_frontend(self) -> GmailFrontend:
        fe = GmailFrontend(
            gcp_project="proj",
            allowed_senders={"alice@x.com"},
        )
        fe._on_message = AsyncMock()  # type: ignore[assignment]
        return fe

    async def test_ignores_sent_messages(self):
        fe = self._make_frontend()
        msg = _make_msg(labels=["SENT", "INBOX"])
        await fe._handle_message(msg)
        fe._on_message.assert_not_awaited()  # type: ignore[union-attr]

    async def test_ignores_auto_submitted(self):
        fe = self._make_frontend()
        msg = _make_msg(
            extra_headers=[{"name": "Auto-Submitted", "value": "auto-replied"}],
        )
        await fe._handle_message(msg)
        fe._on_message.assert_not_awaited()  # type: ignore[union-attr]

    async def test_auto_submitted_no_is_allowed(self):
        fe = self._make_frontend()
        msg = _make_msg(
            extra_headers=[{"name": "Auto-Submitted", "value": "no"}],
        )
        await fe._handle_message(msg)
        fe._on_message.assert_awaited_once()  # type: ignore[union-attr]

    async def test_ignores_sender_not_in_allowlist(self):
        fe = self._make_frontend()
        msg = _make_msg(from_addr="Eve <eve@evil.com>")
        await fe._handle_message(msg)
        fe._on_message.assert_not_awaited()  # type: ignore[union-attr]

    async def test_allowlist_case_insensitive(self):
        fe = self._make_frontend()
        msg = _make_msg(from_addr="Alice <ALICE@X.COM>")
        await fe._handle_message(msg)
        fe._on_message.assert_awaited_once()  # type: ignore[union-attr]

    async def test_domain_wildcard_accepts_matching_domain(self):
        fe = GmailFrontend(
            gcp_project="proj",
            allowed_senders={"*@gofreight.com"},
        )
        fe._on_message = AsyncMock()  # type: ignore[assignment]
        msg = _make_msg(from_addr="Bob <bob@gofreight.com>")
        await fe._handle_message(msg)
        fe._on_message.assert_awaited_once()  # type: ignore[union-attr]

    async def test_domain_wildcard_rejects_other_domain(self):
        fe = GmailFrontend(
            gcp_project="proj",
            allowed_senders={"*@gofreight.com"},
        )
        fe._on_message = AsyncMock()  # type: ignore[assignment]
        msg = _make_msg(from_addr="Eve <eve@evil.com>")
        await fe._handle_message(msg)
        fe._on_message.assert_not_awaited()  # type: ignore[union-attr]

    async def test_domain_wildcard_case_insensitive(self):
        fe = GmailFrontend(
            gcp_project="proj",
            allowed_senders={"*@GoFreight.COM"},
        )
        fe._on_message = AsyncMock()  # type: ignore[assignment]
        msg = _make_msg(from_addr="Bob <BOB@gofreight.com>")
        await fe._handle_message(msg)
        fe._on_message.assert_awaited_once()  # type: ignore[union-attr]

    async def test_star_accepts_anyone(self):
        fe = GmailFrontend(
            gcp_project="proj",
            allowed_senders={"*"},
        )
        fe._on_message = AsyncMock()  # type: ignore[assignment]
        msg = _make_msg(from_addr="Random <random@nowhere.net>")
        await fe._handle_message(msg)
        fe._on_message.assert_awaited_once()  # type: ignore[union-attr]

    async def test_mixed_entries_route_each_correctly(self):
        fe = GmailFrontend(
            gcp_project="proj",
            allowed_senders={"alice@x.com", "*@gofreight.com"},
        )
        fe._on_message = AsyncMock()  # type: ignore[assignment]

        # Exact email match.
        await fe._handle_message(_make_msg(from_addr="Alice <alice@x.com>"))
        # Domain match.
        await fe._handle_message(
            _make_msg(thread_id="t2", msg_id="m2", from_addr="Bob <bob@gofreight.com>")
        )
        # Neither.
        await fe._handle_message(
            _make_msg(thread_id="t3", msg_id="m3", from_addr="Eve <eve@evil.com>")
        )

        assert fe._on_message.await_count == 2  # type: ignore[union-attr]

    async def test_ignores_empty_body(self):
        fe = self._make_frontend()
        msg = _make_msg(body_text="   \n  ")
        await fe._handle_message(msg)
        fe._on_message.assert_not_awaited()  # type: ignore[union-attr]

    async def test_processes_valid_email(self):
        fe = self._make_frontend()
        msg = _make_msg(thread_id="t99", msg_id="m99", subject="Test")
        await fe._handle_message(msg)

        chat_id = _thread_key("t99")
        assert fe._sessions[chat_id] == "m99"
        assert fe._sender_names_map[chat_id] == "Alice"
        assert fe._sender_emails[chat_id] == "alice@x.com"
        assert fe._subjects[chat_id] == "Test"
        fe._on_message.assert_awaited_once()  # type: ignore[union-attr]

    async def test_text_format_with_subject(self):
        fe = self._make_frontend()
        msg = _make_msg(subject="Important", body_text="content here")
        await fe._handle_message(msg)

        call_args = fe._on_message.call_args  # type: ignore[union-attr]
        text = call_args[0][1]
        assert "Subject: Important" in text
        assert "content here" in text

    async def test_text_format_without_subject(self):
        fe = self._make_frontend()
        msg = _make_msg(subject="", body_text="just body")
        await fe._handle_message(msg)

        call_args = fe._on_message.call_args  # type: ignore[union-attr]
        text = call_args[0][1]
        assert "Subject:" not in text
        assert "just body" in text

    async def test_on_message_not_set(self):
        fe = self._make_frontend()
        fe._on_message = None
        msg = _make_msg()
        # Should not raise
        await fe._handle_message(msg)


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


class TestSend:
    def _make_frontend(self) -> GmailFrontend:
        return GmailFrontend(gcp_project="proj", allowed_senders={"a@x.com"})

    async def test_noop_when_no_session(self):
        fe = self._make_frontend()
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            await fe.send(999, Response(body="hi"))
            mock_exec.assert_not_called()

    async def test_calls_gws_reply(self):
        fe = self._make_frontend()
        fe._sessions[1] = "msg_abc"

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        with patch(
            "asyncio.create_subprocess_exec", return_value=mock_proc
        ) as mock_exec:
            await fe.send(1, Response(body="Reply text"))

            mock_exec.assert_called_once()
            call_args = mock_exec.call_args[0]
            assert call_args[0] == "gws"
            assert "gmail" in call_args
            assert "+reply" in call_args
            assert "--message-id" in call_args
            idx = list(call_args).index("--message-id")
            assert call_args[idx + 1] == "msg_abc"
            idx = list(call_args).index("--body")
            assert call_args[idx + 1] == "Reply text"

    async def test_appends_stats_when_present(self):
        fe = self._make_frontend()
        fe._sessions[1] = "msg_abc"

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        resp = Response(body="Answer", cost=0.05, model="opus")

        with patch(
            "asyncio.create_subprocess_exec", return_value=mock_proc
        ) as mock_exec:
            await fe.send(1, resp)

            call_args = mock_exec.call_args[0]
            idx = list(call_args).index("--body")
            body_sent = call_args[idx + 1]
            assert "Answer" in body_sent
            assert "---" in body_sent
            assert "$0.0500" in body_sent

    async def test_no_stats_when_absent(self):
        fe = self._make_frontend()
        fe._sessions[1] = "msg_abc"

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        resp = Response(body="Plain reply")

        with patch(
            "asyncio.create_subprocess_exec", return_value=mock_proc
        ) as mock_exec:
            await fe.send(1, resp)

            call_args = mock_exec.call_args[0]
            idx = list(call_args).index("--body")
            assert call_args[idx + 1] == "Plain reply"

    async def test_send_logs_error_on_failure(self):
        fe = self._make_frontend()
        fe._sessions[1] = "msg_abc"

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"oops")
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await fe.send(1, Response(body="hi"))
            # No exception raised; error is logged


# ---------------------------------------------------------------------------
# send_typing
# ---------------------------------------------------------------------------


class TestSendTyping:
    async def test_returns_none(self):
        fe = GmailFrontend(gcp_project="proj", allowed_senders={"a@x.com"})
        result = await fe.send_typing(123)
        assert result is None


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


class TestStart:
    async def test_start_calls_sweep_and_creates_watch_task(self):
        fe = GmailFrontend(gcp_project="proj", allowed_senders={"a@x.com"})
        on_message = AsyncMock()

        with (
            patch.object(fe, "_sweep_unread", new_callable=AsyncMock) as mock_sweep,
            patch("asyncio.create_task") as mock_create_task,
        ):
            await fe.start(on_message)

            assert fe._on_message is on_message
            mock_sweep.assert_awaited_once()
            mock_create_task.assert_called_once()
            assert fe._watch_task is mock_create_task.return_value


# ---------------------------------------------------------------------------
# _sweep_unread
# ---------------------------------------------------------------------------


class TestSweepUnread:
    async def test_sweep_no_unread(self):
        fe = GmailFrontend(gcp_project="proj", allowed_senders={"a@x.com"})
        with patch.object(
            fe, "_list_unread_ids", new_callable=AsyncMock, return_value=[]
        ):
            await fe._sweep_unread()
            # No further calls expected

    async def test_sweep_fetches_and_handles(self):
        fe = GmailFrontend(gcp_project="proj", allowed_senders={"a@x.com"})
        fake_msg = _make_msg(msg_id="m1")

        with (
            patch.object(
                fe,
                "_list_unread_ids",
                new_callable=AsyncMock,
                return_value=["m1", "m2"],
            ),
            patch.object(
                fe, "_fetch_message", new_callable=AsyncMock, return_value=fake_msg
            ),
            patch.object(fe, "_handle_message", new_callable=AsyncMock) as mock_handle,
        ):
            await fe._sweep_unread()

            assert mock_handle.await_count == 2

    async def test_sweep_skips_fetch_exception(self):
        fe = GmailFrontend(gcp_project="proj", allowed_senders={"a@x.com"})

        async def fetch_side_effect(mid):
            if mid == "m1":
                raise RuntimeError("boom")
            return _make_msg(msg_id=mid)

        with (
            patch.object(
                fe,
                "_list_unread_ids",
                new_callable=AsyncMock,
                return_value=["m1", "m2"],
            ),
            patch.object(
                fe,
                "_fetch_message",
                new_callable=AsyncMock,
                side_effect=fetch_side_effect,
            ),
            patch.object(fe, "_handle_message", new_callable=AsyncMock) as mock_handle,
        ):
            await fe._sweep_unread()
            # m1 raises, m2 succeeds -> only m2 handled
            assert mock_handle.await_count == 1

    async def test_sweep_skips_none_fetch(self):
        fe = GmailFrontend(gcp_project="proj", allowed_senders={"a@x.com"})

        with (
            patch.object(
                fe, "_list_unread_ids", new_callable=AsyncMock, return_value=["m1"]
            ),
            patch.object(
                fe, "_fetch_message", new_callable=AsyncMock, return_value=None
            ),
            patch.object(fe, "_handle_message", new_callable=AsyncMock) as mock_handle,
        ):
            await fe._sweep_unread()
            mock_handle.assert_not_awaited()

    async def test_sweep_handle_exception_continues(self):
        fe = GmailFrontend(gcp_project="proj", allowed_senders={"a@x.com"})

        with (
            patch.object(
                fe, "_list_unread_ids", new_callable=AsyncMock, return_value=["m1"]
            ),
            patch.object(
                fe, "_fetch_message", new_callable=AsyncMock, return_value=_make_msg()
            ),
            patch.object(
                fe,
                "_handle_message",
                new_callable=AsyncMock,
                side_effect=RuntimeError("bad"),
            ),
        ):
            # Should not raise
            await fe._sweep_unread()


# ---------------------------------------------------------------------------
# _list_unread_ids
# ---------------------------------------------------------------------------


class TestListUnreadIds:
    async def test_success_returns_ids(self):
        fe = GmailFrontend(gcp_project="proj", allowed_senders={"a@x.com"})
        stdout = json.dumps({"messages": [{"id": "a1"}, {"id": "a2"}]}).encode()

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (stdout, b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await fe._list_unread_ids()
            assert result == ["a1", "a2"]

    async def test_success_no_messages_key(self):
        fe = GmailFrontend(gcp_project="proj", allowed_senders={"a@x.com"})
        stdout = json.dumps({}).encode()

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (stdout, b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await fe._list_unread_ids()
            assert result == []

    async def test_failure_returns_empty(self):
        fe = GmailFrontend(gcp_project="proj", allowed_senders={"a@x.com"})

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"error stuff")
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await fe._list_unread_ids()
            assert result == []


# ---------------------------------------------------------------------------
# _fetch_message
# ---------------------------------------------------------------------------


class TestFetchMessage:
    async def test_success_returns_dict(self):
        fe = GmailFrontend(gcp_project="proj", allowed_senders={"a@x.com"})
        payload = {"id": "m1", "threadId": "t1", "payload": {}}
        stdout = json.dumps(payload).encode()

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (stdout, b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await fe._fetch_message("m1")
            assert result == payload

    async def test_failure_returns_none(self):
        fe = GmailFrontend(gcp_project="proj", allowed_senders={"a@x.com"})

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"not found")
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await fe._fetch_message("m1")
            assert result is None


# ---------------------------------------------------------------------------
# _read_stream
# ---------------------------------------------------------------------------


class TestReadStream:
    async def test_processes_json_lines(self):
        fe = GmailFrontend(gcp_project="proj", allowed_senders={"a@x.com"})

        msg1 = _make_msg(msg_id="m1", thread_id="t1")
        msg2 = _make_msg(msg_id="m2", thread_id="t2")

        lines = [
            json.dumps(msg1).encode() + b"\n",
            json.dumps(msg2).encode() + b"\n",
            b"",  # EOF
        ]
        line_iter = iter(lines)

        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(side_effect=lambda: next(line_iter))

        mock_proc = MagicMock()
        mock_proc.stdout = mock_stdout
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.returncode = 0

        fe._watch_proc = mock_proc

        with patch.object(fe, "_handle_message", new_callable=AsyncMock) as mock_handle:
            count = await fe._read_stream()
            assert count == 2
            assert mock_handle.await_count == 2

    async def test_skips_non_json_lines(self):
        fe = GmailFrontend(gcp_project="proj", allowed_senders={"a@x.com"})

        msg = _make_msg(msg_id="m1")
        lines = [
            b"not json at all\n",
            json.dumps(msg).encode() + b"\n",
            b"",
        ]
        line_iter = iter(lines)

        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(side_effect=lambda: next(line_iter))

        mock_proc = MagicMock()
        mock_proc.stdout = mock_stdout
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.returncode = 0

        fe._watch_proc = mock_proc

        with patch.object(fe, "_handle_message", new_callable=AsyncMock) as mock_handle:
            count = await fe._read_stream()
            assert count == 1
            mock_handle.assert_awaited_once()

    async def test_empty_stream(self):
        fe = GmailFrontend(gcp_project="proj", allowed_senders={"a@x.com"})

        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(return_value=b"")

        mock_proc = MagicMock()
        mock_proc.stdout = mock_stdout
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.returncode = 0

        fe._watch_proc = mock_proc

        with patch.object(fe, "_handle_message", new_callable=AsyncMock) as mock_handle:
            count = await fe._read_stream()
            assert count == 0
            mock_handle.assert_not_awaited()

    async def test_no_proc_returns_zero(self):
        fe = GmailFrontend(gcp_project="proj", allowed_senders={"a@x.com"})
        fe._watch_proc = None
        count = await fe._read_stream()
        assert count == 0

    async def test_logs_stderr_on_nonzero_exit(self):
        fe = GmailFrontend(gcp_project="proj", allowed_senders={"a@x.com"})

        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(return_value=b"")

        mock_stderr = AsyncMock()
        mock_stderr.read = AsyncMock(return_value=b"fatal error")

        mock_proc = MagicMock()
        mock_proc.stdout = mock_stdout
        mock_proc.stderr = mock_stderr
        mock_proc.wait = AsyncMock(return_value=1)
        mock_proc.returncode = 1

        fe._watch_proc = mock_proc

        count = await fe._read_stream()
        assert count == 0
        mock_stderr.read.assert_awaited_once()


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


class TestStop:
    async def test_stop_cancels_task_and_terminates_proc(self):
        fe = GmailFrontend(gcp_project="proj", allowed_senders={"a@x.com"})

        mock_task = MagicMock()
        mock_proc = MagicMock()
        mock_proc.wait = AsyncMock()

        fe._watch_task = mock_task
        fe._watch_proc = mock_proc

        await fe.stop()

        mock_task.cancel.assert_called_once()
        mock_proc.terminate.assert_called_once()
        mock_proc.wait.assert_awaited_once()

    async def test_stop_noop_when_nothing_running(self):
        fe = GmailFrontend(gcp_project="proj", allowed_senders={"a@x.com"})
        # Should not raise
        await fe.stop()

    async def test_stop_only_task(self):
        fe = GmailFrontend(gcp_project="proj", allowed_senders={"a@x.com"})
        mock_task = MagicMock()
        fe._watch_task = mock_task
        fe._watch_proc = None

        await fe.stop()
        mock_task.cancel.assert_called_once()
