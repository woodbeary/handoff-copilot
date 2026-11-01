from conftest import context_for, transcript_for

from handoff_copilot.drafts import handoff_body
from handoff_copilot.handoff import build_handoff
from handoff_copilot.transcript import Transcript, assign_roles, from_notetaker


def codes(h):
    return [f.code for f in h.flags]


def test_the_case_that_cost_a_screen(org):
    ctx = context_for(org, 5101, 7301)
    h = build_handoff(ctx, transcript_for(7301, "Taylor Brightwater"))
    assert codes(h) == ["contact_mismatch", "screened_by_non_owner", "promise_without_schedule"]
    assert h.contact == "taylor.b@example.net"
    assert h.contact_source == "stated by the candidate at 02:22"
    mismatch = h.flags[0].detail
    assert "profile has t.brightwater@example.com" in mismatch
    assert "last logged email (2026-10-24) went to t.brightwater@example.com" in mismatch
    assert h.promise.startswith("Next step: I'll set up the hiring manager screen")
    assert h.screened_by == (35,) and h.recruiter_id == 31


def test_a_clean_handoff_has_no_flags(org):
    ctx = context_for(org, 5102, 7302)
    h = build_handoff(ctx, transcript_for(7302, "Morgan Ellery"))
    assert codes(h) == []
    assert h.contact == "morgan.ellery@example.org" and h.contact_source == "profile"
    assert [i["id"] for i in h.upcoming] == [7305]
    assert h.promise.startswith("You'll hear from me by Wednesday")


def test_without_a_transcript_it_still_checks_owner_and_calendar(org):
    h = build_handoff(context_for(org, 5101, 7301), None)
    assert codes(h) == ["screened_by_non_owner", "no_next_step"]
    assert h.contact == "t.brightwater@example.com"


def test_last_logged_email_to_an_address_off_the_profile(org):
    ctx = context_for(org, 5102, 7302)
    ctx.notes[0]["email_to"] = "m.ellery@example.net"
    h = build_handoff(ctx, None)
    assert "contact_mismatch" in codes(h)
    assert "m.ellery@example.net, which isn't on the profile" in h.flags[0].detail


def test_two_profile_addresses_need_a_person_unless_the_last_email_says(org):
    ctx = context_for(org, 5102, 7302)
    ctx.candidate["email_addresses"] = [
        {"value": "a@example.org", "type": "work"},
        {"value": "morgan.ellery@example.org", "type": "personal"},
    ]
    h = build_handoff(ctx, None)
    assert h.contact == "morgan.ellery@example.org"
    assert h.contact_source == "profile; the last logged email went here"
    ctx.notes.clear()
    h = build_handoff(ctx, None)
    assert h.contact is None and "contact_ambiguous" in codes(h)


def test_missing_owner_and_missing_contact(org):
    ctx = context_for(org, 5102, 7302)
    ctx.application["recruiter_id"] = None
    ctx.candidate["email_addresses"] = []
    ctx.notes.clear()
    assert codes(build_handoff(ctx, None)) == ["contact_missing", "no_owner"]


def test_a_phone_number_stated_on_the_call_that_is_not_on_file(org):
    data = {
        "entries": [
            {"speaker": "Rec", "text": "Best number?", "starts": 0, "ends": 1},
            {
                "speaker": "Morgan Ellery",
                "text": "Text me at 949-555-0199.",
                "starts": 1,
                "ends": 3,
            },
        ]
    }
    segments = from_notetaker(data)
    t = Transcript(tuple(segments), assign_roles(segments, ["Morgan Ellery"]))
    h = build_handoff(context_for(org, 5102, 7302), t)
    assert codes(h) == ["contact_mismatch"]
    assert "phone ending 0199" in h.flags[0].detail


def test_the_note_names_ids_flags_and_the_promise(org):
    ctx = context_for(org, 5101, 7301)
    body = handoff_body(build_handoff(ctx, transcript_for(7301, "Taylor Brightwater")))
    assert "Use: taylor.b@example.net (stated by the candidate at 02:22)" in body
    assert "Recruiter of record: user #31" in body
    assert "[promise_without_schedule]" in body
    assert "On the calendar: nothing" in body
