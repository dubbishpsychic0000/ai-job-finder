from __future__ import annotations

from app.notifications.whatsapp import MetaWhatsAppProvider, WhatsAppSettings


class _Response:
    ok = True
    status_code = 200


def test_meta_whatsapp_uses_template_body_parameter():
    captured = {}

    def post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _Response()

    provider = MetaWhatsAppProvider(WhatsAppSettings(
        access_token="secret-token", phone_number_id="1189875497552844",
        recipient="212600000000", template_name="job_digest"), post=post)
    assert provider.send("New opportunity")
    assert captured["url"].endswith("/1189875497552844/messages")
    assert captured["json"]["template"]["name"] == "job_digest"
    assert captured["json"]["template"]["components"][0]["parameters"][0]["text"] == "New opportunity"


def test_unconfigured_provider_does_not_send():
    assert not MetaWhatsAppProvider(WhatsAppSettings()).send("No delivery")
