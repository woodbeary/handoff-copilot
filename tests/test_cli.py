import json

from handoff_copilot.audit import AuditLog
from handoff_copilot.cli import main


def test_demo_walks_shadow_promotion_visible_coverage_and_audit(tmp_path, capsys):
    assert main(["demo", "--workdir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Harvest writes during shadow: 0" in out
    assert "no model step (AI opt-out honored)" in out
    assert "stay in shadow: 0 of 20 screens reviewed by a person" in out
    assert "429: Retry-After 1s" in out
    assert "t***@e***.net" in out
    assert "Dropped before you saw them: 1 not_candidate_speech, 1 not_in_transcript" in out
    assert "Methods that reject, move, advance, hire or rate: none" in out
    assert "the reply was lost, the read-back found it: delivered" in out
    assert "copies of that note in Greenhouse: 1" in out
    assert "following the cursor" in out
    assert "Feedback within 24 h: 2 of 5 due (1 late, 2 still missing)" in out
    assert "after 48 h: 1 of 3 (applications [5101])" in out
    assert "Drafts confirmed by read-back: 2 of 2" in out
    assert "hash chain intact" in out
    assert AuditLog(tmp_path / "audit.sqlite3").verify() == (True, None)


def test_gate_exits_nonzero_until_labels_clear_the_bars(tmp_path, capsys):
    path = tmp_path / "reviews.jsonl"
    rows = [
        {"screen": f"s{i}", "item": "quote", "verdict": "useful", "reviewer_id": 35}
        for i in range(3)
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows))
    assert main(["gate", str(path)]) == 1
    assert "stay in shadow" in capsys.readouterr().out


def test_coverage_and_verify_audit_commands(tmp_path, capsys):
    assert main(["coverage"]) == 0
    assert "Feedback within 24 h: 2 of 5 due" in capsys.readouterr().out
    log = AuditLog(tmp_path / "a.sqlite3")
    log.append("handoff-copilot", "x")
    log.db.close()
    assert main(["verify-audit", str(tmp_path / "a.sqlite3")]) == 0
    assert "intact" in capsys.readouterr().out
