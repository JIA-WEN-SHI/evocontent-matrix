from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "services" / "api"))

from app.services.account_service import (  # noqa: E402
    _compute_exposure_kpi_gate,
    _normalize_collection_plan,
    _normalize_feedback_plan,
    _onboarding_missing_fields,
)


def test_collection_plan_preserves_loop_gate_and_schedule():
    plan = _normalize_collection_plan(
        {
            "mode": "hybrid",
            "steps": [{"tool": "mcp_search", "query": "日本移民", "limit": 8}],
            "daily_schedule": {"posts_per_day": 2, "time_slots": ["09:00", "19:30"]},
            "loop_gate": {"min_case_per_day": 5, "min_asset_per_day": 4},
            "topic_refresh": {"window_days": 10, "refresh_every_hours": 12, "source_priority": ["case", "asset"]},
        }
    )
    assert plan["daily_schedule"]["posts_per_day"] == 2
    assert plan["daily_schedule"]["time_slots"] == ["09:00", "19:30"]
    assert plan["loop_gate"]["min_case_per_day"] == 5
    assert plan["loop_gate"]["min_asset_per_day"] == 4
    assert plan["topic_refresh"]["window_days"] == 10


def test_onboarding_missing_fields_detects_required_sections():
    strategy = {
        "primary_goal": "曝光增长",
        "persona_name": "",
        "ip_positioning": "日本签证顾问",
        "content_pillars": [],
        "focus_keywords": [],
        "hotspot_queries": [],
    }
    collection = {"steps": []}
    missing = _onboarding_missing_fields(strategy, collection)
    assert "persona_name" in missing
    assert "content_pillars" in missing
    assert "focus_keywords_or_hotspot_queries" in missing
    assert "collection_plan.steps" in missing


def test_exposure_kpi_gate_pass_and_fail():
    feedback = _normalize_feedback_plan(
        {
            "exposure_gate": {"min_impressions": 300, "min_engagement_rate": 0.05},
        }
    )
    passed = _compute_exposure_kpi_gate(
        feedback,
        {
            "metrics_jsonb": {
                "impressions": 1000,
                "likes": 20,
                "collects": 20,
                "comments_count": 10,
                "shares": 10,
            }
        },
    )
    assert passed["status"] == "ok"
    assert passed["passed"] is True

    failed = _compute_exposure_kpi_gate(
        feedback,
        {
            "metrics_jsonb": {
                "impressions": 1000,
                "likes": 5,
                "collects": 5,
                "comments_count": 5,
                "shares": 5,
            }
        },
    )
    assert failed["status"] == "ok"
    assert failed["passed"] is False

