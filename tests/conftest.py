from __future__ import annotations

import json
from pathlib import Path

import pytest

from handoff_copilot.context import ScreenContext
from handoff_copilot.rubric import load
from handoff_copilot.transcript import Transcript, assign_roles, from_notetaker

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture
def rubric():
    return load(EXAMPLES / "rubric.toml")


@pytest.fixture
def org() -> dict:
    return json.loads((EXAMPLES / "org.json").read_text())


def transcript_for(interview_id: int, candidate: str) -> Transcript:
    data = json.loads((EXAMPLES / "transcripts" / f"{interview_id}.json").read_text())
    segments = from_notetaker(data)
    return Transcript(tuple(segments), assign_roles(segments, [candidate]))


def context_for(org: dict, application_id: int, interview_id: int, **overrides) -> ScreenContext:
    app = next(a for a in org["applications"] if a["id"] == application_id)
    candidate = next(c for c in org["candidates"] if c["id"] == app["candidate_id"])
    interviews = [i for i in org["interviews"] if i["application_id"] == application_id]
    fields = dict(
        application=app,
        candidate=candidate,
        screen=next(i for i in interviews if i["id"] == interview_id),
        panel=[p for p in org["interviewers"] if p["interview_id"] == interview_id],
        interviews=interviews,
        notes=[n for n in org["notes"] if n["candidate_id"] == candidate["id"]],
        transcript=None,
        ai_opt_out=False,
    )
    fields.update(overrides)
    return ScreenContext(**fields)
