from importlib import import_module


class FakeBridge:
    def __init__(self):
        self.calls = []
        self.saved = []
        self.result = None

    def state(self):
        return {"status": "running", "queries": ["AI 办公"], "collected": len(self.saved)}

    def execute(self, operation, arguments):
        self.calls.append((operation, arguments))
        return {"ok": True, "data": {"kind": "note", "version": f"v{len(self.calls)}", "note": {"title": "AI教程", "text": "先整理任务再核查输入，这是实际可见正文", "source_url": "https://www.xiaohongshu.com/explore/6a089a9d0000000038021ad6"}}}

    def save(self, summary, model_name):
        self.saved.append(summary)
        return {"saved": True, "collected": len(self.saved)}

    def finish(self, status, message):
        self.result = {"status": status, "message": message}
        return self.result


class FakeModel:
    name = "fake-test-model"

    def __init__(self, actions):
        self.actions = iter(actions)
        self.observations = []

    def choose(self, observation, queries, saved):
        self.observations.append(observation)
        return next(self.actions)


def test_model_uses_updated_visible_observations_and_import_requires_read():
    run = import_module("app.agents.browser.collector").run_collection
    bridge = FakeBridge()
    model = FakeModel([("read_note", {"version": "v1"}), ("save_note", {"summary": "先整理工作任务，再核查输入数据；内容仅为可见正文的摘要。"}), ("finish", {})])
    result = run(bridge, lambda: model)
    assert result["status"] == "completed"
    assert [call[0] for call in bridge.calls] == ["observe", "read_note"]
    assert model.observations[1]["version"] == "v2"
    assert len(bridge.saved) == 1


def test_cannot_save_before_read_or_execute_arbitrary_model_action():
    run = import_module("app.agents.browser.collector").run_collection
    for action in [("save_note", {"summary": "模型编造正文"}), ("publish", {}), ("javascript", {"script": "x"})]:
        bridge = FakeBridge()
        result = run(bridge, lambda: FakeModel([action]))
        assert result["status"] == "failed"
        assert bridge.saved == []
        assert [call[0] for call in bridge.calls] == ["observe"]


def test_model_call_budget_and_empty_finish_do_not_report_success():
    run = import_module("app.agents.browser.collector").run_collection
    bridge = FakeBridge()
    model = FakeModel([("observe", {})] * 20)
    result = run(bridge, lambda: model)
    assert len(model.observations) == 12
    assert result["status"] == "failed"
    bridge = FakeBridge()
    assert run(bridge, lambda: FakeModel([("finish", {})]))["status"] == "failed"


def test_cancelled_job_never_constructs_or_calls_model():
    run = import_module("app.agents.browser.collector").run_collection
    bridge = FakeBridge()
    bridge.state = lambda: {"status": "cancelled", "queries": []}
    def forbidden():
        raise AssertionError("Model must not start")
    run(bridge, forbidden)
    assert bridge.calls == [] and bridge.saved == []


def test_failed_read_cannot_be_confirmed_by_fallback_observe():
    run = import_module("app.agents.browser.collector").run_collection
    bridge = FakeBridge()
    original = bridge.execute
    bridge.execute = lambda op, args: {"ok": False} if op == "read_note" else original(op, args)
    result = run(bridge, lambda: FakeModel([("read_note", {"version": "v1"}), ("save_note", {"summary": "这是一段只观察未确认读取的测试正文摘要。"}), ("finish", {})]))
    assert result["status"] == "failed" and not bridge.saved


def test_lost_save_receipt_preserves_server_confirmed_partial_success():
    run = import_module("app.agents.browser.collector").run_collection
    bridge = FakeBridge()
    def lost(*args):
        bridge.saved.append("committed")
        raise TimeoutError("lost receipt")
    bridge.save = lost
    result = run(bridge, lambda: FakeModel([("read_note", {"version": "v1"}), ("save_note", {"summary": "这是一段服务端已保存但回应丢失的测试摘要。"})]))
    assert result["status"] == "partial" and len(bridge.saved) == 1
