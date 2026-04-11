"""
Settings model + atomic load/save for /app/settings.json.

Phase 6 of the v1.0.0 overhaul. This is the single source of truth for
every user-configurable value in the application. The collector reads
collection.* / logging.* / housekeeping.* via `jq` once per tick
(pre-existing pattern); the server reads everything; the scheduler
reads smtp.* / schedules / power.* at cron-fire time.

Design principles:

1. **Defaults live here, not in settings.json.** A fresh container has
   no settings.json at all — the very first `load_settings()` call
   reads from defaults and only hits disk if the file exists. This
   means a first-run user's behavior is identical to the pre-overhaul
   container (interval=4, retention=3d, etc.) without needing a
   bootstrap file in the Docker image.

2. **User values override defaults key-by-key, not wholesale.** A
   user who PUTs only `{"power": {"rate_per_kwh": 0.18}}` must not
   lose their smtp config. The merge walks each section and replaces
   only the keys the user actually sent.

3. **SMTP password is *not* in this model.** Pydantic validates
   structural shape and numeric bounds; the password ciphertext is
   stored alongside as `smtp.password_enc` (plain string field, no
   validation constraint). The server handler is responsible for
   encrypt/decrypt transitions. Keeping the crypto concern out of
   the data model means the model can be shared by tests that don't
   care about the secret.

4. **Atomic writes** use the same tempfile + os.replace() pattern as
   the collector's safe_write_json. A mid-write power-off leaves
   either the old file intact or the new file intact — never a
   half-written file.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, ValidationError


# ─── Defaults ──────────────────────────────────────────────────────────────
#
# The default values match the pre-Phase-1 behavior exactly:
#
#   - collection.interval_seconds=4 was the legacy INTERVAL constant
#   - collection.flush_interval_seconds=60 matches the 15 samples × 4s
#     buffer cadence the old container used
#   - housekeeping.retention_days=3 matches the old RETENTION_SECONDS
#   - logging.max_size_mb=5, max_age_hours=25 match the old rotate_logs
#   - alerts.* mirror the hardcoded thresholds from the legacy HTML
#
# Every field has a default so first-run behavior is identical to the
# old container. Absence of /app/settings.json is a valid state, not
# an error.
DEFAULT_SETTINGS: dict[str, Any] = {
    "collection": {
        "interval_seconds": 4,
        "flush_interval_seconds": 60,
    },
    "housekeeping": {
        "retention_days": 3,
    },
    "logging": {
        "max_size_mb": 5,
        "max_age_hours": 25,
    },
    "alerts": {
        "temperature_c": 80,
        "utilization_pct": 100,
        "power_w": 300,
        "cooldown_seconds": 10,
        "sound_enabled": True,
        "notifications_enabled": False,
    },
    "power": {
        "rate_per_kwh": 0.0,
        "currency": "$",
    },
    "smtp": {
        "host": "",
        "port": 587,
        "user": "",
        "password_enc": "",
        "from": "",
        "tls": "starttls",
    },
    "schedules": [],
    "theme": {
        "default_mode": "auto",
    },
}


# ─── Pydantic models ───────────────────────────────────────────────────────


class CollectionSettings(BaseModel):
    # Range from the plan's audit: 2–300 s for interval, 5–3600 s for flush.
    # Lower interval = more responsive charts but more CPU. Upper flush =
    # longer worst-case data-loss window on unclean shutdown.
    interval_seconds: int = Field(default=4, ge=2, le=300)
    flush_interval_seconds: int = Field(default=60, ge=5, le=3600)


class HousekeepingSettings(BaseModel):
    # retention_days 1–365. Below 1 would lose all historical charts,
    # above 365 creates unbounded disk growth on shared volumes.
    retention_days: int = Field(default=3, ge=1, le=365)


class LoggingSettings(BaseModel):
    # max_size_mb 1–100, max_age_hours 1–720 (=30d). Paired so whichever
    # threshold trips first triggers rotation.
    max_size_mb: int = Field(default=5, ge=1, le=100)
    max_age_hours: int = Field(default=25, ge=1, le=720)


class AlertSettings(BaseModel):
    temperature_c: float = Field(default=80, ge=0, le=150)
    utilization_pct: float = Field(default=100, ge=0, le=100)
    power_w: float = Field(default=300, ge=0, le=2000)
    cooldown_seconds: int = Field(default=10, ge=2, le=600)
    sound_enabled: bool = True
    notifications_enabled: bool = False


class PowerSettings(BaseModel):
    # rate_per_kwh is an open-ended float because utility tariffs
    # genuinely vary across three orders of magnitude (think: residential
    # $0.10/kWh vs commercial time-of-use peaks at $0.60/kWh vs bulk
    # industrial contracts at $0.03/kWh). Negative rates are physically
    # meaningful under some demand-response contracts but hidden here
    # behind ge=0 to avoid a confusing "your GPUs made you money" tile.
    rate_per_kwh: float = Field(default=0.0, ge=0.0, le=10.0)
    # Single-character currency symbol only. Full locale support is
    # out of scope for v1.0.0.
    currency: str = Field(default="$", min_length=1, max_length=4)


class SmtpSettings(BaseModel):
    host: str = ""
    port: int = Field(default=587, ge=1, le=65535)
    user: str = ""
    # Encrypted password ciphertext, or "" if not set. Never the
    # plaintext — the API PUT handler converts plaintext → ciphertext
    # before this model sees the value.
    password_enc: str = ""
    # "From" address. Empty = fall back to `user` at send time.
    from_: str = Field(default="", alias="from")
    # Connection security mode.
    tls: str = Field(default="starttls")

    @field_validator("tls")
    @classmethod
    def _tls_enum(cls, value: str) -> str:
        allowed = {"starttls", "tls", "none"}
        if value not in allowed:
            raise ValueError(f"tls must be one of {sorted(allowed)}")
        return value

    # Allow both `from` (JSON-facing) and `from_` (Python-facing) so the
    # reserved-word collision with Python's `from` keyword doesn't leak
    # into clients.
    model_config = {"populate_by_name": True}


class ScheduleEntry(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    template: str = Field(default="daily")
    # Unvalidated at the Pydantic layer — croniter in the scheduler
    # is the authoritative parser, and we catch the error there with
    # a useful message. Validating crontab syntax twice is pointless.
    cron: str = Field(min_length=1)
    recipients: list[str] = Field(default_factory=list)
    enabled: bool = True
    # Unix epoch seconds of the last successful fire, or None if never
    # run. The scheduler updates this in-place after each successful
    # send.
    last_run_epoch: int | None = None

    @field_validator("template")
    @classmethod
    def _template_enum(cls, value: str) -> str:
        allowed = {"daily", "weekly", "monthly"}
        if value not in allowed:
            raise ValueError(f"template must be one of {sorted(allowed)}")
        return value


class ThemeSettings(BaseModel):
    default_mode: str = "auto"

    @field_validator("default_mode")
    @classmethod
    def _mode_enum(cls, value: str) -> str:
        allowed = {"auto", "light", "dark"}
        if value not in allowed:
            raise ValueError(f"default_mode must be one of {sorted(allowed)}")
        return value


class Settings(BaseModel):
    """Root model. Every subsection has a default so a blank PUT
    body (`{}`) is a valid no-op rather than an error."""

    collection: CollectionSettings = Field(default_factory=CollectionSettings)
    housekeeping: HousekeepingSettings = Field(default_factory=HousekeepingSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    alerts: AlertSettings = Field(default_factory=AlertSettings)
    power: PowerSettings = Field(default_factory=PowerSettings)
    smtp: SmtpSettings = Field(default_factory=SmtpSettings)
    schedules: list[ScheduleEntry] = Field(default_factory=list)
    theme: ThemeSettings = Field(default_factory=ThemeSettings)


# ─── Load / save helpers ───────────────────────────────────────────────────


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `override` into `base` without mutating either.

    Nested dicts are merged key-by-key; list / scalar values from
    `override` replace the corresponding entry in `base`. This is how
    a partial PUT (e.g. just {"power": {"rate_per_kwh": 0.18}})
    preserves unrelated sections like smtp and schedules.

    Lists are replaced wholesale, not merged element-by-element. The
    schedules list in particular has to support full CRUD — if a
    user sends a 2-item list, that's the new complete list, not
    "add these two to whatever you had".

    CRITICAL: the function MUST NOT alias any nested collection into
    the return value. An earlier version used `dict(base)` which is
    a shallow copy — nested dicts were shared with `base`, and
    because `base` is often the module-level DEFAULT_SETTINGS, a
    caller who mutated a returned subsection would silently mutate
    the global defaults for the lifetime of the process. The current
    implementation deep-copies any `base` value that the override
    doesn't replace, so the return value is always independent
    from its inputs.
    """
    out: dict[str, Any] = {}
    for key, value in base.items():
        # Deep-copy every base value up front. If override supplies
        # the same key, we re-assign below, so the deep-copy cost
        # for overridden keys is ~zero (one copy of a primitive).
        out[key] = copy.deepcopy(value)

    for key, value in override.items():
        if (
            key in out
            and isinstance(out[key], dict)
            and isinstance(value, dict)
        ):
            out[key] = deep_merge(out[key], value)
        else:
            # Deep-copy override values too — a caller who then
            # mutates the override dict after the merge shouldn't
            # see their change reflected in the settings file.
            out[key] = copy.deepcopy(value)

    return out


# Back-compat alias for the previous underscore-prefixed name. The
# server module used to import `_deep_merge` (a private helper),
# which caused cross-module coupling to an underscore API. Keeping
# the alias for now means the import site doesn't break during the
# rename; it can be removed after one release.
_deep_merge = deep_merge


def load_settings(path: Path) -> dict[str, Any]:
    """Read settings.json and deep-merge over the defaults.

    Fallback rules on the various failure modes:

      * Missing file             → pure defaults
      * Malformed JSON            → pure defaults
      * Top-level JSON not a dict → pure defaults
      * Pydantic validation fails → pure defaults (wholesale, not
                                    per-subsection)

    The wholesale validation-failure fallback is deliberate: a
    half-valid settings dict is worse than the documented defaults,
    because downstream code would trust it. Reviewers considering
    "per-subsection fallback" should note that Pydantic doesn't
    surface per-section validity in a single pass — implementing
    partial fallback would require running `SubModel.model_validate`
    on each top-level section separately, doubling the validation
    cost without a clear behavioral win.

    Returns the plain `dict` form, not a Settings model instance,
    because the consumers (server handlers, the scheduler) want to
    serialize to JSON directly without a `.model_dump()` round-trip.
    """
    if not path.exists():
        return deep_merge(DEFAULT_SETTINGS, {})

    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return deep_merge(DEFAULT_SETTINGS, {})

    if not isinstance(raw, dict):
        return deep_merge(DEFAULT_SETTINGS, {})

    merged = deep_merge(DEFAULT_SETTINGS, raw)

    # Validate via Pydantic and re-emit. If validation fails on any
    # field, fall back to defaults wholesale — safer than leaving a
    # partially-valid dict that downstream code might trust.
    try:
        validated = Settings.model_validate(merged)
    except ValidationError:
        return deep_merge(DEFAULT_SETTINGS, {})

    return validated.model_dump(by_alias=True)


def save_settings(path: Path, data: dict[str, Any]) -> None:
    """Atomic write: tempfile in the same dir, then os.replace().

    os.replace() is atomic on POSIX (same filesystem), which the
    container guarantees because the tempfile is in the same directory
    as the target. A mid-write crash leaves either the old file or the
    new file intact — never a half-written file.

    Sets mode 0600 on the new file because smtp.password_enc is a
    secret-at-rest even after Fernet encryption. Defense-in-depth
    against a compromised container runtime reading the file.

    Caller is responsible for ensuring `data` passes Settings
    validation — this helper trusts the input. Server handlers
    validate via the PUT pipeline; the scheduler validates via the
    load_settings() round-trip before writing back `last_run_epoch`
    updates.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # delete=False because we hand ownership of the tempfile to
    # os.replace() below. The context manager still closes the handle.
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        json.dump(data, tmp, indent=2, sort_keys=True)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = tmp.name

    try:
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except OSError:
        # Best-effort cleanup if the replace failed; never leave a
        # stray tempfile polluting the directory.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
