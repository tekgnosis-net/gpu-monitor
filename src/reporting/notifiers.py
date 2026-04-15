"""
Async notification senders for GPU alert channels.

Provides send functions for four channels:
  * ntfy.sh     — HTTP POST to a topic URL with priority headers
  * Pushover    — HTTP POST to api.pushover.net with user/app keys
  * Webhook     — configurable HTTP POST/PUT with custom headers + body
  * Email alert — plain-text email via the existing mailer module

Each sender follows the mailer.py pattern:
  * Async/await for non-blocking I/O
  * Raises NotifierError on failure (typed, with channel name)
  * Accepts a shared aiohttp.ClientSession for connection reuse
  * Logs warnings on failure, never crashes the caller

The dispatch_alert() function fires all enabled channels in parallel
via asyncio.gather — one dead channel does not block others.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from email.message import EmailMessage
from typing import Any

import aiohttp

from reporting import crypto, mailer

log = logging.getLogger("gpu-monitor.notifiers")

TIMEOUT = aiohttp.ClientTimeout(total=15)


class NotifierError(Exception):
    """Base error for notification channel failures."""


# ─── ntfy.sh ───────────────────────────────────────────────────────────────


async def send_ntfy(
    *,
    topic_url: str,
    title: str,
    message: str,
    priority: str = "high",
    token: str | None = None,
    session: aiohttp.ClientSession,
) -> None:
    """POST a plain-text notification to an ntfy topic.

    ntfy accepts the message as the raw body with metadata in headers:
      X-Title:    notification title
      X-Priority: min/low/default/high/urgent

    Token-based auth (for ntfy.sh cloud with private topics or
    self-hosted instances with ACLs) is supported via the standard
    Authorization: Bearer header. Pass the decrypted access token
    as `token`; omit or pass None/empty for public topics.
    """
    if not topic_url:
        raise NotifierError("ntfy: topic_url is empty")

    headers = {
        "X-Title": title,
        "X-Priority": priority,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with session.post(
            topic_url,
            data=message.encode("utf-8"),
            headers=headers,
            timeout=TIMEOUT,
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise NotifierError(
                    f"ntfy: HTTP {resp.status} from {topic_url}: {body[:200]}"
                )
    except aiohttp.ClientError as exc:
        raise NotifierError(f"ntfy: connection error: {exc}") from exc


# ─── Pushover ──────────────────────────────────────────────────────────────


PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"


async def send_pushover(
    *,
    user_key: str,
    app_token: str,
    title: str,
    message: str,
    priority: int = 1,
    session: aiohttp.ClientSession,
) -> None:
    """POST a notification to the Pushover API.

    Pushover uses form-encoded body with token/user/message fields.
    Priority: -2 (lowest) to 2 (emergency). Emergency (2) requires
    retry/expire params which we don't support yet — cap at 1 (high).
    """
    if not user_key or not app_token:
        raise NotifierError("pushover: user_key and app_token are required")

    data = {
        "token": app_token,
        "user": user_key,
        "title": title,
        "message": message,
        "priority": str(min(priority, 1)),  # cap at 1, emergency needs extra params
    }
    try:
        async with session.post(
            PUSHOVER_API_URL,
            data=data,
            timeout=TIMEOUT,
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise NotifierError(
                    f"pushover: HTTP {resp.status}: {body[:200]}"
                )
    except aiohttp.ClientError as exc:
        raise NotifierError(f"pushover: connection error: {exc}") from exc


# ─── Generic webhook ───────────────────────────────────────────────────────


def _render_template(template: str, data: dict[str, Any]) -> str:
    """Simple {{key}} substitution. No Jinja2 — keeps it zero-dependency
    and avoids template injection. Keys not found in data are left as-is
    (the user sees their placeholder, which is a clear signal something
    is misconfigured rather than a silent empty string)."""
    result = template
    for key, value in data.items():
        result = result.replace("{{" + key + "}}", str(value))
    return result


async def send_webhook(
    *,
    url: str,
    method: str = "POST",
    headers: dict[str, str] | None = None,
    body_template: str | None = None,
    auth_token: str | None = None,
    payload: dict[str, Any],
    session: aiohttp.ClientSession,
) -> None:
    """Send an alert to a user-configured webhook endpoint.

    If body_template is set (non-empty string), render it with {{key}}
    substitution from payload and send as the raw body. Content-Type
    is taken from headers if specified, otherwise defaults to
    text/plain for templates.

    If body_template is empty/None, send payload as JSON with
    Content-Type: application/json.
    """
    if not url:
        raise NotifierError("webhook: url is empty")

    req_headers = dict(headers or {})

    # Inject auth token as Bearer if configured
    if auth_token:
        req_headers.setdefault("Authorization", f"Bearer {auth_token}")

    try:
        if body_template:
            # Custom template mode
            body = _render_template(body_template, payload)
            req_headers.setdefault("Content-Type", "text/plain")
            async with session.request(
                method, url, data=body.encode("utf-8"),
                headers=req_headers, timeout=TIMEOUT,
            ) as resp:
                if resp.status >= 400:
                    resp_body = await resp.text()
                    raise NotifierError(
                        f"webhook: HTTP {resp.status} from {url}: {resp_body[:200]}"
                    )
        else:
            # Default JSON mode
            req_headers.setdefault("Content-Type", "application/json")
            async with session.request(
                method, url, json=payload,
                headers=req_headers, timeout=TIMEOUT,
            ) as resp:
                if resp.status >= 400:
                    resp_body = await resp.text()
                    raise NotifierError(
                        f"webhook: HTTP {resp.status} from {url}: {resp_body[:200]}"
                    )
    except aiohttp.ClientError as exc:
        raise NotifierError(f"webhook: connection error: {exc}") from exc


# ─── Email alert ───────────────────────────────────────────────────────────


async def send_alert_email(
    *,
    subject: str,
    body_text: str,
    smtp_config: dict[str, Any],
    recipients: list[str],
    secret_key: bytes,
) -> None:
    """Build a minimal plain-text alert email and send via the existing
    SMTP mailer. Reuses the SMTP configuration from settings.smtp —
    no separate SMTP setup needed for alerts."""
    if not recipients:
        raise NotifierError("email: no recipients configured")

    host = smtp_config.get("host") or ""
    if not host:
        raise NotifierError("email: SMTP host is not configured (set it in Settings → SMTP)")

    from_addr = smtp_config.get("from") or smtp_config.get("user") or "gpu-monitor@localhost"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(recipients)
    msg.set_content(body_text)

    try:
        password = crypto.decrypt(smtp_config.get("password_enc", ""), secret_key)
    except crypto.CryptoError as exc:
        raise NotifierError(f"email: cannot decrypt SMTP password: {exc}") from exc

    try:
        await mailer.send_message(
            msg,
            host=host,
            port=int(smtp_config.get("port") or 587),
            user=smtp_config.get("user", "") or "",
            password=password,
            tls=smtp_config.get("tls") or "starttls",
            timeout=15.0,
        )
    except mailer.MailerError as exc:
        raise NotifierError(f"email: send failed: {exc}") from exc


# ─── Dispatcher ────────────────────────────────────────────────────────────


def build_alert_data(
    *,
    gpu_index: int,
    gpu_name: str,
    metric: str,
    value: float,
    threshold: float,
    unit: str = "",
) -> dict[str, Any]:
    """Build the standard alert data dict used by all channels."""
    message = f"{gpu_name} (GPU {gpu_index}): {metric} is {value}{unit} (threshold {threshold}{unit})"
    return {
        "gpu_index": gpu_index,
        "gpu_name": gpu_name,
        "metric": metric,
        "value": value,
        "threshold": threshold,
        "message": message,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


async def dispatch_alert(
    *,
    channels_config: dict[str, Any],
    alert_data: dict[str, Any],
    smtp_config: dict[str, Any],
    secret_key: bytes,
    instance_name: str = "",
) -> list[str]:
    """Fire all enabled notification channels for one alert.

    Returns a list of channel names that succeeded. Failures are
    logged as warnings but do NOT raise — one dead channel must not
    block others from firing. Uses asyncio.gather with
    return_exceptions=True for parallel dispatch.

    instance_name: when set, replaces "GPU Monitor" in notification
    titles so multiple instances reporting to the same channel are
    distinguishable (e.g. "ML-Rig-01 Alert" vs "GPU Monitor Alert").
    """
    prefix = instance_name.strip() if instance_name else "GPU Monitor"
    title = f"{prefix} Alert"
    message = alert_data["message"]
    tasks: list[tuple[str, Any]] = []

    async with aiohttp.ClientSession() as session:
        # ntfy
        ntfy = channels_config.get("ntfy", {})
        if ntfy.get("enabled") and ntfy.get("topic_url"):
            # Decrypt the ntfy access token if configured (for
            # self-hosted instances with ACLs or private cloud topics).
            ntfy_token = None
            if ntfy.get("token_enc"):
                try:
                    ntfy_token = crypto.decrypt(ntfy["token_enc"], secret_key)
                except crypto.CryptoError as exc:
                    log.warning("dispatch: cannot decrypt ntfy token: %s", exc)

            tasks.append(("ntfy", send_ntfy(
                topic_url=ntfy["topic_url"],
                title=title,
                message=message,
                priority=ntfy.get("priority", "high"),
                token=ntfy_token or None,
                session=session,
            )))

        # Pushover
        pushover = channels_config.get("pushover", {})
        if pushover.get("enabled"):
            try:
                user_key = crypto.decrypt(
                    pushover.get("user_key_enc", ""), secret_key)
                app_token = crypto.decrypt(
                    pushover.get("app_token_enc", ""), secret_key)
            except crypto.CryptoError as exc:
                log.warning("dispatch: cannot decrypt pushover keys: %s", exc)
                user_key = app_token = ""

            if user_key and app_token:
                tasks.append(("pushover", send_pushover(
                    user_key=user_key,
                    app_token=app_token,
                    title=title,
                    message=message,
                    priority=pushover.get("priority", 1),
                    session=session,
                )))

        # Webhook
        webhook = channels_config.get("webhook", {})
        if webhook.get("enabled") and webhook.get("url"):
            auth_token = None
            if webhook.get("auth_token_enc"):
                try:
                    auth_token = crypto.decrypt(
                        webhook["auth_token_enc"], secret_key)
                except crypto.CryptoError as exc:
                    log.warning("dispatch: cannot decrypt webhook auth token: %s", exc)

            tasks.append(("webhook", send_webhook(
                url=webhook["url"],
                method=webhook.get("method", "POST"),
                headers=webhook.get("headers"),
                body_template=webhook.get("body_template") or None,
                auth_token=auth_token,
                payload=alert_data,
                session=session,
            )))

        # Email
        email_cfg = channels_config.get("email", {})
        if email_cfg.get("enabled") and email_cfg.get("recipients"):
            tasks.append(("email", send_alert_email(
                subject=f"{prefix} Alert: {alert_data.get('gpu_name', 'GPU')} {alert_data.get('metric', 'alert')}",
                body_text=message,
                smtp_config=smtp_config,
                recipients=email_cfg["recipients"],
                secret_key=secret_key,
            )))

        if not tasks:
            return []

        # Fire all channels in parallel
        channel_names = [name for name, _ in tasks]
        coros = [coro for _, coro in tasks]
        results = await asyncio.gather(*coros, return_exceptions=True)

    succeeded = []
    for name, result in zip(channel_names, results):
        if isinstance(result, Exception):
            log.warning("dispatch: channel %s failed: %s", name, result)
        else:
            succeeded.append(name)
            log.info("dispatch: channel %s fired successfully", name)

    return succeeded
