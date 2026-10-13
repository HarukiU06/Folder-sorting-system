"""The Claude API key, kept in the Windows Credential Manager (via ``keyring``).

It is never written to AutoShelf's settings, database, logs or any file.
"""

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

SERVICE = "AutoShelf"
USERNAME = "claude-api-key"


class SecretError(Exception):
    """The key store couldn't be used."""


def get_api_key() -> str | None:
    """The saved Claude API key, if any."""
    try:
        return keyring.get_password(SERVICE, USERNAME)
    except KeyringError as exc:
        raise SecretError(f"Couldn't read the API key from the credential store: {exc}") from exc


def set_api_key(key: str) -> None:
    """Save (or replace) the Claude API key."""
    key = key.strip()
    if not key:
        raise SecretError("The API key is empty")
    try:
        keyring.set_password(SERVICE, USERNAME, key)
    except KeyringError as exc:
        raise SecretError(f"Couldn't save the API key: {exc}") from exc


def delete_api_key() -> None:
    """Forget the saved key (nothing happens if there is none)."""
    try:
        keyring.delete_password(SERVICE, USERNAME)
    except PasswordDeleteError:
        pass
    except KeyringError as exc:
        raise SecretError(f"Couldn't remove the API key: {exc}") from exc


def masked(key: str | None) -> str:
    """A key as shown in the app: only its last four characters."""
    return f"saved (...{key[-4:]})" if key else "not saved"
