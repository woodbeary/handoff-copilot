"""The handoff check: canonical contact, owner and next step, compared across sources.

This runs locally with no model. It reads the candidate's own words on the call, the
profile and the latest logged email, the application's owners and the interviews on
the calendar, and it flags where they disagree. It never edits the profile, never
picks between two addresses on its own and never says anything about the candidate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .context import ScreenContext
from .privacy import WRITTEN_EMAIL, find_contacts
from .transcript import CANDIDATE, INTERVIEWER, Transcript, squash

NEXT_STEP = re.compile(
    r"\b(next step|i'll (?:send|set up|schedule|loop in|follow up|get back|reach out|introduce)"
    r"|you'll hear|we'll (?:schedule|set up|follow up|get back|reach out))",
    re.IGNORECASE,
)
UPCOMING = ("scheduled", "to_be_scheduled")


@dataclass(frozen=True)
class Flag:
    code: str
    detail: str


@dataclass(frozen=True)
class Handoff:
    candidate_id: int
    application_id: int
    interview_id: int
    screen_ended: str | None
    contact: str | None
    contact_source: str
    recruiter_id: int | None
    coordinator_id: int | None
    screened_by: tuple[int, ...]
    promise: str | None
    promise_at: float | None
    upcoming: tuple[dict, ...]
    flags: tuple[Flag, ...]


def clock(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 60:02d}:{whole % 60:02d}"


def _stated(transcript: Transcript | None, kind: str) -> list[tuple[str, float]]:
    if transcript is None:
        return []
    return [
        (value, s.starts)
        for s in transcript.lines(CANDIDATE)
        for k, value in find_contacts(s.text)
        if k == kind
    ]


def _last_logged_email(notes: list[dict]) -> tuple[list[str], str | None]:
    emails = [n for n in notes if n.get("type") == "EMAIL" and n.get("email_to")]
    if not emails:
        return [], None
    latest = max(emails, key=lambda n: n.get("created_at") or "")
    to = [a.lower() for a in WRITTEN_EMAIL.findall(latest["email_to"])]
    return to, (latest.get("created_at") or "")[:10] or None


def _contact(ctx: ScreenContext, transcript: Transcript | None) -> tuple[str | None, str, list]:
    profile = [e["value"].lower() for e in ctx.candidate.get("email_addresses") or []]
    stated = _stated(transcript, "email")
    last_to, last_date = _last_logged_email(ctx.notes)
    flags: list[Flag] = []
    use: str | None = None
    source = ""
    if stated:
        use, at = stated[-1]
        source = f"stated by the candidate at {clock(at)}"
        if use not in profile:
            detail = f"the candidate stated {use} on the call; the profile has "
            detail += ", ".join(profile) or "no address"
            if last_to:
                detail += f"; the last logged email ({last_date}) went to {', '.join(last_to)}"
            flags.append(Flag("contact_mismatch", detail + ". Confirm before the next invite."))
    elif len(profile) == 1:
        use, source = profile[0], "profile"
    elif profile and last_to and last_to[0] in profile:
        use, source = last_to[0], "profile; the last logged email went here"
    elif profile:
        flags.append(
            Flag(
                "contact_ambiguous",
                f"the profile has {len(profile)} addresses and nothing "
                "says which one is current. A person picks.",
            )
        )
    else:
        flags.append(Flag("contact_missing", "no email on the profile and none stated on the call"))
    off_profile = [a for a in last_to if a not in profile]
    if off_profile and not stated:
        flags.append(
            Flag(
                "contact_mismatch",
                f"the last logged email ({last_date}) went to "
                f"{', '.join(off_profile)}, which isn't on the profile",
            )
        )
    phones = {re.sub(r"\D", "", p["value"])[-10:] for p in ctx.candidate.get("phone_numbers") or []}
    for number, at in _stated(transcript, "phone"):
        if number not in phones:
            flags.append(
                Flag(
                    "contact_mismatch",
                    f"the candidate stated a phone ending {number[-4:]} at "
                    f"{clock(at)} that isn't on the profile",
                )
            )
    return use, source, flags


def _from_cue(text: str) -> str:
    """The promise itself: from the sentence holding the next-step cue to the end of the line."""
    sentences = re.split(r"(?<=[.!?])\s+", squash(text))
    first = next(i for i, s in enumerate(sentences) if NEXT_STEP.search(s))
    return " ".join(sentences[first:])


def build_handoff(ctx: ScreenContext, transcript: Transcript | None) -> Handoff:
    use, source, flags = _contact(ctx, transcript)

    app = ctx.application
    recruiter, coordinator = app.get("recruiter_id"), app.get("coordinator_id")
    screened_by = tuple(r["user_id"] for r in ctx.panel if r.get("user_id"))
    if recruiter is None:
        flags.append(Flag("no_owner", "the application has no recruiter of record to hand to"))
    elif screened_by and not set(screened_by) & {recruiter, coordinator}:
        flags.append(
            Flag(
                "screened_by_non_owner",
                f"user #{screened_by[0]} ran the screen and the "
                f"recruiter of record is user #{recruiter}. This note is the handoff.",
            )
        )

    promise = promise_at = None
    if transcript is not None:
        said = [s for s in transcript.lines(INTERVIEWER) if NEXT_STEP.search(s.text)]
        if said:
            promise, promise_at = _from_cue(said[-1].text), said[-1].starts
    upcoming = tuple(
        i for i in ctx.interviews if i["id"] != ctx.screen["id"] and i.get("status") in UPCOMING
    )
    if promise and not upcoming:
        flags.append(
            Flag(
                "promise_without_schedule",
                f"a next step was promised at {clock(promise_at)} "
                "and no later interview is scheduled or waiting to be scheduled",
            )
        )
    if not promise and not upcoming:
        flags.append(
            Flag(
                "no_next_step",
                "nothing was promised on the call and nothing is on the "
                "calendar, so the candidate may not know what happens next",
            )
        )

    return Handoff(
        candidate_id=ctx.candidate["id"],
        application_id=app["id"],
        interview_id=ctx.screen["id"],
        screen_ended=ctx.screen.get("ends_at"),
        contact=use,
        contact_source=source,
        recruiter_id=recruiter,
        coordinator_id=coordinator,
        screened_by=screened_by,
        promise=promise,
        promise_at=promise_at,
        upcoming=upcoming,
        flags=tuple(flags),
    )
