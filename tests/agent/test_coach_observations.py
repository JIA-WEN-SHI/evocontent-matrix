from datetime import datetime, timezone

import pytest

from app.agents.coach import orchestrator as coach


NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
YESTERDAY = "2026-10-06T12:00:00+00:00"
COUNTERS = {"views": 100, "likes": 2, "collects": 1, "comments_count": 1, "shares": 0}


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(coach, "_now_local", lambda: NOW.astimezone(coach._CN_TZ))


def post(values=None, *, observed_at=YESTERDAY, published_at=YESTERDAY):
    return {
        "status": "published",
        "published_at": published_at,
        "updated_at": YESTERDAY,
        "metrics_jsonb": {
            "metrics_mode": "real",
            "post_metrics": dict(COUNTERS),
            "post_metrics_synced_at": YESTERDAY,
            "observation": {
                "values": dict(COUNTERS if values is None else values),
                "observed_at": observed_at,
                "provider": "xiaohongshu_cli",
                "source_ref": "https://www.xiaohongshu.com/explore/note1",
                "provenance": {},
            },
        },
    }


def summary(*rows):
    return coach._build_yesterday_summary({"pipeline_rows": list(rows)})


@pytest.mark.parametrize("rows", [[], [{"status": "published", "published_at": YESTERDAY}],
                                  [{"status": "approved", "updated_at": YESTERDAY}]])
def test_missing_evidence_is_unknown(rows):
    result = summary(*rows)
    for key in ("impressions", "interactions", "engagement_rate", "review_pass_rate",
                "approved_count", "rejected_count"):
        assert result[key] is None


def test_nested_metrics_use_actual_observation_day_not_publication_day():
    result = summary(post(published_at="2026-10-01T12:00:00Z"))
    assert result["impressions"] == 100
    assert result["interactions"] == 4
    assert result["engagement_rate"] == 0.04
    assert summary(post(observed_at=NOW.isoformat()))["impressions"] is None


def test_observation_day_uses_china_timezone():
    assert summary(post(observed_at="2026-10-05T17:00:00Z",
                        published_at="2026-10-01T12:00:00Z"))["impressions"] == 100
    assert summary(post(observed_at="2026-10-06T17:00:00Z"))["impressions"] is None


@pytest.mark.parametrize("timestamp", [None, "invalid", "2026-10-06T12:00:00"])
def test_invalid_observation_time_does_not_fallback_to_sync_or_update_time(timestamp):
    assert summary(post(observed_at=timestamp))["impressions"] is None


def test_legacy_nested_sync_timestamp_is_accepted_without_observation_object():
    row = post()
    del row["metrics_jsonb"]["observation"]
    assert summary(row)["engagement_rate"] == 0.04


def test_explicit_zeros_are_preserved_but_zero_denominator_rate_is_unknown():
    result = summary(post({key: 0 for key in COUNTERS}))
    assert result["impressions"] == 0
    assert result["interactions"] == 0
    assert result["engagement_rate"] is None
    result = summary(post({**{key: 0 for key in COUNTERS}, "views": 100}))
    assert result["engagement_rate"] == 0


def test_partial_observation_never_borrows_counters_or_claims_full_totals():
    result = summary(post({"likes": 2}))
    assert result["impressions"] is None
    assert result["interactions"] is None
    assert result["engagement_rate"] is None
    result = summary(post(), post({"views": 100, "likes": 2}))
    assert result["impressions"] == 200
    assert result["interactions"] is None
    assert result["engagement_rate"] is None


@pytest.mark.parametrize("invalid", [None, True, "bad", -1, float("nan"), float("inf")])
def test_invalid_counters_remain_unknown(invalid):
    result = summary(post({**COUNTERS, "views": invalid}))
    assert result["impressions"] is None
    assert result["engagement_rate"] is None


def test_synthetic_or_unpublished_or_prepublication_metrics_are_not_evidence():
    synthetic = post()
    synthetic["metrics_jsonb"]["metrics_mode"] = "synthetic_preview"
    assert summary(synthetic)["impressions"] is None
    assert summary(post(published_at=None))["impressions"] is None
    assert summary(post(published_at=NOW.isoformat()))["impressions"] is None


def test_review_rate_uses_explicit_review_events_not_task_update_time():
    result = summary({"status": "approved", "updated_at": YESTERDAY},
                     {"review_jsonb": {"rejected_at": YESTERDAY, "rejection_reason": "risk"}})
    assert result["approved_count"] == 0
    assert result["rejected_count"] == 1
    assert result["review_pass_rate"] == 0
    assert result["top_fail_reasons"] == [{"reason": "risk", "count": 1}]


@pytest.mark.parametrize("values", [{}, {"impressions": None, "engagement_rate": None,
                                        "review_pass_rate": None}, {"impressions": 100}])
def test_unknown_rates_generate_no_performance_or_stability_advice(values):
    assert coach._build_prompt_suggestions(values, {}) == []


def test_explicit_zero_rates_still_support_advice():
    suggestions = coach._build_prompt_suggestions(
        {"impressions": 100, "engagement_rate": 0, "review_pass_rate": 0}, {})
    assert {item["evidence"]["metric"] for item in suggestions} == {
        "engagement_rate", "review_pass_rate"}


@pytest.mark.parametrize("values", [{}, {"impressions": None, "engagement_rate": None,
                                        "review_pass_rate": None}])
def test_reply_labels_unknown_without_printing_none_or_zero(values):
    reply = coach._build_coach_reply(values, [])
    assert "昨日曝光：未知" in reply
    assert "昨日互动率：未知" in reply
    assert "审核通过率：未知" in reply
    assert "None" not in reply


def test_reply_preserves_explicit_zero():
    reply = coach._build_coach_reply(
        {"impressions": 0, "engagement_rate": 0, "review_pass_rate": 0}, [])
    assert "昨日曝光：0" in reply
    assert "昨日互动率：0" in reply
    assert "审核通过率：0" in reply
