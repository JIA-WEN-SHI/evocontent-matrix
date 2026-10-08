from __future__ import annotations

from datetime import datetime, timezone
import re
import time
from typing import Any, Dict, Iterable, List, Optional

from fastapi import HTTPException, status
from supabase import Client

from app.models import KbOctopusImportItem, KbOctopusImportRequest, KbTopicRecommendRequest

_ENTITY_TABLE_MAP = {
    "case": "cases",
    "asset": "assets",
    "user_need": "user_needs",
    "review": "reviews",
}

_TOPIC_STATUS = {"todo", "drafted", "produced", "published", "archived"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_uuid(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if text else None


def resolve_domain(client: Client, domain_slug: str) -> Dict[str, Any]:
    rows = client.table("domains").select("id,slug,name").eq("slug", domain_slug).limit(1).execute().data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="domain not found")
    return rows[0]


def normalize_topic_status(value: str) -> str:
    normalized = str(value or "").strip().lower() or "todo"
    return normalized if normalized in _TOPIC_STATUS else "todo"


def soft_delete_patch(deleted: Optional[bool]) -> Dict[str, Any]:
    if deleted is None:
        return {}
    return {"deleted_at": now_iso() if deleted else None}


def upsert_tags_for_entity(
    client: Client,
    *,
    domain_id: str,
    account_id: str | None,
    entity_type: str,
    entity_id: str,
    tags: Iterable[str],
    created_by: str,
    source_type: str = "manual",
    source_ref: str = "",
) -> List[str]:
    normalized_tags = []
    seen = set()
    for raw in tags:
        tag = str(raw or "").strip()
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized_tags.append(tag[:120])

    if not normalized_tags:
        return []

    tag_ids: List[str] = []
    for tag in normalized_tags:
        current = (
            client.table("tags")
            .select("id")
            .eq("domain_id", domain_id)
            .eq("name", tag)
            .eq("category", "general")
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
            .data
            or []
        )
        if current:
            tag_id = str(current[0]["id"])
        else:
            try:
                inserted = (
                    client.table("tags")
                    .insert(
                        {
                            "domain_id": domain_id,
                            "account_id": account_id,
                            "name": tag,
                            "category": "general",
                            "status": "active",
                            "source_type": source_type,
                            "source_ref": source_ref,
                            "created_by": created_by,
                        }
                    )
                    .execute()
                    .data
                    or []
                )
                if not inserted:
                    continue
                tag_id = str(inserted[0]["id"])
            except Exception:  # noqa: BLE001
                # Tag may already exist with different case, recover by case-insensitive lookup.
                current_ci = (
                    client.table("tags")
                    .select("id")
                    .eq("domain_id", domain_id)
                    .ilike("name", tag)
                    .eq("category", "general")
                    .is_("deleted_at", "null")
                    .limit(1)
                    .execute()
                    .data
                    or []
                )
                if not current_ci:
                    continue
                tag_id = str(current_ci[0]["id"])
        tag_ids.append(tag_id)

    existing_rows = (
        client.table("entity_tags")
        .select("tag_id")
        .eq("domain_id", domain_id)
        .eq("entity_type", entity_type)
        .eq("entity_id", entity_id)
        .is_("deleted_at", "null")
        .execute()
        .data
        or []
    )
    existing_ids = {str(row.get("tag_id") or "") for row in existing_rows}
    for tag_id in tag_ids:
        if tag_id in existing_ids:
            continue
        client.table("entity_tags").insert(
            {
                "domain_id": domain_id,
                "account_id": account_id,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "tag_id": tag_id,
                "source_type": source_type,
                "source_ref": source_ref,
                "created_by": created_by,
            }
        ).execute()
    return tag_ids


def _normalize_octopus_item(item: Dict[str, Any], idx: int, source: str) -> Dict[str, Any]:
    title = str(item.get("title") or "").strip()
    content = str(item.get("content") or "").strip()
    url = str(item.get("url") or "").strip()
    source_ref = str(item.get("source_ref") or "").strip() or url or f"{source}:{idx + 1}"
    if not title:
        title = content.split("\n")[0][:80] if content else f"未命名样本#{idx + 1}"
    return {
        "title": title[:500],
        "content": content[:20000],
        "url": url[:2000],
        "author": str(item.get("author") or "").strip()[:200],
        "platform": str(item.get("platform") or "xiaohongshu").strip()[:80] or "xiaohongshu",
        "metrics": item.get("metrics") if isinstance(item.get("metrics"), dict) else {},
        "tags": item.get("tags") if isinstance(item.get("tags"), list) else [],
        "captured_at": str(item.get("captured_at") or "").strip()[:80],
        "raw": item.get("raw") if isinstance(item.get("raw"), dict) else {},
        "source_ref": source_ref[:1000],
    }


def _split_case_sentences(text: str, limit: int = 4) -> List[str]:
    normalized = str(text or "").replace("\r", "\n")
    chunks = re.split(r"[。\n！？!?；;]+", normalized)
    result: List[str] = []
    for chunk in chunks:
        sentence = str(chunk or "").strip()
        if not sentence:
            continue
        result.append(sentence[:120])
        if len(result) >= limit:
            break
    return result


def _build_asset_candidates_from_case(item: Dict[str, Any]) -> List[Dict[str, str]]:
    title = str(item.get("title") or "").strip()
    content = str(item.get("content") or "").strip()
    metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
    key_points = _split_case_sentences(content, limit=4)
    point_text = "；".join(key_points) if key_points else (content[:160] if content else title)
    try:
        likes = int(float(metrics.get("likes") or 0))
    except (TypeError, ValueError):
        likes = 0
    try:
        collects = int(float(metrics.get("collects") or 0))
    except (TypeError, ValueError):
        collects = 0
    try:
        comments = int(float(metrics.get("comments_count") or 0))
    except (TypeError, ValueError):
        comments = 0
    metric_text = f"互动参考: 点赞{likes} 收藏{collects} 评论{comments}"

    return [
        {
            "type": "method_card",
            "summary": f"方法卡 | {title[:80]}",
            "usable_scene": "内容策划/脚本拆解",
            "content": f"案例标题: {title}\n可复用方法:\n1) 问题界定\n2) 关键步骤拆解\n3) 风险提示\n4) 行动建议\n案例要点: {point_text}\n{metric_text}",
        },
        {
            "type": "topic_angle",
            "summary": f"选题角度 | {title[:80]}",
            "usable_scene": "选题池刷新",
            "content": f"基于案例《{title}》提炼角度:\n- 争议角度: 常见误区与反直觉结论\n- 实操角度: 从条件到执行清单\n- 决策角度: 不同人群该怎么选\n素材依据: {point_text}",
        },
        {
            "type": "opening_template",
            "summary": f"开头模板 | {title[:80]}",
            "usable_scene": "短内容开头",
            "content": f"开头模板A: 先给结论 -> 你以为X，其实Y\n开头模板B: 先给风险 -> 这一步不做，后面都白做\n开头模板C: 先给案例 -> 今天这个案例，3个细节决定结果\n案例关键词: {title[:40]}",
        },
        {
            "type": "title_template",
            "summary": f"标题模板 | {title[:80]}",
            "usable_scene": "标题生成",
            "content": f"标题模板A: 【人群+目标】如何在N步内完成X\n标题模板B: 别再被X误导，真正要看这3点\n标题模板C: 同样是做X，为什么有人快一倍\n案例参考: {title}",
        },
    ]


def _auto_generate_assets_from_case(
    client: Client,
    *,
    domain_id: str,
    account_id: str | None,
    item: Dict[str, Any],
    created_by: str,
    source_type: str,
) -> int:
    source_ref = str(item.get("source_ref") or "").strip()
    if not source_ref:
        return 0
    created = 0
    candidates = _build_asset_candidates_from_case(item)
    for candidate in candidates:
        candidate_type = str(candidate.get("type") or "insight").strip()[:80] or "insight"
        candidate_source_ref = f"auto_case_asset:{source_ref}:{candidate_type}"
        exists = (
            client.table("assets")
            .select("id")
            .eq("domain_id", domain_id)
            .eq("source_ref", candidate_source_ref)
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
            .data
            or []
        )
        if exists:
            continue
        rows = (
            client.table("assets")
            .insert(
                {
                    "domain_id": domain_id,
                    "account_id": account_id,
                    "type": candidate_type,
                    "content": str(candidate.get("content") or "")[:20000],
                    "source": str(item.get("url") or source_type)[:1000],
                    "usable_scene": str(candidate.get("usable_scene") or "")[:1000],
                    "is_verified": False,
                    "summary": str(candidate.get("summary") or "")[:5000],
                    "source_type": "auto_case_asset",
                    "source_ref": candidate_source_ref[:1000],
                    "created_by": created_by,
                }
            )
            .execute()
            .data
            or []
        )
        if not rows:
            continue
        created += 1
        asset_id = str(rows[0].get("id") or "")
        if asset_id:
            tags = item.get("tags") if isinstance(item.get("tags"), list) else []
            upsert_tags_for_entity(
                client,
                domain_id=domain_id,
                account_id=account_id,
                entity_type="asset",
                entity_id=asset_id,
                tags=[*tags, "auto_case_asset"],
                created_by=created_by,
                source_type="auto_case_asset",
                source_ref=candidate_source_ref,
            )
    return created


def import_octopus_payload(
    client: Client,
    request: KbOctopusImportRequest,
    *,
    created_by: str,
) -> Dict[str, Any]:
    entity_type = str(request.entity_type or "").strip().lower()
    table_name = _ENTITY_TABLE_MAP.get(entity_type)
    if not table_name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid entity_type")

    domain = resolve_domain(client, request.domain_slug)
    domain_id = str(domain["id"])
    account_id = normalize_uuid(request.account_id)
    actor = str(created_by or "system:kb_import").strip() or "system:kb_import"

    raw_items = request.items or []
    normalized_items = [
        _normalize_octopus_item(
            item.model_dump() if hasattr(item, "model_dump") else dict(item),
            idx,
            request.source,
        )
        for idx, item in enumerate(raw_items)
    ]

    ingestion = (
        client.table("ingestion_logs")
        .insert(
            {
                "domain_id": domain_id,
                "account_id": account_id,
                "source": request.source,
                "source_run_id": str(request.source_run_id or "").strip(),
                "entity_type": entity_type,
                "status": "processing",
                "request_payload": {
                    "source": request.source,
                    "source_run_id": request.source_run_id,
                    "entity_type": entity_type,
                    "received": len(normalized_items),
                },
                "normalized_count": len(normalized_items),
                "created_by": actor,
            }
        )
        .execute()
        .data
        or []
    )
    if not ingestion:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="failed to create ingestion log")
    ingestion_id = str(ingestion[0]["id"])

    inserted_ids: List[str] = []
    failed_items: List[Dict[str, Any]] = []
    success_count = 0
    auto_assets_created_count = 0

    for idx, item in enumerate(normalized_items):
        title = str(item["title"])
        source_ref = str(item["source_ref"])
        try:
            existed_query = (
                client.table(table_name)
                .select("id")
                .eq("domain_id", domain_id)
                .eq("source_ref", source_ref)
                .is_("deleted_at", "null")
            )
            # cases table allows same source_ref across different titles in edge cases;
            # keep historical behavior for case while avoiding invalid columns for other entities.
            if entity_type == "case":
                existed_query = existed_query.eq("title", title)

            existed = existed_query.limit(1).execute().data or []
            entity_id = ""
            if existed:
                entity_id = str(existed[0]["id"])
            else:
                if entity_type == "case":
                    payload = {
                        "domain_id": domain_id,
                        "account_id": account_id,
                        "platform": item["platform"],
                        "author": item["author"],
                        "url": item["url"],
                        "title": title,
                        "content": item["content"],
                        "metrics": item["metrics"],
                        "analysis": "",
                        "hook": "",
                        "structure": "",
                        "source_type": request.source,
                        "source_ref": source_ref,
                        "created_by": actor,
                    }
                elif entity_type == "asset":
                    payload = {
                        "domain_id": domain_id,
                        "account_id": account_id,
                        "type": "insight",
                        "content": item["content"] or title,
                        "source": item["url"] or request.source,
                        "usable_scene": "",
                        "is_verified": False,
                        "summary": title,
                        "source_type": request.source,
                        "source_ref": source_ref,
                        "created_by": actor,
                    }
                elif entity_type == "user_need":
                    payload = {
                        "domain_id": domain_id,
                        "account_id": account_id,
                        "user_type": "",
                        "original_text": item["content"] or title,
                        "scenario": "",
                        "demand_type": "",
                        "emotion": "",
                        "real_problem": title,
                        "source_type": request.source,
                        "source_ref": source_ref,
                        "created_by": actor,
                    }
                else:
                    payload = {
                        "domain_id": domain_id,
                        "account_id": account_id,
                        "topic_id": None,
                        "pipeline_task_id": None,
                        "content_item_ref": item["url"],
                        "platform": item["platform"],
                        "metrics": item["metrics"],
                        "success_points": "",
                        "failure_points": "",
                        "improvement": "",
                        "summary_text": title,
                        "source_type": request.source,
                        "source_ref": source_ref,
                        "created_by": actor,
                    }
                created = client.table(table_name).insert(payload).execute().data or []
                if not created:
                    raise RuntimeError("insert returned empty")
                entity_id = str(created[0]["id"])

            upsert_tags_for_entity(
                client,
                domain_id=domain_id,
                account_id=account_id,
                entity_type=entity_type,
                entity_id=entity_id,
                tags=item["tags"],
                created_by=actor,
                source_type=request.source,
                source_ref=source_ref,
            )
            if entity_type == "case":
                try:
                    auto_assets_created_count += _auto_generate_assets_from_case(
                        client,
                        domain_id=domain_id,
                        account_id=account_id,
                        item=item,
                        created_by=actor,
                        source_type=request.source,
                    )
                except Exception:  # noqa: BLE001
                    # Auto asset extraction should not break case ingestion.
                    pass
            inserted_ids.append(entity_id)
            success_count += 1
        except Exception as exc:  # noqa: BLE001
            failed_items.append(
                {
                    "index": idx,
                    "title": title,
                    "source_ref": source_ref,
                    "reason": str(exc),
                }
            )

    failed_count = len(failed_items)
    final_status = "success"
    if failed_count and success_count:
        final_status = "partial_failed"
    elif failed_count and not success_count:
        final_status = "failed"

    client.table("ingestion_logs").update(
        {
            "status": final_status,
            "success_count": success_count,
            "failed_count": failed_count,
            "error_message": "; ".join([str(row["reason"]) for row in failed_items[:5]]) if failed_items else None,
            "updated_at": now_iso(),
        }
    ).eq("id", ingestion_id).execute()

    return {
        "status": final_status,
        "ingestion_log_id": ingestion_id,
        "entity_type": entity_type,
        "received": len(normalized_items),
        "success_count": success_count,
        "failed_count": failed_count,
        "auto_assets_created_count": auto_assets_created_count,
        "failed_items": failed_items,
        "inserted_ids": inserted_ids,
    }


def _extract_keywords(items: Iterable[str], limit: int = 10) -> List[str]:
    counter: Dict[str, int] = {}
    for raw in items:
        text = str(raw or "").strip().lower()
        if not text:
            continue
        chinese = re.findall(r"[\u4e00-\u9fff]{2,6}", text)
        latin = re.findall(r"[a-z0-9]{3,20}", text)
        for token in chinese + latin:
            if len(token) < 2:
                continue
            if token in {"内容", "用户", "我们", "这个", "那个", "today", "with"}:
                continue
            counter[token] = counter.get(token, 0) + 1
    sorted_tokens = sorted(counter.items(), key=lambda kv: kv[1], reverse=True)
    return [token for token, _ in sorted_tokens[:limit]]


def recommend_topics(
    client: Client,
    request: KbTopicRecommendRequest,
    *,
    created_by: str,
) -> Dict[str, Any]:
    domain = resolve_domain(client, request.domain_slug)
    domain_id = str(domain["id"])
    account_id = normalize_uuid(request.account_id)

    case_ids = [str(x).strip() for x in request.case_ids if str(x).strip()]
    asset_ids = [str(x).strip() for x in request.asset_ids if str(x).strip()]
    need_ids = [str(x).strip() for x in request.need_ids if str(x).strip()]
    limit = max(1, min(int(request.limit or 10), 30))

    case_rows = []
    if case_ids:
        case_rows = (
            client.table("cases")
            .select("id,title,content")
            .eq("domain_id", domain_id)
            .is_("deleted_at", "null")
            .in_("id", case_ids)
            .execute()
            .data
            or []
        )
    asset_rows = []
    if asset_ids:
        asset_rows = (
            client.table("assets")
            .select("id,summary,content")
            .eq("domain_id", domain_id)
            .is_("deleted_at", "null")
            .in_("id", asset_ids)
            .execute()
            .data
            or []
        )
    need_rows = []
    if need_ids:
        need_rows = (
            client.table("user_needs")
            .select("id,real_problem,original_text")
            .eq("domain_id", domain_id)
            .is_("deleted_at", "null")
            .in_("id", need_ids)
            .execute()
            .data
            or []
        )

    text_pool: List[str] = []
    text_pool.extend([str(row.get("title") or "") for row in case_rows])
    text_pool.extend([str(row.get("summary") or row.get("content") or "") for row in asset_rows])
    text_pool.extend([str(row.get("real_problem") or row.get("original_text") or "") for row in need_rows])

    keywords = _extract_keywords(text_pool, limit=12)
    if not keywords:
        keywords = ["选题", "增长", "复盘", "实操"]

    candidates: List[str] = []
    for idx, kw in enumerate(keywords):
        if len(candidates) >= limit:
            break
        if need_rows:
            need_hint = str(need_rows[idx % len(need_rows)].get("real_problem") or "").strip()
            title = f"{kw}：{need_hint[:18] or '用户问题'}的实操解法"
        else:
            title = f"{kw}：本周可执行内容方案"
        candidates.append(title[:200])

    created_topics: List[Dict[str, Any]] = []
    skipped_count = 0
    actor = str(created_by or "system:kb_topic_recommend").strip() or "system:kb_topic_recommend"
    created_at = now_iso()

    for title in candidates:
        existing = (
            client.table("topics")
            .select("*")
            .eq("domain_id", domain_id)
            .eq("title", title)
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
            .data
            or []
        )
        if existing:
            skipped_count += 1
            continue

        payload = {
            "domain_id": domain_id,
            "account_id": account_id,
            "title": title,
            "topic_description": "由知识库组合推荐生成，可编辑后进入生产。",
            "target_user": "",
            "platform": request.platform or "xiaohongshu",
            "structure_type": "problem_solution",
            "status": "todo",
            "reason": f"source:cases={len(case_rows)},assets={len(asset_rows)},needs={len(need_rows)}",
            "source_type": "kb_recommend",
            "source_ref": f"recommend:{created_at}",
            "created_by": actor,
        }
        inserted = client.table("topics").insert(payload).execute().data or []
        if not inserted:
            continue
        topic = inserted[0]
        topic_id = str(topic["id"])
        created_topics.append(topic)

        for case_row in case_rows:
            try:
                client.table("topic_case_links").insert(
                    {"topic_id": topic_id, "case_id": case_row["id"], "created_by": actor}
                ).execute()
            except Exception:  # noqa: BLE001
                pass
        for asset_row in asset_rows:
            try:
                client.table("topic_asset_links").insert(
                    {"topic_id": topic_id, "asset_id": asset_row["id"], "created_by": actor}
                ).execute()
            except Exception:  # noqa: BLE001
                pass
        for need_row in need_rows:
            try:
                client.table("topic_need_links").insert(
                    {"topic_id": topic_id, "user_need_id": need_row["id"], "created_by": actor}
                ).execute()
            except Exception:  # noqa: BLE001
                pass

    ai_job_payload = {
        "domain_id": domain_id,
        "account_id": account_id,
        "job_type": "topic_recommend",
        "status": "success",
        "input_jsonb": {
            "case_ids": case_ids,
            "asset_ids": asset_ids,
            "need_ids": need_ids,
            "limit": limit,
        },
        "model_name": "rule_based_v1",
        "output_jsonb": {
            "created_count": len(created_topics),
            "skipped_count": skipped_count,
            "keywords": keywords,
        },
        "finished_at": now_iso(),
        "source_type": "system",
        "source_ref": f"recommend:{created_at}",
        "created_by": actor,
    }
    try:
        client.table("ai_jobs").insert(ai_job_payload).execute()
    except Exception:  # noqa: BLE001
        pass

    return {
        "status": "ok",
        "created_count": len(created_topics),
        "skipped_count": skipped_count,
        "topics": created_topics,
        "source_summary": {
            "cases": len(case_rows),
            "assets": len(asset_rows),
            "needs": len(need_rows),
            "keywords": keywords,
        },
    }


def replay_rpa_run(
    client: Client,
    run_id: str,
    *,
    created_by: str,
) -> Dict[str, Any]:
    run_rows = (
        client.table("rpa_task_runs")
        .select("id,domain_id,entity_type,source_run_id,retry_count")
        .eq("id", run_id)
        .is_("deleted_at", "null")
        .limit(1)
        .execute()
        .data
        or []
    )
    if not run_rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="rpa run not found")
    run = run_rows[0]
    domain_id = str(run["domain_id"])
    entity_type = str(run.get("entity_type") or "").strip().lower()
    if entity_type not in _ENTITY_TABLE_MAP:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="rpa run has invalid entity_type")

    domain_rows = client.table("domains").select("slug").eq("id", domain_id).limit(1).execute().data or []
    if not domain_rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="domain not found")
    domain_slug = str(domain_rows[0]["slug"])

    clean_rows = (
        client.table("rpa_clean_results")
        .select("normalized_item,accepted")
        .eq("run_id", run_id)
        .eq("accepted", True)
        .order("created_at", desc=False)
        .limit(500)
        .execute()
        .data
        or []
    )
    if not clean_rows:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="no accepted clean results for replay")

    items: List[KbOctopusImportItem] = []
    for row in clean_rows:
        normalized = row.get("normalized_item")
        if not isinstance(normalized, dict):
            continue
        item = KbOctopusImportItem(
            title=str(normalized.get("title") or "")[:500],
            content=str(normalized.get("content") or "")[:20000],
            url=str(normalized.get("url") or "")[:2000],
            author=str(normalized.get("author") or "")[:200],
            platform=str(normalized.get("platform") or "xiaohongshu")[:80],
            metrics=normalized.get("metrics") if isinstance(normalized.get("metrics"), dict) else {},
            tags=normalized.get("tags") if isinstance(normalized.get("tags"), list) else [],
            captured_at=str(normalized.get("captured_at") or "")[:80],
            raw=normalized.get("raw") if isinstance(normalized.get("raw"), dict) else {},
            source_ref=str(normalized.get("source_ref") or "")[:1000],
        )
        items.append(item)

    if not items:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="accepted clean results are empty")

    replay_source_run_id = f"replay:{run_id}:{int(time.time())}"
    request = KbOctopusImportRequest(
        domain_slug=domain_slug,
        source="rpa_replay",
        source_run_id=replay_source_run_id,
        entity_type=entity_type,
        items=items,
    )
    result = import_octopus_payload(client, request, created_by=created_by)

    client.table("rpa_task_runs").update(
        {
            "retry_count": int(run.get("retry_count") or 0) + 1,
            "updated_at": now_iso(),
        }
    ).eq("id", run_id).execute()

    return {
        "status": "ok",
        "run_id": run_id,
        "replay_source_run_id": replay_source_run_id,
        **result,
    }
