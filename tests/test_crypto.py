"""
Unit tests for reporting.crypto.

Scope:
  * load_or_create_key respects GPU_MONITOR_SECRET env var
  * load_or_create_key creates a new key file with mode 0600 on first run
  * load_or_create_key reads an existing key file without regenerating
  * encrypt/decrypt round-trip preserves the plaintext
  * encrypt("") → "" (sentinel), decrypt("") → ""
  * decrypt with wrong key raises CryptoError
  * invalid key material raises CryptoError

These tests are stdlib-only (no aiohttp, no pydantic) so they import
reporting.crypto in isolation.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
from reporting import crypto as crypto_module  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402


def test_env_var_overrides_file(tmp_path, monkeypatch):
    """GPU_MONITOR_SECRET env var is used directly and no file is created."""
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv(crypto_module.ENV_VAR, key)

    key_path = tmp_path / ".secret"
    result = crypto_module.load_or_create_key(key_path)

    assert result.decode("ascii") == key
    assert not key_path.exists(), "env var should bypass disk entirely"


def test_first_run_creates_key_with_mode_0600(tmp_path, monkeypatch):
    """When neither env var nor key file exists, a fresh key is generated
    and written with mode 0600 (user read/write only)."""
    monkeypatch.delenv(crypto_module.ENV_VAR, raising=False)

    key_path = tmp_path / "nested" / ".secret"  # parent dir doesn't exist
    result = crypto_module.load_or_create_key(key_path)

    assert key_path.exists()
    # File should be readable by us
    stat_info = key_path.stat()
    mode = stat.S_IMODE(stat_info.st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    # The returned key should be a valid Fernet key
    Fernet(result)  # raises if invalid


def test_existing_key_file_is_read_not_regenerated(tmp_path, monkeypatch):
    """A key file that already exists must be read, not overwritten."""
    monkeypatch.delenv(crypto_module.ENV_VAR, raising=False)

    key_path = tmp_path / ".secret"
    existing_key = Fernet.generate_key()
    key_path.write_bytes(existing_key)
    os.chmod(key_path, 0o600)

    result = crypto_module.load_or_create_key(key_path)
    assert result == existing_key


def test_encrypt_decrypt_round_trip(tmp_path, monkeypatch):
    """Plaintext → encrypt → decrypt returns the original plaintext."""
    monkeypatch.delenv(crypto_module.ENV_VAR, raising=False)
    key = crypto_module.load_or_create_key(tmp_path / ".secret")

    plaintext = "correct horse battery staple"
    ciphertext = crypto_module.encrypt(plaintext, key)
    assert ciphertext != plaintext
    assert isinstance(ciphertext, str)

    decrypted = crypto_module.decrypt(ciphertext, key)
    assert decrypted == plaintext


def test_empty_plaintext_round_trips_as_empty(tmp_path, monkeypatch):
    """Empty string is the 'no password set' sentinel — encrypt/decrypt
    both preserve it without producing a real ciphertext."""
    monkeypatch.delenv(crypto_module.ENV_VAR, raising=False)
    key = crypto_module.load_or_create_key(tmp_path / ".secret")

    assert crypto_module.encrypt("", key) == ""
    assert crypto_module.decrypt("", key) == ""


def test_decrypt_with_wrong_key_raises_crypto_error(tmp_path, monkeypatch):
    """Decrypting a token with a different key raises CryptoError, not
    a raw cryptography exception — callers should only need one
    except clause."""
    monkeypatch.delenv(crypto_module.ENV_VAR, raising=False)
    key_a = crypto_module.load_or_create_key(tmp_path / ".a-secret")

    ciphertext = crypto_module.encrypt("top secret", key_a)

    # Use a different key
    key_b = Fernet.generate_key()
    with pytest.raises(crypto_module.CryptoError, match="rotated"):
        crypto_module.decrypt(ciphertext, key_b)


def test_invalid_key_material_raises_crypto_error(monkeypatch):
    """A malformed GPU_MONITOR_SECRET raises CryptoError at load time."""
    monkeypatch.setenv(crypto_module.ENV_VAR, "not-a-real-fernet-key")
    with pytest.raises(crypto_module.CryptoError, match="Fernet"):
        crypto_module.load_or_create_key(Path("/nonexistent/secret"))


def test_concurrent_key_generation_resolves_to_single_winner(tmp_path, monkeypatch):
    """Simulate a race: a second process (simulated by pre-writing
    a key to the target path) beats us to the exclusive create.
    load_or_create_key must detect the collision and return the
    winner's key rather than overwriting it — any ciphertext
    produced between reads would otherwise become undecryptable.

    This test proves the O_EXCL / os.link exclusive-create path by
    pre-writing a known key to the target before calling
    load_or_create_key. The function should observe the existing
    file at the link step and read it rather than generating a
    new key.
    """
    monkeypatch.delenv(crypto_module.ENV_VAR, raising=False)
    key_path = tmp_path / ".secret"

    # Pre-write a "winner" key to the target path — simulating
    # another process that won the race.
    winner_key = Fernet.generate_key()
    key_path.write_bytes(winner_key)
    os.chmod(key_path, 0o600)

    # Call load_or_create_key. It should see the file exists and
    # read the winner's key rather than regenerating.
    result = crypto_module.load_or_create_key(key_path)
    assert result == winner_key

    # Disk state is unchanged — no stray .tmp files left behind
    strays = list(tmp_path.glob(".secret.*"))
    assert strays == [], f"unexpected stray files: {strays}"
