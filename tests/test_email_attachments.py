from __future__ import annotations

from app.email.service import resolve_attachment


def _write_pdf(path):
    path.write_bytes(b"%PDF-1.7\nfixture")
    return path


def test_resolve_attachment_prefers_french_cv(tmp_path):
    french = _write_pdf(tmp_path / "CV_Omar_Benhamid_FR.pdf")
    _write_pdf(tmp_path / "CV_Omar_Benhamid_EN.pdf")

    assert resolve_attachment(tmp_path, "fr") == str(french)


def test_resolve_attachment_prefers_english_cv(tmp_path):
    _write_pdf(tmp_path / "CV_Omar_Benhamid_FR.pdf")
    english = _write_pdf(tmp_path / "CV_Omar_Benhamid_EN.pdf")

    assert resolve_attachment(tmp_path, "en") == str(english)


def test_resolve_attachment_uses_generic_pdf_when_language_cv_is_missing(tmp_path):
    generic = _write_pdf(tmp_path / "CV_Omar_Benhamid.pdf")

    assert resolve_attachment(tmp_path, "fr") == str(generic)
    assert resolve_attachment(tmp_path, "en") == str(generic)


def test_resolve_attachment_returns_missing_error_sentinel_when_no_cv_exists(tmp_path):
    assert resolve_attachment(tmp_path, "fr") == ""
    assert resolve_attachment(tmp_path, "en") == ""


def test_resolve_attachment_rejects_unreadable_or_unsupported_candidate(tmp_path):
    (tmp_path / "CV_Omar_Benhamid_FR.pdf").write_text(
        "not a PDF", encoding="utf-8",
    )
    (tmp_path / "CV_Omar_Benhamid.pdf").write_bytes(b"%PDF-1.7\nfixture")

    assert resolve_attachment(tmp_path, "fr") == str(
        tmp_path / "CV_Omar_Benhamid.pdf",
    )
