"""Unit tests for privacyfence.pii_detector -- the local, regex-based PII
scan gate.py runs over approval-popup content before showing it.

The one invariant that matters most: scan_text/detect_categories never
return the matched substring itself, only category labels -- the detector
must not become a new place PII gets copied to (e.g. into logs or the
audit trail). Everything else here is about keeping the heuristic patterns
useful (catch real formats) without being so loose they flag this app's own
routine identifiers (Drive file IDs, Jira/Confluence keys, spreadsheet
values) as PII on every popup.
"""
from __future__ import annotations

from privacyfence import pii_detector
from privacyfence.pii_detector import (
    _iban_valid,
    _luhn_valid,
    detect_categories,
    detect_pii_categories,
    init_pii_detection,
    is_pii_detection_enabled,
    scan_text,
    set_pii_detection_changed_listener,
    set_pii_detection_enabled,
)


class TestLuhnValid:
    def test_valid_card_number(self):
        assert _luhn_valid("4111 1111 1111 1111") is True

    def test_invalid_checksum(self):
        assert _luhn_valid("1234567890123456") is False

    def test_too_short_is_rejected_before_checksum(self):
        assert _luhn_valid("1234") is False

    def test_too_long_is_rejected_before_checksum(self):
        assert _luhn_valid("1" * 20) is False


class TestIbanValid:
    def test_valid_iban(self):
        assert _iban_valid("DE89370400440532013000") is True

    def test_valid_iban_with_spaces(self):
        assert _iban_valid("DE89 3704 0044 0532 0130 00") is True

    def test_too_short_is_rejected_before_checksum(self):
        assert _iban_valid("DE8937040044") is False

    def test_wrong_shape_is_rejected_before_checksum(self):
        # Doesn't start with 2 letters + 2 digits.
        assert _iban_valid("123456789012345") is False

    def test_right_shape_wrong_checksum(self):
        assert _iban_valid("DE00370400440532013000") is False


class TestNoFalsePositivesOnPlainText:
    def test_empty_string(self):
        assert detect_categories("") == []

    def test_plain_sentence(self):
        assert detect_categories("Hey, let's grab lunch tomorrow at noon.") == []

    def test_drive_file_id_is_not_flagged(self):
        text = "drive file id 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
        assert detect_categories(text) == []

    def test_jira_issue_key_is_not_flagged(self):
        assert detect_categories("See ticket MYPROJ-1234 for details.") == []

    def test_random_16_digit_non_luhn_number_is_not_a_credit_card(self):
        assert detect_categories("Order number 1234567890123456 was placed.") == []

    def test_random_alnum_that_looks_like_iban_prefix_fails_checksum(self):
        assert detect_categories("The Drive file ID is DA12ABCDEFGHIJKLMNOPQRS.") == []


class TestLanguageAgnosticPatterns:
    def test_email_address(self):
        assert detect_categories("Reach me at jane.doe@example.com please.") == ["Email address"]

    def test_valid_iban_passes_checksum(self):
        assert detect_categories("Wire to DE89370400440532013000 today.") == ["IBAN (bank account number)"]

    def test_valid_credit_card_passes_luhn(self):
        assert detect_categories("Card: 4111 1111 1111 1111 exp 10/29") == ["Credit card number"]

    def test_international_phone_number(self):
        assert detect_categories("Call me at +36 20 123 4567 anytime.") == [
            "Phone number (international format)"
        ]

    def test_local_format_phone_without_country_code_is_not_flagged(self):
        # Deliberately conservative: bare local-format numbers are too easy
        # to confuse with other digit sequences without a leading '+'.
        assert detect_categories("Call the office at 06 1 234 5678.") == []

    def test_ip_address(self):
        assert detect_categories("The server is reachable at 192.168.1.100.") == ["IP address"]


class TestHungarianPatterns:
    def test_taj_number_with_label(self):
        assert detect_categories("TAJ sz\u00e1m: 123 456 789") == ["Hungarian TAJ number (social security)"]

    def test_bare_nine_digit_number_without_label_is_not_flagged(self):
        assert detect_categories("A jelent\u00e9s 123 456 789 sorban tal\u00e1lhat\u00f3.") == []

    def test_ado_azonosito_jel(self):
        assert detect_categories("Ad\u00f3azonos\u00edt\u00f3 jel: 8123456789") == ["Hungarian tax ID (ad\u00f3azonos\u00edt\u00f3 jel)"]

    def test_ten_digits_starting_with_8_but_more_digits_follow_is_not_flagged(self):
        # \b8\d{9}\b requires a word boundary right after the 10th digit.
        assert detect_categories("A rendsz\u00e1m 8123456789X nem ad\u00f3azonos\u00edt\u00f3.") == []

    def test_id_card_number(self):
        assert detect_categories("Szem\u00e9lyi igazolv\u00e1ny sz\u00e1ma: 123456AB") == ["Hungarian ID card number"]

    def test_personal_data_label_with_suffix(self):
        # Hungarian is agglutinative -- "d\u00e1tum\u00e1t"/"lakc\u00edm\u00e9t" carry a
        # possessive/case suffix glued directly onto the base word.
        assert detect_categories("K\u00e9rem adja meg a sz\u00fclet\u00e9si d\u00e1tum\u00e1t \u00e9s lakc\u00edm\u00e9t.") == [
            "Hungarian personal data reference"
        ]

    def test_base_form_label(self):
        assert detect_categories("Sz\u00fclet\u00e9si d\u00e1tum: 1990.01.02, Lakc\u00edm: Budapest") == [
            "Hungarian personal data reference"
        ]


class TestGermanPatterns:
    def test_tax_id_with_label(self):
        assert detect_categories("Steuer-IdNr. 65 929 970 489") == ["German tax ID (Steuer-IdNr.)"]

    def test_social_insurance_number(self):
        assert detect_categories("Versicherungsnummer 65100794J003") == ["German social insurance number"]

    def test_personal_data_label_with_inflection(self):
        assert detect_categories(
            "Bitte Geburtsdatum und Anschrift angeben, Geburtsorts ebenfalls."
        ) == ["German personal data reference"]


class TestEnglishPatterns:
    def test_us_social_security_number(self):
        assert detect_categories("His SSN is 123-45-6789 on file.") == ["US Social Security Number"]

    def test_uk_national_insurance_number(self):
        assert detect_categories("NI number: AB123456C") == ["UK National Insurance number"]

    def test_lowercase_ni_shape_is_not_flagged(self):
        # The real format is uppercase-only; case-insensitive matching here
        # would flag ordinary lowercase text that happens to share the shape.
        assert detect_categories("ni number: ab123456c") == []

    def test_personal_data_label(self):
        assert detect_categories("Please provide your date of birth and passport number.") == [
            "English personal data reference"
        ]


class TestMultipleCategoriesAndDeduplication:
    def test_multiple_distinct_categories_all_reported_sorted(self):
        text = "Email jane@example.com or call +36 20 123 4567."
        assert detect_categories(text) == [
            "Email address",
            "Phone number (international format)",
        ]

    def test_repeated_matches_of_same_category_deduplicated(self):
        text = "Contact jane@example.com or john@example.com."
        assert detect_categories(text) == ["Email address"]


class TestScanTextNeverCarriesMatchedSubstring:
    def test_pii_match_objects_have_no_text_field(self):
        matches = scan_text("Reach me at jane.doe@example.com please.")
        assert len(matches) == 1
        assert matches[0].category == "Email address"
        assert not hasattr(matches[0], "text")
        assert not hasattr(matches[0], "matched_text")
        assert not hasattr(matches[0], "value")


class TestEnabledToggle:
    def test_enabled_by_default(self):
        assert is_pii_detection_enabled() is True

    def test_init_pii_detection_sets_initial_state(self):
        init_pii_detection(False)
        assert is_pii_detection_enabled() is False

    def test_set_pii_detection_enabled_updates_state(self):
        set_pii_detection_enabled(False)
        assert is_pii_detection_enabled() is False
        set_pii_detection_enabled(True)
        assert is_pii_detection_enabled() is True

    def test_detect_pii_categories_returns_empty_when_disabled(self):
        set_pii_detection_enabled(False)
        assert detect_pii_categories("jane.doe@example.com") == []

    def test_detect_pii_categories_scans_when_enabled(self):
        set_pii_detection_enabled(True)
        assert detect_pii_categories("jane.doe@example.com") == ["Email address"]

    def test_changed_listener_fires_on_toggle(self):
        calls = []
        set_pii_detection_changed_listener(lambda: calls.append(1))

        set_pii_detection_enabled(False)

        assert calls == [1]

    def test_changed_listener_not_required(self):
        set_pii_detection_changed_listener(None)
        # Must not raise even with no listener registered.
        set_pii_detection_enabled(False)
        set_pii_detection_enabled(True)
