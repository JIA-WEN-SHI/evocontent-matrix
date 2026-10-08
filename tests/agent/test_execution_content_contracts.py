from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.agents.subagents import analysis_agent, collector_agent, copy_agent, review_agent
from app.agents.subagents.base import load_source_rows
from app.execution import router
from app.orchestration import publish_feedback
from app.runtime.accounts.kb_libraries import derive_runtime_guidance
from app.runtime.accounts.kb_libraries import load_kb_libraries
from app.tools.collection import playwright_tools
from app.tools.publishing import publisher


class Query:
    def __init__(self, client, table):
        self.client, self.table_name = client, table
        self.filters, self.patch = [], None

    def select(self, *args):
        return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def is_(self, field, value):
        self.filters.append((field, None))
        return self

    def order(self, field, **kwargs):
        if self.table_name == "intelligence_items":
            assert field == "captured_at", "This table has no updated_at column"
        return self

    def limit(self, *args):
        return self

    def update(self, patch):
        self.patch = patch
        return self

    def execute(self):
        rows = [row for row in self.client.rows.get(self.table_name, [])
                if all(row.get(field) == value for field, value in self.filters)]
        if self.patch is not None:
            self.client.updates.append(self.patch)
        return SimpleNamespace(data=rows)


class Client:
    def __init__(self, rows=None):
        self.rows, self.updates = rows or {}, []

    def table(self, name):
        return Query(self, name)


@pytest.fixture
def sources():
    return Client({
        "domains": [{"id": "d", "slug": "japan_immigration"}],
        "intelligence_items": [{"id": "intel-1", "domain_id": "d", "account_id": "a",
                                "raw_text": "日本移民规划应先核对申请条件和材料。", "source_url": "https://example.test/source",
                                "captured_at": "2026-09-01T00:00:00Z", "meta_jsonb": {}}],
    })


def call_analysis(client, **kwargs):
    return analysis_agent.run_analysis_agent(client, domain_slug="japan_immigration", domain_id="d",
                                             account_id="a", triggered_by="test", trace_id="t", **kwargs)


def call_copy(client):
    return copy_agent.run_copy_agent(client, domain_slug="japan_immigration", account_id="a",
                                    triggered_by="test", trace_id="t")


@pytest.fixture
def content_mocks(monkeypatch):
    libs = {"rulebooks": [{"rule_code": "truth", "rule_text": "禁止编造事实", "rule_jsonb": {"hard_block_patterns": ["保证获批"]}}],
            "playbooks": [{"playbook_code": "method", "version": 1, "method_steps": ["先核对原文"]}],
            "io_rules": [{"rule_code": "copy-output", "output_template": {"must_explain_why": True}}]}
    model = Mock()
    model.analyze_hot_posts.return_value = {"status": "ok", "results": [{"core_pain_point": "材料不清晰", "structure_pattern": "清单"}]}
    model.generate_draft.return_value = {
        "title": "日本移民规划先核对这些材料",
        "body": "先说结论：日本移民规划先核对当前申请条件。\n\n"
                + "经营管理签证的适用条件、申请材料和当前办理要求，需要以官方信息为准。" * 2
                + "\n\n在决定路径之前，先整理个人情况、已有材料和仍需核实的问题。" * 2
                + "\n\n日本永住与其他路径的条件不同，应逐项比较。\n\n#日本移民",
    }
    for module in (analysis_agent, copy_agent, review_agent):
        monkeypatch.setattr(module, "load_kb_libraries", lambda *a, **k: libs)
    monkeypatch.setattr(analysis_agent, "get_router", lambda: model)
    monkeypatch.setattr(copy_agent, "get_router", lambda: model)
    return model


def test_analysis_reads_collected_schema_and_keeps_real_evidence(sources, content_mocks):
    result = call_analysis(sources, payload={"analysis_prompt": "自定义分析"})
    assert result["status"] == "success"
    assert result["evidence_refs"][0]["source_ref"] == "intel-1"
    assert len(result["evidence_refs"]) == 1
    assert result["result"]["sample_count"] == 1
    assert "sample_window_days" not in result["result"]
    prompt = content_mocks.analyze_hot_posts.call_args.kwargs["analysis_prompt"]
    assert "自定义分析" in prompt and "禁止编造事实" in prompt


def test_sources_are_account_scoped_and_exclude_deleted_cases():
    client = Client({"cases": [
        {"id": "own", "domain_id": "d", "account_id": "a"},
        {"id": "other", "domain_id": "d", "account_id": "b"},
        {"id": "deleted", "domain_id": "d", "account_id": "a", "deleted_at": "yesterday"},
    ]})
    assert [r["id"] for r in load_source_rows(client, table="cases", domain_id="d", account_id="a")] == ["own"]


def test_analysis_empty_or_database_failure_never_succeeds(content_mocks):
    assert call_analysis(Client())["status"] == "blocked"
    bad = Mock()
    bad.table.side_effect = RuntimeError("database unavailable")
    assert call_analysis(bad)["code"] == "source_read_failed"
    content_mocks.analyze_hot_posts.assert_not_called()


def test_analysis_rejects_invalid_model_response(sources, content_mocks):
    content_mocks.analyze_hot_posts.return_value = {"status": "failed", "results": []}
    assert call_analysis(sources)["code"] == "invalid_analysis_result"


def test_copy_consumes_analysis_and_completes_contract(sources, content_mocks):
    result = call_copy(sources)
    assert result["status"] == "success", result
    post = result["result"]
    assert post["cta"] in post["body"]
    assert len(post["hashtags"]) >= 3
    assert all(tag in post["body"] for tag in post["hashtags"])
    assert post["why_this_writing"]["analysis_results"]
    assert post["review_required"] is True
    strategy = content_mocks.generate_draft.call_args.kwargs["strategy"]
    assert "材料不清晰" in strategy["draft"]
    assert result["evidence_refs"][0]["source_ref"] == "intel-1"


@pytest.mark.parametrize("draft,code", [({"title": "x", "body": ""}, "incomplete_draft"),
                                         ({"title": "日本移民申请条件核对清单", "body": "保证获批"}, "content_quality_failed")])
def test_copy_blocks_empty_or_forbidden_content(sources, content_mocks, draft, code):
    content_mocks.generate_draft.return_value = draft
    assert call_copy(sources)["code"] == code


def test_copy_does_not_generate_without_sources(sources, content_mocks):
    sources.rows["intelligence_items"] = []
    assert call_copy(sources)["status"] == "blocked"
    content_mocks.generate_draft.assert_not_called()


def test_copy_enforces_io_length_bounds_even_when_weighted_score_passes(sources, content_mocks, monkeypatch):
    monkeypatch.setattr(copy_agent, "load_kb_libraries", lambda *a, **k: {
        "io_rules": [{"rule_code": "long-body", "quality_gate_jsonb": {"body_chars": [700, 1000]}}],
    })
    result = call_copy(sources)
    assert result["code"] == "content_quality_failed"
    assert "body_length" in result["result"]["quality_gate"]["failed_checks"]


def test_io_contract_guidance_includes_output_and_quality():
    guidance = derive_runtime_guidance({"io_rules": [{"rule_code": "copy", "quality_gate_jsonb": {
        "title_chars": [12, 25], "body_chars": [180, 800], "hashtags_min": 4},
        "output_template": {"must_explain_why": True}}]})
    assert guidance["quality_gate"] == {"title_min": 12, "title_max": 25, "body_min": 180, "body_max": 800, "hashtag_target": 4}
    assert "must_explain_why" in guidance["prompt_appendix"]


@pytest.mark.parametrize("raw,expected", [
    ({}, ({}, False)), ({"post_metrics": {"likes": 0}}, ({"likes": 0.0}, True)),
    ({"post_metrics": {"likes": "unknown", "views": "NaN", "shares": -1}}, ({}, False)),
    ({"post_metrics": {"likes": 2}, "metrics_mode": "synthetic_preview_only"}, ({}, False)),
])
def test_metrics_keep_observed_zeros_and_never_invent_missing_counts(raw, expected):
    assert publish_feedback._extract_real_metrics(raw) == expected


def test_failed_feedback_sync_keeps_checkpoint_pending_and_never_synthesizes(monkeypatch):
    sync = Mock(return_value={"status": "failed"})
    monkeypatch.setattr(publish_feedback, "sync_pipeline_task_metrics_via_mcp", sync)
    task = {"id": "task-1", "status": "published", "published_at": "2020-01-01T00:00:00Z",
            "publish_jsonb": {"identity": {"feed_id": "post-1"}}, "metrics_jsonb": {}, "payload_jsonb": {"title": "sample"}}
    client = Client({"pipeline_tasks": [task]})
    result = publish_feedback._reconcile_task(client, task=task, domain_id="d",
        account={"config_jsonb": {"feedback_plan": {"synthetic_preview_enabled": True}}}, recent_posts_cache={})
    assert result["metrics_mode"] == "pending" and result["metrics"] == {}
    assert result["ces_score"] is None
    assert client.updates[-1]["publish_jsonb"]["feedback_completed_hours"] == []
    assert client.updates[-1]["publish_jsonb"]["next_feedback_at"]


def test_recovered_identity_is_used_for_same_round_sync(monkeypatch):
    monkeypatch.setattr(publish_feedback, "collect_recent_published_posts", lambda *a, **k: {"items": []})
    monkeypatch.setattr(publish_feedback, "_recover_identity_from_recent_posts", lambda *a: {"feed_id": "recovered"})
    sync = Mock(return_value={"status": "failed"})
    monkeypatch.setattr(publish_feedback, "sync_pipeline_task_metrics_via_mcp", sync)
    task = {"id": "task-1", "account_id": "a", "status": "published", "published_at": "2020-01-01T00:00:00Z"}
    publish_feedback._reconcile_task(Client({"pipeline_tasks": [task]}), task=task,
        domain_id="d", account={}, recent_posts_cache={})
    assert sync.call_args.args[1]["publish_jsonb"]["identity"]["feed_id"] == "recovered"


@pytest.mark.parametrize("metrics", [{"likes": 0}, {"views": 500, "likes": 5}])
def test_partial_metrics_are_kept_without_fabricated_quality_or_attribution(monkeypatch, metrics):
    monkeypatch.setattr(publish_feedback, "sync_pipeline_task_metrics_via_mcp", lambda *a: {"status": "ok"})
    task = {"id": "task-1", "status": "published", "published_at": "2020-01-01T00:00:00Z",
            "publish_jsonb": {"identity": {"feed_id": "post-1"}}, "metrics_jsonb": {"post_metrics": metrics}}
    client = Client({"pipeline_tasks": [task]})
    result = publish_feedback._reconcile_task(client, task=task, domain_id="d", account={}, recent_posts_cache={})
    assert result["real_metrics"] is True and result["metrics"] == metrics
    assert result["ces_score"] is None and result["suggestions_created"] == 0
    analysis = client.updates[-1]["publish_jsonb"]["feedback_analysis"]
    assert analysis["attribution"] == {} and analysis["prompt_upgrade_targets"] == []


def test_library_loading_respects_scope_stage_and_inactive_override():
    client = Client({
        "domains": [{"id": "d", "slug": "topic"}],
        "kb_rulebooks": [
            {"id": "global", "domain_id": "d", "rule_code": "rule", "status": "active", "applies_to": ["copy"]},
            {"id": "override", "domain_id": "d", "account_id": "a", "rule_code": "rule", "status": "inactive", "applies_to": ["copy"]},
        ],
        "kb_playbooks": [
            {"domain_id": "d", "playbook_code": "write", "version": 2, "status": "active", "stage": "copy"},
            {"domain_id": "d", "playbook_code": "write", "account_id": "a", "version": 1, "status": "active", "stage": "copy"},
            {"domain_id": "d", "playbook_code": "review", "version": 1, "status": "active", "stage": "review"},
        ],
        "kb_io_rules": [
            {"domain_id": "d", "rule_code": "out", "status": "active", "direction": "egress", "entity_type": "asset", "target_agent": "copy_agent"},
            {"domain_id": "d", "rule_code": "other", "status": "active", "direction": "egress", "entity_type": "review", "target_agent": "review_agent"},
        ],
    })
    libs = load_kb_libraries(client, domain_slug="topic", account_id="a", applies_to="copy", stage="copy",
                            direction="egress", entity_type="asset", target_agent="copy_agent")
    assert libs["rulebooks"] == []
    assert len(libs["playbooks"]) == 1 and libs["playbooks"][0]["account_id"] == "a"
    assert [row["rule_code"] for row in libs["io_rules"]] == ["out"]


def test_review_only_uses_real_task_refs_and_returns_actions(monkeypatch, content_mocks):
    monkeypatch.setattr(review_agent, "reconcile_publish_feedback", lambda *a, **k: {
        "status": "ok", "items": [
            {"task_id": "real", "real_metrics": True, "metrics_mode": "real", "history_comparison": {
                "status": "compared", "keep_actions": [{"action": "保持结构"}], "adjust_actions": [{"action": "测试标题"}]}},
            {"task_id": "fake", "real_metrics": False, "metrics_mode": "synthetic_preview_only"},
        ]})
    result = review_agent.run_review_agent(Client(), domain_slug="d", account_id="a", triggered_by="test", trace_id="t")
    assert result["status"] == "success"
    assert [ref["source_ref"] for ref in result["evidence_refs"]] == ["real"]
    assert result["result"]["keep_actions"] and result["result"]["adjust_actions"]
    assert result["result"]["discard_actions"] == []


def test_review_waits_for_real_feedback(monkeypatch, content_mocks):
    monkeypatch.setattr(review_agent, "reconcile_publish_feedback", lambda *a, **k: {"status": "ok", "items": []})
    result = review_agent.run_review_agent(Client(), domain_slug="d", account_id="a", triggered_by="test", trace_id="t")
    assert result["code"] == "awaiting_real_metrics" and result["retryable"]
    assert result["evidence_refs"] == []


@pytest.mark.parametrize("collected", [{"status": "failed", "reason": "account_not_found"},
                                       {"status": "ok", "collected": 0}, {"status": "ok", "collected": 8, "preview": []}])
def test_collector_propagates_failure_and_empty_collection(monkeypatch, collected):
    monkeypatch.setattr(collector_agent, "run_account_collection", lambda *a, **k: collected)
    result = collector_agent.run_collector_agent(Client(), domain_slug="d", account_id="a", triggered_by="test", trace_id="t")
    assert result["status"] != "success"
    assert result["evidence_refs"] == []


def test_collector_preserves_source_url_and_capture_time(monkeypatch):
    collected = {"status": "ok", "collected": 1, "preview": [
        {"source_type": "web", "source_url": "https://example.test/real", "captured_at": "2026-09-01T00:00:00Z"}]}
    monkeypatch.setattr(collector_agent, "run_account_collection", lambda *a, **k: collected)
    result = collector_agent.run_collector_agent(Client(), domain_slug="d", account_id="a", triggered_by="test", trace_id="t")
    assert result["status"] == "success"
    assert result["evidence_refs"] == [{"source_type": "web", "source_ref": "https://example.test/real", "timestamp": "2026-09-01T00:00:00Z"}]


@pytest.fixture
def route_settings(monkeypatch):
    settings = SimpleNamespace(publish_method_order="playwright,mcp,rpa", xhs_mcp_readonly_enabled=True, xhs_mcp_bridge_to_playwright=False)
    monkeypatch.setattr(router, "get_settings", lambda: settings)
    return settings


def test_route_supports_list_override_and_filters_unsupported_bridge(route_settings):
    assert router.resolve_publish_route({"channel": "xiaohongshu", "meta_jsonb": {"publish_method_order": ["rpa", "playwright", "rpa"]}})["order"] == ["rpa", "playwright"]
    assert "mcp" not in router.resolve_publish_route({"channel": "other"})["order"]
    assert "mcp" not in router.resolve_publish_route({"channel": "xiaohongshu", "meta_jsonb": {"xhs_publish_tab": "长文"}})["order"]


def test_retired_publish_fallback_calls_neither_primary_nor_backup(monkeypatch, route_settings):
    primary = Mock(return_value={"status": "failed", "error": "not_ready"})
    backup = Mock(return_value={"status": "success", "remote_post_id": "mock-id"})
    monkeypatch.setattr(publisher, "publish_with_playwright", primary)
    monkeypatch.setattr(publisher, "publish_with_mcp", backup)
    result = publisher.publish_with_fallback({"channel": "xiaohongshu"})
    assert result["status"] == "blocked"
    assert result["publish_attempts"] == []
    primary.assert_not_called()
    backup.assert_not_called()


def test_retired_publish_cannot_report_success_from_an_adapter(monkeypatch, route_settings):
    monkeypatch.setattr(publisher, "publish_with_playwright", lambda task: {"status": "success"})
    backup = Mock(side_effect=AssertionError("must not publish again"))
    monkeypatch.setattr(publisher, "publish_with_mcp", backup)
    result = publisher.publish_with_fallback({"channel": "xiaohongshu"})
    assert result["status"] == "blocked"
    assert "published_url" not in result
    backup.assert_not_called()


def test_rpa_override_cannot_reenable_retired_publishing(route_settings):
    result = publisher.publish_with_fallback({"channel": "xiaohongshu", "meta_jsonb": {"publish_method_order": "rpa"}})
    assert result["status"] == "blocked" and result["error"] == "delegated_publishing_disabled"
    assert result["publish_attempts"] == []


def test_note_manager_uses_readable_selector(monkeypatch):
    page = Mock()
    monkeypatch.setattr(playwright_tools, "_navigate_if_needed", lambda *a, **k: None)
    monkeypatch.setattr(playwright_tools, "paced_wait", lambda *a, **k: None)
    playwright_tools._open_creator_note_manager(page)
    page.get_by_text.assert_called_once_with("笔记管理", exact=True)


def test_recent_posts_does_not_misrepresent_dashboard_as_post(monkeypatch):
    page = Mock()
    monkeypatch.setattr(playwright_tools, "_get_channel_account_runtime", lambda *a: {"id": "a"})
    monkeypatch.setattr(playwright_tools, "upsert_account_runtime", lambda *a: None)
    monkeypatch.setattr(playwright_tools, "account_execution_scope", lambda *a, **k: nullcontext())
    monkeypatch.setattr(playwright_tools, "playwright_page_scope", lambda *a, **k: nullcontext(page))
    for name in ("_open_creator_note_manager", "_wait_for_results_loaded"):
        monkeypatch.setattr(playwright_tools, name, lambda *a: None)
    monkeypatch.setattr(playwright_tools, "_safe_current_url", lambda *a: "https://example.test")
    monkeypatch.setattr(playwright_tools, "_collect_profile_note_cards", lambda *a: [])
    monkeypatch.setattr(playwright_tools, "_finish_and_return", lambda page, url, result: result)
    monkeypatch.setattr(playwright_tools, "_collect_dashboard_snapshot", Mock(side_effect=AssertionError("dashboard is not a post")))
    assert playwright_tools.collect_recent_published_posts(Client(), account_id="a")["items"] == []
