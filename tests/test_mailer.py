"""
Integration tests for reporting.mailer.

Spins up a minimal asyncio SMTP listener on an ephemeral port using
the `asyncio.start_server` primitive, captures the received command
sequence + DATA body into a local list, and asserts that the
delivered MIME structure matches what the caller built. This proves
the aiosmtplib → real SMTP wire path works end-to-end without
needing an external mail provider.

Why not aiosmtpd? aiosmtpd's `Controller` uses a sentinel-connection
startup check (`_trigger_server`) that is flaky in containerized
pytest environments — the sentinel fires before the bound socket is
actually accepting, and the whole fixture errors out with
"Connection refused" during start(). UnthreadedController is an
option but it expects a non-running loop, which collides with
pytest-asyncio's managed loop. A hand-rolled SMTP echo server is
~50 lines, has no third-party test dependency, and is trivially
debuggable when the protocol trips.

TLS is deliberately NOT exercised in these tests — setting up a self-
signed CA in every test run would be expensive and fragile. The
STARTTLS / implicit-TLS code paths are trivial parameter flips in
send_message and are covered by type checking + the manual smoke
test documented in the Phase 6 PR body.
"""

from __future__ import annotations

import asyncio
import sys
from email.message import EmailMessage
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
from reporting import mailer  # noqa: E402


# ─── Minimal asyncio SMTP echo server ──────────────────────────────────────


class SmtpCatcher:
    """Collect received messages from the inline SMTP listener.

    Each captured entry is a dict with:
      mail_from   — the argument to MAIL FROM:<addr>
      rcpt_tos    — list of RCPT TO:<addr> arguments
      content     — raw bytes of the DATA body (headers + body, no
                    trailing CRLF.CRLF terminator)
      content_str — content decoded as UTF-8 (errors='replace')
    """

    def __init__(self) -> None:
        self.messages: list[dict] = []


async def _handle_smtp_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    catcher: SmtpCatcher,
) -> None:
    """Minimal ESMTP server implementation — just enough of the
    protocol to let aiosmtplib complete a send() call.

    Supported verbs: EHLO, HELO, MAIL FROM, RCPT TO, DATA,
    QUIT, NOOP, RSET. Anything else gets a 502 "command not
    recognized".
    """
    async def send(line: str) -> None:
        writer.write((line + "\r\n").encode("ascii"))
        await writer.drain()

    async def readline() -> str:
        raw = await reader.readline()
        return raw.decode("ascii", errors="replace").rstrip("\r\n")

    # Greeting
    await send("220 test.local ESMTP ready")

    mail_from: str | None = None
    rcpts: list[str] = []

    try:
        while True:
            line = await readline()
            if not line:
                break

            upper = line.upper()

            if upper.startswith("EHLO"):
                # Multi-line EHLO response advertising minimal
                # extensions. aiosmtplib wants at least SIZE or
                # STARTTLS; we advertise nothing to keep the
                # test server plain-text only.
                writer.write(b"250-test.local Hello\r\n")
                writer.write(b"250 HELP\r\n")
                await writer.drain()
            elif upper.startswith("HELO"):
                await send("250 test.local Hello")
            elif upper.startswith("MAIL FROM:"):
                # Extract the email address inside <...>
                addr = line.split(":", 1)[1].strip()
                if addr.startswith("<") and addr.endswith(">"):
                    addr = addr[1:-1]
                mail_from = addr
                await send("250 OK")
            elif upper.startswith("RCPT TO:"):
                addr = line.split(":", 1)[1].strip()
                if addr.startswith("<") and addr.endswith(">"):
                    addr = addr[1:-1]
                rcpts.append(addr)
                await send("250 OK")
            elif upper == "DATA":
                await send("354 End data with <CRLF>.<CRLF>")
                # Read lines until a line containing only "."
                data_buf = bytearray()
                while True:
                    raw = await reader.readline()
                    if not raw:
                        break
                    if raw == b".\r\n" or raw == b".\n":
                        break
                    # SMTP dot-stuffing: a leading ".." becomes "."
                    if raw.startswith(b".."):
                        data_buf.extend(raw[1:])
                    else:
                        data_buf.extend(raw)
                catcher.messages.append({
                    "mail_from": mail_from,
                    "rcpt_tos": list(rcpts),
                    "content": bytes(data_buf),
                    "content_str": bytes(data_buf).decode("utf-8", errors="replace"),
                })
                mail_from = None
                rcpts = []
                await send("250 OK: queued")
            elif upper == "RSET":
                mail_from = None
                rcpts = []
                await send("250 OK")
            elif upper == "NOOP":
                await send("250 OK")
            elif upper == "QUIT":
                await send("221 Bye")
                break
            else:
                await send("502 Command not recognized")
    except (ConnectionResetError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


# ─── Fixtures ──────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def smtp_server() -> AsyncIterator[tuple[str, int, SmtpCatcher]]:
    """Start the inline asyncio SMTP listener on an ephemeral port
    and yield (host, port, catcher). Stops on fixture teardown."""
    catcher = SmtpCatcher()
    server = await asyncio.start_server(
        lambda r, w: _handle_smtp_client(r, w, catcher),
        host="127.0.0.1",
        port=0,
    )
    sock = server.sockets[0]
    host, port = sock.getsockname()[:2]

    async def _serve() -> None:
        try:
            async with server:
                await server.serve_forever()
        except asyncio.CancelledError:
            pass

    task = asyncio.create_task(_serve())

    try:
        yield (host, port, catcher)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        server.close()
        await server.wait_closed()


# ─── Tests ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_minimal_test_message_reaches_server(smtp_server):
    """Building the test message with build_test_message() and sending
    it through send_message(..., tls='none') lands a message in the
    capture handler."""
    host, port, handler = smtp_server

    msg = mailer.build_test_message(
        from_addr="sender@test.local",
        to_addr="receiver@test.local",
    )
    await mailer.send_message(
        msg,
        host=host,
        port=port,
        user="",
        password="",
        tls="none",
    )

    assert len(handler.messages) == 1
    captured = handler.messages[0]
    # MAIL FROM / RCPT TO envelope reflects the From/To headers
    assert captured["mail_from"] == "sender@test.local"
    assert "receiver@test.local" in captured["rcpt_tos"]

    # Parse the captured bytes back into an EmailMessage using the
    # modern policy — it auto-decodes RFC 2047 headers (the em-dash
    # in the subject) and quoted-printable bodies (the line-wrapped
    # HTML). The legacy compat32 parser leaves headers raw, which
    # would make the em-dash assertion brittle.
    from email import message_from_bytes
    from email.policy import default as default_policy
    parsed = message_from_bytes(captured["content"], policy=default_policy)

    # Decoded subject — the em-dash is mandatory to prove the UTF-8
    # round-trip works end-to-end.
    assert parsed["Subject"] == "GPU Monitor — SMTP test"
    assert parsed["From"] == "sender@test.local"
    assert parsed["To"] == "receiver@test.local"

    # Multipart walk — plain + HTML alternatives
    parts = [p.get_content_type() for p in parsed.walk()]
    assert "text/plain" in parts
    assert "text/html" in parts

    # Decode each part's body to confirm the HTML alternative contains
    # the marker text we embedded.
    html_bodies = [
        p.get_content() for p in parsed.walk()
        if p.get_content_type() == "text/html"
    ]
    assert any("<strong>GPU Monitor</strong>" in body for body in html_bodies)


@pytest.mark.asyncio
async def test_send_empty_host_raises_mailer_error(smtp_server):
    """Empty host is a config error, not a silent no-op."""
    _host, _port, _handler = smtp_server  # use the fixture for parity

    msg = mailer.build_test_message("a@x", "b@x")
    with pytest.raises(mailer.MailerError, match="host is empty"):
        await mailer.send_message(
            msg, host="", port=25, user="", password="", tls="none"
        )


@pytest.mark.asyncio
async def test_send_invalid_tls_mode_raises_mailer_error(smtp_server):
    """Unsupported tls strings are caught before any network I/O."""
    host, port, _handler = smtp_server

    msg = mailer.build_test_message("a@x", "b@x")
    with pytest.raises(mailer.MailerError, match="unsupported tls mode"):
        await mailer.send_message(
            msg, host=host, port=port, user="", password="",
            tls="SSL-v3-please",
        )


@pytest.mark.asyncio
async def test_send_connection_refused_raises_mailer_error():
    """A bogus port that nothing is listening on surfaces as a
    MailerError with 'connection failed' in the message."""
    msg = mailer.build_test_message("a@x", "b@x")
    # Port 1 is reserved and will refuse connection on any POSIX box.
    with pytest.raises(mailer.MailerError, match="connection failed|send failed"):
        await mailer.send_message(
            msg,
            host="127.0.0.1",
            port=1,
            user="",
            password="",
            tls="none",
            timeout=2.0,
        )


@pytest.mark.asyncio
async def test_send_message_with_pre_set_from_to_headers(smtp_server):
    """If the caller already set From/To headers, send_message uses
    them as-is and doesn't overwrite with defaults."""
    host, port, handler = smtp_server

    msg = EmailMessage()
    msg["Subject"] = "pre-set test"
    msg["From"] = "custom-sender@test.local"
    msg["To"] = "custom-recipient@test.local"
    msg.set_content("hello")

    await mailer.send_message(
        msg, host=host, port=port, user="user-that-should-be-ignored",
        password="", tls="none",
    )

    assert len(handler.messages) == 1
    captured = handler.messages[0]
    assert captured["mail_from"] == "custom-sender@test.local"
    assert "custom-recipient@test.local" in captured["rcpt_tos"]
    # The user= parameter should NOT have been used as a From header
    assert "user-that-should-be-ignored" not in captured["content_str"]
