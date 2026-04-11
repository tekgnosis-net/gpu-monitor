"""
Fernet symmetric encryption for SMTP password at rest.

Phase 6 of the v1.0.0 overhaul. The server stores SMTP credentials in
/app/settings.json so the scheduler subprocess can read them at cron
fire time. We don't want to store the password as plaintext, and we
don't want to require the user to manage an external KMS for a
homelab tool. Fernet (from the `cryptography` package) gives us
AES-128-CBC + HMAC-SHA256 authenticated encryption with a 32-byte
URL-safe-base64-encoded key.

Key lifecycle:

    1. If the environment variable GPU_MONITOR_SECRET is set, use it
       directly. This is the recommended path for users who want to
       manage the key externally (Docker secrets, host env var,
       orchestrator injection).
    2. Otherwise fall back to /app/history/.secret. If the file
       exists, read it; if not, generate a fresh Fernet key and
       write it with mode 0600 so a compromised container runtime
       can't casually slurp it.
    3. If both are absent AND the history directory isn't writable,
       raise — there's nowhere to put the key and silently returning
       plaintext would defeat the entire point.

Key loss recovery: losing the key means every stored password_enc
becomes un-decryptable. The user has to clear SMTP config and
re-enter the password via the settings UI. This is documented in
the README's "Security" section (Phase 8).

Why not per-install key derivation? Keeping the secret file simple
means admin-level users can inspect and rotate it without needing
an `rekey` command. Rotation is manual: delete the file, restart,
re-enter SMTP password.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


DEFAULT_KEY_PATH = Path("/app/history/.secret")
ENV_VAR = "GPU_MONITOR_SECRET"


class CryptoError(Exception):
    """Raised when key material is unavailable or a token can't be
    decrypted. Distinct from cryptography's InvalidToken so the server
    handler can catch both in one except clause without importing
    cryptography directly."""


def load_or_create_key(path: Path = DEFAULT_KEY_PATH) -> bytes:
    """Return the Fernet key material, creating it if necessary.

    Lookup order:
      1. GPU_MONITOR_SECRET env var (must be a valid Fernet key)
      2. `path` file contents
      3. Generate a fresh key, write it with mode 0600, return it

    The env-var path bypasses disk entirely so users who mount a
    read-only /app can still use the feature by injecting the secret
    at runtime. Per-tick reads aren't a concern because the server
    module caches the result; scheduler subprocess reads once at
    startup.
    """
    env_value = os.environ.get(ENV_VAR)
    if env_value:
        key_bytes = env_value.encode("ascii")
        _assert_valid_key(key_bytes)
        return key_bytes

    path = Path(path)
    if path.exists():
        try:
            key_bytes = path.read_bytes().strip()
        except OSError as exc:
            raise CryptoError(f"cannot read key file {path}: {exc}") from exc
        _assert_valid_key(key_bytes)
        return key_bytes

    # First-run generation must be atomic against concurrent
    # initializers. Two processes starting at the same time (e.g.
    # the server and the scheduler subprocess) could both observe
    # the file missing, both generate different keys, both write
    # their own tempfile, and both call os.replace. The last
    # os.replace wins — which means any ciphertext produced between
    # the two reads using the loser's key becomes undecryptable
    # once the winner clobbers the key file.
    #
    # The fix is an exclusive create: O_CREAT | O_EXCL | O_WRONLY
    # via Path.open("xb") which raises FileExistsError if another
    # process beat us to it. On collision we re-read the winning
    # process's file rather than overwriting. The tempfile pattern
    # from save_settings protects against partial writes (the file
    # appears atomically via os.replace), but without the O_EXCL
    # guard on the target, two well-formed tempfile writes could
    # still race on the rename.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CryptoError(
            f"cannot create key file parent dir {path.parent}: {exc}. "
            f"Set {ENV_VAR} env var or fix /app/history permissions."
        ) from exc

    # Write the new key to a unique tempfile first, then attempt
    # to exclusively create the target by linking. If link fails
    # with EEXIST, another process won the race — unlink our
    # tempfile and re-read the winner's key.
    try:
        key_bytes = Fernet.generate_key()
        fd, tmp_path_str = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "wb") as tmp_fh:
                tmp_fh.write(key_bytes)
                tmp_fh.flush()
                os.fsync(tmp_fh.fileno())
            os.chmod(tmp_path, 0o600)

            # os.link() creates the target atomically IFF it doesn't
            # already exist — raises FileExistsError otherwise.
            # This is the kernel-level exclusive-create primitive
            # the whole atomic-create-or-reuse pattern is built on.
            try:
                os.link(tmp_path, path)
            except FileExistsError:
                # Lost the race. Unlink our tempfile and re-read
                # the winner's key.
                os.unlink(tmp_path)
                winner_bytes = path.read_bytes().strip()
                _assert_valid_key(winner_bytes)
                return winner_bytes

            # Won the race. Remove the tempfile link — the target
            # now has the real name. If unlink fails that's
            # cosmetic (the tempfile stays behind) but the key
            # file is correctly in place.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        except OSError:
            # Best-effort cleanup of the tempfile before re-raising.
            try:
                if tmp_path.exists():
                    os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise CryptoError(
            f"cannot create key file {path}: {exc}. "
            f"Set {ENV_VAR} env var or fix /app/history permissions."
        ) from exc

    return key_bytes


def _assert_valid_key(key: bytes) -> None:
    """Fernet keys are 44 ASCII chars of URL-safe base64 encoding a
    32-byte random value. Instantiating Fernet() raises ValueError on
    any deviation; we convert that to CryptoError so callers have a
    single exception type to handle."""
    try:
        Fernet(key)
    except (ValueError, TypeError) as exc:
        raise CryptoError(
            f"invalid Fernet key material: {exc}. "
            f"Expected 44 URL-safe base64 characters."
        ) from exc


def encrypt(plaintext: str, key: bytes) -> str:
    """Encrypt a plaintext password, returning the URL-safe base64
    token as a `str` suitable for JSON serialization.

    An empty plaintext is returned as an empty string — settings.json
    uses "" as the "no password set" sentinel, so a caller who passes
    "" by accident doesn't accidentally end up with a real ciphertext
    that the UI then reports as "password set".
    """
    if plaintext == "":
        return ""
    try:
        token = Fernet(key).encrypt(plaintext.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise CryptoError(f"encryption failed: {exc}") from exc
    return token.decode("ascii")


def decrypt(token: str, key: bytes) -> str:
    """Decrypt a previously-encrypted password token.

    Empty string in → empty string out (matching the encrypt() sentinel).
    InvalidToken (tampered, truncated, wrong key) → CryptoError.

    Decryption failures should NOT crash the scheduler — an expired or
    rotated key should leave the schedule disabled with a clear log
    message, not a Python traceback.
    """
    if token == "":
        return ""
    try:
        plaintext = Fernet(key).decrypt(token.encode("ascii"))
    except InvalidToken as exc:
        raise CryptoError(
            "SMTP password could not be decrypted. "
            "The Fernet key may have been rotated; "
            "re-enter the password in Settings → SMTP."
        ) from exc
    except (ValueError, TypeError) as exc:
        raise CryptoError(f"decryption failed: {exc}") from exc
    return plaintext.decode("utf-8")
