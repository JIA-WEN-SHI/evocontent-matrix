"""Seed a reviewed plan's libraries and create one draft, never approve or publish."""

import argparse
import json
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--plan", required=True)
    args = parser.parse_args()
    plan_path = Path(args.plan)
    plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    scope = {"domain_slug": plan["domain_slug"], "account_id": args.account_id}
    marker = plan_path.stem
    with httpx.Client(base_url="http://127.0.0.1:8000", trust_env=False, timeout=240,
                      headers={"x-user-id": "local-ai-replan", "x-user-role": "admin"}) as api:
        def request(method, path, **kwargs):
            response = api.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        added = {}
        for entity in ("cases", "assets", "topics"):
            existing = request("GET", f"/api/kb/{entity}", params={**scope, "limit": 200})["items"]
            rows = []
            if entity == "topics":
                rows = [{"title": title, "topic_description": "输入示例、分步提示词、输出样例和人工检查；不虚构亲测。",
                         "target_user": "普通上班族、独立创作者和 AI 初学者", "structure_type": "实操教程",
                         "status": "todo", "reason": "AI 内容分享首周选题规划", "source_type": "account_plan",
                         "source_ref": marker} for title in plan["topic_queue"]]
            else:
                for sample in plan["browser_samples"]:
                    common = {"source_type": "chrome_browser_ui", "source_ref": sample["source_url"]}
                    if entity == "cases":
                        rows.append({**common, "title": sample["title"], "url": sample["source_url"],
                                     "content": sample["raw_text"], "metrics": {},
                                     "analysis": "仅作为可见页面观察案例；读者需要具体输入、步骤和交付样例，互动数字不代表效果验证。"})
                    else:
                        rows.append({**common, "type": "insight", "content": sample["raw_text"],
                                     "source": sample["source_url"], "usable_scene": "AI 工作流教程选题与边界说明",
                                     "summary": "从可见笔记提炼需求信号，不复用原文，不承诺未经验证的效率或收益。",
                                     "is_verified": False})
            key = "title" if entity == "topics" else "source_ref"
            seen = {row.get(key) for row in existing}
            added[entity] = 0
            for row in rows:
                if row[key] not in seen:
                    request("POST", f"/api/kb/{entity}", json={**scope, **row})
                    seen.add(row[key])
                    added[entity] += 1

        tasks = request("GET", "/api/pipeline/tasks", params={**scope, "limit": 200})["items"]
        task = next((row for row in tasks if (row.get("intent_jsonb") or {}).get("account_plan_ref") == marker), None)
        if task is None:
            brief = (
                "本次只生成一篇小红书待审核草稿，题目是 AI写周报别一口气问：拆成4步。"
                "核心观点：先整理事实，再分类，再写稿，再人工核对；工作流不是换一份万能提示词。"
                "面向普通上班族，给出四段可直接改用的提示词，不能虚构作者亲测或效率百分比。"
                "所有例子标注为虚构示例：本周整理20条反馈，其中8条重复项；周三提交初稿；"
                "评审未完成，预计下周一确认。数字只来自示例，不能把预计写成已经完成。"
                "第1步提示词只提取事实并保留原句，缺项写待确认；第2步分类已完成、进行中、阻塞和下周计划；"
                "第3步按已核实信息生成简洁周报，不补数据；第4步列出数字、人名、日期和状态待查清单。"
                "给一段两三句的输出样例及人工验收标准。提醒脱敏且只使用公司允许的工具，发送前人工确认。"
                "不要介绍移民、签证、预算咨询，也不要改写采集笔记的原文。"
                "正文600到1000字，短段落，3个相关话题，结尾问读者最想解决的办公任务。"
            )
            task = request("POST", "/api/pipeline/tasks", json={**scope, "channel": "xiaohongshu",
                           "content_type": "post", "intent_jsonb": {"account_plan_ref": marker,
                           "topic": plan["topic_queue"][0], "source_opinion": brief}, "payload_jsonb": {}})
            print(json.dumps({"created_task_id": task["id"]}, ensure_ascii=False), flush=True)
            result = request("POST", f"/api/pipeline/tasks/{task['id']}/run")
            print(json.dumps({"run_status": result.get("status")}, ensure_ascii=False), flush=True)
        task = request("GET", f"/api/pipeline/tasks/{task['id']}")
        if task.get("published_at"):
            raise RuntimeError("Unexpected published state; inspect task before proceeding")
        print(json.dumps({"libraries_added": added, "task_id": task["id"], "status": task["status"],
                          "title": task["payload_jsonb"].get("title"), "body": task["payload_jsonb"].get("body")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
