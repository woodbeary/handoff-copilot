from datetime import UTC, datetime

from handoff_copilot import metrics

NOW = datetime(2026, 10, 29, 18, 0, tzinfo=UTC)


def test_feedback_within_24_hours_on_the_synthetic_org(org):
    report = metrics.feedback_coverage(
        org["interviews"], org["interviewers"], org["scorecards"], NOW
    )
    assert report == {"due": 5, "on_time": 2, "late": 1, "missing": 2, "on_time_rate": 0.4}


def test_feedback_is_not_due_until_24_hours_pass(org):
    early = datetime(2026, 10, 27, 18, 0, tzinfo=UTC)
    report = metrics.feedback_coverage(
        org["interviews"], org["interviewers"], org["scorecards"], early
    )
    assert report["due"] == 3 and report["missing"] == 0


def test_screens_with_nothing_next_on_the_calendar(org):
    report = metrics.next_step_gaps(org["applications"], org["interviews"], NOW)
    assert report == {"checked": 3, "gaps": 1, "applications": [5101]}


def test_rejected_applications_are_not_gaps(org):
    report = metrics.next_step_gaps(
        org["applications"], org["interviews"], datetime(2026, 11, 30, tzinfo=UTC)
    )
    assert 5105 not in report["applications"]
    assert report["applications"] == [5101, 5103]


def test_deliveries_count_what_is_in_the_ats_not_what_ran():
    notes = [{"body": "x\n\nref: hc-a"}, {"body": None}]
    assert metrics.deliveries(notes, ["hc-a", "hc-b"]) == {
        "submitted": 2,
        "confirmed": 1,
        "missing": ["hc-b"],
    }
