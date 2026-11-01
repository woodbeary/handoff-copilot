import pytest

from handoff_copilot.privacy import contains_contact, find_contacts, keep_fields, redact


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        ("write to t.brightwater@example.com today", "write to t***@e***.com today"),
        ("my email is taylor dot b at example dot net", "my email is t***@e***.net"),
        ("it's taylor.b at example.net", "it's t***@e***.net"),
        ("call (714) 555-0142 after five", "call …0142 after five"),
        ("or +1 714.555.0142", "or …0142"),
    ],
)
def test_redact_masks_written_and_spoken_contacts(text, masked):
    assert redact(text) == masked


def test_redact_masks_known_names_longest_first():
    names = {"Taylor Brightwater": "[candidate]", "Taylor": "[candidate]", "Casey": "[interviewer]"}
    text = "Thanks, Casey. Taylor Brightwater here, call me Taylor."
    assert redact(text, names) == "Thanks, [interviewer]. [candidate] here, call me [candidate]."


def test_redact_is_safe_to_run_twice():
    text = "taylor dot b at example dot net or (714) 555-0142, says Taylor"
    once = redact(text, {"Taylor": "[candidate]"})
    assert redact(once, {"Taylor": "[candidate]"}) == once


def test_redaction_errs_toward_masking():
    assert redact("I worked at example dot com for years") == "I w***@e***.com for years"


def test_find_contacts_reads_spoken_addresses_only_after_an_email_cue():
    assert find_contacts("Please use my personal email, taylor dot b at example dot net.") == [
        ("email", "taylor.b@example.net")
    ]
    assert find_contacts("I worked at example dot com for years") == []
    assert find_contacts("reach me at T.Bright@Example.COM or 714-555-0142") == [
        ("email", "t.bright@example.com"),
        ("phone", "7145550142"),
    ]


def test_contains_contact_ignores_long_ids():
    assert contains_contact("sent to someone@example.org")
    assert contains_contact("call 714-555-0142")
    assert not contains_contact('{"job": 5171942007, "application": 5101}')


def test_keep_fields_drops_what_the_allowlist_does_not_name():
    record = {"id": 1, "first_name": "A", "addresses": [{"value": "x"}], "company": "C"}
    assert keep_fields(record, ("id", "first_name")) == {"id": 1, "first_name": "A"}
