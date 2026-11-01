from __future__ import annotations

import json
from pathlib import Path

import pytest

from handoff_copilot.rubric import load
from handoff_copilot.transcript import Transcript, assign_roles, from_notetaker

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture
def rubric():
    return load(EXAMPLES / "rubric.toml")


def transcript_for(interview_id: int, candidate: str) -> Transcript:
    data = json.loads((EXAMPLES / "transcripts" / f"{interview_id}.json").read_text())
    segments = from_notetaker(data)
    return Transcript(tuple(segments), assign_roles(segments, [candidate]))
