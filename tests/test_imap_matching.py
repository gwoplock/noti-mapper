"""Matching rules against a corpus of real carrier subject lines.

The near-miss negatives matter more than the positives. A rule that fires on
"Out for delivery" twice a day is how this project gets uninstalled.
"""

import pytest

from imap_input.matching import Criteria, compile_problems, decode_subject, sender_address

CARRIER_SENDERS = ["amazon.com", "ups.com", "fedex.com", "usps.com"]
DELIVERED_PATTERNS = [
    r"^Delivered:",
    r"^Your package (was|has been) delivered",
    r"\bwas delivered\b",
]


def _criteria() -> Criteria:
    return Criteria(senders=CARRIER_SENDERS, subject_patterns=DELIVERED_PATTERNS)


# -- subjects that should fire ------------------------------------------------


@pytest.mark.parametrize(
    "subject",
    [
        "Delivered: your package from Amazon",
        "Delivered: 1 item",
        "Your package was delivered",
        "Your package has been delivered",
        "Update: your UPS package was delivered at 2:14 PM",
    ],
)
def test_delivery_subjects_match(subject: str) -> None:
    assert _criteria().subject_matches(subject) is True


# -- near misses that must not fire -------------------------------------------


@pytest.mark.parametrize(
    "subject",
    [
        "Out for delivery",
        "Your package is out for delivery",
        "Your order has shipped",
        "Shipment on the way",
        "Delivery attempted: nobody home",
        "We were unable to deliver your package",
        "Delivery exception: weather delay",
        "Save 20% on your next order",
        "Your Amazon.com order of 'Widget' has shipped",
        "Scheduled delivery: Tomorrow",
        "Action required: reschedule your delivery",
        "Re: Delivered: your package",  # a human replying is not the carrier
    ],
)
def test_near_miss_subjects_do_not_match(subject: str) -> None:
    assert _criteria().subject_matches(subject) is False


# -- the sender allowlist -----------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "auto-confirm@amazon.com",
        "mcinfo@ups.com",
        "noreply@mail.ups.com",
        "TrackingUpdates@fedex.com",
        "auto-reply@usps.com",
    ],
)
def test_carrier_addresses_are_allowed(address: str) -> None:
    assert _criteria().sender_allowed(address) is True


@pytest.mark.parametrize(
    "address",
    [
        "someone@example.net",
        "phish@ups.com.evil.example",
        "ups.com@evil.example",
        "noreply@notups.com",
        "",
        "not-an-address",
    ],
)
def test_other_addresses_are_not_allowed(address: str) -> None:
    assert _criteria().sender_allowed(address) is False


def test_an_allowlist_entry_with_an_at_matches_only_that_address() -> None:
    criteria = Criteria(senders=["mcinfo@ups.com"], subject_patterns=["."])
    assert criteria.sender_allowed("mcinfo@ups.com") is True
    assert criteria.sender_allowed("someoneelse@ups.com") is False


def test_matching_is_case_insensitive_on_the_sender() -> None:
    assert _criteria().sender_allowed("Auto-Confirm@AMAZON.COM") is True


# -- both must match ----------------------------------------------------------


def test_both_the_sender_and_the_subject_must_match() -> None:
    criteria = _criteria()

    good = criteria.evaluate(sender="mcinfo@ups.com", subject="Delivered: your package")
    assert good.matched is True

    wrong_sender = criteria.evaluate(
        sender="marketing@example.net", subject="Delivered: your package"
    )
    assert wrong_sender.matched is False
    assert "not on the allowlist" in wrong_sender.reason

    wrong_subject = criteria.evaluate(sender="mcinfo@ups.com", subject="Out for delivery")
    assert wrong_subject.matched is False
    assert "matches no pattern" in wrong_subject.reason


# -- RFC 2047 -----------------------------------------------------------------


def test_encoded_word_subjects_are_decoded() -> None:
    # What a carrier actually sends when the subject is not plain ASCII.
    encoded = "=?UTF-8?B?RGVsaXZlcmVkOiB5b3VyIGNhZsOpIG9yZGVy?="
    assert _criteria().subject_matches(encoded) is False, "matching the raw header is the bug"
    assert decode_subject(encoded) == "Delivered: your café order"
    assert _criteria().subject_matches(decode_subject(encoded)) is True


def test_a_mixed_encoding_subject_is_decoded() -> None:
    encoded = "=?utf-8?q?Delivered=3A?= your caf=C3=A9 order"
    assert decode_subject(encoded).startswith("Delivered:")


def test_an_unknown_charset_does_not_raise() -> None:
    assert "package" in decode_subject("=?not-a-charset?B?cGFja2FnZQ==?=")


def test_a_missing_subject_is_the_empty_string() -> None:
    assert decode_subject(None) == ""


def test_sender_address_extracts_from_a_display_name() -> None:
    assert sender_address('"UPS Quantum View" <mcinfo@ups.com>') == "mcinfo@ups.com"
    assert sender_address(None) == ""


def test_compile_problems_reports_only_bad_patterns() -> None:
    assert compile_problems(["^ok$", "also ok"]) == []
    assert len(compile_problems(["(", "^fine$", "["])) == 2
