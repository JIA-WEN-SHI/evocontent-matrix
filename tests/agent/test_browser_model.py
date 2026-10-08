from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock


def test_results_must_search_account_keywords_before_opening_recommended_notes():
    module = import_module("app.agents.browser.model")
    model = object.__new__(module.BrowserModel)
    model.name = "fake-model"
    create = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[
        SimpleNamespace(function=SimpleNamespace(name="search", arguments='{"version":"v","query":"AI 工作流"}'))
    ]))]))
    model.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    model.choose({"kind": "results", "version": "v", "search_queries_used": []}, ["AI 工作流"], [])
    assert [tool["function"]["name"] for tool in create.call_args.kwargs["tools"]] == ["search"]
    model.choose({"kind": "results", "version": "v2", "search_queries_used": ["AI 工作流"]}, ["AI 工作流"], [])
    assert "open_note" in [tool["function"]["name"] for tool in create.call_args.kwargs["tools"]]
