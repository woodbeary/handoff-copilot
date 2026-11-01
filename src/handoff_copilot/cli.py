"""Command line: a demo on the synthetic org, plus coverage, the promotion check and audit."""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from . import __version__, metrics
from .audit import AuditLog
from .copilot import Consent, Copilot
from .evidence import KeywordExtractor, PromptExtractor
from .fake_harvest import FakeHarvest
from .greenhouse import GreenhouseConnector, HarvestClient
from .rollout import Mode, ShadowStore, load_reviews, promotion_check
from .rubric import load as load_rubric

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
DEMO_NOW = "2026-10-29T18:00:00Z"
SCREENS = ((5101, 7301), (5102, 7302), (5103, 7303))
WRITE_WORDS = ("reject", "move", "advance", "hire", "rate", "score", "unreject")


def _connector(fake: FakeHarvest, log=None, per_page: int = 500) -> GreenhouseConnector:
    client = HarvestClient("synthetic-client", "synthetic-secret", fake, log=log)
    client.per_page = per_page
    return GreenhouseConnector(client)


def _say(line: str = "") -> None:
    print(line)


def _consent() -> Consent:
    return Consent("asked at the start of the call", {"candidate": True, "interviewer": True})


def demo(examples: Path, workdir: Path, reviewer: int) -> int:
    rubric = load_rubric(examples / "rubric.toml")
    fake = FakeHarvest.from_dir(examples, limit=200)
    audit = AuditLog(workdir / "audit.sqlite3")
    shadow = ShadowStore(workdir / "shadow.jsonl")

    _say("1. Shadow: drafts nobody sees, nothing written to Greenhouse")
    copilot = Copilot(
        _connector(fake), rubric, KeywordExtractor(), audit, mode=Mode.SHADOW, shadow=shadow
    )
    for app, interview in SCREENS:
        r = copilot.screen(app, interview, _consent())
        model = "model step ran" if r.model_called else "no model step (AI opt-out honored)"
        flags = list(r.flags) or "none"
        _say(f"   application {app}: {r.kept} quotes kept, flags {flags}, {model}")
    posts = [c for c in fake.calls if c[0] == "POST" and c[1].startswith("/v3/")]
    _say(f"   Harvest writes during shadow: {len(posts)}")

    _say("\n2. Promotion check: does the shadow earn visible drafts?")
    decision = promotion_check([])
    _say(
        "   "
        + ("promote" if decision.promote else "stay in shadow")
        + ": "
        + "; ".join(decision.reasons)
    )

    _say("\n3. Visible, after a person promotes it. Screen 7301, the stand-in model answer")
    canned = (examples / "model_output_7301.json").read_text()
    prompts: list[str] = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        return canned

    fake.queue_429(retry_after=1)
    connector = _connector(fake, log=lambda line: _say(f"   harvest | {line}"))
    visible = Copilot(
        connector, rubric, PromptExtractor(complete, "stand-in model"), audit, mode=Mode.VISIBLE
    )
    result = visible.screen(5101, 7301, _consent())
    contact_line = next(line for line in prompts[0].splitlines() if "reach you" in line)
    reply_line = next(line for line in prompts[0].splitlines() if "personal email" in line)
    _say("   What the model was shown (redacted before the prompt):")
    _say(f"     {contact_line}\n     {reply_line}")
    for draft in result.drafts:
        _say("")
        _say("   " + draft.body.replace("\n", "\n   "))

    _say(f"\n4. A person (user #{reviewer}) reads both drafts and submits them as private notes")
    tools = [m for m in dir(connector) if not m.startswith("_") and callable(getattr(connector, m))]
    _say(f"   Connector methods: {', '.join(tools)}")
    risky = [t for t in tools if any(w in t for w in WRITE_WORDS)]
    _say(f"   Methods that reject, move, advance, hire or rate: {risky or 'none'}")
    handoff_draft, scorecard_draft = result.drafts[1], result.drafts[0]
    delivery = visible.submit(handoff_draft.ref, reviewer)
    _say(f"   handoff note {handoff_draft.ref}: {delivery.status} as note #{delivery.note_id}")
    fake.drop_next_note_response()
    delivery = visible.submit(scorecard_draft.ref, reviewer)
    _say(
        f"   scorecard draft {scorecard_draft.ref}: the reply was lost, the read-back found it: "
        f"{delivery.status} as note #{delivery.note_id}"
    )
    copies = sum(f"ref: {scorecard_draft.ref}" in (n["body"] or "") for n in fake.tables["notes"])
    _say(f"   copies of that note in Greenhouse: {copies}")

    _say("\n5. Coverage from Greenhouse timestamps (demo clock " + DEMO_NOW + ")")
    report(fake, DEMO_NOW, [d.ref for d in result.drafts], per_page=2)

    ok, bad = audit.verify()
    rows = audit.rows()
    _say(f"\n6. Audit log: {len(rows)} rows, hash chain {'intact' if ok else f'broken at {bad}'}")
    for row in rows[-4:]:
        _say(f"   {row['seq']:>3} {row['actor']:<16} {row['action']:<24} {row['detail']}")
    _say(f"\n   Files: {workdir}")
    return 0


def report(fake: FakeHarvest, now: str, refs: list[str], per_page: int = 500) -> None:
    connector = _connector(fake, log=lambda line: _say(f"   harvest | {line}"), per_page=per_page)
    c = connector.client
    interviews = c.list("interviews")
    panel = c.list("interviewers")
    cards = c.list("scorecards")
    apps = c.list("applications")
    at = datetime.fromisoformat(now.replace("Z", "+00:00"))
    fb = metrics.feedback_coverage(interviews, panel, cards, at)
    gaps = metrics.next_step_gaps(apps, interviews, at)
    _say(
        f"   Feedback within 24 h: {fb['on_time']} of {fb['due']} due "
        f"({fb['late']} late, {fb['missing']} still missing)"
    )
    _say(
        f"   Screens with nothing next on the calendar after 48 h: {gaps['gaps']} of "
        f"{gaps['checked']} (applications {gaps['applications']})"
    )
    if refs:
        notes = c.list("notes")
        d = metrics.deliveries(notes, refs)
        _say(f"   Drafts confirmed by read-back: {d['confirmed']} of {d['submitted']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="handoff-copilot", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("demo", help="run the synthetic walk-through")
    d.add_argument("--examples", type=Path, default=EXAMPLES)
    d.add_argument("--workdir", type=Path)
    d.add_argument("--reviewer", type=int, default=35)
    c = sub.add_parser("coverage", help="feedback and next-step coverage on the synthetic org")
    c.add_argument("--examples", type=Path, default=EXAMPLES)
    c.add_argument("--now", default=DEMO_NOW)
    g = sub.add_parser("gate", help="promotion check on a person's shadow labels (JSON lines)")
    g.add_argument("reviews", type=Path)
    v = sub.add_parser("verify-audit", help="check an audit log's hash chain")
    v.add_argument("path", type=Path)
    args = parser.parse_args(argv)

    if args.command == "demo":
        workdir = args.workdir or Path(tempfile.mkdtemp(prefix="handoff-copilot-"))
        workdir.mkdir(parents=True, exist_ok=True)
        return demo(args.examples, workdir, args.reviewer)
    if args.command == "coverage":
        report(FakeHarvest.from_dir(args.examples), args.now, [])
        return 0
    if args.command == "gate":
        decision = promotion_check(load_reviews(args.reviews))
        _say("promote" if decision.promote else "stay in shadow")
        for reason in decision.reasons:
            _say(f"  {reason}")
        _say(f"  {decision.stats}")
        return 0 if decision.promote else 1
    ok, bad = AuditLog(args.path).verify()
    _say("intact" if ok else f"broken at row {bad}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
