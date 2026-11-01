"""The hiring signals a team listens for, and the ones a copilot must never carry.

A signal names evidence to listen for ("something they built themselves"), not a
judgment. The loader refuses signals that grade or decide, and signals about legally
protected characteristics, so a rubric can't turn the copilot into a scorer.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

GRADING_WORDS = (
    "rating",
    "rate",
    "score",
    "rank",
    "recommend",
    "recommendation",
    "reject",
    "advance",
    "hire",
    "no-hire",
    "risk",
    "fit",
    "verdict",
    "pass",
    "fail",
)

PROTECTED_TOPICS = (
    "age",
    "birth",
    "citizenship",
    "citizen",
    "nationality",
    "national origin",
    "immigration",
    "visa",
    "religion",
    "disability",
    "health",
    "pregnancy",
    "marital",
    "family",
    "children",
    "race",
    "ethnicity",
    "gender",
    "sex",
    "orientation",
    "veteran",
    "salary history",
)


class RubricError(ValueError):
    pass


@dataclass(frozen=True)
class Signal:
    id: str
    label: str
    description: str
    look_for: tuple[str, ...]


@dataclass(frozen=True)
class Rubric:
    name: str
    version: str
    signals: tuple[Signal, ...]

    def get(self, signal_id: str) -> Signal | None:
        return next((s for s in self.signals if s.id == signal_id), None)


def mentions(text: str, terms: tuple[str, ...]) -> str | None:
    """The first term that appears in `text` as whole words, ignoring case."""
    lowered = text.casefold().replace("_", " ")
    for term in terms:
        if re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", lowered):
            return term
    return None


def _check(signal: Signal) -> None:
    surface = " ".join([signal.id, signal.label, *signal.look_for])
    if word := mentions(surface, GRADING_WORDS):
        raise RubricError(
            f"signal {signal.id!r} mentions {word!r}: signals name evidence to listen for, "
            "they never grade or decide"
        )
    if word := mentions(surface, PROTECTED_TOPICS):
        raise RubricError(
            f"signal {signal.id!r} mentions {word!r}, a protected topic; eligibility is "
            "checked by your application questions and HR, not inferred from a call"
        )


def parse(data: dict) -> Rubric:
    meta = data.get("rubric", {})
    raw = data.get("signal", [])
    if not raw:
        raise RubricError("a rubric needs at least one [[signal]]")
    signals = []
    for entry in raw:
        signal = Signal(
            id=str(entry.get("id", "")),
            label=str(entry.get("label", "")),
            description=str(entry.get("description", "")),
            look_for=tuple(str(p) for p in entry.get("look_for", [])),
        )
        if not re.fullmatch(r"[a-z][a-z0-9_]*", signal.id) or not signal.label:
            raise RubricError(f"signal needs a snake_case id and a label: {entry!r}")
        _check(signal)
        signals.append(signal)
    ids = [s.id for s in signals]
    if len(ids) != len(set(ids)):
        raise RubricError("signal ids must be unique")
    return Rubric(str(meta.get("name", "")), str(meta.get("version", "")), tuple(signals))


def load(path: str | Path) -> Rubric:
    with open(path, "rb") as fh:
        return parse(tomllib.load(fh))
