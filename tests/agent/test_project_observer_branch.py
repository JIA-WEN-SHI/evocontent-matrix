import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.model_router import ModelRouter
from app.orchestration import pipeline_runner as runner


BRANCH = "project_observer"
PROJECT = {
    "name": "Fooocus", "industry": "插画", "source_url": "https://github.com/lllyasviel/Fooocus",
    "summary": "基于 SDXL，用扩散模型生成图片，自动处理提示词和部分参数。",
    "advantages": "支持局部重绘、扩图，可以离线运行。",
    "input": "一句画面描述", "output": "对应的图片",
    "affected_role": "靠插画接单的人", "technology_keywords": ["SDXL", "扩散模型"],
    "imagined_scene": "他放大了图片。手里的数位笔，还没放下。",
    "checked_at": "2026-10-07T08:00:00Z",
}
BODY = (
    "雨夜街边的一家旧书店。\n\n输入这句描述，输出对应的图片。\n\n"
    "它基于 SDXL，用扩散模型生成图片，自动处理提示词和部分参数，支持局部重绘、扩图和离线运行。[项目介绍](https://github.com/lllyasviel/Fooocus)\n\n"
    "想象一个靠插画接单的人看到这个演示。\n\n他放大了图片。手里的数位笔，还没放下。\n\n"
    "这个项目叫 Fooocus。"
)
TITLE = "AI + 插画：一句描述生成图片"


def router():
    model = ModelRouter.__new__(ModelRouter)
    model.openai = model.anthropic = None
    model.openai_model_draft = "test-model"
    return model


def strategy():
    return {"content_branch": BRANCH, "project_reference": deepcopy(PROJECT),
            "focus_keywords": ["AI", "工作流"], "draft": "旧规则要求每篇输出分步教程"}


def test_observer_prompt_replaces_lead_generation_format():
    prompt = router()._build_draft_system_prompt(strategy(), "xiaohongshu")
    assert "共情" in prompt and "第三" in prompt
    assert "我觉得" in prompt and "不是固定" in prompt
    assert "开头必须出现“先说结论：”" not in prompt
    assert "结尾有评论互动引导" not in prompt
    assert "正文附带 3-6" not in prompt
    assert "旧规则要求每篇输出分步教程" not in prompt
    assert "AI + 行业" in prompt


def test_tutorial_prompt_keeps_existing_format():
    prompt = router()._build_draft_system_prompt({}, "xiaohongshu")
    assert "开头必须出现“先说结论：”" in prompt


def test_observer_fallback_uses_only_supplied_project_and_keeps_name_last():
    draft = router().generate_draft(strategy(), [], "xiaohongshu")
    assert draft["body"].endswith("Fooocus。")
    assert "SDXL" in draft["body"] and "输入" in draft["body"] and "输出" in draft["body"]
    assert "先说结论" not in draft["body"] and "日本" not in draft["body"]
    assert "私信" not in draft["body"] and "#" not in draft["body"]
    assert 'AI' in draft['title'] and PROJECT['industry'] in draft['title']


def test_observer_fallback_does_not_reuse_old_emotional_headline():
    cfg = strategy()
    cfg['project_reference']['headline'] = '一句描述生成图片，画笔还握在手里'
    assert router().generate_draft(cfg, [], 'xiaohongshu')['title'].startswith('AI + 插画')


@pytest.mark.parametrize('title', ['一句描述生成图片，画笔还握在手里', 'AI + 编程：一句描述生成图片'])
def test_observer_title_requires_ai_and_correct_industry(title):
    report = runner._evaluate_xhs_quality(title=title, body=BODY, focus_keywords=[], forbidden_claims=[],
        quality_gate_config={'content_branch': BRANCH, 'project_reference': PROJECT})
    assert not report['passed'] and 'title_industry' in report['failed_checks']


def test_observer_without_source_does_not_invent_project():
    draft = router().generate_draft({"content_branch": BRANCH}, [], "xiaohongshu")
    assert not draft["body"]


def test_observer_fallback_rewrite_does_not_pad_or_append():
    draft = {"title": TITLE, "body": BODY}
    rewritten = router().rewrite_draft(strategy=strategy(), intel=[], channel="xiaohongshu", draft=draft)
    assert rewritten == draft


def test_observer_model_rewrite_receives_branch_and_full_rules():
    model = router()
    model.openai = Mock()
    model.openai.responses.create.return_value = SimpleNamespace(output_text=json.dumps({"title": TITLE, "body": BODY}))
    model.rewrite_draft(strategy=strategy(), intel=[], channel="xiaohongshu", draft={"title": TITLE, "body": BODY})
    request = model.openai.responses.create.call_args.kwargs["input"]
    payload = json.loads(request[1]["content"])
    assert payload["strategy"]["content_branch"] == BRANCH
    rules = "\n".join(payload["hard_rules"])
    assert "共情" in rules and "项目名称" in rules
    assert "结尾必须有互动引导" not in rules


def test_observer_polish_preserves_short_body_and_final_reveal():
    result = runner._heuristic_polish_draft(title=TITLE, body=BODY, focus_keywords=["AI"],
                                          style_samples=[], content_branch=BRANCH)
    assert result == {"title": TITLE, "body": BODY}


def quality(body=BODY, project=PROJECT):
    return runner._evaluate_xhs_quality(title=TITLE, body=body, focus_keywords=["AI", "工作流"],
        forbidden_claims=[], quality_gate_config={"content_branch": BRANCH, "project_reference": project,
                                                  "body_min": 350, "hashtag_target": 3})


def test_observer_quality_accepts_short_post_without_tags_or_cta():
    result = quality()
    assert result["passed"], result
    assert "cta_presence" not in result["failed_checks"] and "hashtag_count" not in result["failed_checks"]


@pytest.mark.parametrize("body,failed", [
    (BODY + "\n\n欢迎在评论区留言。", "observer_style"),
    (BODY.replace("这个项目叫 Fooocus。", "我觉得这个项目值得关注。"), "project_name_last"),
    (BODY.replace("想象一个", "一个"), "imagined_reaction"),
    (BODY.replace("输入这句描述，输出对应的图片。", "这段话是万能模板。"), "input_output"),
])
def test_observer_quality_blocks_contract_breaks(body, failed):
    result = quality(body)
    assert not result["passed"] and failed in result["failed_checks"]


def test_observer_quality_requires_source_even_for_good_text():
    result = quality(project={})
    assert not result["passed"] and "project_source" in result["failed_checks"]


@pytest.mark.parametrize('missing_source,bad_model', [(False, False), (True, False), (False, True)])
def test_pipeline_carries_branch_through_generation_quality_and_rewrite(monkeypatch, missing_source, bad_model):
    account = {"id": "a", "account_name": "AI实用笔记", "config_jsonb": {"strategy_profile": {
        "prompt_overrides": {"content_branch": BRANCH, "draft_appendix": "每篇都写四步教程"},
        "focus_keywords": ["AI", "工作流"], "quality_gate": {"body_min": 350}}}}
    task = {"id": "t", "channel": "xiaohongshu", "payload_jsonb": {"channel_account_id": "a"},
            "intent_jsonb": {"content_branch": BRANCH, "project_reference": {} if missing_source else PROJECT, "source_opinion": "每篇都写四步教程"}}
    domain = {"id": "d", "slug": "ai_content", "config_jsonb": {"prompt_templates": {"draft": "评论引导"}}}
    model = Mock()
    model.generate_draft.return_value = {"title": TITLE, "body": BODY + ('\n欢迎留言' if bad_model else '')}
    model.rewrite_draft.return_value = model.generate_draft.return_value
    monkeypatch.setattr(runner, "get_router", lambda: model)
    monkeypatch.setattr(runner, "_update_pipeline_task", lambda _c, _id, patch: {**task, **patch})
    monkeypatch.setattr(runner, "_get_channel_account_runtime", lambda *_: account)
    monkeypatch.setattr(runner, "upsert_account_runtime", lambda *_: None)
    monkeypatch.setattr(runner, "_load_memory_items_for_task", lambda *_a, **_k: [])
    monkeypatch.setattr(runner, "load_active_prompt_versions", lambda *_a, **_k: {})
    monkeypatch.setattr(runner, "_fetch_recent_intel", lambda *_a, **_k: [])
    monkeypatch.setattr(runner, "load_kb_libraries", lambda *_a, **_k: {})
    monkeypatch.setattr(runner, "_collect_style_samples", lambda *_a, **_k: [])
    monkeypatch.setattr(runner, "_latest_optimization_hint", lambda *_a: {})
    monkeypatch.setattr(runner, "_pipeline_audit", lambda *_a, **_k: None)
    result = runner._draft_pipeline_task(None, task, domain)
    if missing_source:
        assert result['status'] == 'blocked' and result['reason'] == 'project_reference_required'
        model.generate_draft.assert_not_called()
        return
    used = model.generate_draft.call_args.kwargs["strategy"]
    assert used["content_branch"] == BRANCH
    assert "每篇都写四步教程" not in used["draft"]
    saved = result["task"]["payload_jsonb"]
    assert saved["content_branch"] == BRANCH
    if bad_model:
        assert saved['analysis_jsonb']['quality_gate']['reference_layout_applied']
        assert '欢迎留言' not in saved['body']
    else:
        assert saved["body"] == BODY
    assert saved["analysis_jsonb"]["quality_gate"]["final"]["passed"]
    assert saved["analysis_jsonb"]["content_branch"]["name"] == "AI 项目观察·共情短文"
    evidence = saved['analysis_jsonb']['evidence_map']
    assert 'CTA' not in evidence['body_framework']['template']
    imagined = [section for section in evidence['sections'] if section.get('source_type') == 'imagined']
    assert imagined and all(not section['evidence_refs'] for section in imagined)
    assert result["task"]["status"] == "pending_review"
    if not bad_model:
        model.rewrite_draft.assert_not_called()


def test_project_normalization_keeps_urls_and_latin_names():
    assert runner._normalize_space('AutoGPT\t https://github.com/test') == 'AutoGPT https://github.com/test'


def test_observer_title_must_not_reveal_name():
    result = runner._evaluate_xhs_quality(title='Fooocus 图片生成项目', body=BODY,
        focus_keywords=[], forbidden_claims=[], quality_gate_config={'content_branch': BRANCH, 'project_reference': PROJECT})
    assert not result['passed'] and 'project_name_last' in result['failed_checks']


def test_legacy_pipeline_does_not_inherit_new_account_branch(monkeypatch):
    task = {'id': 'legacy', 'channel': 'xiaohongshu', 'payload_jsonb': {'channel_account_id': 'a'}, 'intent_jsonb': {}}
    account = {'id': 'a', 'config_jsonb': {'strategy_profile': {'prompt_overrides': {'content_branch': BRANCH}}}}
    monkeypatch.setattr(runner, '_update_pipeline_task', lambda *_: task)
    monkeypatch.setattr(runner, '_get_channel_account_runtime', lambda *_: account)
    def capture(_intent, _strategy):
        assert _intent.get('content_branch') == 'tutorial'
        raise RuntimeError('branch captured')
    monkeypatch.setattr(runner, 'resolve_content_branch', capture)
    with pytest.raises(RuntimeError, match='branch captured'):
        runner._draft_pipeline_task(None, task, {'id': 'd', 'slug': 'ai_content'})


def test_copy_agent_observer_uses_account_branch_without_japanese_tags(monkeypatch):
    from app.agents.subagents import copy_agent
    from fake_store import MemoryStore
    client = MemoryStore()
    client.tables.update({'domains': [{'id': 'd', 'slug': 'ai_content'}], 'channel_accounts': [
        {'id': 'a', 'config_jsonb': {'strategy_profile': {'prompt_overrides': {
            'content_branch': BRANCH, 'project_reference': PROJECT}}}}]})
    model = Mock()
    model.generate_draft.return_value = {'title': TITLE, 'body': BODY}
    monkeypatch.setattr(copy_agent, 'get_router', lambda: model)
    monkeypatch.setattr(copy_agent, 'load_kb_libraries', lambda *_a, **_k: {})
    result = copy_agent.run_copy_agent(client, domain_slug='ai_content', account_id='a',
                                     triggered_by='test', trace_id='observer')
    assert result['status'] == 'success', result
    assert result['result']['body'] == BODY and result['result']['cta'] == ''
    assert result['result']['hashtags'] == []
    assert model.generate_draft.call_args.kwargs['strategy']['content_branch'] == BRANCH
