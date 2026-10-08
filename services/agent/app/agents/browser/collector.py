from __future__ import annotations

import time


OPERATIONS = {"observe", "search", "open_note", "scroll_note", "close_note", "read_note"}
TERMINAL = {"completed", "partial", "cancelled", "failed", "model_unavailable"}


class ModelUnavailable(Exception):
    pass


def run_collection(bridge, model_factory, clock=time.monotonic):
    started = clock()
    saved_urls = []
    try:
        state = bridge.state()
        if state["status"] in TERMINAL:
            return state
        if state.get("mode") == "background_text":
            from app.agents.browser.capture import run_background_collection
            return run_background_collection(bridge,model_factory,clock=clock)
        model = model_factory()
        result = bridge.execute("observe", {})
        if not result.get("ok") or not result.get("data"):
            raise ValueError("无法确认小红书公开页面")
        observation = result["data"]
        read_url = None
        searches = []
        for _ in range(12):
            if clock() - started >= 600:
                break
            state = bridge.state()
            if state["status"] in TERMINAL:
                return state
            if len(saved_urls) >= 3:
                return bridge.finish("completed", "已完成 3 篇公开笔记采集")
            observation["search_queries_used"] = searches
            action, arguments = model.choose(observation, state["queries"], saved_urls)
            # A stop between inference and execution must not cause a new action/write.
            if bridge.state()["status"] in TERMINAL:
                return bridge.state()
            if action == "finish":
                return bridge.finish("completed" if saved_urls else "failed", "采集完成" if saved_urls else "未采集到可核对的公开正文")
            if action == "save_note":
                note = observation.get("note") or {}
                if not read_url or read_url != note.get("source_url") or observation.get("kind") != "note":
                    raise ValueError("必须先读取本轮可见正文，再保存摘要")
                summary = arguments.get("summary", "")
                if not isinstance(summary, str) or not 10 <= len(summary.strip()) <= 3000:
                    raise ValueError("笔记摘要无效")
                receipt = bridge.save(summary, model.name)
                if receipt.get("saved") and read_url not in saved_urls:
                    saved_urls.append(read_url)
                observation["last_save"] = receipt
                continue
            if action not in OPERATIONS:
                raise ValueError("模型请求了不允许的浏览器操作")
            result = bridge.execute(action, arguments)
            confirmed_read = action == "read_note" and result.get("ok") and not result.get("reobserved")
            if not result.get("ok") or not result.get("data"):
                # One fresh observation replaces a failed/stale operation; never replay it.
                result = bridge.execute("observe", {})
                if not result.get("ok") or not result.get("data"):
                    raise ValueError("页面操作无法确认，采集已停止")
                read_url = None
            observation = result["data"]
            if action == "search" and arguments.get("query"):
                searches = (searches + [arguments["query"]])[-3:]
            if confirmed_read and observation.get("kind") == "note" and len((observation.get("note") or {}).get("text", "").strip()) >= 10:
                read_url = observation["note"]["source_url"]
            elif read_url != (observation.get("note") or {}).get("source_url"):
                read_url = None
        return bridge.finish("partial" if saved_urls else "failed", "已达到模型调用或采集时间上限")
    except ModelUnavailable:
        return bridge.finish("partial" if saved_urls else "model_unavailable", "当前模型接口不支持所需图像与工具调用，或模型调用失败，请检查浏览器模型配置")
    except Exception:
        # Reconcile a lost save receipt through a bounded read, never another write.
        try:
            confirmed_count = bridge.state().get("collected", 0)
        except Exception:
            confirmed_count = 0
        return bridge.finish("partial" if saved_urls or confirmed_count else "failed", "采集已停止：页面、连接或保存结果无法确认，请核对资料列表后重新开始")
