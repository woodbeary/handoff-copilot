import pytest
from conftest import transcript_for

from handoff_copilot.transcript import (
    CANDIDATE,
    INTERVIEWER,
    Transcript,
    TranscriptError,
    assign_roles,
    from_notetaker,
)


def entries(*rows):
    return {
        "entries": [dict(zip(("speaker", "text", "starts", "ends"), r, strict=True)) for r in rows]
    }


def test_reads_the_documented_notetaker_shape():
    t = transcript_for(7301, "Taylor Brightwater")
    assert len(t.segments) == 18
    assert t.roles == {"Casey Lindqvist": INTERVIEWER, "Taylor Brightwater": CANDIDATE}
    assert t.segments[0].starts == 0.0 and t.segments[-1].ends == 173.0


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"entries": []},
        {"entries": [{"speaker": "A", "text": "hi", "starts": 0}]},
        entries(("A", "hi", 5.0, 4.0)),
        entries(("", "hi", 0.0, 1.0)),
        entries(("A", "hi", -1.0, 1.0)),
    ],
)
def test_rejects_entries_it_cannot_trust(data):
    with pytest.raises(TranscriptError):
        from_notetaker(data)


def test_candidate_is_matched_by_name_ignoring_case():
    segments = from_notetaker(entries(("TAYLOR BRIGHTWATER", "hi", 0, 1), ("Rec", "hello", 1, 2)))
    assert assign_roles(segments, ["Taylor Brightwater"]) == {
        "TAYLOR BRIGHTWATER": CANDIDATE,
        "Rec": INTERVIEWER,
    }


def test_refuses_to_guess_when_no_label_or_two_labels_match():
    generic = from_notetaker(entries(("Speaker 1", "hi", 0, 1), ("Speaker 2", "yo", 1, 2)))
    with pytest.raises(TranscriptError, match="found 0"):
        assign_roles(generic, ["Taylor Brightwater"])
    twice = from_notetaker(entries(("Taylor", "hi", 0, 1), ("Taylor Brightwater", "yo", 1, 2)))
    with pytest.raises(TranscriptError, match="found 2"):
        assign_roles(twice, ["Taylor Brightwater", "Taylor"])


def test_a_person_can_set_roles_by_hand():
    generic = from_notetaker(entries(("Speaker 1", "hi", 0, 1), ("Speaker 2", "yo", 1, 2)))
    roles = assign_roles(generic, [], overrides={"Speaker 2": CANDIDATE})
    assert roles == {"Speaker 1": INTERVIEWER, "Speaker 2": CANDIDATE}
    with pytest.raises(TranscriptError, match="not in the transcript"):
        assign_roles(generic, [], overrides={"Speaker 9": CANDIDATE})


def test_text_between_joins_overlapping_lines_of_one_role():
    segments = from_notetaker(
        entries(("Cand", "first  part", 0, 2), ("Cand", "second\npart", 2, 4), ("Rec", "q", 4, 5))
    )
    t = Transcript(tuple(segments), {"Cand": CANDIDATE})
    assert t.text_between(CANDIDATE, 0, 4) == ("first part second part", (0, 1))
    assert t.text_between(CANDIDATE, 4.5, 5) == ("", ())
