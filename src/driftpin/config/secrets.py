"""Secret storage: OS keyring first, Fernet-encrypted file as fallback.

Environment variables always take precedence so CI can override without
touching the local encrypted store. Secrets are never logged and never
written into any run artifact.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
from pathlib import Path

import keyring
from cryptography.fernet import Fernet
from keyring.errors import KeyringError

_SERVICE_NAME = "driftpin"
_KEY_FILE_NAME = ".secret_key"
_STORE_FILE_NAME = "secrets.enc"


class SecretStore:
    """Reads and writes provider API keys, preferring the OS keyring.

    The OS keyring has no notion of "project" — it's one flat namespace per
    (service, username) pair for the whole machine. Keyring entries are
    scoped by hashing the resolved `.driftpin/` path into the lookup key, so
    two Driftpin projects with different provider credentials on the same
    machine don't silently overwrite each other. `get()` still falls back to
    the pre-scoping unscoped key name for one release, so a credential
    stored before this existed keeps resolving.
    """

    def __init__(self, driftpin_dir: Path) -> None:
        self._driftpin_dir = driftpin_dir

    def _project_id(self) -> str:
        return hashlib.sha256(str(self._driftpin_dir.resolve()).encode("utf-8")).hexdigest()[:16]

    def _scoped_key(self, key: str) -> str:
        return f"{self._project_id()}:{key}"

    def get(self, key: str, env_var: str | None = None) -> str | None:
        if env_var and (value := os.environ.get(env_var)):
            return value

        try:
            value = keyring.get_password(_SERVICE_NAME, self._scoped_key(key))
            if value is not None:
                return value
            legacy_value = keyring.get_password(_SERVICE_NAME, key)
            if legacy_value is not None:
                return legacy_value
        except KeyringError:
            pass

        return self._get_from_file(key)

    def set(self, key: str, value: str) -> None:
        try:
            keyring.set_password(_SERVICE_NAME, self._scoped_key(key), value)
            return
        except KeyringError:
            pass
        self._set_in_file(key, value)

    def _fernet_key_path(self) -> Path:
        return self._driftpin_dir / _KEY_FILE_NAME

    def _store_path(self) -> Path:
        return self._driftpin_dir / _STORE_FILE_NAME

    def _load_or_create_fernet(self) -> Fernet:
        key_path = self._fernet_key_path()
        if key_path.exists():
            return Fernet(key_path.read_bytes())

        self._driftpin_dir.mkdir(parents=True, exist_ok=True)
        new_key = Fernet.generate_key()
        key_path.write_bytes(new_key)
        with contextlib.suppress(OSError):  # best-effort on platforms without POSIX bits
            key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return Fernet(new_key)

    def _get_from_file(self, key: str) -> str | None:
        store_path = self._store_path()
        if not store_path.exists():
            return None
        fernet = self._load_or_create_fernet()
        for line in store_path.read_text(encoding="utf-8").splitlines():
            stored_key, _, token = line.partition("=")
            if stored_key == key:
                return fernet.decrypt(token.encode("utf-8")).decode("utf-8")
        return None

    def _set_in_file(self, key: str, value: str) -> None:
        fernet = self._load_or_create_fernet()
        store_path = self._store_path()
        entries: dict[str, str] = {}
        if store_path.exists():
            for line in store_path.read_text(encoding="utf-8").splitlines():
                stored_key, _, token = line.partition("=")
                if stored_key and stored_key != key:
                    entries[stored_key] = token

        entries[key] = fernet.encrypt(value.encode("utf-8")).decode("utf-8")
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_text(
            "\n".join(f"{k}={v}" for k, v in entries.items()) + "\n", encoding="utf-8"
        )
