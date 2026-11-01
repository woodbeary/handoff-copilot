from __future__ import annotations

import json
from pathlib import Path

import pytest

from handoff_copilot.context import ScreenContext
from handoff_copilot.fake_harvest import FakeHarvest
from handoff_copilot.greenhouse import GreenhouseConnector, HarvestClient
from handoff_copilot.rubric import load
from handoff_copilot.transcript import Transcript, assign_roles, from_notetaker

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class FakeClock:
    """Time that only moves when someone sleeps, so every wait is visible to a test."""

    def __init__(self, start: float = 1_793_300_000.0) -> None:
        self.t = start
        self.slept: list[float] = []

    def time(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def rubric():
    return load(EXAMPLES / "rubric.toml")


@pytest.fixture
def org() -> dict:
    return json.loads((EXAMPLES / "org.json").read_text())


@pytest.fixture
def fake(clock) -> FakeHarvest:
    return FakeHarvest.from_dir(EXAMPLES, now=clock.time)


def client_for(fake, clock, **kwargs) -> HarvestClient:
    return HarvestClient(
        "synthetic-client",
        "synthetic-secret",
        fake,
        sleep=clock.sleep,
        now=clock.time,
        jitter=lambda: 0.0,
        **kwargs,
    )


@pytest.fixture
def connector(fake, clock) -> GreenhouseConnector:
    return GreenhouseConnector(client_for(fake, clock))


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
