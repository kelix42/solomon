"""Tests for the sensitivity filter."""
from solomon.ingestion.sensitivity_filter import scrub


def test_ssn_is_redacted():
    r = scrub("Customer SSN is 123-45-6789, please verify.")
    assert "[REDACTED-SSN]" in r.redacted_text
    assert "123-45-6789" not in r.redacted_text
    assert "SSN" in r.matches


def test_email_is_redacted():
    r = scrub("Contact alice@example.com about the contract")
    assert "[REDACTED-EMAIL]" in r.redacted_text
    assert "alice@example.com" not in r.redacted_text


def test_phone_is_redacted():
    r = scrub("Call me at 555-867-5309 tomorrow")
    assert "[REDACTED-PHONE]" in r.redacted_text
    assert "555-867-5309" not in r.redacted_text


def test_flagged_document_returns_empty():
    r = scrub("Anything at all here", document_flagged_sensitive=True)
    assert r.skip_document is True
    assert r.redacted_text == ""
    assert "FLAGGED_SENSITIVE" in r.matches


def test_clean_text_passes_through():
    r = scrub("This text has no sensitive material.")
    assert r.matches == []
    assert "no sensitive material" in r.redacted_text


def test_empty_string_is_safe():
    r = scrub("")
    assert r.redacted_text == ""
    assert r.matches == []
