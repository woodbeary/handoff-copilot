"""Drafts a person submits: a scorecard draft and a handoff note.

The scorecard draft carries evidence only. It has no rating field, so no code path can
fill one. The interviewer reads it, then writes their own scorecard in the ATS.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field

from .evidence import Dropped, Evidence
from .handoff import Handoff, clock
from .rubric import Rubric

SCORECARD = "scorecard"
HANDOFF = "handoff"


def reference(kind: str, application_id: int, interview_id: int) -> str:
    """Stable per draft, so a retry or a rerun finds the note it already posted."""
    digest = hashlib.sha256(f"{kind}:{application_id}:{interview_id}".encode()).hexdigest()
    return f"hc-{digest[:12]}"


@dataclass
class Draft:
    kind: str
    ref: str
    candidate_id: int
    application_id: int
    body: str
    flags: tuple[str, ...] = ()
    status: str = "draft"
    submitted_by: int | None = None
    note_id: int | None = None
    meta: dict = field(default_factory=dict)


def scorecard_body(
    rubric: Rubric,
    evidence: list[Evidence],
    dropped: list[Dropped],
    extractor: str,
    *,
    unavailable: str | None = None,
    error: str | None = None,
) -> str:
    lines = ["SCORECARD DRAFT: evidence only, no rating. The interviewer writes the scorecard."]
    if unavailable:
        lines.append(unavailable)
        return "\n".join(lines)
    lines.append(
        f"Rubric: {rubric.name} ({rubric.version}) · drafted by: {extractor} · "
        f"{len(evidence)} quotes kept, {len(dropped)} dropped"
    )
    if error:
        lines.append(f"The draft is empty because the model output was unusable ({error}).")
    for signal in rubric.signals:
        lines.append("")
        lines.append(signal.label)
        quotes = [e for e in evidence if e.signal == signal.id]
        if not quotes:
            lines.append("  Nothing heard on this call. Ask in the next round if it matters.")
        for e in quotes:
            lines.append(f'  [{clock(e.starts)}] "{e.quote}"')
    if dropped:
        counts = Counter(d.reason for d in dropped)
        lines.append("")
        lines.append(
            "Dropped before you saw them: "
            + ", ".join(f"{n} {reason}" for reason, n in sorted(counts.items()))
        )
    return "\n".join(lines)


def handoff_body(h: Handoff) -> str:
    lines = [
        "HANDOFF NOTE: a draft for the next owner. A person checks the flags and submits it.",
        f"Candidate #{h.candidate_id} · application #{h.application_id} · "
        f"screen: interview #{h.interview_id}, ended {h.screen_ended or 'unknown'}",
        "",
        "Contact",
        f"  Use: {h.contact} ({h.contact_source})" if h.contact else "  Use: a person decides",
        "Owner",
        f"  Recruiter of record: {_user(h.recruiter_id)} · coordinator: {_user(h.coordinator_id)}"
        f" · screen run by: {', '.join(_user(u) for u in h.screened_by) or 'unknown'}",
        "Next step",
    ]
    if h.promise:
        lines.append(f'  Promised at {clock(h.promise_at or 0)}: "{h.promise}"')
    else:
        lines.append("  Promised on the call: nothing")
    if h.upcoming:
        for i in h.upcoming:
            lines.append(
                f"  On the calendar: interview #{i['id']} ({i.get('status')}, "
                f"{i.get('starts_at') or 'no time yet'})"
            )
    else:
        lines.append("  On the calendar: nothing")
    if h.flags:
        lines += ["", "Check before you submit"]
        lines += [f"  [{f.code}] {f.detail}" for f in h.flags]
    return "\n".join(lines)


def _user(user_id: int | None) -> str:
    return f"user #{user_id}" if user_id is not None else "none"


def with_footer(body: str, ref: str) -> str:
    return f"{body}\n\nref: {ref}"
