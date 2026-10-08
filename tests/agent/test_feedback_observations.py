from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import httpx
from postgrest import SyncPostgrestClient

from app.orchestration import publish_feedback as feedback, reflection
from app.tools.integrations import xhs_mcp_readonly as mcp


NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
URL = "https://www.xiaohongshu.com/explore/note1"
COMPLETE = {"views": 100, "likes": 10, "collects": 3, "comments": 2, "shares": 1, "follows": -1}
ACCOUNT = {"id": "a", "config_jsonb": {"collection_plan": {"mode": "xhs_cli"}}}


class Query:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self.filters, self.orders = [], []
        self.wire_filters = []
        self.window, self.cap, self.patch, self.new = None, None, None, None

    def select(self, *args):
        return self

    def eq(self, key, value):
        self.wire_filters.append((key, value))
        def match(row):
            actual = row
            for part in key.replace("->>", "->").split("->"):
                actual = actual.get(part) if isinstance(actual, dict) else None
            expected = json.loads(value) if isinstance(actual, dict) and isinstance(value, str) else value
            return actual == expected

        self.filters.append(match)
        return self

    def is_(self, key, value):
        return self.eq(key, None if value == "null" else value)

    def in_(self, key, values):
        self.filters.append(lambda row: row.get(key) in values)
        return self

    def gte(self, key, value):
        self.filters.append(lambda row: str(row.get(key) or "") >= value)
        return self

    def order(self, key, desc=False, **kwargs):
        self.orders.append((key, desc))
        return self

    def range(self, start, end):
        self.window = (start, end)
        return self

    def limit(self, cap):
        self.cap = cap
        return self

    def update(self, patch):
        self.patch = deepcopy(patch)
        return self

    def insert(self, payload):
        self.new = deepcopy(payload)
        return self

    def execute(self):
        self.db.reads.append((self.name, self.window, self.cap))
        rows = [row for row in self.db.rows.get(self.name, []) if all(f(row) for f in self.filters)]
        for key, desc in reversed(self.orders):
            rows.sort(key=lambda row: (row.get(key) is None, str(row.get(key) or "")), reverse=desc)
        if self.window:
            rows = rows[self.window[0]:self.window[1] + 1]
        if self.cap is not None:
            rows = rows[:self.cap]
        if self.patch is not None:
            self.db.writes.append((self.name, self.patch))
            for row in rows:
                row.update(deepcopy(self.patch))
        if self.new is not None:
            self.db.writes.append((self.name, self.new))
            row = {"id": f"insert-{len(self.db.writes)}", **self.new}
            self.db.rows.setdefault(self.name, []).append(row)
            rows = [row]
        return SimpleNamespace(data=deepcopy(rows))


class DB:
    def __init__(self, tasks=(), accounts=(ACCOUNT,)):
        self.rows = deepcopy({"pipeline_tasks": list(tasks), "channel_accounts": list(accounts),
                              "domains": [{"id": "d", "slug": "ai_content"}]})
        self.reads, self.writes = [], []

    def table(self, name):
        return Query(self, name)


def task(*, values=None, observed_at=None, published_at=None, task_id="t", completed=()):
    values = {"likes": 0} if values is None else values
    observed_at = observed_at or NOW.isoformat()
    return {"id": task_id, "domain_id": "d", "account_id": "a", "channel": "xiaohongshu",
            "status": "published", "stage": "feedback_pending", "created_at": "2026-10-01T00:00:00Z",
            "updated_at": NOW.isoformat(), "published_at": published_at or (NOW - timedelta(hours=25)).isoformat(),
            "payload_jsonb": {"title": "actual post", "body": "actual body"},
            "publish_jsonb": {"identity": {"feed_id": "note1", "published_url": URL},
                              "feedback_completed_hours": list(completed)},
            "metrics_jsonb": {"metrics_mode": "real", "post_metrics": values,
                              "post_metrics_synced_at": observed_at,
                              "observation": {"values": values, "observed_at": observed_at,
                                              "provider": "xiaohongshu_cli", "source_ref": URL,
                                              "provenance": {"method": "public_note_detail"}}}}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(reflection, "datetime", Clock)
    monkeypatch.setattr(feedback, "_CANDIDATE_SCAN_OFFSETS", {})
    monkeypatch.setattr(feedback, "_now", lambda: NOW)
    monkeypatch.setattr(mcp, "_now_iso", lambda: NOW.isoformat())
    monkeypatch.setattr(feedback, "sync_pipeline_task_metrics_via_mcp", Mock(return_value={"status": "failed", "reason": "unavailable"}))
    monkeypatch.setattr(feedback, "collect_recent_published_posts", Mock(return_value={"items": []}))
    monkeypatch.setattr(reflection, "get_router", Mock(return_value=Mock(reflect_strategy=Mock(return_value={"draft": "suggested"}))))


def reconcile(row, account=ACCOUNT, db=None):
    db = db or DB([row])
    result = feedback._reconcile_task(db, task=deepcopy(row), domain_id="d", account=account, recent_posts_cache={})
    return result, db, db.rows["pipeline_tasks"][0]["publish_jsonb"]


@pytest.mark.parametrize("raw,expected", [
    ({}, {}), (None, {}), ([], {}), ({"likes": 0}, {"likes": 0}),
    ({"data": {"interact_info": {"liked_count": "4", "collected_count": 0, "comment_count": "2"}}},
     {"likes": 4, "collects": 0, "comments_count": 2}),
    ({"followers_delta": -3}, {"follows": -3}),
    ({"follows": 0, "likes": True, "views": "NaN", "shares": "inf", "collects": -1}, {"follows": 0}),
])
def test_mcp_unknown_is_not_zero_and_follows_survive(raw, expected):
    normalized = mcp._normalize_metrics(raw)
    assert {key: value for key, value in normalized.items() if key != "raw"} == expected


@pytest.mark.parametrize("values", [{"likes": 0}, {"views": 0, "likes": 3}, {}])
def test_mcp_partial_counters_have_no_rate_score(values):
    assert mcp._score_from_metrics(values) is None


def test_missing_published_at_has_no_checkpoint():
    row = task()
    row["published_at"] = None
    result, _, saved = reconcile(row, account={})
    assert saved["feedback_completed_hours"] == []
    assert saved["next_feedback_at"] is None
    assert result["feedback_state"] == "pending_publication_time"
    feedback.sync_pipeline_task_metrics_via_mcp.assert_not_called()


@pytest.mark.parametrize("age,completed,expected,missed", [
    (0.5, [], None, []), (1, [], 1, []), (4, [], 3, [1]), (25, [], 24, [1, 3]),
    (25, [24], None, [1, 3]), (2, [1], None, []), (25, [1], 24, [3]),
])
def test_single_latest_eligible_checkpoint(age, completed, expected, missed):
    result = feedback._observation_checkpoint(NOW - timedelta(hours=age), NOW, [1, 3, 24], completed)
    assert result == {"checkpoint_hours": expected, "missed_hours": missed}


def test_first_24h_read_does_not_fill_1h_and_3h():
    result, _, saved = reconcile(task())
    assert saved["feedback_completed_hours"] == [24]
    assert saved["feedback_missed_hours"] == [1, 3]
    assert len(saved["feedback_snapshots"]) == 1
    assert saved["feedback_snapshots"][0]["observed_at"] == NOW.isoformat()
    assert result["metrics_captured_at"] == NOW.isoformat()
    feedback.sync_pipeline_task_metrics_via_mcp.assert_not_called()


def test_stale_manual_observation_does_not_complete_later_checkpoint():
    row = task(observed_at=(NOW - timedelta(hours=23)).isoformat())
    row["metrics_jsonb"]["post_metrics_synced_at"] = NOW.isoformat()
    row["metrics_jsonb"]["observation"]["provider"] = "manual"
    result, _, saved = reconcile(row)
    assert saved["feedback_completed_hours"] == [1]
    assert saved["feedback_missed_hours"] == []
    assert result["metrics_captured_at"] == row["metrics_jsonb"]["observation"]["observed_at"]
    assert saved["last_feedback_at"] == result["metrics_captured_at"]


@pytest.mark.parametrize("observed", [None, "invalid", "2026-10-07T12:00:00", "2026-10-08T12:00:00Z", "2026-10-01T12:00:00Z"])
def test_invalid_or_missing_observation_time_never_completes_checkpoint(observed):
    row = task()
    row["metrics_jsonb"]["observation"]["observed_at"] = observed
    _, _, saved = reconcile(row)
    assert saved["feedback_completed_hours"] == []
    assert saved["feedback_analysis"]["basic_review"]["status"] == "pending"


def test_legacy_persisted_metrics_use_actual_sync_timestamp():
    row = task(observed_at=(NOW - timedelta(hours=21)).isoformat())
    del row["metrics_jsonb"]["observation"]
    _, _, saved = reconcile(row)
    assert saved["feedback_completed_hours"] == [3]
    assert saved["feedback_missed_hours"] == [1]


def test_observation_values_are_authoritative_even_if_post_metrics_disagree():
    row = task()
    row["metrics_jsonb"]["post_metrics"] = COMPLETE
    result, _, _ = reconcile(row)
    assert result["metrics"] == {"likes": 0}
    assert result["ces_score"] is None


@pytest.mark.parametrize("values", [{"views": 0, "likes": 0}, {"likes": 0}, COMPLETE])
def test_first_post_has_basic_review_without_fake_comparison(values):
    result, _, saved = reconcile(task(values=values))
    analysis = saved["feedback_analysis"]
    assert analysis["basic_review"]["status"] == "completed"
    assert analysis["basic_review"]["values"] == values
    assert analysis["basic_review"]["evidence_refs"][0]["source_ref"] == URL
    assert analysis["full_comparison_ready"] is False
    assert analysis["ces"] == {}
    assert result["feedback_state"] == "basic_review_done"
    assert result["status"] == "done"
    assert result["suggestions_created"] == 0
    assert analysis["prompt_upgrade_targets"] == []


def test_partial_metrics_no_full_ces_even_with_comparable_history():
    row = task(values={"views": 100, "likes": 4})
    history = [task(task_id=f"h{i}", values=COMPLETE, completed=[24],
                    published_at=f"2026-10-0{i}T00:00:00Z") for i in range(1, 4)]
    result, _, saved = reconcile(row, db=DB([row, *history]))
    assert result["ces_score"] is None
    assert saved["feedback_analysis"]["full_comparison_ready"] is False
    assert set(saved["feedback_analysis"]["basic_review"]["missing_fields"]) == {"collects", "comments", "shares", "follows"}


def test_full_comparison_requires_complete_metrics_and_three_history_posts():
    row = task(values=COMPLETE, completed=[1, 3])
    history = [task(task_id=f"h{i}", values=COMPLETE, completed=[24],
                    published_at=f"2026-10-0{i}T00:00:00Z") for i in range(1, 4)]
    result, _, saved = reconcile(row, db=DB([row, *history]))
    assert result["feedback_state"] == "retro_done"
    assert saved["feedback_analysis"]["full_comparison_ready"] is True
    assert isinstance(result["ces_score"], float)
    assert saved["feedback_analysis"]["attribution"]["causal_claim"] is False


def test_retry_same_snapshot_no_duplicate_review_or_terminal_write():
    _, db, saved = reconcile(task())
    before = deepcopy(saved)
    writes = len(db.writes)
    result, _, saved = reconcile(db.rows["pipeline_tasks"][0], db=db)
    assert saved == before
    assert len(db.writes) == writes
    assert result["changed"] is False


def test_later_refresh_does_not_replace_checkpoint_snapshot():
    _, db, saved = reconcile(task(observed_at=(NOW - timedelta(minutes=30)).isoformat()))
    snapshot = deepcopy(saved["feedback_snapshots"])
    row = db.rows["pipeline_tasks"][0]
    row["metrics_jsonb"] = task(values={"likes": 5})["metrics_jsonb"]
    _, _, saved = reconcile(row, db=db)
    assert saved["feedback_snapshots"] == snapshot
    assert saved["feedback_analysis"]["basic_review"]["values"] == {"likes": 5}


def test_cli_missing_identity_never_calls_legacy_collectors():
    row = task()
    row["publish_jsonb"]["identity"] = {}
    reconcile(row)
    feedback.collect_recent_published_posts.assert_not_called()
    feedback.sync_pipeline_task_metrics_via_mcp.assert_not_called()


def test_non_cli_mcp_failure_is_returned_without_fabricating_checkpoints():
    row = task(values={})
    result, _, saved = reconcile(row, account={})
    assert result["provider_status"] == "failed"
    assert result["provider_error"] == "unavailable"
    assert saved["feedback_completed_hours"] == []
    feedback.sync_pipeline_task_metrics_via_mcp.assert_called_once()


def test_completed_tasks_do_not_starve_due_older_tasks():
    done = []
    for i in range(75):
        row = task(task_id=f"done-{i}", completed=[1, 3, 24])
        row.update(status="done", stage="done")
        done.append(row)
    due = task(task_id="due")
    due["updated_at"] = "2026-10-01T00:00:00Z"
    db = DB([*done, due])
    result = feedback.reconcile_publish_feedback(db, domain_slug="ai_content", limit=1)
    assert [item["task_id"] for item in result["items"]] == ["due"]
    assert all(row["updated_at"] == NOW.isoformat() for row in db.rows["pipeline_tasks"] if row["id"].startswith("done-"))
    assert len(db.reads) < 20


def test_polling_rotates_unfinished_tasks_and_skips_not_yet_due():
    rows = [task(task_id=f"t{i}", values={}) for i in range(4)]
    for row in rows:
        row["updated_at"] = "2026-10-01T00:00:00Z"
    future = task(task_id="future", values={}, published_at=(NOW - timedelta(minutes=10)).isoformat())
    future["updated_at"] = "2026-09-01T00:00:00Z"
    db = DB([future, *rows])
    first = feedback.reconcile_publish_feedback(db, domain_slug="ai_content", limit=2)
    second = feedback.reconcile_publish_feedback(db, domain_slug="ai_content", limit=2)
    assert [item["task_id"] for item in first["items"]] == ["t0", "t1"]
    assert [item["task_id"] for item in second["items"]] == ["t2", "t3"]


def test_nested_metrics_used_by_legacy_reflection_with_unknowns_preserved():
    row = task(values={"likes": 0, "follows": -2})
    rows = reflection._load_recent_published(DB([row]), "d")
    assert rows[0]["metrics"] == {"likes": 0, "follows": -2}
    assert rows[0]["missing_fields"] == ["views", "collects", "comments", "shares"]
    assert rows[0]["_score"] is None


def test_legacy_reflection_partial_or_foreign_samples_hold_without_model_call():
    rows = [task(task_id=f"t{i}", values=COMPLETE, completed=[24]) for i in range(4)]
    for i, row in enumerate(rows):
        row["account_id"] = f"a{i}"
    db = DB(rows)
    result = reflection.reflect_and_upgrade(db, "ai_content")
    assert result["status"] == "hold"
    assert result["reason"] == "insufficient_evidence"
    reflection.get_router.assert_not_called()
    assert db.writes == []


def test_legacy_reflection_creates_only_pending_account_suggestion():
    rows = [task(task_id=f"t{i}", values=COMPLETE, completed=[1, 3, 24]) for i in range(4)]
    db = DB(rows)
    before = deepcopy(db.rows["domains"])
    result = reflection.reflect_and_upgrade(db, "ai_content")
    assert result["status"] == "pending_confirmation"
    assert db.rows["domains"] == before
    assert all(table == "memory_items" for table, _ in db.writes)
    suggestion = db.rows["memory_items"][0]
    assert suggestion["account_id"] == "a" and suggestion["status"] == "pending"
    assert all(row["id"] in suggestion["content"] for row in rows)
    samples = reflection.get_router.return_value.reflect_strategy.call_args.args
    assert {row["id"] for row in samples[0]}.isdisjoint({row["id"] for row in samples[1]})


def test_no_work_terminal_tasks_are_not_rewritten():
    row = task(completed=[1, 3, 24])
    row.update(status="done", stage="done")
    db = DB([row])
    result = feedback.reconcile_publish_feedback(db, domain_slug="ai_content", limit=1)
    assert result["items"] == []
    assert db.writes == []


def test_bounded_polling_eventually_passes_long_no_work_prefix():
    rows = [task(task_id=f"future-{i:04}", published_at=(NOW - timedelta(minutes=10)).isoformat(), values={})
            for i in range(260)]
    for row in rows:
        row["updated_at"] = "2026-09-01T00:00:00Z"
    db = DB([*rows, task(task_id="due")])
    first = feedback.reconcile_publish_feedback(db, domain_slug="ai_content", limit=1)
    second = feedback.reconcile_publish_feedback(db, domain_slug="ai_content", limit=1)
    assert first["items"] == []
    assert [item["task_id"] for item in second["items"]] == ["due"]
    assert len(db.reads) < 25


@pytest.mark.parametrize("defect", ["partial", "missing_time", "synthetic"])
def test_full_comparison_excludes_incomplete_or_unobserved_history(defect):
    row = task(values=COMPLETE)
    history = [task(task_id=f"h{i}", values=COMPLETE, completed=[24],
                    published_at=f"2026-10-0{i}T00:00:00Z") for i in range(1, 4)]
    metrics = history[-1]["metrics_jsonb"]
    if defect == "partial":
        metrics["observation"]["values"] = {"views": 100, "likes": 3}
    elif defect == "missing_time":
        metrics["observation"]["observed_at"] = None
    else:
        metrics["metrics_mode"] = "synthetic_preview_only"
    _, _, saved = reconcile(row, db=DB([row, *history]))
    assert saved["feedback_analysis"]["full_comparison_ready"] is False
    assert saved["feedback_analysis"]["ces"] == {}


def test_mcp_success_persists_normalized_observation_with_safe_identity(monkeypatch):
    row = task(values={})
    row["publish_jsonb"]["identity"]["published_url"] = URL + "?xsec_token=private"
    fetch = Mock(return_value={"status": "ok", "metrics": {"likes": 0, "follows": -2}, "tool_name": "get_feed_detail"})
    monkeypatch.setattr(mcp, "fetch_post_metrics_via_mcp", fetch)
    db = DB([row])
    result = mcp.sync_pipeline_task_metrics_via_mcp(db, row)
    assert result["status"] == "ok"
    saved = db.rows["pipeline_tasks"][0]["metrics_jsonb"]
    assert saved["metrics_mode"] == "real"
    assert saved["observation"] == {"values": {"likes": 0, "follows": -2}, "observed_at": NOW.isoformat(),
                                    "provider": "xhs_mcp_readonly", "source_ref": URL,
                                    "provenance": {"tool_name": "get_feed_detail"}}
    assert fetch.call_args.kwargs["published_url"] == URL + "?xsec_token=private"
    assert saved["post_metrics_score"] is None


def test_mcp_task_persistence_failure_is_not_success(monkeypatch):
    monkeypatch.setattr(mcp, "fetch_post_metrics_via_mcp", Mock(return_value={"status": "ok", "metrics": {"likes": 0}}))
    db = DB([task()])
    table = db.table

    def failing_table(name):
        query = table(name)
        if name == "pipeline_tasks":
            query.execute = Mock(side_effect=RuntimeError("write failed"))
        return query

    db.table = failing_table
    result = mcp.sync_pipeline_task_metrics_via_mcp(db, task())
    assert result["status"] == "failed"
    assert result["reason"] == "metrics_persistence_failed"


def test_batch_returns_partial_and_continues_after_provider_failure():
    db = DB([task(task_id="bad", values={}), task(task_id="good")], accounts=[{"id": "a", "config_jsonb": {}}])
    result = feedback.reconcile_publish_feedback(db, domain_slug="ai_content")
    assert result["status"] == "partial"
    assert result["summary"]["provider_failures"] == 1
    assert {item["task_id"] for item in result["items"]} == {"bad", "good"}


def test_api_observation_id_and_aliases_survive_reconcile():
    row = task(values={"likes": 0, "comments_count": 2, "followers_delta": -4})
    row["metrics_jsonb"]["observation_id"] = "observation-uuid-1"
    result, _, saved = reconcile(row)
    assert result["observation_id"] == "observation-uuid-1"
    assert result["metrics"] == {"likes": 0, "comments": 2, "follows": -4}
    assert saved["feedback_snapshots"][0]["observation_id"] == "observation-uuid-1"
    assert saved["feedback_analysis"]["basic_review"]["observation_id"] == "observation-uuid-1"
    assert set(saved["feedback_unavailable_fields"]) == {"views", "collects", "shares"}


def test_terminal_late_first_observation_refresh_never_claims_full_score():
    row = task(values=COMPLETE)
    row.update(status="done", stage="done")
    row["metrics_jsonb"]["observation_id"] = "late-observation"
    history = [task(task_id=f"h{i}", values=COMPLETE, completed=[1, 3, 24],
                    published_at=f"2026-10-0{i}T00:00:00Z") for i in range(1, 4)]
    for historical in history:
        historical.update(status="done", stage="done")
    db = DB([row, *history])
    result = feedback.reconcile_publish_feedback(db, domain_slug="ai_content", limit=1)
    assert result["items"][0]["task_id"] == "t"
    refreshed = result["items"][0]
    assert refreshed["feedback_missed_hours"] == [1, 3]
    assert refreshed["feedback_state"] == "basic_review_done"
    assert refreshed["full_comparison_ready"] is False
    assert refreshed["ces_score"] is None
    assert refreshed["status"] == "done"


def test_replayed_older_observation_cannot_backfill_missed_checkpoint():
    _, db, saved = reconcile(task())
    before = deepcopy(saved)
    row = db.rows["pipeline_tasks"][0]
    row["metrics_jsonb"] = task(observed_at=(NOW - timedelta(hours=23)).isoformat())["metrics_jsonb"]
    row["metrics_jsonb"]["observation"]["provider"] = "manual"
    _, _, saved = reconcile(row, db=db)
    assert saved == before


def test_legacy_reflection_with_missed_checkpoints_has_no_full_score():
    row = task(values=COMPLETE, completed=[24])
    row["publish_jsonb"]["feedback_missed_hours"] = [1, 3]
    assert reflection._load_recent_published(DB([row]), "d")[0]["_score"] is None


def test_terminal_observation_refresh_is_not_starved_by_active_queue():
    rows = [task(task_id=f"active-{i}", values={}) for i in range(10)]
    terminal = task(task_id="terminal", completed=[24])
    terminal.update(status="done", stage="done")
    terminal["metrics_jsonb"]["observation_id"] = "new-observation"
    terminal["publish_jsonb"]["last_feedback_at"] = (NOW - timedelta(minutes=10)).isoformat()
    db = DB([*rows, terminal])
    first = feedback.reconcile_publish_feedback(db, domain_slug="ai_content", limit=1)
    second = feedback.reconcile_publish_feedback(db, domain_slug="ai_content", limit=1)
    assert first["items"][0]["task_id"].startswith("active-")
    assert second["items"][0]["task_id"] == "terminal"


def test_pipeline_task_scope_is_filtered_before_limit():
    rows = [task(task_id=f"other-{i}") for i in range(75)]
    selected = task(task_id="selected")
    db = DB([*rows, selected])
    result = feedback.reconcile_publish_feedback(db, domain_slug="ai_content", account_id="a",
                                                 pipeline_task_id="selected", limit=1)
    assert [item["task_id"] for item in result["items"]] == ["selected"]
    assert len(db.writes) == 1
    assert all(row["status"] == "published" for row in db.rows["pipeline_tasks"] if row["id"].startswith("other-"))


@pytest.mark.parametrize("mismatch", ["account_id", "domain_id", "id"])
def test_pipeline_task_scope_never_falls_back_to_another_task(mismatch):
    row = task(task_id="selected")
    row[mismatch] = "foreign"
    db = DB([task(task_id="other"), row])
    result = feedback.reconcile_publish_feedback(db, domain_slug="ai_content", account_id="a",
                                                 pipeline_task_id="selected", limit=1)
    assert result["items"] == []
    assert db.writes == []


def test_legacy_reflection_honors_saved_custom_checkpoint_schedule():
    row = task(values=COMPLETE, completed=[24])
    row["publish_jsonb"]["feedback_schedule_hours"] = [24]
    assert reflection._load_recent_published(DB([row]), "d")[0]["_score"] is not None


def test_legacy_reflection_rejects_checkpoint_not_reached_at_observed_time():
    row = task(values=COMPLETE, completed=[1, 3, 24], observed_at=(NOW - timedelta(hours=23)).isoformat())
    assert reflection._load_recent_published(DB([row]), "d")[0]["_score"] is None


def test_batch_task_write_failure_is_partial_and_does_not_stop_other_tasks():
    db = DB([task(task_id="bad"), task(task_id="good")])
    table = db.table

    def failing_table(name):
        query = table(name)
        execute = query.execute

        def run():
            if name == "pipeline_tasks" and query.patch is not None:
                affected = [row for row in db.rows[name] if all(f(row) for f in query.filters)]
                if any(row["id"] == "bad" for row in affected):
                    raise RuntimeError("write failed")
            return execute()

        query.execute = run
        return query

    db.table = failing_table
    result = feedback.reconcile_publish_feedback(db, domain_slug="ai_content")
    assert result["status"] == "partial"
    assert result["summary"]["task_failures"] == 1
    bad = next(item for item in result["items"] if item["task_id"] == "bad")
    assert bad["error"] == "feedback_reconciliation_failed"
    assert next(item for item in result["items"] if item["task_id"] == "good")["feedback_state"] == "basic_review_done"


def test_zero_views_is_observed_but_rate_unavailable():
    result, _, saved = reconcile(task(values={"views": 0, "likes": 0}))
    assert result["real_metrics"] is True
    assert result["basic_review"]["rate_available"] is False
    assert saved["feedback_completed_hours"] == [24]
    assert result["ces_score"] is None


def test_feedback_pending_suggestion_persists_own_observation_and_evidence():
    row = task(values={**COMPLETE, "likes": 0}, completed=[1, 3])
    row["metrics_jsonb"]["observation_id"] = "own-observation"
    history = [task(task_id=f"h{i}", values=COMPLETE, completed=[1, 3, 24],
                    published_at=f"2026-10-0{i}T00:00:00Z") for i in range(1, 4)]
    result, db, _ = reconcile(row, db=DB([row, *history]))
    assert result["suggestions_created"] == 1
    body = json.loads(db.rows["memory_items"][0]["content"])
    own = next(ref for ref in body["evidence_refs"] if ref["task_id"] == "t")
    assert own["observation"]["values"]["likes"] == 0
    assert own["observation"]["observed_at"] == NOW.isoformat()
    assert own["observation"]["observation_id"] == "own-observation"
    assert body["hold"] is True and body["executable"] is False
    assert "proposal" not in body


def test_reflection_valid_proposal_has_before_after_and_saved_evidence():
    rows = [task(task_id=f"t{i}", values=COMPLETE, completed=[1, 3, 24]) for i in range(4)]
    account = {"id": "a", "config_jsonb": {"publish_preferences": {"next_publish_slot_local": "10:00"}, "sop_latest_version": 2}}
    proposal = {"target": "publish_preferences", "before": {"next_publish_slot_local": "10:00"},
                "after": {"next_publish_slot_local": "20:00"}}
    reflection.get_router.return_value.reflect_strategy.return_value = {"proposal": proposal}
    db = DB(rows, accounts=[account])
    reflection.reflect_and_upgrade(db, "ai_content")
    body = json.loads(db.rows["memory_items"][0]["content"])
    assert body["proposal"]["before"] == proposal["before"]
    assert body["proposal"]["after"] == proposal["after"]
    assert body["proposal"]["before_version"] == 2
    assert body["executable"] is True and body["hold"] is False
    assert all(ref["task_id"] in {row["id"] for row in rows} and ref["observation"]["values"] for ref in body["evidence_refs"])
    assert body["evidence_refs"]
    assert db.rows["channel_accounts"][0] == account


def test_reflection_prompt_only_or_stale_proposal_is_non_executable_hold():
    rows = [task(task_id=f"t{i}", values=COMPLETE, completed=[1, 3, 24]) for i in range(4)]
    reflection.get_router.return_value.reflect_strategy.return_value = {"proposal": {
        "target": "publish_preferences", "before": {"next_publish_slot_local": "wrong"},
        "after": {"next_publish_slot_local": "20:00"}}}
    db = DB(rows)
    reflection.reflect_and_upgrade(db, "ai_content")
    body = json.loads(db.rows["memory_items"][0]["content"])
    assert body["hold"] is True and body["executable"] is False
    assert "proposal" not in body


@pytest.mark.parametrize("conflict", ["updated_at", "revision", "account_id", "domain_id", "legacy_identity", "observation"])
def test_feedback_cas_preserves_interleaved_correction_and_creates_no_suggestion(conflict):
    row = task(values={**COMPLETE, "likes": 0}, completed=[1, 3])
    row["publish_jsonb"]["manual_publication"] = {"revision": "revision-1"}
    history = [task(task_id=f"h{i}", values=COMPLETE, completed=[1, 3, 24],
                    published_at=f"2026-10-0{i}T00:00:00Z") for i in range(1, 4)]
    db = DB([row, *history])
    table = db.table
    corrected = []

    def interleaving_table(name):
        query = table(name)
        execute = query.execute

        def run():
            if name == "pipeline_tasks" and query.patch is not None and not corrected:
                current = db.rows[name][0]
                if conflict == "updated_at":
                    current["updated_at"] = (NOW + timedelta(seconds=1)).isoformat()
                elif conflict == "revision":
                    current["publish_jsonb"]["manual_publication"]["revision"] = "revision-2"
                    current["publish_jsonb"]["feedback_completed_hours"] = []
                elif conflict in {"account_id", "domain_id"}:
                    current[conflict] = "foreign"
                elif conflict == "observation":
                    current["metrics_jsonb"] = task(values={"likes": 7})["metrics_jsonb"]
                else:
                    current["publish_jsonb"]["identity"]["feed_id"] = "corrected-note"
                corrected.append(deepcopy(current))
            return execute()

        query.execute = run
        return query

    db.table = interleaving_table
    result, _, _ = reconcile(row, db=db)
    assert db.rows["pipeline_tasks"][0] == corrected[0]
    assert result["status"] == "stale"
    assert result["changed"] is False and result["suggestions_created"] == 0
    assert result["full_comparison_ready"] is False
    assert result["metrics_stale"] is True
    assert result["metrics"] == row["metrics_jsonb"]["observation"]["values"]
    assert db.rows.get("memory_items", []) == []


def test_full_feedback_can_propose_real_evidence_safeguards_with_before_after():
    row = task(values={**COMPLETE, "likes": 0}, completed=[1, 3])
    row["metrics_jsonb"]["observation_id"] = "own-observation"
    history = [task(task_id=f"h{i}", values=COMPLETE, completed=[1, 3, 24],
                    published_at=f"2026-10-0{i}T00:00:00Z") for i in range(1, 4)]
    before = {"checkpoints_hours": [1, 3, 24], "synthetic_preview_enabled": True,
              "require_real_metrics_for_upgrade": False, "auto_retro_after_last_checkpoint": True}
    account = {"id": "a", "config_jsonb": {**ACCOUNT["config_jsonb"], "feedback_plan": before, "sop_latest_version": 3}}
    db = DB([row, *history], accounts=[account])
    result, _, _ = reconcile(row, account=account, db=db)
    assert result["suggestions_created"] == 1
    body = json.loads(db.rows["memory_items"][0]["content"])
    assert body["hold"] is False and body["executable"] is True
    proposal = body["proposal"]
    assert proposal["target"] == "feedback_plan" and proposal["before"] == before
    assert proposal["after"] == {**before, "synthetic_preview_enabled": False, "require_real_metrics_for_upgrade": True}
    assert proposal["before_version"] == 3
    own = next(ref for ref in body["evidence_refs"] if ref["task_id"] == "t")
    assert own["observation"]["observation_id"] == "own-observation"
    assert body["auto_apply"] is False and body["causal_claim"] is False
    assert db.rows["channel_accounts"][0] == account


def test_correction_during_mcp_refresh_is_stale_not_a_new_write_snapshot(monkeypatch):
    row = task(values={}, completed=[1, 3])
    row["publish_jsonb"]["manual_publication"] = {"revision": "revision-1"}
    db = DB([row])
    corrected = []

    def sync(*args):
        current = db.rows["pipeline_tasks"][0]
        current["publish_jsonb"]["manual_publication"]["revision"] = "revision-2"
        current["publish_jsonb"]["feedback_completed_hours"] = []
        current["metrics_jsonb"] = task()["metrics_jsonb"]
        current["updated_at"] = (NOW + timedelta(seconds=1)).isoformat()
        corrected.append(deepcopy(current))
        return {"status": "ok"}

    monkeypatch.setattr(feedback, "sync_pipeline_task_metrics_via_mcp", sync)
    result, _, _ = reconcile(row, account={}, db=db)
    assert result["status"] == "stale"
    assert db.rows["pipeline_tasks"][0] == corrected[0]
    assert db.writes == []
    assert db.rows.get("memory_items", []) == []


def data_api_updates(db, requests):
    table = db.table
    pending = []

    def transport(request):
        requests.append(request)
        assert len(str(request.url)) < 8000
        query = pending[-1]
        rows = [row for row in db.rows["pipeline_tasks"] if all(f(row) for f in query.filters)]
        patch = json.loads(request.content)
        for row in rows:
            row.update(deepcopy(patch))
        return httpx.Response(200, json=deepcopy(rows))

    http = httpx.Client(transport=httpx.MockTransport(transport))
    api = SyncPostgrestClient("https://data-api.example.test/rest/v1", http_client=http)

    def bridge(name):
        query = table(name)
        execute = query.execute

        def run():
            if name != "pipeline_tasks" or query.patch is None:
                return execute()
            pending.append(query)
            wire = api.from_(name).update(query.patch)
            for key, value in query.wire_filters:
                wire = wire.eq(key, value) if value is not None else wire.is_(key, "null")
            return wire.execute()

        query.execute = run
        return query

    db.table = bridge
    return http


def test_large_feedback_json_uses_bounded_real_data_api_request_url():
    row = task()
    row["publish_jsonb"]["manual_publication"] = {"revision": "revision-1"}
    row["publish_jsonb"]["publication_history"] = [{"archived_body": "x" * 33000}]
    row["metrics_jsonb"]["raw_provider_payload"] = {"body": "y" * 33000}
    row["payload_jsonb"]["body"] = "z" * 33000
    db, requests = DB([row]), []
    with data_api_updates(db, requests):
        result, _, saved = reconcile(row, db=db)
    assert result["status"] == "done" and result["changed"] is True
    assert len(requests) == 1
    assert len(str(requests[0].url)) < 8000
    assert len(requests[0].url.query) <= 3000
    assert "publish_jsonb" not in requests[0].url.params
    assert "metrics_jsonb" not in requests[0].url.params
    assert saved["publication_history"] == row["publish_jsonb"]["publication_history"]


@pytest.mark.parametrize("updated_at", [None, "invalid", "2026-10-07T12:00:00"])
def test_large_unknown_feedback_without_usable_timestamp_fails_closed(updated_at):
    row = task()
    row["updated_at"] = updated_at
    row["publish_jsonb"]["publication_history"] = [{"unknown": "x" * 33000}]
    db, requests = DB([row]), []
    before = deepcopy(db.rows)
    with data_api_updates(db, requests):
        result, _, _ = reconcile(row, db=db)
    assert result["status"] == "stale" and result["suggestions_created"] == 0
    assert requests == [] and db.rows == before
