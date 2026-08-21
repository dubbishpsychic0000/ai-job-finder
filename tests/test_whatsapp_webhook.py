from __future__ import annotations

import app.api.main as api


def test_whatsapp_webhook_verify(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr("app.config.get_settings", lambda: get_settings().model_copy(update={
        "whatsapp_webhook_verify_token": "verify-me"}))
    assert api.verify_whatsapp_webhook("subscribe", "verify-me", "challenge").body == b"challenge"
