import * as api from "@/lib/api";
import type { KbAssetRecord, KbCaseRecord, KbReviewRecord, KbTopicRecord, KbUserNeedRecord } from "@/lib/types";

export type Kind = "case" | "asset" | "user_need" | "topic" | "review";
export type KnowledgeRow = KbCaseRecord | KbAssetRecord | KbUserNeedRecord | KbTopicRecord | KbReviewRecord;
export type Scope = { domain_slug: string; account_id?: string };
export type Draft = Record<string, string>;
export const kinds: Kind[] = ["case", "asset", "user_need", "topic", "review"];
export const labels: Record<Kind, string> = { case: "案例", asset: "素材", user_need: "用户需求", topic: "选题", review: "复盘" };
export const topicStatuses = { todo: "待处理", drafted: "已起草", produced: "已制作", published: "已发布", archived: "已归档" };
export const platforms = { xiaohongshu: "小红书", douyin: "抖音", wechat: "微信", bilibili: "哔哩哔哩", other: "其他" };
export const metricLabels = { views: "浏览量", likes: "点赞数", comments: "评论数", shares: "分享数", collects: "收藏数", leads_count: "线索数" };
export type Field = { key: string; label: string; max?: number; required?: boolean; multiline?: boolean; options?: Record<string, string>; checkbox?: boolean };
const platformField: Field = { key: "platform", label: "平台", max: 80, options: platforms };
export const fields: Record<Kind, Field[]> = {
  case: [
    { key: "title", label: "标题", max: 500, required: true }, platformField,
    { key: "author", label: "作者", max: 200 }, { key: "url", label: "来源链接", max: 2000 },
    { key: "content", label: "正文", max: 20000, multiline: true },
  ],
  asset: [
    { key: "content", label: "素材正文", max: 20000, required: true, multiline: true },
    { key: "summary", label: "摘要", max: 5000, multiline: true },
    { key: "type", label: "素材类型", max: 80, options: { insight: "观点", fact: "事实", quote: "引用", method: "方法", example: "示例" } },
    { key: "source", label: "来源", max: 1000 }, { key: "usable_scene", label: "适用场景", max: 1000 },
    { key: "is_verified", label: "已核实", checkbox: true },
  ],
  user_need: [
    { key: "original_text", label: "需求原文", max: 20000, required: true, multiline: true },
    { key: "real_problem", label: "实际问题", max: 2000, multiline: true },
    { key: "user_type", label: "用户类型", max: 120 }, { key: "scenario", label: "使用场景", max: 1000 },
    { key: "demand_type", label: "需求类型", max: 120 }, { key: "emotion", label: "情绪", max: 120 },
  ],
  topic: [
    { key: "title", label: "选题标题", max: 500, required: true },
    { key: "topic_description", label: "选题说明", max: 10000, multiline: true },
    { key: "target_user", label: "目标用户", max: 1000 }, platformField,
    { key: "structure_type", label: "内容结构", max: 120 },
    { key: "status", label: "状态", options: topicStatuses },
    { key: "reason", label: "选题理由", max: 2000, multiline: true },
  ],
  review: [
    { key: "summary_text", label: "复盘摘要", max: 5000, required: true, multiline: true },
    { key: "content_item_ref", label: "作品链接或名称", max: 2000 }, platformField,
    { key: "success_points", label: "有效做法", max: 10000, multiline: true },
    { key: "failure_points", label: "存在问题", max: 10000, multiline: true },
    { key: "improvement", label: "下一步改进", max: 10000, multiline: true },
  ],
};

export function rowTitle(row: KnowledgeRow): string {
  if ("title" in row) return row.title || "未命名";
  if ("original_text" in row) return row.real_problem || row.original_text;
  if ("summary_text" in row) return row.summary_text || row.content_item_ref || "未填写摘要";
  return row.summary || row.content;
}

export function rowDetail(row: KnowledgeRow): string {
  if ("topic_description" in row) return row.topic_description;
  if ("original_text" in row) return row.original_text;
  if ("summary_text" in row) return row.improvement || row.success_points;
  return row.content;
}

export function makeDraft(kind: Kind, row?: KnowledgeRow): Draft {
  const source = (row || {}) as unknown as Record<string, unknown>;
  const draft: Draft = {};
  for (const field of fields[kind]) draft[field.key] = String(source[field.key] ?? (field.checkbox ? false : ""));
  if (!row) Object.assign(draft, { platform: "xiaohongshu", type: "insight", status: "todo" });
  if (kind === "review") draft.topic_id = String(source.topic_id || "");
  if (kind === "case" || kind === "review") {
    const metrics = (source.metrics || {}) as Record<string, unknown>;
    for (const key of Object.keys(metricLabels)) draft[`metric_${key}`] = metrics[key] == null ? "" : String(metrics[key]);
  }
  return draft;
}

export function makePayload(kind: Kind, draft: Draft, row?: KnowledgeRow): Record<string, unknown> {
  const payload: Record<string, unknown> = {};
  for (const field of fields[kind]) {
    const value = (draft[field.key] || "").trim();
    if (field.required && !value) throw new Error(`请填写${field.label}。`);
    if (field.max && [...value].length > field.max) throw new Error(`${field.label}不能超过 ${field.max} 字。`);
    payload[field.key] = field.checkbox ? value === "true" : value;
  }
  if (kind === "topic" && !Object.hasOwn(topicStatuses, String(payload.status))) throw new Error("请选择有效的选题状态。");
  if (kind === "case" && payload.url) {
    try {
      const url = new URL(String(payload.url));
      if (!["http:", "https:"].includes(url.protocol)) throw new Error();
    } catch { throw new Error("来源链接须为完整的 HTTP 或 HTTPS 地址。"); }
  }
  if (kind === "review") payload.topic_id = draft.topic_id || "";
  if (kind === "case" || kind === "review") {
    const metrics = { ...(row && "metrics" in row ? row.metrics : {}) };
    for (const key of Object.keys(metricLabels)) {
      const value = (draft[`metric_${key}`] || "").trim();
      if (!value) { delete metrics[key]; continue; }
      const number = Number(value);
      if (!Number.isSafeInteger(number) || number < 0) throw new Error("数据指标须为非负整数。");
      metrics[key] = number;
    }
    payload.metrics = metrics;
  }
  return payload;
}

export const listRows = {
  case: api.getKbCases, asset: api.getKbAssets, user_need: api.getKbUserNeeds,
  topic: api.getKbTopics, review: api.getKbReviews,
};
export const patchRow = {
  case: api.patchKbCase, asset: api.patchKbAsset, user_need: api.patchKbUserNeed,
  topic: api.patchKbTopic, review: api.patchKbReview,
};

export async function saveRow(kind: Kind, scope: Scope, draft: Draft, row?: KnowledgeRow): Promise<KnowledgeRow> {
  const payload = makePayload(kind, draft, row);
  if (row) return patchRow[kind](row.id, payload);
  const input = { ...scope, ...payload };
  // Required text fields have already been validated above; shared helpers own request serialization.
  switch (kind) {
    case "case": return api.createKbCase({ ...input, title: String(payload.title) });
    case "asset": return api.createKbAsset({ ...input, content: String(payload.content) });
    case "user_need": return api.createKbUserNeed({ ...input, original_text: String(payload.original_text) });
    case "topic": return api.createKbTopic({ ...input, title: String(payload.title) });
    case "review": return api.createKbReview(input);
  }
}

export function parseOctopusJson(text: string): Array<Record<string, unknown>> {
  let parsed: unknown;
  try { parsed = JSON.parse(text.replace(/^\uFEFF/, "")); } catch { throw new Error("JSON 格式无效，请检查引号、逗号和括号。"); }
  const isObject = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
  const items = Array.isArray(parsed) ? parsed : isObject(parsed) ? parsed.items : undefined;
  if (!Array.isArray(items) || !items.length) throw new Error("请提供非空数组，或含 items 数组的对象。");
  if (items.length > 500) throw new Error("单次最多导入 500 条，请拆分文件。");
  const limits: Record<string, number> = { title: 500, content: 20000, url: 2000, author: 200, platform: 80, captured_at: 80, source_ref: 1000 };
  return items.map((item: unknown, index: number) => {
    const fail = (message: string): never => { throw new Error(`第 ${index + 1} 条：${message}`); };
    if (!isObject(item)) return fail("必须是对象。");
    for (const [key, limit] of Object.entries(limits)) {
      if (item[key] !== undefined && (typeof item[key] !== "string" || [...String(item[key])].length > limit)) fail(`${key} 须为不超过 ${limit} 字的文本。`);
    }
    if (!String(item.title || "").trim() && !String(item.content || "").trim()) fail("标题和正文至少填写一项。");
    for (const key of ["metrics", "raw"]) if (item[key] !== undefined && !isObject(item[key])) fail(`${key} 必须是对象。`);
    if (item.tags !== undefined && (!Array.isArray(item.tags) || item.tags.length > 30 || item.tags.some(tag => typeof tag !== "string" || !tag.trim() || [...tag].length > 120))) fail("tags 须为最多 30 个非空文本标签，每个不超过 120 字。");
    const unknown = Object.keys(item).filter(key => !Object.hasOwn(limits, key) && !["metrics", "raw", "tags"].includes(key));
    if (unknown.length) fail(`不支持字段 ${unknown.join("、")}；原始扩展数据请放入 raw。`);
    return item;
  });
}

export async function prepareImportItems(items: Array<Record<string, unknown>>, scope: Scope): Promise<Array<Record<string, unknown>>> {
  return Promise.all(items.map(async item => {
    if (String(item.source_ref || "").trim() || String(item.url || "").trim()) return item;
    // The backend otherwise falls back to octopus:<row number>, which collides across imports.
    const identity = JSON.stringify([scope.domain_slug, scope.account_id || "", ...["title", "content", "author", "platform"].map(key => String(item[key] || "").trim())]);
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(identity));
    const hash = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
    return { ...item, source_ref: `octopus:manual:${hash}` };
  }));
}
