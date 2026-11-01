import sqlite3

import pytest

from handoff_copilot.audit import AuditError, AuditLog


def filled(path=":memory:"):
    log = AuditLog(path)
    log.append(
        "handoff-copilot",
        "read:candidates",
        records={"candidate": 4201},
        fields=["id", "email_addresses"],
    )
    log.append("handoff-copilot", "extract", prompt_sha256="ab" * 32, detail="3 kept")
    log.append("user:35", "submitted:private_note", records={"note": 8004})
    return log


def test_the_chain_verifies_and_stores_field_names_not_values():
    log = filled()
    assert log.verify() == (True, None)
    first = log.rows()[0]
    assert first["fields"] == '["email_addresses", "id"]'
    assert first["prev"] == "0" * 64


def test_update_and_delete_are_refused():
    log = filled()
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        log.db.execute("UPDATE audit SET detail = 'edited' WHERE seq = 2")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        log.db.execute("DELETE FROM audit WHERE seq = 2")


def test_an_edit_made_around_the_triggers_breaks_the_chain():
    log = filled()
    log.db.executescript("DROP TRIGGER audit_no_update;")
    log.db.execute("UPDATE audit SET detail = 'edited' WHERE seq = 2")
    assert log.verify() == (False, 2)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"detail": "sent to t.brightwater@example.com"},
        {"detail": "called (714) 555-0142"},
        {"records": {"email": "a@example.org"}},
    ],
)
def test_contact_values_never_reach_the_log(kwargs):
    with pytest.raises(AuditError):
        AuditLog().append("handoff-copilot", "oops", **kwargs)


def test_ten_digit_ids_are_fine():
    log = AuditLog()
    log.append("handoff-copilot", "read:jobs", records={"job": 5171942007})
    assert log.verify() == (True, None)


def test_the_chain_continues_across_reopen(tmp_path):
    path = tmp_path / "audit.sqlite3"
    filled(path).db.close()
    log = AuditLog(path)
    log.append("handoff-copilot", "after-restart")
    assert log.verify() == (True, None) and len(log.rows()) == 4
