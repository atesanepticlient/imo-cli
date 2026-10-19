"""Client-side authentication for the imo command.

Tokens never go in the project directory.  On macOS and Windows `keyring`
uses the native Keychain / Credential Manager; a missing secure backend is an
intentional error rather than falling back to a plaintext file.
"""

from __future__ import annotations

import json
import os
from getpass import getpass
from typing import Any

import httpx
import keyring
import typer
from keyring.errors import KeyringError
from rich.console import Console

SERVICE_NAME = "imo-cli-auth"
ACCOUNT_NAME = "default"
DEFAULT_AUTH_URL = "http://127.0.0.1:8765"
REQUEST_TIMEOUT = 12.0


class AuthenticationError(RuntimeError):
    """The remote server did not authorize this installation."""


def auth_url() -> str:
    return os.getenv("IMO_AUTH_URL", DEFAULT_AUTH_URL).rstrip("/")


def _load_credential() -> dict[str, str] | None:
    try:
        encoded = keyring.get_password(SERVICE_NAME, ACCOUNT_NAME)
    except KeyringError as exc:
        raise AuthenticationError(
            "A secure OS credential vault is required. On macOS use Keychain; "
            "on Windows use Credential Manager."
        ) from exc
    if not encoded:
        return None
    try:
        data = json.loads(encoded)
        if isinstance(data.get("token"), str) and isinstance(data.get("url"), str):
            return data
    except (TypeError, ValueError):
        pass
    return None


def _save_credential(token: str, url: str) -> None:
    try:
        keyring.set_password(SERVICE_NAME, ACCOUNT_NAME, json.dumps({"token": token, "url": url}))
    except KeyringError as exc:
        raise AuthenticationError(
            "Could not save the token in the OS credential vault. "
            "No credentials were written to disk."
        ) from exc


def _request(method: str, endpoint: str, *, token: str | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        response = httpx.request(
            method, f"{auth_url()}{endpoint}", headers=headers, json=body, timeout=REQUEST_TIMEOUT
        )
    except httpx.HTTPError as exc:
        raise AuthenticationError(
            f"Could not reach the authentication service at {auth_url()}. "
            "Check IMO_AUTH_URL or start `imo admin`."
        ) from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {"detail": response.text or "Unexpected server response"}
    if response.is_error:
        raise AuthenticationError(str(payload.get("detail", "Authentication rejected")))
    return payload


def login(console: Console) -> None:
    """Authenticate once with an administrator-issued key."""
    credential = _load_credential()
    if credential:
        try:
            _request("POST", "/api/auth/validate", token=credential["token"])
            console.print("[green]Already logged in. Your saved secure token is valid.[/green]")
            return
        except AuthenticationError:
            # A rejected token is deliberately removed before requesting a new key.
            try:
                keyring.delete_password(SERVICE_NAME, ACCOUNT_NAME)
            except KeyringError:
                pass

    key = getpass("Login key: ").strip()
    if not key:
        raise typer.Exit(1)
    result = _request("POST", "/api/auth/login", body={"key": key})
    token = result.get("token")
    if not isinstance(token, str):
        raise AuthenticationError("The server did not return a usable authentication token.")
    _save_credential(token, auth_url())
    console.print("[green]Login successful. The token is stored in your OS credential vault.[/green]")


def require_auth() -> None:
    """Validate the saved token and its observed IP before a protected command."""
    credential = _load_credential()
    if not credential:
        raise AuthenticationError("Not logged in. Run `imo login` first.")
    _request("POST", "/api/auth/validate", token=credential["token"])


def logout(console: Console) -> None:
    try:
        keyring.delete_password(SERVICE_NAME, ACCOUNT_NAME)
    except KeyringError as exc:
        raise AuthenticationError("Could not remove the saved credential from the OS vault.") from exc
    console.print("[green]Logged out on this computer.[/green]")
