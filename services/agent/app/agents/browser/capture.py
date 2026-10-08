import time


def collect_public_note(bridge, initial_observation: dict, *, clock=time.monotonic) -> dict:
    source = (initial_observation.get("note") or {}).get("source_url")
    started = clock()
    observation = initial_observation

    def execute(operation, target=None):
        nonlocal observation
        arguments = {"version": observation["version"]}
        if target:
            arguments["target_id"] = target["target_id"]
        result = bridge.execute(operation,arguments)
        data = result.get("data") or {}
        if not result.get("ok") or result.get("reobserved") or data.get("kind") != "note" or (data.get("note") or {}).get("source_url") != source or (data.get("capture") or {}).get("source_url") != source:
            raise ValueError("公开资料未确认或来源变化")
        observation = data

    execute("read_note")
    comment_actions = 0
    image_actions = 0
    body_actions = 0
    while clock()-started < 240:
        state = bridge.state()
        if state["status"] in {"cancelled","failed","partial","completed","model_unavailable"}:
            raise RuntimeError("采集已结束")
        if state.get("sequence",0) >= 118:
            break
        capture = observation["capture"]
        targets = observation.get("detail_targets",[])
        body = next((t for t in targets if t["operation"] == "expand_body"),None)
        reply = next((t for t in targets if t["operation"] == "expand_reply"),None)
        image = next((t for t in targets if t["operation"] == "next_image"),None)
        if body and body_actions < 2:
            execute("expand_body",body); body_actions += 1
        elif capture.get("comments_status") == "partial" and len(capture.get("comments",[])) < 100 and comment_actions < 25:
            execute("expand_reply",reply) if reply else execute("scroll_comments")
            comment_actions += 1
        elif image and len(capture.get("image_candidates",[])) < 24 and image_actions < 24:
            execute("next_image",image); image_actions += 1
        else:
            break
    if bridge.state().get("sequence",0) < 120:
        execute("read_note")
    return observation


def run_background_collection(bridge, model_factory, clock=time.monotonic):
    started = clock()
    saved = []
    model = None
    calls = 0
    searches = []
    try:
        model = model_factory()
        result = bridge.execute("observe",{})
        if not result.get("ok") or not result.get("data"):
            raise ValueError("公开页面无法确认")
        observation = result["data"]
        if observation["kind"] == "note":
            result = bridge.execute("close_note",{"version":observation["version"]})
            if not result.get("ok") or result.get("data",{}).get("kind") != "results":
                raise ValueError("请从公开搜索结果开始")
            observation = result["data"]
        while calls < 12 and clock()-started < 600 and len(saved) < 3:
            state = bridge.state()
            if state["status"] in {"completed","partial","cancelled","failed","model_unavailable"}:
                return state
            observation["mode"] = "background_text"
            observation["search_queries_used"] = searches
            calls += 1
            action, arguments = model.choose(observation,state["queries"],saved)
            if action == "finish":
                break
            if action not in {"observe","search","open_note","close_note"}:
                raise ValueError("后台选择工具不允许")
            result = bridge.execute(action,arguments)
            if not result.get("ok") or not result.get("data") or result.get("reobserved"):
                raise ValueError("页面动作未确认，请重试")
            observation = result["data"]
            if action == "search":
                searches = (searches+[arguments["query"]])[-3:]
            if action == "open_note" and observation["kind"] == "note":
                observation = collect_public_note(bridge,observation,clock=clock)
                receipt = bridge.save(None,model.name)
                if receipt.get("saved"):
                    source = observation["capture"]["source_url"]
                    if source not in saved:
                        saved.append(source)
                summary = None
                if calls < 12 and clock()-started < 555:
                    calls += 1
                    try:
                        summary = model.summarize(observation)
                    except Exception:
                        summary = None
                if summary is not None and (not isinstance(summary,str) or not 10 <= len(summary.strip()) <= 3000):
                    summary = None
                if summary and receipt.get("item_id"):
                    try:
                        bridge.update_summary(receipt["item_id"],summary,model.name)
                    except Exception:
                        pass
                if len(saved) < 3 and bridge.state().get("sequence",0) < 118:
                    result = bridge.execute("close_note",{"version":observation["version"]})
                    if not result.get("ok") or not result.get("data"):
                        break
                    observation = result["data"]
                else:
                    break
        return bridge.finish("completed" if saved else "failed","已保存公开原文、评论与配图索引" if saved else "未采集到可核对的公开资料")
    except Exception:
        try:
            confirmed = bridge.state().get("collected",0)
        except Exception:
            confirmed = 0
        return bridge.finish("partial" if confirmed or saved else "failed","资料采集已停止，请核对已保存内容和页面状态")
    finally:
        if model and hasattr(model,"close"):
            model.close()
