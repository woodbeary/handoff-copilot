"""One screening call, end to end: consent, reads, the handoff check, evidence, drafts.

The order matters. Consent is checked before anything is read. The handoff check runs
on local data with no model. The model, when one is allowed, sees only a redacted
transcript with speakers named by role. Every step leaves an audit row.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from .audit import AuditLog
from .context import ScreenContext
from .drafts import HANDOFF, SCORECARD, Draft, handoff_body, reference, scorecard_body
from .evidence import Extractor, verify
from .greenhouse import Delivery
from .handoff import build_handoff
from .privacy import redact
from .rollout import Mode, ShadowStore
from .rubric import Rubric
from .transcript import Transcript, TranscriptError, assign_roles, from_notetaker

REQUIRED_PARTIES = ("candidate", "interviewer")
OPTED_OUT = (
    "The candidate opted out of AI-assisted review for this job. No model read this "
    "screen; write the scorecard from your own notes."
)


class ConsentMissing(PermissionError):
    pass


class ShadowModeError(PermissionError):
    pass


class Connector(Protocol):
    name: str

    def screen_context(self, application_id: int, interview_id: int) -> ScreenContext: ...

    def post_private_note(
        self, *, candidate_id: int, application_id: int, body: str, ref: str, user_id: int
    ) -> Delivery: ...


@dataclass(frozen=True)
class Consent:
    """Who agreed to the recording. California requires every party (Penal Code 632)."""

    method: str
    agreed: Mapping[str, bool]

    def missing(self) -> list[str]:
        parties = list(dict.fromkeys([*REQUIRED_PARTIES, *self.agreed]))
        return [p for p in parties if not self.agreed.get(p)]


@dataclass
class ScreenResult:
    mode: Mode
    drafts: list[Draft]
    shadow_refs: list[str]
    flags: tuple[str, ...]
    kept: int
    dropped: Counter = field(default_factory=Counter)
    model_called: bool = False


def _masks(ctx: ScreenContext, transcript: Transcript | None) -> dict[str, str]:
    names = {n: "[candidate]" for n in ctx.candidate_names}
    for label, role in transcript.roles.items() if transcript else ():
        tag = f"[{role}]"
        names.setdefault(label, tag)
        words = label.split()
        if len(words) > 1 and all(re.fullmatch(r"[A-Z][a-z'-]+", w) for w in words):
            names.setdefault(words[0], tag)
    return names


class Copilot:
    def __init__(
        self,
        connector: Connector,
        rubric: Rubric,
        extractor: Extractor,
        audit: AuditLog,
        *,
        mode: Mode = Mode.SHADOW,
        shadow: ShadowStore | None = None,
        actor: str = "handoff-copilot",
    ) -> None:
        if mode is Mode.SHADOW and shadow is None:
            raise ValueError("shadow mode needs a ShadowStore")
        self.connector, self.rubric, self.extractor = connector, rubric, extractor
        self.audit = audit
        self.mode, self.shadow, self.actor = mode, shadow, actor
        self.pending: dict[str, Draft] = {}

    def screen(
        self,
        application_id: int,
        interview_id: int,
        consent: Consent,
        roles: Mapping[str, str] | None = None,
    ) -> ScreenResult:
        records: dict[str, object] = {"application": application_id, "interview": interview_id}
        if missing := consent.missing():
            self.audit.append(
                self.actor,
                "refused:no_consent",
                records=records,
                detail=f"no recorded consent from: {', '.join(missing)}",
            )
            raise ConsentMissing(f"no recorded consent from {', '.join(missing)}; nothing was read")

        ctx = self.connector.screen_context(application_id, interview_id)
        records["candidate"] = ctx.candidate["id"]
        for resource, names in ctx.fields_seen.items():
            self.audit.append(
                self.actor,
                f"read:{resource}",
                records=records,
                fields=names,
                detail=f"via {self.connector.name}",
            )

        transcript, problem = None, "no transcript for this interview"
        if ctx.transcript is not None:
            try:
                segments = from_notetaker(ctx.transcript)
                transcript = Transcript(
                    tuple(segments), assign_roles(segments, ctx.candidate_names, roles)
                )
            except TranscriptError as e:
                problem = f"transcript unusable: {e}"

        handoff = build_handoff(ctx, transcript)
        masks = _masks(ctx, transcript)
        evidence, dropped, model_called = [], [], False
        unavailable, error = None, None
        if ctx.ai_opt_out:
            unavailable = OPTED_OUT
            self.audit.append(
                self.actor,
                "skipped:ai_opt_out",
                records=records,
                detail="candidate opted out of AI-assisted review for this job",
            )
        elif transcript is None:
            unavailable = f"No evidence drafted: {problem}."
            self.audit.append(self.actor, "skipped:no_transcript", records=records, detail=problem)
        else:
            redacted = transcript.with_text(lambda text: redact(text, masks))
            extraction = self.extractor.extract(redacted, self.rubric)
            model_called = True
            evidence, dropped = verify(extraction.claims, redacted, self.rubric)
            error = extraction.error
            self.audit.append(
                self.actor,
                "extract",
                records=records,
                prompt_sha256=extraction.prompt_sha256,
                detail=f"{extraction.extractor}: {len(extraction.claims)} claims, "
                f"{len(evidence)} kept, {len(dropped)} dropped" + (f"; {error}" if error else ""),
            )

        cand, app = ctx.candidate["id"], application_id
        drafts = [
            Draft(
                SCORECARD,
                reference(SCORECARD, app, interview_id),
                cand,
                app,
                scorecard_body(
                    self.rubric,
                    evidence,
                    dropped,
                    getattr(self.extractor, "name", "extractor"),
                    unavailable=unavailable,
                    error=error,
                ),
            ),
            Draft(
                HANDOFF,
                reference(HANDOFF, app, interview_id),
                cand,
                app,
                handoff_body(handoff),
                flags=tuple(f.code for f in handoff.flags),
            ),
        ]
        result = ScreenResult(
            self.mode,
            [],
            [],
            tuple(f.code for f in handoff.flags),
            len(evidence),
            Counter(d.reason for d in dropped),
            model_called,
        )

        if self.mode is Mode.SHADOW:
            at = datetime.now(UTC).isoformat()
            for d in drafts:
                self.shadow.write(
                    {
                        "ref": d.ref,
                        "kind": d.kind,
                        "application_id": app,
                        "interview_id": interview_id,
                        "flags": list(d.flags),
                        "body": redact(d.body, masks),
                        "at": at,
                    }
                )
            result.shadow_refs = [d.ref for d in drafts]
            self.audit.append(
                self.actor,
                "shadow:stored",
                records=records,
                detail="2 drafts stored, none shown, nothing written to the ATS",
            )
            return result

        self.pending.update({d.ref: d for d in drafts})
        result.drafts = drafts
        self.audit.append(
            self.actor,
            "visible:shown",
            records=records,
            detail="2 drafts shown; nothing is written until a person submits",
        )
        return result

    def submit(self, ref: str, reviewer_id: int, body: str | None = None) -> Delivery:
        """A person submits a draft, optionally edited. The note is posted as private."""
        if self.mode is Mode.SHADOW:
            raise ShadowModeError("shadow mode never writes to the ATS")
        draft = self.pending[ref]
        if draft.status != "draft":
            raise ValueError(f"{ref} is already {draft.status}")
        delivery = self.connector.post_private_note(
            candidate_id=draft.candidate_id,
            application_id=draft.application_id,
            body=body if body is not None else draft.body,
            ref=ref,
            user_id=reviewer_id,
        )
        draft.status, draft.submitted_by, draft.note_id = "submitted", reviewer_id, delivery.note_id
        self.audit.append(
            f"user:{reviewer_id}",
            "submitted:private_note",
            records={"application": draft.application_id, "note": delivery.note_id},
            detail=f"{draft.kind} {ref}: {delivery.status}" + ("; edited" if body else ""),
        )
        return delivery

    def discard(self, ref: str, reviewer_id: int) -> None:
        draft = self.pending[ref]
        draft.status = "discarded"
        self.audit.append(
            f"user:{reviewer_id}",
            "discarded",
            records={"application": draft.application_id},
            detail=f"{draft.kind} {ref}",
        )
