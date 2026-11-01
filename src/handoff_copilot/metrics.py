"""The baseline TA leadership asks for, from Greenhouse's own timestamps.

Count these before the copilot exists, then during shadow, then after:

* feedback filed within 24 hours of an interview ending. Harvest's reference says owed
  feedback is an `interviewers` row with a `user_id` and no `scorecard_id`, once the
  interview is `awaiting_feedback` or `received`; there is no webhook for a submitted
  scorecard, so this comes from polling.
* screens that ended with nothing later on the calendar for an application still in
  process: the candidate nobody followed up with.
* drafts confirmed in the ATS by reading them back, not jobs that ran.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

PAST = ("awaiting_feedback", "complete", "received")
UPCOMING = ("scheduled", "to_be_scheduled")


def _at(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def feedback_coverage(
    interviews: Iterable[dict],
    panel: Iterable[dict],
    scorecards: Iterable[dict],
    now: datetime,
    within: timedelta = timedelta(hours=24),
) -> dict:
    ivs = {i["id"]: i for i in interviews}
    cards = {s["id"]: s for s in scorecards}
    due = on_time = late = missing = 0
    for row in panel:
        iv = ivs.get(row["interview_id"])
        if not iv or not row.get("user_id") or not iv.get("ends_at") or iv["status"] not in PAST:
            continue
        ended = _at(iv["ends_at"])
        if now - ended < within:
            continue
        due += 1
        card = cards.get(row.get("scorecard_id"))
        if card and card.get("submitted_at"):
            if _at(card["submitted_at"]) - ended <= within:
                on_time += 1
            else:
                late += 1
        else:
            missing += 1
    return {
        "due": due,
        "on_time": on_time,
        "late": late,
        "missing": missing,
        "on_time_rate": round(on_time / due, 3) if due else None,
    }


def next_step_gaps(
    applications: Iterable[dict],
    interviews: Iterable[dict],
    now: datetime,
    within: timedelta = timedelta(hours=48),
) -> dict:
    interviews = list(interviews)
    open_apps = {a["id"] for a in applications if a.get("status") == "in_process"}
    checked, gaps = 0, []
    for app_id in sorted(open_apps):
        mine = [i for i in interviews if i["application_id"] == app_id]
        past = [i for i in mine if i["status"] in PAST and i.get("ends_at")]
        if not past:
            continue
        last = max(past, key=lambda i: i["ends_at"])
        if now - _at(last["ends_at"]) < within:
            continue
        checked += 1
        later = [
            i
            for i in mine
            if i["status"] in UPCOMING or (i.get("starts_at") and i["starts_at"] > last["ends_at"])
        ]
        if not later:
            gaps.append(app_id)
    return {"checked": checked, "gaps": len(gaps), "applications": gaps}


def deliveries(notes: Iterable[dict], refs: Iterable[str]) -> dict:
    bodies = [n.get("body") or "" for n in notes]
    refs = list(refs)
    missing = [r for r in refs if not any(f"ref: {r}" in b for b in bodies)]
    return {"submitted": len(refs), "confirmed": len(refs) - len(missing), "missing": missing}
