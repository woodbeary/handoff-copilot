import json
import re

import pytest
from conftest import EXAMPLES, client_for

from handoff_copilot.audit import AuditLog
from handoff_copilot.copilot import Consent, ConsentMissing, Copilot, ShadowModeError
from handoff_copilot.drafts import Draft
from handoff_copilot.evidence import KeywordExtractor, PromptExtractor
from handoff_copilot.greenhouse import GreenhouseConnector, HarvestClient
from handoff_copilot.rollout import Mode, ShadowStore

OK = Consent("asked at the start of the call", {"candidate": True, "interviewer": True})
PII = re.compile(
    r"@example\.|example dot|555-01|\(\d{3}\)|Taylor|Brightwater|Casey|Lindqvist|Morgan|Ellery"
    r"|Jamie|Okafor",
    re.IGNORECASE,
)
CANNED = (EXAMPLES / "model_output_7301.json").read_text()


class Recorder:
    def __init__(self, inner):
        self.inner, self.sent = inner, []

    def __call__(self, method, url, headers, body):
        self.sent.append((method, url, body))
        return self.inner(method, url, headers, body)


@pytest.fixture
def audit():
    return AuditLog()


@pytest.fixture
def recorder(fake):
    return Recorder(fake)


@pytest.fixture
def gh(recorder, clock):
    return GreenhouseConnector(client_for(recorder, clock))


def visible(gh, rubric, audit, extractor=None):
    return Copilot(gh, rubric, extractor or KeywordExtractor(), audit, mode=Mode.VISIBLE)


def writes(recorder):
    return [(m, u) for m, u, _ in recorder.sent if m != "GET" and "/v3/" in u]


def test_without_every_partys_consent_nothing_is_read(gh, rubric, audit, recorder):
    copilot = visible(gh, rubric, audit)
    for consent in (
        Consent("none", {}),
        Consent("half", {"candidate": True}),
        Consent("third party", {"candidate": True, "interviewer": True, "hiring_manager": False}),
    ):
        with pytest.raises(ConsentMissing):
            copilot.screen(5101, 7301, consent)
    assert recorder.sent == []
    assert [r["action"] for r in audit.rows()] == ["refused:no_consent"] * 3


def test_shadow_writes_nothing_to_the_ats_and_keeps_no_pii(gh, rubric, audit, recorder, tmp_path):
    store = ShadowStore(tmp_path / "shadow.jsonl")
    copilot = Copilot(gh, rubric, KeywordExtractor(), audit, mode=Mode.SHADOW, shadow=store)
    results = [copilot.screen(a, i, OK) for a, i in ((5101, 7301), (5102, 7302), (5103, 7303))]
    assert writes(recorder) == []
    assert all(r.drafts == [] and len(r.shadow_refs) == 2 for r in results)
    text = store.path.read_text()
    assert len(text.splitlines()) == 6
    assert not PII.search(text)
    with pytest.raises(ShadowModeError):
        copilot.submit(results[0].shadow_refs[0], 35)


def test_visible_drafts_wait_for_a_person(gh, rubric, audit, recorder, fake):
    copilot = visible(gh, rubric, audit)
    result = copilot.screen(5101, 7301, OK)
    assert writes(recorder) == []
    scorecard, handoff = result.drafts
    assert (scorecard.kind, handoff.kind) == ("scorecard", "handoff")
    assert handoff.flags == (
        "contact_mismatch",
        "screened_by_non_owner",
        "promise_without_schedule",
    )

    delivery = copilot.submit(handoff.ref, 35, body=handoff.body + "\nConfirmed by phone.")
    assert writes(recorder) == [("POST", "https://harvest.greenhouse.io/v3/notes")]
    posted = json.loads(next(b for m, u, b in recorder.sent if m == "POST" and "notes" in u))
    assert set(posted) == {
        "candidate_id",
        "application_id",
        "body",
        "note_type",
        "visibility",
        "user_id",
    }
    assert (posted["visibility"], posted["note_type"], posted["user_id"]) == ("private", "NOTE", 35)
    assert posted["body"].endswith(f"Confirmed by phone.\n\nref: {handoff.ref}")
    assert delivery.status == "delivered"
    with pytest.raises(ValueError, match="already submitted"):
        copilot.submit(handoff.ref, 35)

    rerun = visible(gh, rubric, audit)
    again = rerun.screen(5101, 7301, OK).drafts[1]
    assert again.ref == handoff.ref
    assert rerun.submit(again.ref, 35).status == "already_delivered"
    assert sum(f"ref: {handoff.ref}" in (n["body"] or "") for n in fake.tables["notes"]) == 1


def test_the_model_sees_a_redacted_transcript_with_roles_only(gh, rubric, audit):
    prompts = []

    def complete(prompt):
        prompts.append(prompt)
        return CANNED

    result = visible(gh, rubric, audit, PromptExtractor(complete, "stand-in")).screen(
        5101, 7301, OK
    )
    prompt = prompts[0]
    assert not PII.search(prompt)
    assert "candidate: Please use my personal email, t***@e***.net." in prompt
    assert "Hi [candidate], thanks" in prompt
    assert result.kept == 5
    assert dict(result.dropped) == {
        "not_candidate_speech": 1,
        "not_in_transcript": 1,
        "outside_time_range": 1,
        "protected_topic": 1,
        "unknown_signal": 1,
    }
    extract = next(r for r in audit.rows() if r["action"] == "extract")
    assert extract["prompt_sha256"] and "10 claims, 5 kept, 5 dropped" in extract["detail"]


def test_an_ai_opt_out_means_no_model_call_but_still_a_handoff(gh, rubric, audit):
    def never(_prompt):
        raise AssertionError("the model must not be called")

    result = visible(gh, rubric, audit, PromptExtractor(never)).screen(5103, 7303, OK)
    scorecard, handoff = result.drafts
    assert not result.model_called
    assert "opted out of AI-assisted review" in scorecard.body
    assert handoff.flags == ("promise_without_schedule",)
    assert "skipped:ai_opt_out" in [r["action"] for r in audit.rows()]


def test_an_unusable_transcript_skips_the_model_until_a_person_sets_roles(
    fake, clock, rubric, audit
):
    url = "https://media.synthetic.invalid/transcripts/7301.json"
    labels = {"Casey Lindqvist": "Speaker 1", "Taylor Brightwater": "Speaker 2"}
    for entry in fake.transcripts[url]["entries"]:
        entry["speaker"] = labels[entry["speaker"]]
    gh = GreenhouseConnector(client_for(fake, clock))
    result = visible(gh, rubric, audit).screen(5101, 7301, OK)
    assert not result.model_called
    assert "transcript unusable: expected exactly one candidate speaker" in result.drafts[0].body
    fixed = visible(gh, rubric, audit).screen(5101, 7301, OK, roles={"Speaker 2": "candidate"})
    assert fixed.model_called and fixed.kept > 0


def test_unusable_model_output_leaves_an_empty_draft_not_a_guess(gh, rubric, audit):
    result = visible(gh, rubric, audit, PromptExtractor(lambda _p: "sure! here you go")).screen(
        5101, 7301, OK
    )
    assert result.kept == 0
    assert "model output was unusable" in result.drafts[0].body


def test_nothing_in_the_package_can_reject_move_hire_or_rate():
    words = re.compile(r"reject|move|advance|hire|rate|rating|score|rank")
    for cls in (Copilot, GreenhouseConnector, HarvestClient):
        public = [m for m in dir(cls) if not m.startswith("_")]
        assert not [m for m in public if words.search(m)], cls
    assert not [f for f in Draft.__dataclass_fields__ if words.search(f)]


def test_every_request_is_a_read_or_a_private_note(gh, rubric, audit, recorder):
    copilot = visible(gh, rubric, audit)
    for draft in copilot.screen(5101, 7301, OK).drafts:
        copilot.submit(draft.ref, 35)
    for method, url, _ in recorder.sent:
        assert method == "GET" or url in (
            "https://auth.greenhouse.io/token",
            "https://harvest.greenhouse.io/v3/notes",
        )


def test_the_audit_log_names_fields_and_holds_no_values(gh, rubric, audit):
    copilot = visible(gh, rubric, audit)
    copilot.discard(copilot.screen(5101, 7301, OK).drafts[0].ref, 35)
    rows = audit.rows()
    read = next(r for r in rows if r["action"] == "read:candidates")
    assert json.loads(read["fields"]) == sorted(
        ["id", "first_name", "last_name", "preferred_name", "email_addresses", "phone_numbers"]
    )
    assert rows[-1]["action"] == "discarded" and rows[-1]["actor"] == "user:35"
    assert not any(PII.search(json.dumps(r)) for r in rows)
    assert audit.verify() == (True, None)
