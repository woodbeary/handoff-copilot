"""Shadow first, then visible drafts. There is no third mode.

* shadow: drafts are written to a local store nobody is shown, and nothing reaches the
  ATS. A person later lays them beside the scorecards and notes recruiters actually
  filed and labels each item.
* visible: drafts are shown to the recruiter, who edits and submits them, or discards.

Moving from shadow to visible is a decision a person makes when `promotion_check` says
the labels clear the bars. Nothing in this package submits, rejects or rates on its own,
in any mode.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Mode(Enum):
    SHADOW = "shadow"
    VISIBLE = "visible"


@dataclass(frozen=True)
class Bars:
    """Placeholders. Agree the real numbers with TA ops before the shadow starts."""

    min_screens: int = 20
    min_quote_precision: float = 0.85
    min_evidence_recall: float = 0.60
    min_flag_precision: float = 0.80


QUOTE_VERDICTS = ("useful", "wrong")
FLAG_VERDICTS = ("real", "false")


@dataclass(frozen=True)
class Review:
    """One person's label on one shadow item.

    item is "quote" (verdict useful | wrong), "missed" (an evidence line the person
    found and the copilot didn't; verdict missed) or "flag" (verdict real | false).
    """

    screen: str
    item: str
    verdict: str
    reviewer_id: int


def wilson_lower(successes: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    p = successes / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - spread) / (1 + z * z / n)


@dataclass(frozen=True)
class Decision:
    promote: bool
    reasons: tuple[str, ...]
    stats: dict


def promotion_check(reviews: Iterable[Review], bars: Bars = Bars()) -> Decision:  # noqa: B008
    reviews = list(reviews)
    screens = {r.screen for r in reviews}
    useful = sum(r.item == "quote" and r.verdict == "useful" for r in reviews)
    wrong = sum(r.item == "quote" and r.verdict == "wrong" for r in reviews)
    missed = sum(r.item == "missed" for r in reviews)
    real = sum(r.item == "flag" and r.verdict == "real" for r in reviews)
    false = sum(r.item == "flag" and r.verdict == "false" for r in reviews)
    stats = {
        "screens_reviewed": len(screens),
        "quote_precision_lower": round(wilson_lower(useful, useful + wrong), 3),
        "evidence_recall_lower": round(wilson_lower(useful, useful + missed), 3),
        "flag_precision_lower": round(wilson_lower(real, real + false), 3),
        "counts": {
            "useful": useful,
            "wrong": wrong,
            "missed": missed,
            "real": real,
            "false": false,
        },
    }
    reasons = []
    if len(screens) < bars.min_screens:
        reasons.append(f"{len(screens)} of {bars.min_screens} screens reviewed by a person")
    checks = (
        ("quote precision", stats["quote_precision_lower"], bars.min_quote_precision),
        ("evidence recall", stats["evidence_recall_lower"], bars.min_evidence_recall),
        ("flag precision", stats["flag_precision_lower"], bars.min_flag_precision),
    )
    for label, lower, bar in checks:
        if lower < bar:
            reasons.append(f"{label}: lower bound {lower:.2f} is under the bar {bar:.2f}")
    return Decision(not reasons, tuple(reasons), stats)


def load_reviews(path: str | Path) -> list[Review]:
    reviews = []
    for n, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip():
            continue
        data = json.loads(line)
        review = Review(
            str(data["screen"]), data["item"], data["verdict"], int(data["reviewer_id"])
        )
        ok = (
            (review.item == "quote" and review.verdict in QUOTE_VERDICTS)
            or (review.item == "flag" and review.verdict in FLAG_VERDICTS)
            or (review.item == "missed" and review.verdict == "missed")
        )
        if not ok:
            raise ValueError(f"line {n}: unknown item or verdict: {data}")
        reviews.append(review)
    return reviews


class ShadowStore:
    """Append-only JSON lines of shadow drafts. Bodies are redacted before they land here."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def write(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line]
