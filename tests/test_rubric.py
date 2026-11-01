import pytest

from handoff_copilot.rubric import RubricError, parse


def rubric_with(id_, label, look_for=()):
    return {"signal": [{"id": id_, "label": label, "look_for": list(look_for)}]}


def test_example_rubric_loads(rubric):
    assert [s.id for s in rubric.signals] == [
        "hands_on_ownership",
        "debugging_hard_failures",
        "works_with_users",
        "why_this_role",
    ]


@pytest.mark.parametrize(
    ("id_", "label"),
    [
        ("culture_fit", "Culture fit"),
        ("overall", "Overall rating"),
        ("hire_signal", "Hire signal"),
        ("red_flags", "Risk"),
        ("next", "Recommend for onsite"),
        ("gate", "Pass or fail"),
    ],
)
def test_signals_cannot_grade_or_decide(id_, label):
    with pytest.raises(RubricError, match="never grade"):
        parse(rubric_with(id_, label))


@pytest.mark.parametrize(
    ("id_", "label"),
    [
        ("age_range", "Age range"),
        ("visa_status", "Work visa"),
        ("family_plans", "Family plans"),
        ("pay", "Salary history"),
        ("origin", "National origin"),
    ],
)
def test_signals_cannot_touch_protected_topics(id_, label):
    with pytest.raises(RubricError, match="protected topic"):
        parse(rubric_with(id_, label))


@pytest.mark.parametrize(
    ("description", "match"),
    [
        ("Rate how strong a culture fit they are.", "never grade"),
        ("Ask whether family plans could get in the way.", "protected topic"),
    ],
)
def test_the_description_the_model_reads_is_checked_too(description, match):
    data = {"signal": [{"id": "teamwork", "label": "Teamwork", "description": description}]}
    with pytest.raises(RubricError, match=match):
        parse(data)


def test_whole_words_only_so_ordinary_words_pass():
    rubric = parse(rubric_with("stage_presence", "Benefits of the approach", ["managed"]))
    assert rubric.signals[0].id == "stage_presence"


@pytest.mark.parametrize(
    "data",
    [
        {"signal": []},
        rubric_with("Bad Id", "Label"),
        rubric_with("ok", ""),
        {"signal": [{"id": "a", "label": "A"}, {"id": "a", "label": "B"}]},
    ],
)
def test_malformed_rubrics_are_refused(data):
    with pytest.raises(RubricError):
        parse(data)
