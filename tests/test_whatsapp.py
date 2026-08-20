from __future__ import annotations

from app.notifications.whatsapp import MetaWhatsAppProvider, WhatsAppSettings


class _Response:
    ok = True
    status_code = 200

    @staticmethod
    def json():
        return {"messages": [{"id": "wamid.test"}]}


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
    assert captured["json"]["to"] == "212600000000"
    assert captured["json"]["template"]["name"] == "job_digest"
    assert captured["json"]["template"]["components"][0]["parameters"][0]["text"] == "New opportunity"


def test_unconfigured_provider_does_not_send():
    assert not MetaWhatsAppProvider(WhatsAppSettings()).send("No delivery")


def test_meta_whatsapp_normalizes_template_parameter_whitespace():
    captured = {}

    def post(_url, **kwargs):
        captured.update(kwargs)
        return _Response()

    provider = MetaWhatsAppProvider(WhatsAppSettings(
        access_token="secret-token", phone_number_id="id", recipient="212600000000",
        template_name="job_digest"), post=post)
    assert provider.send("Run complete\nJobs: 1\tErrors: 0")
    text = captured["json"]["template"]["components"][0]["parameters"][0]["text"]
    assert text == "Run complete Jobs: 1 Errors: 0"


def test_meta_whatsapp_normalizes_e164_recipient():
    captured = {}

    def post(_url, **kwargs):
        captured.update(kwargs)
        return _Response()

    provider = MetaWhatsAppProvider(WhatsAppSettings(
        access_token="secret-token", phone_number_id="id", recipient="+212 600-000-000",
        template_name="job_digest"), post=post)
    assert provider.send("test")
    assert captured["json"]["to"] == "212600000000"
