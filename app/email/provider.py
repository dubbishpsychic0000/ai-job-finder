"""Email providers — thin transport layer.

  log     -> print JSON to stdout (default, zero setup, safe)
  smtp    -> real SMTP via smtplib
  gmail   -> Gmail API client (requires google-auth credentials)
"""
from __future__ import annotations

import json
import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from app.config import RunnerSettings

logger = logging.getLogger(__name__)


EMAIL_MODES = ("dry_run", "draft", "live")
GMAIL_REAUTH_REQUIRED = "Gmail authorization was rejected; re-authorize before retrying."
GMAIL_DRAFT_OUTCOME_UNCERTAIN = "Gmail draft outcome is uncertain; recovery required."
GMAIL_DRAFT_NOT_ATTEMPTED = "Gmail draft was not attempted."


@dataclass(frozen=True)
class DraftReconciliation:
    status: str
    draft_id: str = ""
    matches: int = 0


class ProviderError(Exception):
    pass


def create_draft(settings, *, to: str, subject: str, body: str,
                 attachments: list[str] | None = None,
                 attempt_key: str = "") -> tuple[bool, str, str]:
    """Create a Gmail Draft and classify failures around the irreversible API call.

    The custom marker is an app-defined reconciliation hint, not a Gmail
    idempotency guarantee. Recovery must fail closed if Gmail omits it.
    """
    if not settings.enable_email:
        return False, "", "email sending disabled (ENABLE_EMAIL=false)"
    from app.email import gmail_oauth

    try:
        service = gmail_oauth.authenticated_service(settings)
    except gmail_oauth.OAuthError:
        logger.warning("Gmail draft was not attempted because authorization is required")
        return False, "", GMAIL_REAUTH_REQUIRED
    except Exception as exc:
        logger.warning("Gmail draft was not attempted; service setup failed (%s)", type(exc).__name__)
        return False, "", f"{GMAIL_DRAFT_NOT_ATTEMPTED} Service setup failed ({type(exc).__name__})."

    try:
        msg = _build_mime(
            to, subject, body, attachments or [], draft_attempt_key=attempt_key,
        )
    except Exception as exc:
        logger.warning("Gmail draft was not attempted; message preparation failed (%s)",
                       type(exc).__name__)
        return False, "", f"{GMAIL_DRAFT_NOT_ATTEMPTED} Message preparation failed ({type(exc).__name__})."

    try:
        created = (service.users().drafts()
                   .create(userId="me", body={"message": {"raw": msg}}).execute())
        draft_id = str(created.get("id", "")).strip()
    except Exception as exc:
        logger.warning("Gmail draft request outcome is uncertain (%s)", type(exc).__name__)
        return False, "", GMAIL_DRAFT_OUTCOME_UNCERTAIN
    if not draft_id:
        logger.warning("Gmail draft response lacked an ID; outcome is uncertain")
        return False, "", GMAIL_DRAFT_OUTCOME_UNCERTAIN
    return True, draft_id, ""


def reconcile_draft(settings, attempt_key: str, *, max_drafts: int = 500) -> DraftReconciliation:
    """Find drafts with this marker; malformed/partial scans are inconclusive."""
    if not settings.enable_email or settings.email_mode != "draft":
        return DraftReconciliation("unavailable")
    if not attempt_key or max_drafts <= 0:
        return DraftReconciliation("unavailable")
    try:
        from app.email import gmail_oauth

        service = gmail_oauth.authenticated_service(settings)
        matches: list[str] = []
        page_token = None
        seen_page_tokens: set[str] = set()
        seen_draft_ids: set[str] = set()
        scanned = 0
        while True:
            page_size = min(100, max_drafts - scanned)
            if page_size <= 0:
                return DraftReconciliation("incomplete", matches=len(matches))
            response = service.users().drafts().list(
                userId="me", maxResults=page_size, pageToken=page_token,
            ).execute()
            if not isinstance(response, dict):
                return DraftReconciliation("unavailable", matches=len(matches))
            raw_next_page_token = response.get("nextPageToken")
            if raw_next_page_token is not None and not isinstance(
                raw_next_page_token, str,
            ):
                return DraftReconciliation("unavailable", matches=len(matches))
            drafts = response.get("drafts", [])
            if "drafts" not in response and raw_next_page_token:
                return DraftReconciliation("unavailable", matches=len(matches))
            if not isinstance(drafts, list) or len(drafts) > page_size:
                return DraftReconciliation("unavailable", matches=len(matches))
            scanned += len(drafts)
            for draft in drafts:
                if not isinstance(draft, dict):
                    return DraftReconciliation("unavailable", matches=len(matches))
                draft_id = str(draft.get("id") or "")
                if not draft_id or draft_id in seen_draft_ids:
                    return DraftReconciliation("unavailable", matches=len(matches))
                seen_draft_ids.add(draft_id)
                full = service.users().drafts().get(
                    userId="me", id=draft_id, format="full",
                ).execute()
                if (
                    not isinstance(full, dict)
                    or str(full.get("id") or "") != draft_id
                    or not isinstance(full.get("message"), dict)
                ):
                    return DraftReconciliation("unavailable", matches=len(matches))
                message = full["message"]
                if not isinstance(message.get("payload"), dict):
                    return DraftReconciliation("unavailable", matches=len(matches))
                payload = message["payload"]
                if not isinstance(payload.get("headers"), list):
                    return DraftReconciliation("unavailable", matches=len(matches))
                if any(
                    not isinstance(header, dict)
                    or not header.get("name")
                    or "value" not in header
                    for header in payload["headers"]
                ):
                    return DraftReconciliation("unavailable", matches=len(matches))
                headers = {
                    str(header.get("name", "")).casefold(): str(header.get("value", ""))
                    for header in payload["headers"]
                    if isinstance(header, dict)
                }
                if headers.get("x-wca-draft-attempt") == attempt_key:
                    matches.append(str(full.get("id") or draft_id))
            next_page_token = raw_next_page_token
            if next_page_token is not None:
                if not next_page_token or next_page_token in seen_page_tokens:
                    return DraftReconciliation("incomplete", matches=len(matches))
                seen_page_tokens.add(next_page_token)
            page_token = next_page_token
            if not page_token:
                break
        if len(matches) == 1:
            return DraftReconciliation("found", draft_id=matches[0], matches=1)
        if len(matches) > 1:
            return DraftReconciliation("multiple", matches=len(matches))
        return DraftReconciliation("not_found")
    except Exception as exc:
        logger.warning("Gmail draft reconciliation unavailable (%s)", type(exc).__name__)
        return DraftReconciliation("unavailable")


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


def _build_mime(to, subject, body, attachments, *, draft_attempt_key: str = "") -> str:
    import base64

    m = EmailMessage()
    m["To"] = to
    m["Subject"] = subject
    if draft_attempt_key:
        m["X-WCA-Draft-Attempt"] = draft_attempt_key
    m.set_content(body)
    for path in attachments:
        p = Path(path)
        if p.exists():
            m.add_attachment(p.read_bytes(), maintype="application", subtype="pdf", filename=p.name)
    return base64.urlsafe_b64encode(m.as_bytes()).decode("utf-8")
