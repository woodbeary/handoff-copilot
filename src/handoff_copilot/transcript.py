"""Transcripts: the documented Greenhouse Notetaker shape, and who is the candidate.

Greenhouse documents the transcript behind `GET /v3/interview_media` as a JSON object
holding only an `entries` array, each entry holding only `speaker`, `text`, `starts`
and `ends`, with times in seconds from the start of the recording. That is the one
shape this module reads. Speakers come from the source's labels; nothing here guesses
who spoke from audio.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

CANDIDATE = "candidate"
INTERVIEWER = "interviewer"


class TranscriptError(ValueError):
    """The transcript can't be used as evidence, so nothing downstream runs on it."""


@dataclass(frozen=True)
class Segment:
    index: int
    speaker: str
    text: str
    starts: float
    ends: float


@dataclass(frozen=True)
class Transcript:
    segments: tuple[Segment, ...]
    roles: Mapping[str, str]

    def role_of(self, segment: Segment) -> str:
        return self.roles.get(segment.speaker, INTERVIEWER)

    def lines(self, role: str) -> list[Segment]:
        return [s for s in self.segments if self.role_of(s) == role]

    def runs_between(
        self, role: str, starts: float, ends: float
    ) -> list[tuple[str, tuple[int, ...]]]:
        """`role`'s segments that overlap [starts, ends], one text per unbroken run.

        A run ends wherever another role speaks, so a quote can span one speaker's
        consecutive lines but never splice two answers across someone else's turn.
        Text is whitespace-collapsed.
        """
        runs: list[list[Segment]] = [[]]
        for s in self.segments:
            if self.role_of(s) != role:
                if runs[-1]:
                    runs.append([])
            elif s.ends >= starts and s.starts <= ends:
                runs[-1].append(s)
        return [
            (squash(" ".join(s.text for s in run)), tuple(s.index for s in run))
            for run in runs
            if run
        ]

    def with_text(self, rewrite) -> Transcript:
        """A copy with every segment's text passed through `rewrite` (used for redaction)."""
        segments = tuple(
            Segment(s.index, s.speaker, rewrite(s.text), s.starts, s.ends) for s in self.segments
        )
        return Transcript(segments, self.roles)


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def from_notetaker(data: Mapping) -> list[Segment]:
    entries = data.get("entries") if isinstance(data, Mapping) else None
    if not isinstance(entries, list) or not entries:
        raise TranscriptError("expected an object with a non-empty 'entries' array")
    segments = []
    for i, entry in enumerate(entries):
        try:
            speaker = str(entry["speaker"]).strip()
            text = str(entry["text"])
            starts, ends = float(entry["starts"]), float(entry["ends"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TranscriptError(f"entry {i} is missing speaker, text, starts or ends") from exc
        if not speaker or ends < starts or starts < 0:
            raise TranscriptError(f"entry {i} has no speaker or impossible times")
        segments.append(Segment(i, speaker, text, starts, ends))
    segments.sort(key=lambda s: (s.starts, s.index))
    return segments


def _norm(name: str) -> str:
    return squash(name).casefold()


def assign_roles(
    segments: Iterable[Segment],
    candidate_names: Iterable[str],
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Map speaker labels to roles. Exactly one label must be the candidate.

    A label is the candidate when it equals one of `candidate_names` (full, preferred or
    first name from the ATS record), ignoring case. Every other label is an interviewer.
    If no label or more than one label matches, the caller must set roles by hand through
    `overrides`; the copilot refuses to guess, because evidence is only ever quoted from
    the candidate.
    """
    labels = sorted({s.speaker for s in segments})
    if overrides:
        unknown = set(overrides) - set(labels)
        if unknown:
            raise TranscriptError(f"role overrides name speakers not in the transcript: {unknown}")
        roles = {label: overrides.get(label, INTERVIEWER) for label in labels}
    else:
        wanted = {_norm(n) for n in candidate_names if n and n.strip()}
        roles = {label: CANDIDATE if _norm(label) in wanted else INTERVIEWER for label in labels}
    matches = [label for label, role in roles.items() if role == CANDIDATE]
    if len(matches) != 1:
        raise TranscriptError(
            f"expected exactly one candidate speaker, found {len(matches)}; set roles by hand"
        )
    return roles
