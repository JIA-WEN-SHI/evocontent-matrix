"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BarChart3, Workflow, UserCog, Play, RefreshCw, ExternalLink, FilePenLine } from "lucide-react";
import {
  DEFAULT_DOMAIN_SLUG,
  applyPendingStrategyItemChange,
  chatWithCoach,
  confirmCoachAction,
  getAccountSopCurrent,
  getAccountSopSnapshots,
  getCoachBrief,
  getOpsOverview,
  getPipelineTasks,
  getPendingStrategyItems,
  getPublishFeedbackSummary,
  getRuntimeContext,
  getChannelAccountStrategy,
  runAccountCollection,
  runAccountLoopDaily,
  collectionResultNotice,
  runChiefEvolution,
  updateMemoryItemStatus,
} from "@/lib/api";
import type {
  CoachAction,
  CoachBrief,
  OpsOverview,
  OpsReferenceItem,
  PendingStrategyItem,
  PipelineTask,
  PublishFeedbackSummaryItem,
  RuntimeContext,
  SopCurrentView,
  SopSnapshotRecord,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { buildLandingDemoFixture } from "@/lib/demo/landingDemo";
import { toUserFacingError } from "@/lib/user-facing-errors";
import { TaskActions } from "@/components/task-actions";
import { BrowserConnectionPanel } from "@/components/accounts/BrowserConnectionPanel";
import { BrowserCaptureDetails } from "@/components/accounts/BrowserCaptureDetails";

type ChatMsg = {
  role: "user" | "assistant";
  text: string;
};

type FocusPane = "public" | "account" | "publish" | "sop";
type PublishDecision = "approved" | "rejected" | "pending";
type DemoStage = "idle" | "collected" | "sop_ready";

const DOMAIN_SLUG = DEFAULT_DOMAIN_SLUG;

function shouldUseDemoFallback(): boolean {
  if (process.env.NEXT_PUBLIC_LANDING_DEMO_FALLBACK === "true") {
    return true;
  }
  if (typeof window === "undefined") {
    return false;
  }
  const params = new URLSearchParams(window.location.search);
  return params.get("demo") === "1";
}

function fmtTime(value?: string | null): string {
  if (!value) return "-";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "-";
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mi = String(d.getMinutes()).padStart(2, "0");
  return `${mm}/${dd} ${hh}:${mi}`;
}

function fmtCountdown(value?: string | null, nowTs?: number): string {
  if (!value) return "未设置";
  const target = new Date(value).getTime();
  if (Number.isNaN(target)) return "未设置";
  const now = nowTs ?? Date.now();
  const diff = target - now;
  if (diff <= 0) return "已到期";
  const hours = Math.floor(diff / 3_600_000);
  const mins = Math.floor((diff % 3_600_000) / 60_000);
  const secs = Math.floor((diff % 60_000) / 1000);
  return `${hours}h ${mins}m ${secs}s`;
}

function clip(text: string | null | undefined, max = 52): string {
  const value = String(text || "").trim();
  if (!value) return "无标题";
  return value.length > max ? `${value.slice(0, max)}...` : value;
}

function strategySourceLabel(value: string | null | undefined): string {
  const raw = String(value || "").toLowerCase();
  if (!raw) return "策略分析";
  if (raw.includes("review")) return "复盘结论";
  if (raw.includes("reflection")) return "系统复盘";
  if (raw.includes("memory")) return "历史沉淀";
  if (raw.includes("daily")) return "今日闭环";
  return "策略分析";
}

function canApplyStrategy(item: PendingStrategyItem): boolean {
  const blockedText = /\bhold\b|insufficient[ _-]evidence|competitor[ _-]only|证据不足|仅竞品/i;
  function blocked(value: unknown): boolean {
    if (typeof value === "string") return blockedText.test(value);
    if (Array.isArray(value)) return value.some(blocked);
    if (value && typeof value === "object") {
      const record = value as Record<string, unknown>;
      if (["hold", "insufficient_evidence", "competitor_only"].some(key => record[key] === true)) return true;
      if (["executable", "has_own_account_evidence"].some(key => record[key] === false)) return true;
      return Object.values(record).some(blocked);
    }
    return false;
  }
  let content: unknown = item.content;
  try { content = JSON.parse(item.content); } catch { /* Plain-text suggestions are supported. */ }
  return Boolean(item.proposed_changes?.length) && !blocked([item.title, item.tags, content, item.proposed_changes]);
}

function affectedStageLabel(value: string | null | undefined): string {
  const raw = String(value || "").toLowerCase();
  const map: Record<string, string> = {
    collector_agent: "采集环节",
    analysis_agent: "分析环节",
    copy_agent: "文案环节",
    review_agent: "复盘环节",
    draft_writer: "文案环节",
    memory: "策略沉淀",
    strategy: "策略层",
  };
  return map[raw] || "策略层";
}

function extractHashtagsFromText(text: string): string[] {
  const matches = text.match(/#[^\s#]+/g) || [];
  const normalized = matches
    .map((tag) => tag.trim())
    .filter(Boolean)
    .map((tag) => (tag.startsWith("#") ? tag : `#${tag}`));
  return Array.from(new Set(normalized));
}

function taskTags(task: PipelineTask): string[] {
  const payload = task.payload_jsonb || {};
  const rawTags = payload.tags;
  if (Array.isArray(rawTags)) {
    const normalized = rawTags
      .map((tag) => String(tag || "").trim())
      .filter(Boolean)
      .map((tag) => (tag.startsWith("#") ? tag : `#${tag}`));
    if (normalized.length) return Array.from(new Set(normalized));
  }
  if (typeof payload.hashtag_line === "string" && payload.hashtag_line.trim()) {
    const lineTags = extractHashtagsFromText(payload.hashtag_line);
    if (lineTags.length) return lineTags;
  }
  const body = String(payload.full_body || payload.body || "");
  const bodyTags = extractHashtagsFromText(body);
  if (bodyTags.length) return bodyTags;
  return ["#日本移民", "#经营管理签证", "#日本永住"];
}

function composeTaskPostBody(task: PipelineTask): string {
  const payload = task.payload_jsonb || {};
  const baseBody = String(payload.full_body || payload.body || "").trim();
  const tagsLine = taskTags(task).join(" ").trim();
  if (!baseBody) return tagsLine;
  if (baseBody.includes(tagsLine)) return baseBody;
  return `${baseBody}\n\n${tagsLine}`;
}

function ensureBodyHasTags(body: string, fallbackTags: string[]): string {
  const normalizedBody = body.trim();
  const tagsInBody = extractHashtagsFromText(normalizedBody);
  if (tagsInBody.length) return normalizedBody;
  const tagsLine = fallbackTags.join(" ").trim();
  if (!tagsLine) return normalizedBody;
  if (!normalizedBody) return tagsLine;
  return `${normalizedBody}\n\n${tagsLine}`;
}

function cardImage(item: OpsReferenceItem): string | null {
  const meta = item.meta_jsonb || {};
  const candidates = [
    meta.image,
    meta.image_url,
    meta.cover,
    meta.cover_url,
    meta.thumbnail,
    meta.thumb,
    meta.pic,
    meta.note_cover,
  ];
  for (const value of candidates) {
    if (typeof value === "string" && value.startsWith("http")) {
      return value;
    }
  }
  return null;
}

function cardTitle(item: OpsReferenceItem): string {
  const meta = item.meta_jsonb || {};
  const candidates = [meta.title, meta.note_title, meta.name, item.raw_text];
  for (const value of candidates) {
    if (typeof value === "string" && value.trim()) {
      return clip(value);
    }
  }
  return "未命名信息";
}

function MiniCard({ item, selected, onSelect }: { item: OpsReferenceItem; selected: boolean; onSelect: () => void }) {
  const image = cardImage(item);
  const title = cardTitle(item);
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onSelect}
      className={cn("group grid w-full min-w-0 grid-cols-[44px_minmax(0,1fr)] gap-3 rounded-lg border p-2 text-left transition hover:border-cyan-300/60 hover:bg-slate-900/80", selected ? "border-emerald-300/55 bg-emerald-400/10" : "border-cyan-400/20 bg-slate-900/55")}
    >
      <div className="relative h-11 w-11 overflow-hidden rounded border border-cyan-300/30 bg-slate-950">
        {image ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={image} alt={title} className="h-full w-full object-cover" />
        ) : (
          <div className="h-full w-full bg-[radial-gradient(circle_at_20%_20%,rgba(45,212,191,0.45),transparent_52%),radial-gradient(circle_at_80%_80%,rgba(34,211,238,0.35),transparent_48%)]" />
        )}
      </div>
      <div className="min-w-0">
        <p className="line-clamp-3 break-words text-sm font-medium leading-5 text-cyan-100 group-hover:text-cyan-50">{title}</p>
        <p className="mt-1 flex flex-wrap gap-x-2 gap-y-0.5 text-xs text-cyan-200/70"><span>{fmtTime(item.captured_at)}</span><span className="break-words">{item.source_type.includes("cli") ? "本地采集" : item.source_type.includes("browser") ? "浏览器采集" : item.source_type}</span></p>
      </div>
    </button>
  );
}

function coachBriefText(brief: CoachBrief): string {
  const summary = brief.yesterday_summary;
  const topReason = summary?.top_fail_reasons?.[0]?.reason || "暂无明显失败原因";
  return (
    `昨日数据：曝光 ${summary?.impressions ?? "暂无"}，互动率 ${summary?.engagement_rate ?? "暂无"}，审核通过率 ${summary?.review_pass_rate ?? "暂无"}。\n` +
    `主要问题：${topReason}。\n` +
    `今日动作：${(brief.today_actions || []).slice(0, 5).map((item, idx) => `${idx + 1}.${item.title}`).join("  ")}`
  );
}

export default function LandingPage() {
  const [runtime, setRuntime] = useState<RuntimeContext | null>(null);
  const loadedAccountId = useRef<string | undefined>(undefined);
  const [collectionMode, setCollectionMode] = useState("");
  const [overviewGlobal, setOverviewGlobal] = useState<OpsOverview | null>(null);
  const [overviewAccount, setOverviewAccount] = useState<OpsOverview | null>(null);
  const [publishSummary, setPublishSummary] = useState<PublishFeedbackSummaryItem[]>([]);
  const [pendingStrategy, setPendingStrategy] = useState<PendingStrategyItem[]>([]);
  const [pendingTasks, setPendingTasks] = useState<PipelineTask[]>([]);
  const [sopCurrent, setSopCurrent] = useState<SopCurrentView | null>(null);
  const [sopSnapshots, setSopSnapshots] = useState<SopSnapshotRecord[]>([]);
  const [coachBrief, setCoachBrief] = useState<CoachBrief | null>(null);
  const [coachActions, setCoachActions] = useState<CoachAction[]>([]);
  const [coachActionRunningId, setCoachActionRunningId] = useState<string>("");
  const [messages, setMessages] = useState<ChatMsg[]>([
    { role: "assistant", text: "教练已上线。我会先看昨天数据，再给你今天5条动作。" },
  ]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [actionBusy, setActionBusy] = useState("");
  const actionLock = useRef(false);
  const [notice, setNotice] = useState("");
  const [sopAnalysis, setSopAnalysis] = useState<string[]>([]);
  const [focusPane, setFocusPane] = useState<FocusPane>("public");
  const [selectedSourceId, setSelectedSourceId] = useState("");
  const [nowTs, setNowTs] = useState<number>(Date.now());
  const [publishDecision, setPublishDecision] = useState<PublishDecision>("pending");
  const [publishDecisionNote, setPublishDecisionNote] = useState("");
  const [publishDecisionSavedAt, setPublishDecisionSavedAt] = useState<number | null>(null);
  const [demoCollectTrace, setDemoCollectTrace] = useState<string[]>([]);
  const [demoSopTrace, setDemoSopTrace] = useState<string[]>([]);
  const [demoStage, setDemoStage] = useState<DemoStage>("idle");
  const [selectedTaskId, setSelectedTaskId] = useState<string>("");
  const [draftTitle, setDraftTitle] = useState<string>("");
  const [draftBody, setDraftBody] = useState<string>("");

  const selectTaskForEditing = useCallback((task?: PipelineTask | null) => {
    if (!task) {
      setSelectedTaskId("");
      setDraftTitle("");
      setDraftBody("");
      return;
    }
    const payload = task.payload_jsonb || {};
    setSelectedTaskId(task.id);
    setDraftTitle(String(payload.title || payload.topic || ""));
    setDraftBody(composeTaskPostBody(task));
  }, []);

  const wakeUp = useCallback((pane?: FocusPane) => {
    if (pane) setFocusPane(pane);
  }, []);

  const loadDemoStage = useCallback((stage: DemoStage) => {
    const demo = buildLandingDemoFixture();
    if (stage === "idle") {
      setRuntime(demo.runtime);
      setOverviewGlobal({ ...demo.overviewGlobal, reference_items: [] });
      setOverviewAccount({ ...demo.overviewAccount, reference_items: [] });
      setPublishSummary([]);
      setPendingStrategy([]);
      setPendingTasks([]);
      setSopCurrent(null);
      setSopSnapshots([]);
      setCoachBrief(demo.coachBrief);
      setCoachActions(demo.coachActions);
      selectTaskForEditing(null);
      return;
    }

    setRuntime(demo.runtime);
    setOverviewGlobal(demo.overviewGlobal);
    setOverviewAccount(demo.overviewAccount);
    setPublishSummary(demo.publishSummary);
    setPendingTasks(demo.pendingTasks);
    setSopCurrent(null);
    setSopSnapshots([]);
    setCoachBrief(demo.coachBrief);
    setCoachActions(demo.coachActions);
    setPendingStrategy(stage === "sop_ready" ? demo.pendingStrategy : []);

    const firstTask = demo.pendingTasks[0];
    selectTaskForEditing(firstTask || null);
  }, [selectTaskForEditing]);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    if (shouldUseDemoFallback()) {
      loadDemoStage(demoStage);
      setLoading(false);
      return;
    }
    try {
      const rt = await getRuntimeContext(DOMAIN_SLUG);
      const accountId = rt.publish_account.account_id || undefined;
      if (loadedAccountId.current !== accountId) {
        setPendingTasks([]);
        selectTaskForEditing(null);
        setOverviewAccount(null);
        setSelectedSourceId("");
      }
      loadedAccountId.current = accountId;
      setRuntime(rt);

      const unavailable: string[] = [];
      async function optional<T>(label: string, request: Promise<T>, fallback: T): Promise<T> {
        try { return await request; } catch { unavailable.push(label); return fallback; }
      }
      const [globalRes, accountRes, summaryRes, strategyRes, tasksRes, coachBriefRes, sopCurrentRes, sopSnapshotsRes, accountStrategyRes] = await Promise.all([
        optional("公共数据", getOpsOverview(DOMAIN_SLUG, 30), null),
        accountId ? optional("账号数据", getOpsOverview(DOMAIN_SLUG, 30, accountId), null) : Promise.resolve(null),
        optional("发布反馈", getPublishFeedbackSummary({ domainSlug: DOMAIN_SLUG, accountId, limit: 12 }), []),
        optional("待确认策略", getPendingStrategyItems({ domainSlug: DOMAIN_SLUG, accountId, limit: 8 }), []),
        accountId ? optional("内容任务", getPipelineTasks({ channel: "xiaohongshu", accountId, domainSlug: DOMAIN_SLUG, limit: 100 }), null) : Promise.resolve([]),
        optional("教练简报", getCoachBrief({ domain_slug: DOMAIN_SLUG, account_id: accountId ?? "" }), null),
        accountId ? optional("SOP", getAccountSopCurrent(accountId, { domainSlug: DOMAIN_SLUG }), null) : Promise.resolve(null),
        accountId ? optional("SOP历史", getAccountSopSnapshots(accountId, { domainSlug: DOMAIN_SLUG, limit: 8 }), null) : Promise.resolve(null),
        accountId ? optional("采集计划", getChannelAccountStrategy(accountId), null) : Promise.resolve(null),
      ]);
      if (unavailable.length) setError(`${unavailable.join("、")}暂时加载失败，请刷新重试。`);

      setOverviewGlobal(globalRes);
      setCollectionMode(accountStrategyRes?.collection_plan?.mode || "");
      setOverviewAccount(accountRes);
      setPublishSummary(summaryRes || []);
      setPendingStrategy(strategyRes || []);
      setPendingTasks((tasksRes || []).filter((task) => task.account_id === accountId && !task.published_at && ["queued", "intel_ready", "drafting", "pending_review", "review_rejected", "approved", "publish_failed"].includes(task.status)));
      setSopCurrent(sopCurrentRes || null);
      setSopSnapshots(sopSnapshotsRes?.items || []);
      setCoachBrief(coachBriefRes || null);
      setCoachActions((coachBriefRes?.today_actions as CoachAction[]) || []);
    } catch (err) {
      setError(toUserFacingError(err, "后台数据加载失败，请刷新重试。"));
    } finally {
      setLoading(false);
    }
  }, [demoStage, loadDemoStage, selectTaskForEditing]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const panel = (params.get("panel") || "").trim().toLowerCase();
    const paneMap: Record<string, FocusPane> = {
      public: "public",
      account: "account",
      publish: "publish",
      review: "publish",
      sop: "sop",
      collect: "public",
      strategy: "sop",
    };
    const pane = paneMap[panel];
    if (pane) {
      setFocusPane(pane);
    }
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => {
      setNowTs(Date.now());
    }, 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (shouldUseDemoFallback()) return;
    const timer = window.setInterval(() => {
      void refresh();
    }, 60_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    if (!coachBrief) return;
    setMessages((prev) => {
      if (prev.length !== 1) return prev;
      if (prev[0]?.role !== "assistant") return prev;
      return [{ role: "assistant", text: coachBriefText(coachBrief) }];
    });
  }, [coachBrief]);

  const accountId = runtime?.publish_account.account_id || undefined;
  const accountName = runtime?.publish_account.account_name || "未绑定账号";
  const personaName = runtime?.publish_account.strategy_profile?.persona_name || "未配置人设";
  const demoMode = runtime?.status === "demo_fallback" && shouldUseDemoFallback();
  const latestSopSnapshot = useMemo(
    () => (sopSnapshots[0] || sopCurrent?.sop_latest || null),
    [sopCurrent?.sop_latest, sopSnapshots]
  );
  const sopVersion = Number(latestSopSnapshot?.version || 0);
  const sopUpdatedAt = latestSopSnapshot?.captured_at || "";
  const sopChangeSummary = (latestSopSnapshot?.changed_fields || []).slice(0, 3).join(" / ");
  const activePromptSummary = (sopCurrent?.prompt_versions || [])
    .slice(0, 3)
    .map((item) => `${item.agent_name}:${item.version}`)
    .join(" · ");

  const publicCards = useMemo(() => {
    const items = overviewGlobal?.reference_items || [];
    return items.filter((it) => !it.account_id).slice(0, 18);
  }, [overviewGlobal]);

  const accountCards = useMemo(() => {
    if (!accountId) return [];
    const primary = (overviewAccount?.reference_items || []).filter((it) => it.account_id === accountId);
    if (primary.length > 0) return primary.slice(0, 18);
    const fallback = overviewGlobal?.reference_items || [];
    return fallback.filter((it) => it.account_id === accountId).slice(0, 18);
  }, [overviewAccount, overviewGlobal, accountId]);

  const publicDisplayCards = useMemo(() => {
    if (publicCards.length > 0) return publicCards;
    return accountCards.slice(0, 6);
  }, [publicCards, accountCards]);

  const sourceItems = focusPane === "public" ? publicDisplayCards : accountCards;
  const selectedSource = sourceItems.find((item) => item.id === selectedSourceId) || sourceItems[0] || null;
  const selectSource = (pane: FocusPane, item: OpsReferenceItem) => {
    setFocusPane(pane);
    setSelectedSourceId(item.id);
  };

  const publishQueue = useMemo(() => {
    const source = pendingTasks.slice(0, 6);
    return source.map((item) => ({
      id: item.id,
      title: clip((item.payload_jsonb?.title as string) || (item.payload_jsonb?.topic as string) || "未命名任务", 36),
      status: item.status,
      stage: item.stage,
      updatedAt: item.updated_at,
    }));
  }, [pendingTasks]);

  const selectedTask = useMemo(() => {
    if (!selectedTaskId) return null;
    return pendingTasks.find((task) => task.id === selectedTaskId && (demoMode || task.account_id === accountId)) || null;
  }, [pendingTasks, selectedTaskId, demoMode, accountId]);

  const previewTitle = (draftTitle || String(selectedTask?.payload_jsonb?.title || selectedTask?.payload_jsonb?.topic || "")).trim();
  const previewBody = (draftBody || (selectedTask ? composeTaskPostBody(selectedTask) : "")).trim();
  const previewTagLine = useMemo(() => {
    const fromDraft = extractHashtagsFromText(previewBody);
    if (fromDraft.length) return fromDraft.join(" ");
    if (selectedTask) return taskTags(selectedTask).join(" ");
    return "#日本移民 #经营管理签证 #日本永住";
  }, [previewBody, selectedTask]);

  const nextFeedbackAt = publishSummary.find((row) => !!row.next_feedback_at)?.next_feedback_at || null;
  const nextCountdown = fmtCountdown(nextFeedbackAt, nowTs);

  useEffect(() => {
    const key = `landing.publish-decision.${accountId || "global"}`;
    try {
      const raw = window.localStorage.getItem(key);
      if (!raw) {
        setPublishDecision("pending");
        setPublishDecisionNote("");
        setPublishDecisionSavedAt(null);
        return;
      }
      const parsed = JSON.parse(raw) as {
        decision?: PublishDecision;
        note?: string;
        saved_at?: number;
      };
      const decision = parsed.decision;
      setPublishDecision(decision === "approved" || decision === "rejected" || decision === "pending" ? decision : "pending");
      setPublishDecisionNote(typeof parsed.note === "string" ? parsed.note : "");
      setPublishDecisionSavedAt(typeof parsed.saved_at === "number" ? parsed.saved_at : null);
    } catch {
      setPublishDecision("pending");
      setPublishDecisionNote("");
      setPublishDecisionSavedAt(null);
    }
  }, [accountId]);

  const savePublishDecision = useCallback(() => {
    const key = `landing.publish-decision.${accountId || "global"}`;
    const payload = {
      decision: publishDecision,
      note: publishDecisionNote.trim(),
      saved_at: Date.now(),
    };
    try {
      window.localStorage.setItem(key, JSON.stringify(payload));
      setPublishDecisionSavedAt(payload.saved_at);
    } catch {
      setError("本地备注保存失败，请检查浏览器存储权限。");
    }
  }, [accountId, publishDecision, publishDecisionNote]);

  const focusedTitle =
    focusPane === "public"
      ? "今日公共数据"
      : focusPane === "account"
      ? "本账号数据"
      : focusPane === "publish"
      ? "今日草稿与审核"
      : "今日SOP与分析建议";

  const focusTabs: Array<{ key: FocusPane; label: string; short: string }> = [
    { key: "public", label: "公共素材", short: "公共" },
    { key: "account", label: "账号素材", short: "账号" },
    { key: "publish", label: "草稿审核", short: "草稿" },
    { key: "sop", label: "SOP分析", short: "SOP" },
  ];

  const focusedText =
    focusPane === "public"
      ? `已采集 ${publicCards.length} 条公共线索，优先用于热点判断。`
      : focusPane === "account"
      ? `已采集 ${accountCards.length} 条账号线索，用于账号专属视角。`
      : focusPane === "publish"
      ? `待审核 ${pendingTasks.filter((task) => task.status === "pending_review").length} 条，下一次反馈 ${nextFeedbackAt ? fmtTime(nextFeedbackAt) : "未设置"}。`
      : `当前待确认建议 ${pendingStrategy.length} 条，建议人工确认后生效。`;

  const focusedDetail = useMemo(() => {
    if (focusPane === "public" || focusPane === "account") {
      if (!selectedSource) return [];
      const captured = selectedSource.account_id === accountId && /browser|cli/.test(selectedSource.source_type);
      return [{
        id: selectedSource.id,
        title: String(selectedSource.meta_jsonb?.title || cardTitle(selectedSource)),
        hint: `${selectedSource.source_type} · ${fmtTime(selectedSource.captured_at)}`,
        extra: captured && !demoMode ? "" : selectedSource.raw_text,
        url: selectedSource.source_url || "",
      }];
    }
    if (focusPane === "publish") {
      return [];
    }
    if (demoMode && focusPane === "sop") {
      return [];
    }
    return pendingStrategy.slice(0, 8).map((item) => ({
      id: item.id,
      title: clip(item.title, 40),
      hint: `${affectedStageLabel(item.affected_agent)} · 待确认`,
      extra: clip(item.content, 60),
      url: demoMode ? null : "/accounts?panel=sop",
    }));
  }, [focusPane, selectedSource, accountId, pendingStrategy, demoMode]);

  async function onSend() {
    const text = input.trim();
    if (!text || sending) return;
    wakeUp();
    const history = messages.slice(-6).map((m) => ({ role: m.role, content: m.text }));
    setMessages((prev) => [...prev, { role: "user", text }]);
    setInput("");
    setSending(true);
    if (demoMode) {
      const normalized = text.replace(/\s+/g, "");
      const looksLikePublish = normalized.includes("发一条小红书") || (normalized.includes("发布") && normalized.includes("小红书"));
      const looksLikeCollect = normalized.includes("执行采集") || normalized.includes("开始采集") || normalized.includes("补齐今日采集");
      const looksLikeSop = normalized.includes("sop") || normalized.includes("复盘") || normalized.includes("分析");

      if (looksLikePublish) {
        setMessages((prev) => [...prev, { role: "assistant", text: "AI 代发布已取消，草稿保留，请自行发布。"}]);
        setSending(false);
        return;
      }

      if (looksLikeCollect) {
        await runDemoCollect();
        return;
      }

      if (looksLikeSop) {
        await runDemoSopAnalysis();
        return;
      }

      const coachText =
        `演示教练已接收目标：「${text}」。\n` +
        "建议先执行：1) 补齐今日采集 2) 刷新选题池 3) 推进待审核。\n" +
        "确认后我会逐步推进并同步每一步验收结果。";
      setMessages((prev) => [...prev, { role: "assistant", text: coachText }]);
      setSending(false);
      return;
    }
    try {
      const res = await chatWithCoach({
        message: text,
        domain_slug: DOMAIN_SLUG,
        account_id: accountId,
        conversation_history: history,
      });
      setCoachBrief(res.brief || null);
      setCoachActions((res.pending_actions as CoachAction[]) || []);
      setMessages((prev) => [...prev, { role: "assistant", text: res.coach_reply || "教练已收到你的目标。"}]);
    } catch (err) {
      const msg = toUserFacingError(err, "当前暂时无法完成本次请求，请稍后重试。");
      setMessages((prev) => [...prev, { role: "assistant", text: msg }]);
    } finally {
      setSending(false);
    }
  }

  useEffect(() => {
    if (!pendingTasks.length) {
      selectTaskForEditing(null);
      return;
    }
    const exists = pendingTasks.some((task) => task.id === selectedTaskId);
    if (exists) return;
    const first = pendingTasks[0];
    selectTaskForEditing(first);
  }, [demoMode, pendingTasks, selectedTaskId, selectTaskForEditing]);

  async function runLiveAction(name: string, action: () => Promise<void>) {
    if (actionLock.current || actionBusy || sending || loading) return;
    actionLock.current = true;
    setActionBusy(name);
    setError("");
    setNotice("");
    try {
      await action();
      await refresh();
    } catch (err) {
      setError(toUserFacingError(err, "操作失败，请稍后重试。"));
    } finally {
      actionLock.current = false;
      setActionBusy("");
    }
  }

  async function generateDraft() {
    if (demoMode || !accountId) return;
    const requestedAccountId = accountId;
    let generatedTaskId = "";
    await runLiveAction("generate", async () => {
      const result = await runAccountLoopDaily(requestedAccountId, { domain_slug: DOMAIN_SLUG, flow: "full" });
      if (loadedAccountId.current !== requestedAccountId) return;
      const detail = result.result || result.loop_report || {};
      const taskId = detail.pipeline_task_id;
      if (typeof taskId === "string" && result.status !== "blocked") generatedTaskId = taskId;
      if (result.status === "pending_review") {
        setNotice("今日草稿已就绪，等待你审核。");
      } else if (result.status === "approved") {
        setNotice("今日草稿已通过审核，请自行发布后回填链接。");
      } else if (["published", "metrics_ready", "done"].includes(result.status)) {
        setNotice("今日任务已进入发布后反馈阶段，请查看完整任务详情。");
      } else if (result.status === "blocked") {
        setNotice(result.blocked_reason || String(detail.blocked_reason || detail.message || detail.reason || "暂未达到生成条件，请核对采集与账号配置。"));
      } else if (["awaiting_status", "queued", "drafting", "running", "intel_ready"].includes(result.status)) {
        setNotice("生成状态尚未确认，请刷新数据查看任务进度；本次不会重复生成。");
      } else {
        setNotice(result.blocked_reason || String(detail.reason || "本次未确认草稿生成完成，请核对任务状态。"));
      }
    });
    if (generatedTaskId && loadedAccountId.current === requestedAccountId) {
      setSelectedTaskId(generatedTaskId);
      wakeUp("publish");
    }
  }

  async function collect() {
    if (demoMode) return runDemoCollect();
    if (!accountId || (collectionMode !== "xhs_cli" && !window.confirm(`确认按「${accountName}」的采集计划执行采集？`))) return;
    await runLiveAction("collect", async () => {
      const result = await runAccountCollection({ account_id: accountId, domain_slug: DOMAIN_SLUG });
      if (result.status === "awaiting_browser") {
        setNotice(result.message || "请让助手在已登录的 Chrome 中采集笔记，再导入页面摘要。");
        return;
      }
      if (["queued", "running", "awaiting_user"].includes(result.status)) {
        setNotice("浏览器任务已启动，请查看 Chrome 页面采集的进度。");
        return;
      }
      setNotice(collectionResultNotice(result));
    });
  }

  async function analyzeSop() {
    if (demoMode) return runDemoSopAnalysis();
    if (!accountId || !window.confirm(`确认分析「${accountName}」的内容与反馈，生成 SOP 建议？`)) return;
    await runLiveAction("sop", async () => {
      const result = await runChiefEvolution({ domain_slug: DOMAIN_SLUG, account_id: accountId, apply: false });
      if (result.status !== "ok") throw new Error("分析未完成");
      const report = result.result.analysis_report;
      setSopAnalysis([report.competitor_insights, report.our_weakness, report.extracted_methodology].filter(Boolean));
      setNotice("SOP 分析完成；建议尚未应用，请查看分析结果和待确认策略。");
    });
  }

  async function runDemoCollect() {
    if (!demoMode || sending) return;
    setSending(true);
    wakeUp("public");
    setDemoCollectTrace([]);
    const sleep = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));
    const log = (msg: string) =>
      setDemoCollectTrace((prev) => [...prev, `${new Date().toLocaleTimeString("zh-CN", { hour12: false })} · ${msg}`]);

    log("开始执行采集任务（演示）");
    await sleep(600);
    log("步骤1/3：采集公共热点数据");
    await sleep(700);
    log("步骤2/3：采集账号相关数据");
    await sleep(700);
    log("步骤3/3：完成清洗并更新主屏");
    setDemoStage("collected");
    loadDemoStage("collected");
    setMessages((prev) => [...prev, { role: "assistant", text: "采集完成：公共数据与账号数据已更新，你可以继续生成和审核草稿。"}]);
    wakeUp("account");
    setSending(false);
  }

  function saveDemoDraft() {
    if (!demoMode || !selectedTaskId) return;
    const currentTask = pendingTasks.find((task) => task.id === selectedTaskId);
    const fallbackTags = currentTask ? taskTags(currentTask) : ["#日本移民", "#经营管理签证", "#日本永住"];
    const persistedTitle = draftTitle.trim() || String(currentTask?.payload_jsonb?.title || currentTask?.payload_jsonb?.topic || "未命名草稿");
    const persistedBody = ensureBodyHasTags(
      draftBody.trim() || String(currentTask?.payload_jsonb?.body || currentTask?.payload_jsonb?.full_body || ""),
      fallbackTags
    );
    setPendingTasks((prev) =>
      prev.map((task) =>
        task.id === selectedTaskId
          ? {
              ...task,
              payload_jsonb: {
                ...task.payload_jsonb,
                title: persistedTitle,
                body: persistedBody,
                full_body: persistedBody,
                tags: fallbackTags,
              },
              updated_at: new Date().toISOString(),
            }
          : task
      )
    );
    setDraftTitle(persistedTitle);
    setDraftBody(persistedBody);
    setMessages((prev) => [...prev, { role: "assistant", text: "草稿已保存。"}]);
  }

  function approveDemoDraft(taskId: string) {
    if (!demoMode || !taskId || sending) return;
    const task = pendingTasks.find((row) => row.id === taskId);
    if (!task) return;
    const fallbackTags = taskTags(task);
    const title =
      task.id === selectedTaskId
        ? draftTitle.trim() || String(task.payload_jsonb?.title || task.payload_jsonb?.topic || "演示草稿")
        : String(task.payload_jsonb?.title || task.payload_jsonb?.topic || "演示草稿");
    const body =
      task.id === selectedTaskId
        ? ensureBodyHasTags(draftBody.trim() || String(task.payload_jsonb?.body || task.payload_jsonb?.full_body || ""), fallbackTags)
        : ensureBodyHasTags(String(task.payload_jsonb?.body || task.payload_jsonb?.full_body || ""), fallbackTags);

    setPendingTasks((prev) => prev.map((row) => row.id === task.id ? {
      ...row, status: "approved", payload_jsonb: { ...row.payload_jsonb, title, body, full_body: body },
    } : row));
    if (task.id === selectedTaskId) {
      setDraftTitle(title);
      setDraftBody(body);
    }
    setMessages((prev) => [...prev, { role: "assistant", text: `《${title}》已通过审核，请自行发布。` }]);
  }

  function rejectDemoTask(taskId: string) {
    if (!demoMode || !taskId) return;
    setPendingTasks((prev) => prev.filter((row) => row.id !== taskId));
    setMessages((prev) => [...prev, { role: "assistant", text: "该内容已驳回，不会进入发布流程。"}]);
  }

  async function runDemoSopAnalysis() {
    if (!demoMode || sending) return;
    setSending(true);
    wakeUp("sop");
    setDemoSopTrace([]);
    const sleep = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));
    const log = (msg: string) =>
      setDemoSopTrace((prev) => [...prev, `${new Date().toLocaleTimeString("zh-CN", { hour12: false })} · ${msg}`]);

    log("开始SOP分析（演示）");
    await sleep(700);
    log("步骤1/3：聚合近14天发布与互动数据");
    await sleep(800);
    log("步骤2/3：对比高表现样本，识别标题与开头问题");
    await sleep(800);
    log("步骤3/3：输出可执行优化建议");
    setDemoStage("sop_ready");
    loadDemoStage("sop_ready");
    setMessages((prev) => [...prev, { role: "assistant", text: "SOP分析完成：右侧已生成可确认的优化建议。"}]);
    setSending(false);
  }

  async function onConfirmCoach(actionId: string) {
    if (!actionId || coachActionRunningId) return;
    setCoachActionRunningId(actionId);
    if (demoMode) {
      const target = coachActions.find((item) => item.id === actionId);
      setCoachActions((prev) => prev.filter((item) => item.id !== actionId));
      setMessages((prev) => [
        ...prev,
        { role: "assistant", text: `演示执行完成：${target?.title || "动作"}。下一步建议继续推进审核与复盘。` },
      ]);
      setCoachActionRunningId("");
      return;
    }
    try {
      const res = await confirmCoachAction({
        action_id: actionId,
        domain_slug: DOMAIN_SLUG,
        account_id: accountId,
        confirm: true,
      });
      const resultText = res.message || (res.status === "ok" ? "动作已执行完成。" : "动作执行失败。");
      setMessages((prev) => [...prev, { role: "assistant", text: `教练执行反馈：${resultText}` }]);
      if (res.next_brief) {
        setCoachBrief(res.next_brief);
        setCoachActions((res.next_brief.today_actions as CoachAction[]) || []);
      } else {
        setCoachActions((prev) => prev.filter((item) => item.id !== actionId));
      }
      await refresh();
    } catch (err) {
      const msg = err instanceof Error ? err.message : "确认执行失败";
      setMessages((prev) => [...prev, { role: "assistant", text: `教练执行失败：${msg}` }]);
    } finally {
      setCoachActionRunningId("");
    }
  }

  async function onAgree(item: PendingStrategyItem) {
    if (!canApplyStrategy(item)) return;
    if (demoMode) {
      setPendingStrategy((prev) => prev.filter((row) => row.id !== item.id));
      return;
    }
    const proposal = item.proposed_changes?.[0];
    if (!accountId || !proposal || !window.confirm(`确认应用「${item.title}」？\n目标：${proposal.label}\n改动后：${proposal.after_summary}`)) return;
    await runLiveAction(item.id, async () => {
      await applyPendingStrategyItemChange(item.id, { accountId, domainSlug: DOMAIN_SLUG, target: proposal.target, proposalFingerprint: proposal.proposal_fingerprint, reason: "来自前置主屏确认" });
      setNotice("策略变更已应用。");
    });
  }

  async function onReject(item: PendingStrategyItem) {
    if (demoMode) {
      setPendingStrategy((prev) => prev.filter((row) => row.id !== item.id));
      return;
    }
    if (!window.confirm(`确认不采用「${item.title}」？`)) return;
    await runLiveAction(item.id, async () => {
      await updateMemoryItemStatus(item.id, "deprecated");
      setNotice("已记录不采用该建议。");
    });
  }

  return (
    <div className="landing-workspace relative min-h-screen overflow-hidden bg-[radial-gradient(circle_at_10%_20%,#0f2b3a_0%,#050910_35%,#02050b_100%)] text-sm text-cyan-50">
      <div className="pointer-events-none absolute inset-0 bg-[linear-gradient(transparent_96%,rgba(34,211,238,0.08)_97%),linear-gradient(90deg,transparent_96%,rgba(34,211,238,0.06)_97%)] bg-[size:36px_36px] opacity-45" />
      <div className="relative mx-auto flex min-h-screen w-full max-w-[1700px] flex-col gap-4 p-3 sm:p-5 lg:p-6">
        <header className="rounded-2xl border border-cyan-400/20 bg-slate-950/65 px-5 py-4 backdrop-blur">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="text-xs uppercase tracking-[0.24em] text-cyan-300/70">NeuroLab Digital Operator</p>
              <h1 className="mt-1 text-xl font-semibold text-cyan-100">内容工作台</h1>
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <button type="button" disabled={sending || loading || Boolean(actionBusy) || (!demoMode && !accountId)} onClick={() => void collect()} className="inline-flex min-h-10 items-center gap-2 rounded-lg border border-emerald-300/45 bg-emerald-300/15 px-4 py-2 text-sm text-emerald-100 hover:bg-emerald-300/25 disabled:opacity-50">
                <Play size={16}/>{actionBusy === "collect" ? "采集中..." : "开始采集"}
              </button>
              <button type="button" disabled={demoMode || sending || loading || Boolean(actionBusy) || !accountId} onClick={() => void generateDraft()} className="inline-flex min-h-10 items-center gap-2 rounded-lg border border-cyan-300/45 bg-cyan-300/15 px-4 py-2 text-sm text-cyan-100 hover:bg-cyan-300/25 disabled:opacity-50">
                <FilePenLine size={16}/>{actionBusy === "generate" ? "生成中..." : "生成今日草稿"}
              </button>
              <button
                type="button"
                disabled={loading || Boolean(actionBusy)}
                onClick={() => {
                  wakeUp();
                  void refresh();
                }}
                className="rounded-lg border border-cyan-300/40 bg-cyan-400/10 px-3 py-2 text-sm text-cyan-100 transition hover:bg-cyan-300/20"
              >
                <span className="inline-flex items-center gap-2"><RefreshCw size={16} className={loading ? "animate-spin" : ""}/>{loading ? "刷新中..." : "刷新数据"}</span>
              </button>
              <div className="rounded-lg border border-cyan-500/30 bg-cyan-400/10 px-3 py-2 text-right text-xs text-cyan-100/90">
                <p>当前账号：{accountName}</p>
                <p className="mt-0.5 text-cyan-200/70">人设：{personaName}</p>
                <p className="mt-0.5 text-cyan-200/70">
                  SOP 版本：{sopVersion > 0 ? `v${sopVersion}` : "未生成"}
                  {sopUpdatedAt ? ` · ${fmtTime(sopUpdatedAt)}` : ""}
                </p>
              </div>
            </div>
          </div>
          {!demoMode && error ? (
            <p role="alert" className="mt-2 text-sm text-amber-200/90">
              {error}
            </p>
          ) : null}
          {notice ? <p role="status" className="mt-2 text-sm text-emerald-200">{notice}</p> : null}
          {actionBusy ? <p role="status" className="mt-2 text-sm">正在处理，请稍候...</p> : null}
          {demoMode ? <p className="mt-2 text-sm text-amber-200">演示模式：所有操作仅模拟，不会发布真实内容。</p> : null}
        </header>

        <nav aria-label="工作台导航" className="flex flex-wrap items-center gap-2 border-b border-cyan-300/20 pb-3">
          <Link href="/dashboard" className="inline-flex items-center gap-2 rounded-lg px-3 py-2 text-cyan-100 hover:bg-cyan-300/10"><BarChart3 size={16}/>数据总览</Link>
          <Link href="/legacy-flow" className="inline-flex items-center gap-2 rounded-lg px-3 py-2 text-cyan-100 hover:bg-cyan-300/10"><Workflow size={16}/>流程执行</Link>
          <Link href="/accounts" className="inline-flex items-center gap-2 rounded-lg px-3 py-2 text-cyan-100 hover:bg-cyan-300/10"><UserCog size={16}/>账号管理</Link>
        </nav>

        <section
          aria-label="内容工作区"
          className="workspace-layout grid min-h-0 grid-cols-1 items-start gap-4"
        >
          <div
            className="workspace-side workspace-sources order-2 grid min-w-0 gap-4 sm:grid-cols-2"
          >
            <article
              className={cn(
                "flex min-w-0 flex-col rounded-2xl border bg-[linear-gradient(160deg,rgba(2,10,24,0.92),rgba(4,18,34,0.7))] p-4 backdrop-blur",
                focusPane === "public" ? "border-cyan-300/70 shadow-[0_0_24px_rgba(34,211,238,0.28)]" : "border-cyan-300/20"
              )}
            >
                <>
                  <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                    <h2 className="text-[13px] font-semibold text-cyan-100">今日公共数据</h2>
                    <span className="text-xs text-cyan-300/70">{publicCards.length} 条</span>
                  </div>
                  <div className="workspace-source-list grid max-h-72 min-h-0 gap-2 overflow-y-auto pr-1">
                    {publicDisplayCards.length ? (
                      publicDisplayCards.map((item) => <MiniCard key={item.id} item={item} selected={focusPane === "public" && selectedSource?.id === item.id} onSelect={() => selectSource("public", item)}/>)
                    ) : (
                      <div className="rounded-lg border border-cyan-300/20 bg-slate-900/45 p-3">
                        <p className="text-sm text-cyan-100/85">暂无公共数据</p>
                        <p className="mt-1 text-xs text-cyan-200/70">先执行一次公共采集后，这里会展示“今日公共输入”的图文列表。</p>
                        <div className="mt-2 flex items-center gap-2">
                          {demoMode ? (
                            <button
                              type="button"
                              onClick={() => void runDemoCollect()}
                              className="rounded border border-cyan-300/35 bg-cyan-300/10 px-2 py-1 text-xs text-cyan-100 transition hover:bg-cyan-300/20"
                            >
                              去执行采集
                            </button>
                          ) : (
                            <Link
                              href="/accounts?panel=collect"
                              className="rounded border border-cyan-300/35 bg-cyan-300/10 px-2 py-1 text-xs text-cyan-100 transition hover:bg-cyan-300/20"
                            >
                              去执行采集
                            </Link>
                          )}
                          <button
                            type="button"
                            onClick={() => {
                              wakeUp("public");
                              void refresh();
                            }}
                            className="rounded border border-cyan-300/35 bg-slate-900/70 px-2 py-1 text-xs text-cyan-200 transition hover:bg-slate-800/80"
                          >
                            重新读取
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                  {publicCards.length === 0 && accountCards.length > 0 ? (
                    <p className="mt-1 text-[11px] text-cyan-300/65">当前未采到“公共输入”，已临时展示账号输入作为兜底参考。</p>
                  ) : null}
                  {demoMode && demoCollectTrace.length > 0 ? (
                    <div className="mt-2 max-h-20 space-y-1 overflow-y-auto rounded-lg border border-cyan-300/20 bg-slate-900/55 p-2 text-[11px] text-cyan-100/85">
                      {demoCollectTrace.slice(-4).map((line, idx) => (
                        <p key={`${idx}-${line}`}>{line}</p>
                      ))}
                    </div>
                  ) : null}
                </>
            </article>

            <article
              className={cn(
                "flex min-w-0 flex-col rounded-2xl border bg-[linear-gradient(160deg,rgba(2,10,24,0.92),rgba(4,18,34,0.7))] p-4 backdrop-blur",
                focusPane === "account" ? "border-cyan-300/70 shadow-[0_0_24px_rgba(34,211,238,0.28)]" : "border-cyan-300/20"
              )}
            >
                <>
                  <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                    <h2 className="text-[13px] font-semibold text-cyan-100">本账号数据</h2>
                    <span className="text-xs text-cyan-300/70">{accountCards.length} 条</span>
                  </div>
                  <div className="workspace-source-list grid max-h-96 min-h-0 gap-2 overflow-y-auto pr-1">
                    {accountCards.length ? accountCards.map((item) => <MiniCard key={item.id} item={item} selected={focusPane === "account" && selectedSource?.id === item.id} onSelect={() => selectSource("account", item)}/>) : <p className="text-sm text-cyan-200/60">暂无账号数据</p>}
                  </div>
                </>
            </article>
          </div>

          <article aria-label="内容详情与教练" className="workspace-detail relative order-1 min-w-0 overflow-hidden rounded-2xl border border-cyan-300/25 bg-slate-950/65 p-3 backdrop-blur sm:p-5">
            <div aria-hidden="true" className="pointer-events-none absolute inset-0 scale-[1.16] opacity-100">
              <div className="character-wave absolute left-1/2 top-[42%] h-[70%] w-[70%] -translate-x-1/2 -translate-y-1/2 rounded-full" />
              <div className="character-core absolute left-1/2 top-[42%] h-[48%] w-[48%] -translate-x-1/2 -translate-y-1/2 rounded-full" />
              <div className="character-ring absolute left-1/2 top-[42%] h-[62%] w-[62%] -translate-x-1/2 -translate-y-1/2 rounded-full" />
              <div className="character-ring-2 absolute left-1/2 top-[42%] h-[78%] w-[78%] -translate-x-1/2 -translate-y-1/2 rounded-full" />
            </div>

            <div className="relative z-10 flex h-full min-h-0 flex-col gap-3">
              <div className="min-w-0 flex-1 rounded-lg border border-cyan-300/25 bg-slate-950/65 p-3 sm:p-4">
                <h3 className="mt-1 text-base font-semibold text-cyan-50">{focusedTitle}</h3>
                <p className="mt-1 text-[13px] text-cyan-100/85">{focusedText}</p>
                <div role="group" aria-label="内容视图" className="mt-3 grid grid-cols-2 gap-1 border-b border-cyan-300/20 pb-2 sm:grid-cols-4">
                  {focusTabs.map((tab) => (
                    <button
                      key={tab.key}
                      type="button"
                      aria-pressed={focusPane === tab.key}
                      onClick={() => wakeUp(tab.key)}
                      className={cn(
                        "min-h-10 rounded border px-2 py-2 text-sm transition",
                        focusPane === tab.key
                          ? "border-cyan-300/70 bg-cyan-300/20 text-cyan-50"
                          : "border-cyan-300/30 bg-slate-900/70 text-cyan-200 hover:border-cyan-300/45"
                      )}
                    >
                      {tab.label}
                    </button>
                  ))}
                </div>
                {(focusPane === "public" || focusPane === "account") && sourceItems.length > 0 ? <label className="workspace-mobile-source mt-3 block text-sm">选择素材
                  <select aria-label="选择素材" value={selectedSource?.id || ""} onChange={(event) => setSelectedSourceId(event.target.value)} className="mt-1 w-full min-w-0 rounded border border-cyan-300/30 bg-slate-950 p-2">
                    {sourceItems.map((item) => <option key={item.id} value={item.id}>{cardTitle(item)}</option>)}
                  </select>
                </label> : null}
                <div className="mt-3 min-w-0">
                  <div className="grid min-w-0 gap-2">
                    {focusedDetail.length ? (
                      focusedDetail.map((item, idx) => {
                      const url = item.url || "";
                      const external = /^https?:\/\//i.test(url);
                      const tone = idx % 2 === 0 ? "from-cyan-500/10 to-transparent" : "from-emerald-500/10 to-transparent";
                      const cls = `min-w-0 rounded-lg border border-cyan-300/20 bg-gradient-to-br ${tone} bg-slate-900/60 px-3 py-3 transition`;
                      return (
                        <article key={item.id} className={cls}>
                        <p className="break-words text-base font-medium text-cyan-50">{item.title}</p>
                        <p className="mt-1 text-xs text-cyan-200/70">{item.hint}</p>
                        {url ? <a
                          href={url}
                          target={external ? "_blank" : undefined}
                          rel={external ? "noreferrer" : undefined}
                          className="mt-2 inline-flex items-center gap-1 text-xs text-cyan-200 underline hover:text-emerald-100"
                        >
                          <ExternalLink size={14} aria-hidden="true"/>{external ? "查看来源" : "查看详情"}
                        </a> : null}
                        {"extra" in item && item.extra ? <p className="mt-2 whitespace-pre-wrap break-words text-sm leading-6 text-cyan-100/90">{String(item.extra)}</p> : null}
                        {!demoMode && accountId && selectedSource?.account_id === accountId && /browser|cli/.test(selectedSource.source_type) && (focusPane === "account" || focusPane === "public") ? <BrowserCaptureDetails key={item.id} accountId={accountId} itemId={item.id} expanded/> : null}
                        </article>
                      );
                      })
                    ) : focusPane === "publish" || (demoMode && focusPane === "sop") ? null : (
                      <p className="text-xs text-cyan-200/70">暂无详细项</p>
                    )}
                  </div>
                </div>
                {!demoMode && accountId && collectionMode !== "xhs_cli" && (focusPane === "public" || focusPane === "account") ? (
                  <BrowserConnectionPanel key={accountId} accountId={accountId} domainSlug={DOMAIN_SLUG} onImported={() => void refresh()} />
                ) : null}
                {!demoMode && focusPane === "publish" ? (
                  <div className="mt-3 min-w-0">
                    <label className="text-sm">内容任务
                      <select aria-label="内容任务" value={selectedTaskId} onChange={(event) => selectTaskForEditing(pendingTasks.find((task) => task.id === event.target.value))} className="ml-2 max-w-full rounded border border-cyan-300/35 bg-slate-950 p-2">
                        {!pendingTasks.length ? <option value="">暂无可操作任务</option> : null}
                        {pendingTasks.map((task) => <option key={task.id} value={task.id}>{clip(String(task.payload_jsonb?.title || task.payload_jsonb?.topic || "未命名任务"), 28)}</option>)}
                      </select>
                    </label>
                    {selectedTask ? <>
                      <TaskActions key={selectedTask.id} task={selectedTask} onUpdated={(updated) => setPendingTasks((items) => items.map((item) => item.id === updated.id ? updated : item))} />
                      <Link href={`/tasks/${selectedTask.id}`} className="text-sm underline">查看完整任务详情</Link>
                    </> : <p className="mt-2 text-sm">暂无可操作任务。</p>}
                  </div>
                ) : null}
                {demoMode && focusPane === "publish" ? (
                  <div className="mt-3 rounded-lg border border-cyan-300/25 bg-slate-900/55 p-3">
                    <div className="flex items-center justify-between gap-2">
                      <p className="text-xs text-cyan-100/90">草稿编辑与审核</p>
                      <select
                        value={selectedTaskId}
                        onChange={(e) => {
                          const nextId = e.target.value;
                          const t = pendingTasks.find((row) => row.id === nextId);
                          selectTaskForEditing(t || null);
                        }}
                        className="rounded border border-cyan-300/35 bg-slate-950/80 px-2 py-1 text-xs text-cyan-100 focus:border-cyan-300/60 focus:outline-none"
                      >
                        {pendingTasks.map((task) => (
                          <option key={task.id} value={task.id}>
                            {clip(String(task.payload_jsonb?.title || task.payload_jsonb?.topic || "未命名任务"), 28)}
                          </option>
                        ))}
                      </select>
                    </div>
                    <input
                      value={draftTitle}
                      onChange={(e) => setDraftTitle(e.target.value)}
                      className="mt-2 w-full rounded border border-cyan-300/25 bg-slate-950/80 px-2 py-1 text-xs text-cyan-100 focus:border-cyan-300/60 focus:outline-none"
                      placeholder="标题"
                    />
                    <textarea
                      value={draftBody}
                      onChange={(e) => setDraftBody(e.target.value)}
                      className="mt-2 min-h-[74px] w-full resize-y rounded border border-cyan-300/25 bg-slate-950/80 px-2 py-1 text-xs text-cyan-100 focus:border-cyan-300/60 focus:outline-none"
                      placeholder="正文"
                    />
                    <details className="mt-2 text-sm">
                      <summary className="cursor-pointer text-cyan-200">帖子预览</summary>
                      <p className="mt-2 break-words font-medium">{previewTitle || "未命名帖子"}</p>
                      <p className="mt-2 whitespace-pre-wrap break-words leading-6">{previewBody}</p>
                      <p className="mt-2 break-words text-xs text-cyan-300/75">{previewTagLine}</p>
                    </details>
                    <div className="mt-2 flex items-center justify-end gap-2">
                      <button
                        type="button"
                        onClick={saveDemoDraft}
                        className="rounded border border-cyan-300/35 bg-cyan-300/10 px-2 py-1 text-xs text-cyan-100 transition hover:bg-cyan-300/20"
                      >
                        保存编辑
                      </button>
                      <button
                        type="button"
                        disabled={!selectedTaskId}
                        onClick={() => approveDemoDraft(selectedTaskId)}
                        className="rounded border border-emerald-300/35 bg-emerald-300/15 px-2 py-1 text-xs text-emerald-100 transition hover:bg-emerald-300/25 disabled:cursor-not-allowed disabled:opacity-60"
                      >
                        审核通过
                      </button>
                      <button
                        type="button"
                        disabled={!selectedTaskId}
                        onClick={() => rejectDemoTask(selectedTaskId)}
                        className="rounded border border-rose-300/35 bg-rose-300/15 px-2 py-1 text-xs text-rose-100 transition hover:bg-rose-300/25 disabled:cursor-not-allowed disabled:opacity-60"
                      >
                        驳回
                      </button>
                    </div>
                  </div>
                ) : null}
                {focusPane === "sop" ? (
                  <div className="mt-3 max-h-[300px] shrink-0 overflow-y-auto rounded-lg border border-cyan-300/25 bg-slate-900/55 p-3 pr-2">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <p className="text-sm text-cyan-100/90">SOP 分析与确认</p>
                      <button
                        type="button"
                        disabled={sending || loading || Boolean(actionBusy) || (!demoMode && !accountId)}
                        onClick={() => void analyzeSop()}
                        className="rounded border border-cyan-300/35 bg-cyan-300/10 px-2 py-1 text-xs text-cyan-100 transition hover:bg-cyan-300/20"
                      >
                        开始分析
                      </button>
                    </div>
                    {sopAnalysis.map((line, index) => <p key={index} className="mt-2 whitespace-pre-wrap text-sm">{line}</p>)}
                    <div className="mt-2 rounded border border-cyan-300/20 bg-slate-950/70 px-2 py-1 text-[11px] text-cyan-200/85">
                      分析窗口：近14天样本 {coachBrief?.context_snapshot?.kb_count ?? 0} 条，当前待确认建议 {pendingStrategy.length} 条。
                    </div>
                    <div className="mt-2 max-h-56 space-y-2 overflow-y-auto pr-1">
                      {pendingStrategy.slice(0, 4).map((item) => {
                        const proposal = item.proposed_changes?.[0];
                        const confidencePct = Math.round((item.confidence || 0) * 100);
                        return (
                          <div key={item.id} className="rounded border border-cyan-300/20 bg-slate-950/70 p-2.5">
                            <div className="flex items-center justify-between gap-2">
                              <p className="text-xs font-medium text-cyan-100">{clip(item.title, 42)}</p>
                              <span className="rounded border border-cyan-300/30 bg-cyan-300/10 px-1.5 py-0.5 text-[10px] text-cyan-200">
                                置信度 {confidencePct}%
                              </span>
                            </div>
                            <p className="mt-1 text-[11px] text-cyan-300/80">
                              建议来源：{strategySourceLabel(item.source)} · 影响环节：{affectedStageLabel(item.affected_agent)} · 更新时间：{fmtTime(item.updated_at)}
                            </p>
                            <p className="mt-1 text-[11px] leading-5 text-cyan-100/90">{clip(item.content, 220)}</p>

                            {proposal && canApplyStrategy(item) ? (
                              <div className="mt-2 rounded border border-cyan-300/20 bg-slate-900/70 p-2">
                                <p className="text-[11px] text-cyan-100/90">建议动作：{proposal.label || proposal.target}</p>
                                <p className="mt-0.5 text-[11px] text-cyan-300/80">原因：{proposal.reason || "-"}</p>
                                <p className="mt-0.5 text-[11px] text-cyan-300/80">改动前：{proposal.before_summary || "-"}</p>
                                <p className="mt-0.5 text-[11px] text-cyan-300/80">改动后：{proposal.after_summary || "-"}</p>
                                <p className="mt-0.5 text-[11px] text-cyan-300/80">预期效果：{proposal.effect || "-"}</p>
                              </div>
                            ) : null}

                            {item.tags?.length ? (
                              <p className="mt-1 text-[10px] text-cyan-300/70">标签：{item.tags.map((tag) => `#${String(tag).replace(/^#/, "")}`).join(" ")}</p>
                            ) : null}

                            <div className="mt-2 flex items-center gap-2">
                              {canApplyStrategy(item) ? <>
                              <button
                                type="button"
                                disabled={Boolean(actionBusy) || loading || (!demoMode && (!accountId || !item.proposed_changes?.length))}
                                onClick={() => void onAgree(item)}
                                className="rounded border border-emerald-300/40 bg-emerald-300/15 px-2 py-0.5 text-[11px] text-emerald-200 transition hover:bg-emerald-300/25"
                              >
                                同意并应用
                              </button>
                              </> : <span className="text-xs text-amber-200">等待本账号真实反馈</span>}
                              <button
                                type="button"
                                disabled={Boolean(actionBusy) || loading}
                                onClick={() => void onReject(item)}
                                className="rounded border border-rose-300/35 bg-rose-300/15 px-2 py-0.5 text-[11px] text-rose-200 transition hover:bg-rose-300/25"
                              >
                                暂不采用
                              </button>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                ) : null}
              </div>

              <div className="shrink-0 rounded-xl border border-cyan-300/25 bg-slate-950/60 p-4">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className="rounded border border-cyan-300/60 bg-cyan-300/20 px-2 py-1 text-xs text-cyan-100">
                      教练模式
                    </span>
                  </div>
                  <span className="text-[11px] text-cyan-300/75">总教练协同 4 个执行环节</span>
                </div>
                {coachBrief ? (
                  <div className="mb-2 rounded-lg border border-cyan-300/20 bg-slate-900/55 px-2 py-1.5 text-xs text-cyan-100/90">
                    昨日曝光 {coachBrief.yesterday_summary?.impressions ?? "暂无"} · 互动率 {coachBrief.yesterday_summary?.engagement_rate ?? "暂无"} · 审核通过率 {coachBrief.yesterday_summary?.review_pass_rate ?? "暂无"}
                  </div>
                ) : null}
                <div className="mb-2 max-h-24 overflow-y-auto pr-1">
                  {messages.slice(-4).map((msg, idx) => (
                    <p key={`${msg.role}-${idx}`} className={cn("mb-1 text-[13px]", msg.role === "assistant" ? "text-cyan-100" : "text-emerald-200")}>
                      <span className="mr-1 text-xs text-cyan-300/70">{msg.role === "assistant" ? "教练" : "你"}:</span>
                      {msg.text}
                    </p>
                  ))}
                </div>
                {coachActions.length > 0 ? (
                  <div className="mb-2 max-h-24 space-y-1 overflow-y-auto pr-1">
                    {coachActions.slice(0, 3).map((action) => (
                      <div key={action.id} className="flex items-center gap-2 rounded border border-cyan-300/25 bg-slate-900/55 px-2 py-1">
                        <div className="min-w-0 flex-1">
                          <p className="line-clamp-1 text-xs text-cyan-100">{action.title}</p>
                          <p className="line-clamp-1 text-[11px] text-cyan-200/70">{action.reason}</p>
                        </div>
                        <button
                          type="button"
                          disabled={coachActionRunningId === action.id}
                          onClick={() => void onConfirmCoach(action.id)}
                          className="rounded border border-cyan-300/40 bg-cyan-300/15 px-2 py-1 text-[11px] text-cyan-100 transition hover:bg-cyan-300/25 disabled:cursor-not-allowed disabled:opacity-60"
                        >
                          {coachActionRunningId === action.id ? "执行中" : "确认执行"}
                        </button>
                      </div>
                    ))}
                  </div>
                ) : null}
                <div className="flex items-center gap-2">
                  <input
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    onFocus={() => wakeUp()}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        void onSend();
                      }
                    }}
                    placeholder="告诉教练你的目标，例如：今天先冲曝光"
                    className="flex-1 rounded-lg border border-cyan-300/30 bg-slate-900/80 px-3 py-2 text-[13px] text-cyan-50 placeholder:text-cyan-200/45 focus:border-cyan-300/60 focus:outline-none"
                  />
                  <button
                    type="button"
                    onClick={() => void onSend()}
                    disabled={sending}
                    className="rounded-lg border border-cyan-300/40 bg-cyan-400/20 px-3 py-2 text-[13px] text-cyan-50 transition hover:bg-cyan-400/30 disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    {sending ? "发送中" : "发送"}
                  </button>
                </div>
              </div>
            </div>
          </article>

          <div
            className="workspace-side workspace-review order-3 grid min-w-0 gap-4 sm:grid-cols-2"
          >
            <article
              className={cn(
                "flex min-w-0 flex-col overflow-hidden rounded-2xl border bg-[linear-gradient(200deg,rgba(2,10,24,0.92),rgba(4,18,34,0.68))] p-4 backdrop-blur",
                focusPane === "publish" ? "border-cyan-300/70 shadow-[0_0_24px_rgba(34,211,238,0.28)]" : "border-cyan-300/20"
              )}
            >
                <>
                  <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                    <h2 className="text-[13px] font-semibold text-cyan-100">今日草稿任务</h2>
                    <span className="text-xs text-cyan-300/75">倒计时 {nextCountdown}</span>
                  </div>
                  <div className="mb-2 rounded-lg border border-cyan-300/25 bg-slate-900/60 px-2 py-1 text-xs text-cyan-100/80">
                    审核通过：{pendingTasks.filter((task) => task.status === "approved").length} | 待审核：{pendingTasks.filter((task) => task.status === "pending_review").length}
                  </div>
                  <div className="max-h-72 min-h-0 space-y-2 overflow-y-auto pr-1">
                    {publishQueue.length ? (
                      publishQueue.map((item) => (
                        <div key={item.id} className={cn("rounded-lg border bg-slate-900/55 p-2", selectedTaskId === item.id ? "border-cyan-300/55" : "border-cyan-400/20")}>
                          <button
                            type="button"
                            onClick={() => {
                              const t = pendingTasks.find((row) => row.id === item.id);
                              selectTaskForEditing(t || null);
                              wakeUp("publish");
                            }}
                            className="w-full min-w-0 break-words text-left text-sm leading-5 text-cyan-50 underline-offset-2 transition hover:text-cyan-100 hover:underline"
                          >
                            {item.title}
                          </button>
                          <p className="mt-1 text-xs text-cyan-200/70">
                            {item.status} / {item.stage} · {fmtTime(item.updatedAt)}
                          </p>
                          {demoMode ? (
                            <div className="mt-2 flex flex-wrap items-center gap-2">
                              <button
                                type="button"
                                onClick={() => {
                                  const t = pendingTasks.find((row) => row.id === item.id);
                                  selectTaskForEditing(t || null);
                                  wakeUp("publish");
                                }}
                                className="rounded border border-cyan-300/35 bg-cyan-300/10 px-2 py-1 text-xs text-cyan-100 transition hover:bg-cyan-300/20"
                              >
                                编辑
                              </button>
                              <button
                                type="button"
                                onClick={() => approveDemoDraft(item.id)}
                                className="rounded border border-emerald-300/35 bg-emerald-300/15 px-2 py-1 text-xs text-emerald-100 transition hover:bg-emerald-300/25"
                              >
                                审核通过
                              </button>
                              <button
                                type="button"
                                onClick={() => rejectDemoTask(item.id)}
                                className="rounded border border-rose-300/35 bg-rose-300/15 px-2 py-1 text-xs text-rose-100 transition hover:bg-rose-300/25"
                              >
                                驳回
                              </button>
                            </div>
                          ) : <div className="mt-2 flex flex-wrap gap-2 text-xs">
                            <button type="button" onClick={() => { selectTaskForEditing(pendingTasks.find((task) => task.id === item.id)); wakeUp("publish"); }} className="underline">编辑与审核</button>
                            <Link href={`/tasks/${item.id}`} className="underline">查看详情</Link>
                          </div>}
                        </div>
                      ))
                    ) : (
                      <p className="text-sm text-cyan-200/60">今日暂无待审核草稿</p>
                    )}
                  </div>
                </>
            </article>

            <article
              className={cn(
                "flex min-w-0 flex-col overflow-hidden rounded-2xl border bg-[linear-gradient(200deg,rgba(2,10,24,0.92),rgba(4,18,34,0.68))] p-4 backdrop-blur",
                focusPane === "sop" ? "border-cyan-300/70 shadow-[0_0_24px_rgba(34,211,238,0.28)]" : "border-cyan-300/20"
              )}
            >
                <>
                  <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                    <h2 className="text-[13px] font-semibold text-cyan-100">今日沉淀SOP</h2>
                    <button type="button" onClick={() => wakeUp("sop")} className="text-xs text-cyan-300 underline">{pendingStrategy.length} 条建议</button>
                  </div>
                  <div className="mb-2 rounded-lg border border-cyan-300/20 bg-slate-900/55 p-2">
                    <p className="text-[11px] uppercase tracking-[0.16em] text-cyan-300/75">SOP 当前版本（主屏/账号页同源）</p>
                    <p className="mt-1 text-xs text-cyan-100/90">
                      {sopVersion > 0 ? `v${sopVersion}` : "未生成版本"}
                      {sopUpdatedAt ? ` · ${fmtTime(sopUpdatedAt)}` : ""}
                    </p>
                    <p className="mt-1 text-[11px] text-cyan-200/70">
                      最近改动：{sopChangeSummary || "暂无变更字段记录"}
                    </p>
                    <p className="mt-1 text-[11px] text-cyan-200/70">
                      激活提示词：{activePromptSummary || "暂无激活提示词版本"}
                    </p>
                  </div>
                  <div className="mb-2 rounded-lg border border-cyan-300/20 bg-slate-900/55 p-2">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <p className="text-xs text-cyan-100/90">本地复盘备注（不改变任务审核状态）</p>
                      <select
                        value={publishDecision}
                        onChange={(e) => setPublishDecision(e.target.value as PublishDecision)}
                        className="rounded border border-cyan-300/35 bg-slate-950/80 px-2 py-1 text-xs text-cyan-100 focus:border-cyan-300/60 focus:outline-none"
                      >
                        <option value="pending">待确认</option>
                        <option value="approved">通过</option>
                        <option value="rejected">不通过</option>
                      </select>
                    </div>
                    <textarea
                      value={publishDecisionNote}
                      onChange={(e) => setPublishDecisionNote(e.target.value)}
                      placeholder="可编辑备注：例如 不通过原因、明天调整点..."
                      className="mt-2 min-h-[54px] w-full resize-y rounded border border-cyan-300/25 bg-slate-950/80 px-2 py-1 text-xs text-cyan-100 placeholder:text-cyan-300/45 focus:border-cyan-300/60 focus:outline-none"
                    />
                    <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
                      <button
                        type="button"
                        onClick={savePublishDecision}
                        className="rounded border border-cyan-300/35 bg-cyan-300/10 px-2 py-1 text-xs text-cyan-100 transition hover:bg-cyan-300/20"
                      >
                        保存判断
                      </button>
                      <span className="text-[11px] text-cyan-300/70">
                        {publishDecisionSavedAt ? `已保存 ${fmtTime(new Date(publishDecisionSavedAt).toISOString())}` : "未保存"}
                      </span>
                    </div>
                  </div>
                  <>
                    <div className="mb-2 rounded-lg border border-cyan-300/20 bg-slate-900/55 p-2">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <p className="text-xs text-cyan-100/90">SOP分析{demoMode ? "（演示）" : ""}</p>
                        <button
                          type="button"
                          disabled={sending || loading || Boolean(actionBusy) || (!demoMode && !accountId)}
                          onClick={() => void analyzeSop()}
                          className="rounded border border-cyan-300/35 bg-cyan-300/10 px-2 py-1 text-xs text-cyan-100 transition hover:bg-cyan-300/20"
                        >
                          开始分析
                        </button>
                      </div>
                      {demoSopTrace.length > 0 ? (
                        <div className="mt-2 max-h-20 space-y-1 overflow-y-auto rounded border border-cyan-300/20 bg-slate-950/70 p-2 text-[11px] text-cyan-100/85">
                          {demoSopTrace.slice(-4).map((line, idx) => (
                            <p key={`${idx}-${line}`}>{line}</p>
                          ))}
                        </div>
                      ) : null}
                    </div>
                  </>
                  <div className="min-h-0 space-y-2 overflow-y-auto pr-1">
                    {pendingStrategy.length ? (
                      pendingStrategy.slice(0, 5).map((item) => (
                        <div key={item.id} className="rounded-lg border border-cyan-400/20 bg-slate-900/55 p-2">
                          <p className="line-clamp-2 text-sm text-cyan-50">{clip(item.title, 36)}</p>
                          <p className="mt-1 line-clamp-2 text-xs text-cyan-200/70">{clip(item.content, 70)}</p>
                          <div className="mt-2 flex flex-wrap items-center gap-2">
                            {canApplyStrategy(item) ? <>
                            <button
                              type="button"
                              disabled={Boolean(actionBusy) || loading || (!demoMode && (!accountId || !item.proposed_changes?.length))}
                              onClick={() => void onAgree(item)}
                              className="rounded border border-emerald-300/40 bg-emerald-300/15 px-2 py-1 text-xs text-emerald-200 transition hover:bg-emerald-300/25"
                            >
                              同意
                            </button>
                            </> : <span className="text-xs text-amber-200">等待本账号真实反馈</span>}
                            <button
                              type="button"
                              disabled={Boolean(actionBusy) || loading}
                              onClick={() => void onReject(item)}
                              className="rounded border border-rose-300/35 bg-rose-300/15 px-2 py-1 text-xs text-rose-200 transition hover:bg-rose-300/25"
                            >
                              不同意
                            </button>
                            <span className="ml-auto text-[11px] text-cyan-300/70">{affectedStageLabel(item.affected_agent)}</span>
                          </div>
                        </div>
                      ))
                    ) : (
                      <p className="text-sm text-cyan-200/60">暂无待确认建议</p>
                    )}
                  </div>
                </>
            </article>
          </div>
        </section>
      </div>

      <style jsx global>{`
        .landing-workspace, .landing-workspace * { letter-spacing: 0; }
        .landing-workspace h2 { font-size: 15px; }
        .landing-workspace input, .landing-workspace textarea, .landing-workspace select { max-width: 100%; min-width: 0; }
        .landing-workspace .workspace-side { align-content: start; }
        .landing-workspace .workspace-side > article { min-width: 0; }
        .landing-workspace .workspace-side p { overflow-wrap: anywhere; }
        .landing-workspace .workspace-side button { flex-shrink: 0; }
        .landing-workspace .workspace-side button, .landing-workspace .workspace-side select { min-height: 32px; }
        .landing-workspace .workspace-source-list { grid-auto-rows: max-content; align-content: start; scrollbar-gutter: stable; scrollbar-width: thin; }
        @media (min-width: 1200px) {
          .landing-workspace .workspace-layout { grid-template-columns: minmax(240px, 1fr) minmax(460px, 2.2fr) minmax(280px, 1.1fr); }
          .landing-workspace .workspace-side { grid-template-columns: minmax(0, 1fr); }
          .landing-workspace .workspace-sources { order: 1; }
          .landing-workspace .workspace-detail { order: 2; }
          .landing-workspace .workspace-mobile-source { display: none; }
          .landing-workspace .workspace-source-list { max-height: clamp(220px, calc(50dvh - 200px), 420px); }
        }
        .character-core {
          background:
            radial-gradient(circle at 30% 30%, rgba(167, 243, 208, 0.75), transparent 45%),
            radial-gradient(circle at 65% 65%, rgba(34, 211, 238, 0.75), transparent 50%),
            radial-gradient(circle at 50% 50%, rgba(20, 184, 166, 0.7), rgba(6, 78, 89, 0.2));
          box-shadow:
            0 0 35px rgba(45, 212, 191, 0.65),
            0 0 120px rgba(34, 211, 238, 0.35);
          animation: pulseCore 6s ease-in-out infinite;
        }
        .character-wave {
          background: radial-gradient(circle, rgba(56, 189, 248, 0.2), rgba(15, 23, 42, 0));
          filter: blur(26px);
          animation: breatheWave 8s ease-in-out infinite;
        }
        .character-ring {
          border: 1px solid rgba(34, 211, 238, 0.45);
          animation: spinRing 14s linear infinite;
        }
        .character-ring-2 {
          border: 1px dashed rgba(94, 234, 212, 0.3);
          animation: spinRing 18s linear infinite reverse;
        }
        @keyframes pulseCore {
          0%, 100% { transform: translate(-50%, -50%) scale(0.95); }
          50% { transform: translate(-50%, -50%) scale(1.06); }
        }
        @keyframes breatheWave {
          0%, 100% { transform: translate(-50%, -50%) scale(0.9); opacity: 0.52; }
          50% { transform: translate(-50%, -50%) scale(1.12); opacity: 0.88; }
        }
        @keyframes spinRing {
          from { transform: translate(-50%, -50%) rotate(0deg); }
          to { transform: translate(-50%, -50%) rotate(360deg); }
        }
        @media (prefers-reduced-motion: reduce) {
          .character-core, .character-wave, .character-ring, .character-ring-2 { animation: none; }
        }
      `}</style>
    </div>
  );
}
