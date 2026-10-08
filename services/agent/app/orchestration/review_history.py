"""Observed comparisons only; action candidates never apply strategy changes."""

from datetime import datetime
from statistics import median
from typing import Any, Callable, Dict


def _map(value):
    return value if isinstance(value, dict) else {}


def _time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _checkpoint(row):
    values = _map(row.get("publish_jsonb")).get("feedback_completed_hours", [])
    return max([value for value in values if isinstance(value, int) and value > 0], default=0) if isinstance(values, list) else 0


def _version(row):
    analysis = _map(_map(row.get("payload_jsonb")).get("analysis_jsonb"))
    return str(_map(_map(analysis.get("active_prompt_versions")).get("draft_writer")).get("version") or "")


def compare_review_history(client: Any, task: Dict[str, Any], metrics: Dict[str, float],
                           extract_metrics: Callable) -> Dict[str, Any]:
    output = {"status": "insufficient_history", "comparisons": [], "keep_actions": [],
              "adjust_actions": [], "discard_actions": [], "evidence_refs": [],
              "confirmation_required": True, "causal_claim": False}
    account, domain, channel = task.get("account_id"), task.get("domain_id"), task.get("channel")
    published = _time(task.get("published_at"))
    checkpoint = _checkpoint(task)
    if not account or not domain or not channel or published is None or not checkpoint or metrics.get("views", 0) <= 0:
        return output
    try:
        rows = (client.table("pipeline_tasks")
                .select("id,domain_id,account_id,channel,status,published_at,payload_jsonb,publish_jsonb,metrics_jsonb")
                .eq("domain_id", domain).eq("account_id", account).eq("channel", channel)
                .order("published_at", desc=True).limit(100).execute().data or [])
    except Exception:
        output["status"] = "history_unavailable"
        return output
    samples = []
    for row in rows:
        timestamp = _time(row.get("published_at"))
        if (not row.get("id") or row["id"] == task.get("id") or timestamp is None or timestamp >= published
                or row.get("account_id") != account or row.get("domain_id") != domain or row.get("channel") != channel
                or row.get("status") not in {"published", "metrics_ready", "done", "reflecting", "reflection_failed"}
                or _checkpoint(row) != checkpoint):
            continue
        observed, real = extract_metrics(_map(row.get("metrics_jsonb")))
        if real and observed.get("views", 0) > 0:
            samples.append((row, observed))
    if len(samples) < 3:
        output["sample_count"] = len(samples)
        return output
    output.update({"status": "compared", "sample_count": len(samples), "checkpoint_hours": checkpoint})
    refs = {}
    for metric in ("likes", "collects", "comments", "shares"):
        relevant = [(row, data) for row, data in samples if metric in data]
        if metric not in metrics or len(relevant) < 3:
            continue
        current_rate = metrics[metric] / metrics["views"]
        baseline = median(data[metric] / data["views"] for _, data in relevant)
        relation = "above" if current_rate > baseline else "below" if current_rate < baseline else "equal"
        comparison = {"metric": metric, "current_rate": current_rate, "historical_median_rate": baseline,
                      "difference": current_rate - baseline, "relation": relation,
                      "history_task_ids": [row["id"] for row, _ in relevant]}
        output["comparisons"].append(comparison)
        action = {"task_id": task["id"], "basis": "observed_history_comparison", "comparison": comparison,
                  "confirmation_required": True, "causal_claim": False}
        if relation in {"above", "equal"}:
            output["keep_actions"].append({**action, "action": f"{metric} / views 未低于同检查点历史中位数，可保留当前版本继续观察；尚不能归因于某个写法。"})
        else:
            output["adjust_actions"].append({**action, "action": f"{metric} / views 低于同检查点历史中位数，建议复核来源与受众差异，再进行单变量测试。"})
        for row, _ in relevant:
            refs[row["id"]] = {"source_type": "pipeline_tasks.metrics_jsonb", "source_ref": row["id"],
                               "timestamp": str(_map(row.get("metrics_jsonb")).get("post_metrics_synced_at") or "")}

    # A retirement candidate requires an identified version and repeated observations.
    version = _version(task)
    current_group = [(row, data) for row, data in samples if version and _version(row) == version]
    prior_group = [(row, data) for row, data in samples if _version(row) and _version(row) != version]
    metric = "collects"
    current_group = [(row, data) for row, data in current_group if metric in data]
    prior_group = [(row, data) for row, data in prior_group if metric in data]
    if version and metric in metrics and len(current_group) >= 2 and len(prior_group) >= 3:
        baseline = median(data[metric] / data["views"] for _, data in prior_group)
        rates = [metrics[metric] / metrics["views"], *[data[metric] / data["views"] for _, data in current_group]]
        if all(rate < baseline for rate in rates):
            output["discard_actions"].append({
                "action": "当前写作提示词版本多次收藏/曝光比低于其他版本历史中位数，可人工评估暂停使用；版本变化不等于因果证明。",
                "task_id": task["id"], "target_agent": "draft_writer", "prompt_version": version,
                "observed_rates": rates, "historical_median_rate": baseline,
                "history_task_ids": [row["id"] for row, _ in current_group + prior_group],
                "basis": "repeated_observed_history", "confirmation_required": True,
                "auto_apply": False, "causal_claim": False,
            })
    output["evidence_refs"] = list(refs.values())
    if not output["comparisons"]:
        output["status"] = "insufficient_comparable_metrics"
    return output
