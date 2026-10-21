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
from pathlib import Path

import requests
from dotenv import dotenv_values

logger = logging.getLogger(__name__)
_PHONE_ID = re.compile(r"^[0-9]{5,32}$")
_RECIPIENT = re.compile(r"^\+?[1-9][0-9]{6,14}$")
_TEMPLATE = re.compile(r"^[a-z0-9_]{1,512}$")
_GRAPH_VERSION = re.compile(r"^v[0-9]+\.[0-9]+$")


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
        root_env = Path(__file__).resolve().parents[2] / ".env"
        file_values = dotenv_values(root_env)

        def value(name: str, default: str = "") -> str:
            return os.getenv(name, str(file_values.get(name) or default)).strip()

        return cls(
            access_token=value("WHATSAPP_ACCESS_TOKEN"),
            phone_number_id=value("WHATSAPP_PHONE_NUMBER_ID"),
            recipient=value("WHATSAPP_RECIPIENT"),
            template_name=value("WHATSAPP_TEMPLATE_NAME"),
            template_language=value("WHATSAPP_TEMPLATE_LANGUAGE", "en"),
            graph_api_version=value("WHATSAPP_GRAPH_API_VERSION", "v23.0"),
        )

    @property
    def configured(self) -> bool:
        return bool(self.access_token and self.phone_number_id and self.recipient and self.template_name)

    def validation_errors(self) -> list[str]:
        errors = []
        if not self.access_token:
            errors.append("WHATSAPP_ACCESS_TOKEN is missing")
        elif len(self.access_token) < 16 or any(char.isspace() for char in self.access_token):
            errors.append("WHATSAPP_ACCESS_TOKEN has an invalid format")
        if not self.phone_number_id:
            errors.append("WHATSAPP_PHONE_NUMBER_ID is missing")
        elif not _PHONE_ID.fullmatch(self.phone_number_id):
            errors.append("WHATSAPP_PHONE_NUMBER_ID must contain 5-32 digits")
        if not self.recipient:
            errors.append("WHATSAPP_RECIPIENT is missing")
        elif not _RECIPIENT.fullmatch(re.sub(r"[ ()-]", "", self.recipient)):
            errors.append("WHATSAPP_RECIPIENT must be an international number (E.164)")
        if not self.template_name:
            errors.append("WHATSAPP_TEMPLATE_NAME is missing")
        elif not _TEMPLATE.fullmatch(self.template_name):
            errors.append("WHATSAPP_TEMPLATE_NAME has an invalid format")
        if not _GRAPH_VERSION.fullmatch(self.graph_api_version):
            errors.append("WHATSAPP_GRAPH_API_VERSION has an invalid format")
        if not self.template_language or not re.fullmatch(
            r"[a-z]{2,3}(?:_[A-Z]{2})?", self.template_language,
        ):
            errors.append("WHATSAPP_TEMPLATE_LANGUAGE has an invalid format")
        return errors


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
                # Meta accepting a request is not evidence of handset delivery.
                # The message id is an auditable reference without logging the
                # recipient or any credential.
                try:
                    message_id = (response.json().get("messages") or [{}])[0].get("id", "")
                except (AttributeError, ValueError):
                    message_id = ""
                self.last_message_id = message_id
                logger.info("WhatsApp request accepted by Meta; handset delivery unverified%s",
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
