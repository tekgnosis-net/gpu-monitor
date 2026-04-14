"""
Tests for reporting.notifiers — async notification senders.

Uses aiohttp's test utilities and unittest.mock to verify each
channel's HTTP request shape without making real network calls.
The mailer path is mocked at the mailer.send_message level so
we don't need a real SMTP server.
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from reporting import notifiers
from reporting import mailer


# ─── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def alert_data():
    """Standard alert data dict matching build_alert_data output."""
    return notifiers.build_alert_data(
        gpu_index=0,
        gpu_name="NVIDIA GeForce RTX 3090",
        metric="temperature",
        value=87.2,
        threshold=80,
        unit="°C",
    )


# ─── build_alert_data ─────────────────────────────────────────────────────

def test_build_alert_data_message_format():
    data = notifiers.build_alert_data(
        gpu_index=1, gpu_name="RTX 4090", metric="power",
        value=350.5, threshold=300, unit="W",
    )
    assert data["gpu_index"] == 1
    assert data["gpu_name"] == "RTX 4090"
    assert data["metric"] == "power"
    assert data["value"] == 350.5
    assert data["threshold"] == 300
    assert "350.5W" in data["message"]
    assert "threshold 300W" in data["message"]
    assert "GPU 1" in data["message"]
    assert "timestamp" in data


# ─── _render_template ─────────────────────────────────────────────────────

def test_render_template_substitutes_keys():
    tpl = "Alert: {{gpu_name}} {{metric}} is {{value}}"
    result = notifiers._render_template(tpl, {
        "gpu_name": "RTX 3090",
        "metric": "temperature",
        "value": 87.2,
    })
    assert result == "Alert: RTX 3090 temperature is 87.2"


def test_render_template_leaves_unknown_keys():
    """Unknown {{keys}} are left as-is so the user sees what's wrong."""
    tpl = "{{gpu_name}} — {{unknown_field}}"
    result = notifiers._render_template(tpl, {"gpu_name": "RTX 3090"})
    assert result == "RTX 3090 — {{unknown_field}}"


# ─── send_ntfy ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_ntfy_posts_correct_shape():
    """ntfy sends a POST with X-Title and X-Priority headers + plain text body."""
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.post = MagicMock(return_value=mock_resp)

    await notifiers.send_ntfy(
        topic_url="https://ntfy.sh/test-topic",
        title="GPU Alert",
        message="Temperature is 87°C",
        priority="high",
        session=session,
    )

    session.post.assert_called_once()
    call_args = session.post.call_args
    assert call_args[0][0] == "https://ntfy.sh/test-topic"
    assert call_args[1]["headers"]["X-Title"] == "GPU Alert"
    assert call_args[1]["headers"]["X-Priority"] == "high"
    assert call_args[1]["data"] == "Temperature is 87°C".encode("utf-8")


@pytest.mark.asyncio
async def test_send_ntfy_injects_auth_token():
    """When a token is provided, send_ntfy adds an Authorization: Bearer header."""
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.post = MagicMock(return_value=mock_resp)

    await notifiers.send_ntfy(
        topic_url="https://ntfy.example.com/private-topic",
        title="Alert",
        message="test",
        token="tk_mytoken123",
        session=session,
    )

    headers = session.post.call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer tk_mytoken123"
    assert headers["X-Title"] == "Alert"


@pytest.mark.asyncio
async def test_send_ntfy_no_auth_header_without_token():
    """When token is None/empty, no Authorization header is sent."""
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.post = MagicMock(return_value=mock_resp)

    await notifiers.send_ntfy(
        topic_url="https://ntfy.sh/public-topic",
        title="Alert",
        message="test",
        session=session,
    )

    headers = session.post.call_args[1]["headers"]
    assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_send_ntfy_raises_on_empty_url():
    session = MagicMock()
    with pytest.raises(notifiers.NotifierError, match="topic_url is empty"):
        await notifiers.send_ntfy(
            topic_url="", title="t", message="m", session=session,
        )


# ─── send_pushover ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_pushover_posts_form_data():
    """Pushover sends form-encoded data with token/user/message."""
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.post = MagicMock(return_value=mock_resp)

    await notifiers.send_pushover(
        user_key="ukey123",
        app_token="atoken456",
        title="GPU Alert",
        message="Power is 350W",
        priority=1,
        session=session,
    )

    session.post.assert_called_once()
    call_args = session.post.call_args
    assert call_args[0][0] == notifiers.PUSHOVER_API_URL
    data = call_args[1]["data"]
    assert data["token"] == "atoken456"
    assert data["user"] == "ukey123"
    assert data["message"] == "Power is 350W"
    assert data["priority"] == "1"


@pytest.mark.asyncio
async def test_send_pushover_raises_on_missing_keys():
    session = MagicMock()
    with pytest.raises(notifiers.NotifierError, match="user_key and app_token"):
        await notifiers.send_pushover(
            user_key="", app_token="tok", title="t", message="m", session=session,
        )


# ─── send_webhook ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_webhook_default_json():
    """Default webhook sends JSON payload."""
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.request = MagicMock(return_value=mock_resp)

    payload = {"gpu_index": 0, "metric": "temperature", "value": 87.2}
    await notifiers.send_webhook(
        url="https://webhook.example.com/alert",
        payload=payload,
        session=session,
    )

    session.request.assert_called_once()
    call_args = session.request.call_args
    assert call_args[0][0] == "POST"
    assert call_args[0][1] == "https://webhook.example.com/alert"
    assert call_args[1]["json"] == payload
    assert call_args[1]["headers"]["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_send_webhook_custom_template():
    """Custom body template uses {{key}} substitution."""
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.request = MagicMock(return_value=mock_resp)

    await notifiers.send_webhook(
        url="https://example.com/hook",
        body_template="ALERT: {{gpu_name}} {{metric}} = {{value}}",
        payload={"gpu_name": "RTX 3090", "metric": "temp", "value": 87},
        session=session,
    )

    call_args = session.request.call_args
    body = call_args[1]["data"]
    assert body == "ALERT: RTX 3090 temp = 87".encode("utf-8")


@pytest.mark.asyncio
async def test_send_webhook_injects_auth_token():
    """Auth token is added as Bearer if configured."""
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.request = MagicMock(return_value=mock_resp)

    await notifiers.send_webhook(
        url="https://example.com/hook",
        auth_token="secret123",
        payload={"test": True},
        session=session,
    )

    headers = session.request.call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer secret123"


# ─── send_alert_email ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_alert_email_calls_mailer():
    """Email alert builds an EmailMessage and calls mailer.send_message."""
    with patch("reporting.notifiers.mailer") as mock_mailer, \
         patch("reporting.notifiers.crypto") as mock_crypto:
        mock_crypto.decrypt.return_value = "plaintext_password"
        mock_mailer.send_message = AsyncMock()
        mock_mailer.MailerError = mailer.MailerError

        await notifiers.send_alert_email(
            subject="GPU Alert: RTX 3090 temperature",
            body_text="Temperature is 87°C",
            smtp_config={
                "host": "smtp.example.com",
                "port": 587,
                "user": "user@example.com",
                "from": "gpu@example.com",
                "password_enc": "encrypted_value",
                "tls": "starttls",
            },
            recipients=["admin@example.com"],
            secret_key=b"fake_key",
        )

        mock_mailer.send_message.assert_called_once()
        msg = mock_mailer.send_message.call_args[0][0]
        assert msg["Subject"] == "GPU Alert: RTX 3090 temperature"
        assert msg["To"] == "admin@example.com"
        assert msg["From"] == "gpu@example.com"


@pytest.mark.asyncio
async def test_send_alert_email_raises_on_no_recipients():
    with pytest.raises(notifiers.NotifierError, match="no recipients"):
        await notifiers.send_alert_email(
            subject="t", body_text="b",
            smtp_config={"host": "smtp.example.com"},
            recipients=[],
            secret_key=b"k",
        )


# ─── dispatch_alert ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_alert_fires_enabled_channels(alert_data):
    """dispatch_alert fires all enabled channels and returns success list."""
    channels = {
        "ntfy": {"enabled": True, "topic_url": "https://ntfy.sh/test", "priority": "high"},
        "pushover": {"enabled": False},
        "webhook": {"enabled": False},
        "email": {"enabled": False},
    }

    with patch("reporting.notifiers.send_ntfy", new_callable=AsyncMock) as mock_ntfy:
        succeeded = await notifiers.dispatch_alert(
            channels_config=channels,
            alert_data=alert_data,
            smtp_config={},
            secret_key=b"key",
        )

    assert "ntfy" in succeeded
    mock_ntfy.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_alert_one_failure_doesnt_block_others(alert_data):
    """If one channel fails, the others should still fire."""
    channels = {
        "ntfy": {"enabled": True, "topic_url": "https://ntfy.sh/test"},
        "pushover": {"enabled": False},
        "webhook": {"enabled": True, "url": "https://webhook.example.com/hook"},
        "email": {"enabled": False},
    }

    with patch("reporting.notifiers.send_ntfy", new_callable=AsyncMock) as mock_ntfy, \
         patch("reporting.notifiers.send_webhook", new_callable=AsyncMock) as mock_webhook:
        # ntfy succeeds, webhook fails
        mock_ntfy.return_value = None
        mock_webhook.side_effect = notifiers.NotifierError("webhook down")

        succeeded = await notifiers.dispatch_alert(
            channels_config=channels,
            alert_data=alert_data,
            smtp_config={},
            secret_key=b"key",
        )

    assert "ntfy" in succeeded
    assert "webhook" not in succeeded


@pytest.mark.asyncio
async def test_dispatch_alert_no_enabled_channels(alert_data):
    """When no channels are enabled, dispatch returns an empty list."""
    channels = {
        "ntfy": {"enabled": False},
        "pushover": {"enabled": False},
        "webhook": {"enabled": False},
        "email": {"enabled": False},
    }

    succeeded = await notifiers.dispatch_alert(
        channels_config=channels,
        alert_data=alert_data,
        smtp_config={},
        secret_key=b"key",
    )
    assert succeeded == []
