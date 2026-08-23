"""Auditable Meta WhatsApp Cloud API transport for queued notifications.

Scheduled outbound notifications use an approved template with one body
placeholder. This avoids attempting free-form messages outside WhatsApp's
customer-service window.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WhatsAppSettings:
    access_token: str = ""
    phone_number_id: str = ""
    recipient: str = ""
    template_name: str = ""
    template_language: str = "en"
    graph_api_version: str = "v23.0"

    @classmethod
    def from_env(cls) -> WhatsAppSettings:
        return cls(
            access_token=os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip(),
            phone_number_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip(),
            recipient=os.getenv("WHATSAPP_RECIPIENT", "").strip(),
            template_name=os.getenv("WHATSAPP_TEMPLATE_NAME", "").strip(),
            template_language=os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "en").strip(),
            graph_api_version=os.getenv("WHATSAPP_GRAPH_API_VERSION", "v23.0").strip(),
        )

    @property
    def configured(self) -> bool:
        return bool(self.access_token and self.phone_number_id and self.recipient and self.template_name)


class MetaWhatsAppProvider:
    """Send rendered messages through a pre-approved ``{{1}}`` template."""

    def __init__(self, settings: WhatsAppSettings | None = None, post=requests.post):
        self.settings = settings or WhatsAppSettings.from_env()
        self._post = post
        self.last_message_id = ""

    @property
    def configured(self) -> bool:
        return self.settings.configured

    def send(self, message: str) -> bool:
        if not self.configured:
            logger.info("WhatsApp delivery not configured; notification remains queued")
            return False
        endpoint = (f"https://graph.facebook.com/{self.settings.graph_api_version}/"
                    f"{self.settings.phone_number_id}/messages")
        # Meta template text parameters reject newlines/tabs.  Keep summaries
        # readable while making every approved one-variable template valid.
        text = re.sub(r"\s+", " ", message).strip()[:1024]
        recipient = re.sub(r"\D", "", self.settings.recipient)
        payload = {
            "messaging_product": "whatsapp", "to": recipient, "type": "template",
            "template": {
                "name": self.settings.template_name,
                "language": {"code": self.settings.template_language},
                "components": [{"type": "body", "parameters": [
                    {"type": "text", "text": text}
                ]}],
            },
        }
        try:
            response = self._post(endpoint, headers={"Authorization": f"Bearer {self.settings.access_token}"},
                                  json=payload, timeout=20)
            if response.ok:
                # Meta accepting a request is distinct from handset delivery,
                # but its message id gives the operator an auditable reference
                # without logging the recipient or any credential.
                try:
                    message_id = (response.json().get("messages") or [{}])[0].get("id", "")
                except (AttributeError, ValueError):
                    message_id = ""
                self.last_message_id = message_id
                logger.info("WhatsApp accepted by Meta%s",
                            f" (message id {message_id})" if message_id else "")
                return True
            # Meta returns useful object/permission diagnostics here. It never
            # receives the token in its response; still truncate the detail so
            # logs remain safe and readable.
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            logger.warning("WhatsApp API rejected notification: HTTP %s: %s",
                           response.status_code, str(detail)[:800])
        except requests.RequestException as exc:
            logger.warning("WhatsApp notification failed: %s", exc)
        return False
