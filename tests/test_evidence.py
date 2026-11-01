import hashlib
import json

import pytest
from conftest import EXAMPLES, transcript_for

from handoff_copilot.evidence import (
    Claim,
    KeywordExtractor,
    PromptExtractor,
    build_prompt,
    parse_claims,
    verify,
)
from handoff_copilot.transcript import CANDIDATE, Transcript, from_notetaker

DECODER = "At my last job I wrote the telemetry decoder for our test stand myself."


@pytest.fixture
def t7301():
    return transcript_for(7301, "Taylor Brightwater")


def reasons(dropped):
    return [d.reason for d in dropped]


def test_an_exact_candidate_quote_is_kept_with_its_segment(t7301, rubric):
    kept, dropped = verify([Claim("hands_on_ownership", DECODER, 22.4, 48.0)], t7301, rubric)
    assert dropped == []
    assert kept[0].quote == DECODER and kept[0].segments == (5,)


def test_whitespace_is_the_only_thing_forgiven(t7301, rubric):
    spaced = DECODER.replace(" the ", "  the\n")
    lower = DECODER.lower()
    kept, dropped = verify(
        [
            Claim("hands_on_ownership", spaced, 22.4, 48.0),
            Claim("hands_on_ownership", lower, 22.4, 48.0),
        ],
        t7301,
        rubric,
    )
    assert [k.quote for k in kept] == [DECODER]
    assert reasons(dropped) == ["not_in_transcript"]


@pytest.mark.parametrize(
    ("claim", "reason"),
    [
        (
            Claim("works_with_users", "The technicians said I was the best they had.", 80, 104),
            "not_in_transcript",
        ),
        (
            Claim(
                "hands_on_ownership",
                "Tell me about something you built yourself, start to finish.",
                16.8,
                22.0,
            ),
            "not_candidate_speech",
        ),
        (Claim("hands_on_ownership", DECODER, 109.3, 130.0), "outside_time_range"),
        (Claim("culture_fit", DECODER, 22.4, 48.0), "unknown_signal"),
        (Claim("why_this_role", "Yes, that's fine.", 15.2, 16.4), "too_short"),
        (
            Claim(
                "why_this_role",
                "I'd like to stay in Orange County, my kids are in school here.",
                134.3,
                139.0,
            ),
            "protected_topic",
        ),
        (Claim("why_this_role", "word " * 80, 0, 200), "too_long"),
    ],
)
def test_every_other_claim_is_dropped_with_a_reason(t7301, rubric, claim, reason):
    kept, dropped = verify([claim], t7301, rubric)
    assert kept == [] and reasons(dropped) == [reason]


def test_duplicates_and_claims_past_the_cap_are_dropped(t7301, rubric):
    lines = [
        "I traced it to the clock on the logging board",
        "fixed it with a resync step.",
        "I ran it on the bench for three days",
        "compared every frame against a reference recorder.",
    ]
    claims = [Claim("debugging_hard_failures", q, 22.4, 74.0) for q in lines]
    claims.insert(1, claims[0])
    kept, dropped = verify(claims, t7301, rubric, per_signal=3)
    assert len(kept) == 3
    assert reasons(dropped) == ["duplicate", "over_limit"]


def test_a_quote_may_span_consecutive_candidate_lines(rubric):
    data = {
        "entries": [
            {"speaker": "Cand", "text": "I built the fault logger", "starts": 0, "ends": 3},
            {"speaker": "Cand", "text": "and shipped it to every vehicle.", "starts": 3, "ends": 6},
        ]
    }
    t = Transcript(tuple(from_notetaker(data)), {"Cand": CANDIDATE})
    claim = Claim("hands_on_ownership", "fault logger and shipped it", 0, 6)
    kept, _ = verify([claim], t, rubric)
    assert kept[0].segments == (0, 1)


def test_the_keyword_baseline_only_quotes_the_candidate(t7301, rubric):
    extraction = KeywordExtractor().extract(t7301, rubric)
    kept, dropped = verify(extraction.claims, t7301, rubric)
    assert extraction.prompt_sha256 is None
    assert {k.signal for k in kept} == {s.id for s in rubric.signals}
    candidate_text = " ".join(s.text for s in t7301.lines(CANDIDATE))
    assert all(k.quote in candidate_text for k in kept)
    assert set(reasons(dropped)) <= {"too_short"}


def test_the_prompt_names_roles_not_speakers(t7301, rubric):
    prompt = build_prompt(t7301, rubric)
    assert "Casey Lindqvist" not in prompt and "Taylor Brightwater:" not in prompt
    assert "] candidate: At my last job" in prompt
    assert "never rate, rank, score, recommend, advance or reject" in prompt


def test_prompt_extractor_hashes_the_prompt_and_parses_fenced_json(t7301, rubric):
    seen = []
    canned = (EXAMPLES / "model_output_7301.json").read_text()

    def complete(prompt):
        seen.append(prompt)
        return f"```json\n{canned}\n```"

    extraction = PromptExtractor(complete, "stand-in").extract(t7301, rubric)
    assert len(extraction.claims) == len(json.loads(canned)["claims"])
    assert extraction.prompt_sha256 == hashlib.sha256(seen[0].encode()).hexdigest()
    assert extraction.error is None


@pytest.mark.parametrize("raw", ["not json", "[]", '{"claims": [{"signal": "x"}]}'])
def test_unusable_model_output_yields_no_claims_and_an_error(t7301, rubric, raw):
    extraction = PromptExtractor(lambda _p: raw).extract(t7301, rubric)
    assert extraction.claims == () and extraction.error.startswith("unusable model output")


def test_parse_claims_reads_the_documented_shape():
    raw = '{"claims": [{"signal": "s", "quote": "q", "starts": "1.5", "ends": 2}]}'
    assert parse_claims(raw) == [Claim("s", "q", 1.5, 2.0)]
