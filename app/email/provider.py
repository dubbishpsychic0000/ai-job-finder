"""Email providers — thin transport layer.

  log     -> print JSON to stdout (default, zero setup, safe)
  smtp    -> real SMTP via smtplib
  gmail   -> Gmail API client (requires google-auth credentials)
"""
from __future__ import annotations

import json
import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path

from app.config import RunnerSettings

logger = logging.getLogger(__name__)


EMAIL_MODES = ("dry_run", "draft", "live")


class ProviderError(Exception):
    pass


def create_draft(settings, *, to: str, subject: str, body: str,
                 attachments: list[str] | None = None) -> tuple[bool, str, str]:
    """Create a Gmail Draft (never sent). Returns (ok, draft_id, error)."""
    if not settings.enable_email:
        return False, "", "email sending disabled (ENABLE_EMAIL=false)"
    try:
        from app.email import gmail_oauth

        service = gmail_oauth.authenticated_service(settings)
        msg = _build_mime(to, subject, body, attachments or [])
        created = (service.users().drafts()
                   .create(userId="me", body={"message": {"raw": msg}}).execute())
        return True, created.get("id", "gmail_draft"), ""
    except Exception as exc:
        logger.exception("Gmail draft creation failed")
        return False, "", str(exc)


def send(settings: RunnerSettings, *, to: str, subject: str, body: str,
         attachments: list[str] | None = None) -> tuple[bool, str, str]:
    """Returns (ok, message_id_or_status, error)."""
    attachments = attachments or []
    if settings.email_provider == "log":
        record = {"to": to, "subject": subject, "body": body, "attachments": attachments}
        logger.info("EMAIL [dry-run/log]: %s", json.dumps(record, ensure_ascii=False)[:2000])
        return True, "log", ""

    if settings.email_provider == "smtp":
        return _send_smtp(settings, to, subject, body, attachments)

    if settings.email_provider == "gmail":
        return _send_gmail(settings, to, subject, body, attachments)

    raise ProviderError(f"unknown EMAIL_PROVIDER {settings.email_provider!r}")


def _send_smtp(settings, to, subject, body, attachments):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.email_from or settings.smtp_user
    msg["To"] = to
    msg.set_content(body)
    for path in attachments:
        p = Path(path)
        if p.exists():
            msg.add_attachment(p.read_bytes(), maintype="application", subtype="pdf", filename=p.name)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            server.starttls()
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
        return True, "smtp", ""
    except Exception as exc:
        logger.exception("SMTP send failed")
        return False, "", str(exc)


def _send_gmail(settings, to, subject, body, attachments):
    """Gmail API send via OAuth (gmail.send scope only).

    Refuses to send unless ENABLE_EMAIL=true (defense in depth: the engine
    already blocks earlier, this is the transport-level guard). Never logs
    tokens or client secrets.
    """
    if not settings.enable_email:
        return False, "", "email sending disabled (ENABLE_EMAIL=false)"
    try:
        from app.email import gmail_oauth

        service = gmail_oauth.authenticated_service(settings)
        msg = _build_mime(to, subject, body, attachments)
        sent = service.users().messages().send(userId="me", body={"raw": msg}).execute()
        return True, sent.get("id", "gmail"), ""
    except Exception as exc:
        logger.exception("Gmail send failed")
        return False, "", str(exc)


def _build_mime(to, subject, body, attachments) -> str:
    import base64

    m = EmailMessage()
    m["To"] = to
    m["Subject"] = subject
    m.set_content(body)
    for path in attachments:
        p = Path(path)
        if p.exists():
            m.add_attachment(p.read_bytes(), maintype="application", subtype="pdf", filename=p.name)
    return base64.urlsafe_b64encode(m.as_bytes()).decode("utf-8")
