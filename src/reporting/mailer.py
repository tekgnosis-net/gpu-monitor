"""
SMTP sender wrapped around aiosmtplib.

Phase 6b of the v1.0.0 overhaul. This is the low-level send primitive
shared by three callers:

  * The Settings → Test button (POST /api/settings/smtp/test)
  * The Report view → Run Now button (POST /api/schedules/{id}/run-now)
  * The background scheduler firing scheduled reports

All three paths construct a MIMEMultipart message (the render module
builds the richer report body with embedded PNGs; test-email builds a
minimal plain+html dual-alternative message) and hand it here for
delivery. This module does not care what's in the message — it only
cares about the SMTP transport details: host, port, TLS mode,
authentication.

Why aiosmtplib instead of smtplib?

  The HTTP server runs on asyncio via aiohttp. A blocking smtplib
  call would freeze the event loop for the duration of the SMTP
  handshake, TLS negotiation, and message transfer — anywhere from
  a few ms on a local relay to 10+ seconds on a slow remote
  provider. aiosmtplib speaks the same protocol but returns control
  to the event loop during I/O waits. The cost is one extra
  dependency (~100 KB).

TLS mode table (from settings.smtp.tls):

  "starttls"  : Plain connection on port 587 (or 25), upgraded to
                TLS via the STARTTLS command. This is the default
                for modern SMTP relays.
  "tls"       : Implicit TLS from connection start on port 465.
                Older style, still used by Gmail/Outlook's SMTPS.
  "none"      : No TLS at all. For local relays / mailpit / the
                test suite. Never use this against a real mail
                provider.

Authentication:

  If both `user` and (decrypted) password are non-empty, we AUTH.
  Otherwise we skip AUTH entirely, which works for local relays
  that accept unauthenticated mail from the docker network.
"""

from __future__ import annotations

import logging
import ssl
from email.message import EmailMessage
from typing import Any

import aiosmtplib


log = logging.getLogger("gpu-monitor.mailer")


class MailerError(Exception):
    """Raised when the SMTP config is invalid or the send fails.
    Distinct from aiosmtplib's SMTPException family so callers have
    a single exception type to catch."""


def _build_ssl_context(verify_host: str) -> ssl.SSLContext:
    """Default SSL context with hostname verification. We deliberately
    do NOT expose "skip verification" as a setting — a homelab user
    who needs to send mail to a self-signed relay can install the
    relay's CA into the container's trust store instead. Silently
    disabling certificate verification on a security-sensitive path
    is the kind of thing that makes security audits fail."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    # verify_host is unused today but kept as an argument for future
    # SAN-pinning scenarios — the server's real hostname may differ
    # from the connection target (e.g. an SRV-resolved IP with a
    # cert for mail.example.com).
    _ = verify_host
    return ctx


async def send_message(
    message: EmailMessage,
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    tls: str,
    timeout: float = 30.0,
) -> None:
    """Send a pre-built EmailMessage via the configured SMTP server.

    The caller is expected to have already populated the message's
    From/To/Subject/body headers and any MIME parts — this function
    only handles the transport. Raises MailerError on any failure
    (connection refused, TLS handshake failure, auth rejection,
    malformed message, timeout).

    `host == ""` is treated as a configuration error rather than a
    silent no-op. The scheduler checks for this before calling, so
    reaching this function with an empty host means a PUT landed
    without the required validation — surface it loudly.
    """
    if not host:
        raise MailerError(
            "SMTP host is empty — configure Settings → SMTP → Host first."
        )

    if tls not in ("starttls", "tls", "none"):
        raise MailerError(f"unsupported tls mode: {tls!r}")

    # Default "From" header if the message doesn't already set one.
    # Caller-set From takes precedence so the test-email path can
    # stamp a distinct sender.
    if "From" not in message:
        message["From"] = user or "gpu-monitor@localhost"

    # If the message has no explicit To: header, fall back to the
    # authenticated user as both sender and recipient. This lets the
    # "send a test email to yourself" path work without the caller
    # threading a recipient through.
    if "To" not in message:
        to_addr = user or "gpu-monitor@localhost"
        message["To"] = to_addr

    # Build the send() kwargs piecewise so we can OMIT username/password
    # entirely when the user is anonymous. Passing username=None still
    # triggers aiosmtplib's AUTH attempt on some versions, which then
    # fails with "AUTH extension not supported" against local relays
    # that don't advertise AUTH. The clean solution is: if and only if
    # BOTH user and password are non-empty, include them. Otherwise
    # skip AUTH entirely.
    send_kwargs: dict[str, Any] = {
        "hostname": host,
        "port": port,
        "use_tls":   tls == "tls",
        "start_tls": tls == "starttls",
        "timeout":   timeout,
    }
    if user and password:
        send_kwargs["username"] = user
        send_kwargs["password"] = password
    if tls != "none":
        send_kwargs["tls_context"] = _build_ssl_context(host)

    try:
        await aiosmtplib.send(message, **send_kwargs)
    except aiosmtplib.SMTPException as exc:
        # aiosmtplib raises an entire hierarchy of exceptions for
        # connection/auth/timeout/etc. We funnel them through
        # MailerError with a clean string so the API response doesn't
        # leak internal library types.
        log.warning("SMTP send failed to %s:%s: %s", host, port, exc)
        raise MailerError(f"SMTP send failed: {exc}") from exc
    except (OSError, ValueError, ssl.SSLError) as exc:
        # Connection-level failures (DNS resolve, connection refused,
        # SSL verification) are OSError / SSLError subclasses not
        # caught by SMTPException.
        log.warning("SMTP connection failed to %s:%s: %s", host, port, exc)
        raise MailerError(f"SMTP connection failed: {exc}") from exc


def build_test_message(from_addr: str, to_addr: str) -> EmailMessage:
    """Construct a minimal "Hello from GPU Monitor" test email with
    plain + HTML alternatives. Used by the Settings → Test SMTP button
    and as the simplest possible integration test subject.

    Kept separate from reporting.render.py's report rendering because
    the test email has no charts and no template parameters — it's
    just a signal that the SMTP credentials work. Pulling matplotlib
    in just to render a smoke test would be perverse."""
    msg = EmailMessage()
    msg["Subject"] = "GPU Monitor — SMTP test"
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(
        "This is a test email from GPU Monitor.\n\n"
        "If you are reading this, your SMTP configuration is working.\n"
    )
    msg.add_alternative(
        """\
<!DOCTYPE html>
<html>
<body style="font-family: -apple-system, 'Segoe UI', sans-serif; padding: 24px; color: #1d1d1f;">
  <h2 style="margin-top: 0;">GPU Monitor — SMTP test</h2>
  <p>This is a test email from <strong>GPU Monitor</strong>.</p>
  <p style="color: #6e6e73;">If you are reading this, your SMTP configuration is working.</p>
</body>
</html>
""",
        subtype="html",
    )
    return msg
