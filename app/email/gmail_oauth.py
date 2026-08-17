"""Gmail OAuth — dual-scope (drafts + send) credential management.

Security invariants enforced here:
  * the ONLY scopes requested are `gmail.compose` (create drafts) and
    `gmail.send` (send); the agent can never read the user's inbox;
  * the OAuth client secret lives in `secrets/client_secret.json` and the refresh
    token in `secrets/gmail_token.json` — both git-ignored;
  * secrets and tokens are NEVER logged, printed, or passed to any LLM prompt;
  * token files get best-effort `0o600` permissions where the OS supports it.

Usage:
    wca gmail-auth          # interactive first-time authorization (one-time)
    (the application engine then reuses the stored refresh token)
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from app.config import RunnerSettings

logger = logging.getLogger(__name__)

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"  # draft creation
# One token must power BOTH live sends and the draft-overflow fallback, so the
# consent flow requests both scopes up front.
GMAIL_ALL_SCOPES = [GMAIL_COMPOSE_SCOPE, GMAIL_SEND_SCOPE]

_SECRET_FILENAME = "client_secret.json"
_TOKEN_FILENAME = "gmail_token.json"
_SECRETS_DIR = Path(__file__).resolve().parent.parent.parent / "secrets"


class OAuthError(Exception):
    """Gmail OAuth is not configured / not authorized yet."""


def secrets_dir() -> Path:
    return _SECRETS_DIR


def client_secret_path(settings: RunnerSettings) -> Path:
    if getattr(settings, "gmail_client_secret_path", ""):
        return Path(settings.gmail_client_secret_path)
    return _SECRETS_DIR / _SECRET_FILENAME


def token_path(settings: RunnerSettings) -> Path:
    if getattr(settings, "gmail_token_path", ""):
        return Path(settings.gmail_token_path)
    return _SECRETS_DIR / _TOKEN_FILENAME


def load_client_config(settings: RunnerSettings) -> dict:
    """Return the Google web-client config dict (never log its contents)."""
    path = client_secret_path(settings)
    if not path.exists():
        raise OAuthError(
            f"{path} missing. Put your OAuth client secret there (git-ignored), "
            "then run `wca gmail-auth`."
        )
    try:
        with path.open("r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise OAuthError(f"cannot read OAuth client secret: {exc}") from exc
    if "web" not in cfg:
        raise OAuthError(f"{path} is not a Google web OAuth client secret file")
    return cfg


def _restrict_permissions(path: Path) -> None:
    """Best-effort 0o600 on POSIX; no-op on Windows."""
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows / exotic fs
        pass


def required_scopes(settings) -> list[str]:
    """Minimal scopes for the active email mode.

    draft  -> gmail.compose (create Drafts, never sends them)
    live   -> gmail.send (send only)
    dry_run-> never touches Gmail; send scope is a harmless placeholder.
    """
    if getattr(settings, "email_mode", "draft") == "draft":
        return [GMAIL_COMPOSE_SCOPE]
    return [GMAIL_SEND_SCOPE]


def _credentials_from_file(path: Path):
    try:
        from google.oauth2.credentials import Credentials
    except ImportError as exc:  # pragma: no cover
        raise OAuthError(f"google-auth is not installed: {exc}") from exc

    if not path.exists():
        return None
    try:
        return Credentials.from_authorized_user_file(str(path))
    except (ValueError, OSError) as exc:
        logger.warning("stored Gmail token unreadable (%s); re-auth required", exc)
        return None


def get_credentials(settings: RunnerSettings):
    """Return usable credentials, refreshing the token if needed."""
    try:
        from google.auth.transport.requests import Request
    except ImportError as exc:  # pragma: no cover
        raise OAuthError(f"google-auth is not installed: {exc}") from exc

    creds = _credentials_from_file(token_path(settings))
    if creds is None:
        raise OAuthError(
            "Gmail not authorized yet — run `wca gmail-auth` once (token is stored "
            f"git-ignored at {token_path(settings)})."
        )
    needed = required_scopes(settings)
    granted = set(creds.scopes or [])
    if needed and not set(needed) <= granted:
        raise OAuthError(
            f"Gmail token lacks the {needed} scope(s) for the current email mode — "
            "re-run `wca gmail-auth` to re-authorize."
        )
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise OAuthError("Gmail credentials are invalid and not refreshable — run `wca gmail-auth` again.")
    return creds


def save_credentials(settings: RunnerSettings, creds) -> None:
    path = token_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(json.loads(creds.to_json()), fh)
    _restrict_permissions(path)
    logger.info("Gmail token stored at %s (git-ignored, scopes=%s)", path,
                ",".join(GMAIL_ALL_SCOPES))


def authorize(settings: RunnerSettings) -> str:
    """One-time interactive OAuth consent flow (scopes for the active mode).

    Uses a FIXED loopback port so the redirect URI is predictable and can be
    registered verbatim (Web clients require an exact match):
        redirect URI:  http://localhost:18320/
    """
    # Fixed port, kept unique to avoid colliding with other dev tools.
    OAUTH_PORT = 18320
    redirect_uri = f"http://localhost:{OAUTH_PORT}/"
    logger.warning(
        "Authorize this exact redirect URI in the Google Cloud Console "
        "(OAuth client) if you see redirect_uri_mismatch: %s", redirect_uri)
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover
        raise OAuthError(f"google-auth-oauthlib is not installed: {exc}") from exc

    client_config = load_client_config(settings)
    flow = InstalledAppFlow.from_client_config(
        client_config, scopes=GMAIL_ALL_SCOPES, redirect_uri=redirect_uri
    )
    creds = flow.run_local_server(port=OAUTH_PORT, prompt="consent")
    save_credentials(settings, creds)
    return token_path(settings).as_posix()

def authenticated_service(settings: RunnerSettings):
    """Build a Gmail API service authenticated with the send-only token."""
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover
        raise OAuthError(f"google-api-python-client is not installed: {exc}") from exc

    creds = get_credentials(settings)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)