"""Hiring signals backed by quoted lines, and the check that keeps a model honest.

An extractor (a model, or the keyword baseline) proposes claims: a signal, a quote and
a time range. `verify` keeps a claim only if the quote is an exact substring of what
the candidate said in that time range (whitespace collapsed, nothing else forgiven).
Everything else is dropped with a reason a person can read. A structured-output schema
can shape the claims; only this check guarantees them.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from .rubric import Rubric, mentions
from .transcript import CANDIDATE, INTERVIEWER, Transcript, squash

QUOTE_TOPICS = (
    "years old",
    "my age",
    "kids",
    "children",
    "pregnant",
    "married",
    "my wife",
    "my husband",
    "religion",
    "church",
    "disability",
    "visa",
    "citizen",
    "green card",
    "born in",
    "salary history",
)

NOT_IN_TRANSCRIPT = "not_in_transcript"
NOT_CANDIDATE = "not_candidate_speech"
OUTSIDE_RANGE = "outside_time_range"


@dataclass(frozen=True)
class Claim:
    signal: str
    quote: str
    starts: float
    ends: float


@dataclass(frozen=True)
class Evidence:
    signal: str
    quote: str
    starts: float
    ends: float
    segments: tuple[int, ...]


@dataclass(frozen=True)
class Dropped:
    claim: Claim
    reason: str


@dataclass(frozen=True)
class Extraction:
    claims: tuple[Claim, ...]
    extractor: str
    prompt_sha256: str | None = None
    error: str | None = None


class Extractor(Protocol):
    name: str

    def extract(self, transcript: Transcript, rubric: Rubric) -> Extraction: ...


def _locate(claim: Claim, transcript: Transcript, quote: str) -> tuple[str | None, tuple[int, ...]]:
    text, segments = transcript.text_between(CANDIDATE, claim.starts, claim.ends)
    if quote in text:
        return None, segments
    other, _ = transcript.text_between(INTERVIEWER, claim.starts, claim.ends)
    if quote in other:
        return NOT_CANDIDATE, ()
    anywhere = squash(" ".join(s.text for s in transcript.lines(CANDIDATE)))
    if quote in anywhere:
        return OUTSIDE_RANGE, ()
    return NOT_IN_TRANSCRIPT, ()


def verify(
    claims: Iterable[Claim],
    transcript: Transcript,
    rubric: Rubric,
    *,
    min_words: int = 4,
    max_chars: int = 300,
    per_signal: int = 3,
) -> tuple[list[Evidence], list[Dropped]]:
    kept: list[Evidence] = []
    dropped: list[Dropped] = []
    seen: set[tuple[str, str]] = set()
    for claim in claims:
        quote = squash(claim.quote)
        reason: str | None = None
        segments: tuple[int, ...] = ()
        if rubric.get(claim.signal) is None:
            reason = "unknown_signal"
        elif len(quote.split()) < min_words:
            reason = "too_short"
        elif len(quote) > max_chars:
            reason = "too_long"
        elif mentions(quote, QUOTE_TOPICS):
            reason = "protected_topic"
        elif (claim.signal, quote) in seen:
            reason = "duplicate"
        elif sum(e.signal == claim.signal for e in kept) >= per_signal:
            reason = "over_limit"
        else:
            reason, segments = _locate(claim, transcript, quote)
        if reason:
            dropped.append(Dropped(claim, reason))
            continue
        seen.add((claim.signal, quote))
        kept.append(Evidence(claim.signal, quote, claim.starts, claim.ends, segments))
    return kept, dropped


class KeywordExtractor:
    """A deterministic baseline with no model: the sentence around each `look_for` phrase."""

    name = "keyword-baseline"

    def extract(self, transcript: Transcript, rubric: Rubric) -> Extraction:
        claims = []
        for segment in transcript.lines(CANDIDATE):
            sentences = re.split(r"(?<=[.!?])\s+", squash(segment.text))
            for signal in rubric.signals:
                for sentence in sentences:
                    if mentions(sentence, tuple(p.casefold() for p in signal.look_for)):
                        claims.append(Claim(signal.id, sentence, segment.starts, segment.ends))
        return Extraction(tuple(claims), self.name)


PROMPT = """You draft evidence for a recruiter's screening-call scorecard.
You never rate, rank, score, recommend, advance or reject anyone.
For each signal, find what the candidate said that is evidence for it.
Copy each quote exactly, character for character, from candidate lines. Never paraphrase.
Never quote the interviewer. Leave out a signal that has no evidence.
Return JSON only, in this shape:
{{"claims": [{{"signal": "<id>", "quote": "<exact>", "starts": <sec>, "ends": <sec>}}]}}

Signals:
{signals}

Transcript (names and contact details are masked):
{lines}
"""


def build_prompt(transcript: Transcript, rubric: Rubric) -> str:
    signals = "\n".join(f"- {s.id}: {s.label}. {s.description}" for s in rubric.signals)
    lines = "\n".join(
        f"[{s.starts:.1f}-{s.ends:.1f}] {transcript.role_of(s)}: {squash(s.text)}"
        for s in transcript.segments
    )
    return PROMPT.format(signals=signals, lines=lines)


def parse_claims(raw: str) -> list[Claim]:
    body = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", body, re.DOTALL)
    if fenced:
        body = fenced.group(1)
    data = json.loads(body)
    claims = []
    for item in data["claims"]:
        claims.append(
            Claim(
                str(item["signal"]), str(item["quote"]), float(item["starts"]), float(item["ends"])
            )
        )
    return claims


class PromptExtractor:
    """Any approved model endpoint, passed in as `complete(prompt) -> text`.

    The copilot hands this extractor a transcript that is already redacted; this class
    never sees raw contact details. Bad output yields no claims and an error, never a
    crash and never a guess.
    """

    def __init__(self, complete: Callable[[str], str], name: str = "model") -> None:
        self.complete = complete
        self.name = name

    def extract(self, transcript: Transcript, rubric: Rubric) -> Extraction:
        prompt = build_prompt(transcript, rubric)
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        try:
            claims = parse_claims(self.complete(prompt))
        except (ValueError, KeyError, TypeError) as exc:
            return Extraction((), self.name, digest, f"unusable model output: {exc}")
        return Extraction(tuple(claims), self.name, digest)
