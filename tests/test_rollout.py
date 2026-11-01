import json

import pytest

from handoff_copilot.rollout import (
    Bars,
    Mode,
    Review,
    ShadowStore,
    load_reviews,
    promotion_check,
    wilson_lower,
)


def labels(screens=25, useful=200, wrong=5, missed=30, real=40, false=2):
    out = []
    for i in range(useful + wrong + missed + real + false):
        screen = f"s{i % screens}"
        if i < useful:
            out.append(Review(screen, "quote", "useful", 35))
        elif i < useful + wrong:
            out.append(Review(screen, "quote", "wrong", 35))
        elif i < useful + wrong + missed:
            out.append(Review(screen, "missed", "missed", 35))
        elif i < useful + wrong + missed + real:
            out.append(Review(screen, "flag", "real", 35))
        else:
            out.append(Review(screen, "flag", "false", 35))
    return out


def test_there_is_no_mode_that_acts_alone():
    assert [m.value for m in Mode] == ["shadow", "visible"]
    with pytest.raises(ValueError):
        Mode("auto")


def test_wilson_lower_bound():
    assert wilson_lower(0, 0) == 0.0
    assert wilson_lower(10, 10) == pytest.approx(0.7225, abs=1e-4)
    assert wilson_lower(85, 100) == pytest.approx(0.7672, abs=1e-4)


def test_promotes_only_when_every_lower_bound_clears_its_bar():
    decision = promotion_check(labels())
    assert decision.promote, decision.reasons
    assert decision.stats["screens_reviewed"] == 25


def test_no_labels_means_stay_in_shadow():
    decision = promotion_check([])
    assert not decision.promote
    assert decision.reasons[0] == "0 of 20 screens reviewed by a person"


def test_each_bar_names_itself_when_it_fails():
    few = promotion_check(labels(screens=10))
    assert few.reasons == ("10 of 20 screens reviewed by a person",)
    sloppy = promotion_check(labels(wrong=40))
    assert any(r.startswith("quote precision") for r in sloppy.reasons)
    blind = promotion_check(labels(missed=200))
    assert any(r.startswith("evidence recall") for r in blind.reasons)
    noisy = promotion_check(labels(false=20))
    assert any(r.startswith("flag precision") for r in noisy.reasons)


def test_small_samples_do_not_promote_on_a_perfect_record():
    perfect = promotion_check(
        labels(screens=20, useful=12, wrong=0, missed=0, real=5, false=0), Bars(min_screens=1)
    )
    assert not perfect.promote


def test_load_reviews_rejects_unknown_verdicts(tmp_path):
    path = tmp_path / "reviews.jsonl"
    path.write_text(
        json.dumps({"screen": "s1", "item": "quote", "verdict": "useful", "reviewer_id": 35}) + "\n"
    )
    assert load_reviews(path) == [Review("s1", "quote", "useful", 35)]
    path.write_text(
        json.dumps({"screen": "s1", "item": "quote", "verdict": "strong hire", "reviewer_id": 35})
        + "\n"
    )
    with pytest.raises(ValueError, match="unknown item or verdict"):
        load_reviews(path)


def test_shadow_store_appends(tmp_path):
    store = ShadowStore(tmp_path / "shadow.jsonl")
    assert store.read() == []
    store.write({"ref": "a"})
    store.write({"ref": "b"})
    assert [r["ref"] for r in store.read()] == ["a", "b"]
