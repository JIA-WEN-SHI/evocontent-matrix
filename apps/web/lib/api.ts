import {
  CoachBrief,
  CoachChatResponse,
  CoachConfirmActionResponse,
  AccountCollectionPlan,
  AccountFeedbackPlan,
  AccountLoginBootstrap,
  AccountPromptVersion,
  AccountRuntimeContext,
  AccountLoopRunDailyResponse,
  AccountLoopStatusResponse,
  AccountOnboardingCommitResponse,
  AuditLog,
  ChannelAccount,
  ChannelAccountStrategyProfile,
  DailyOpsReport,
  DailyOpsStatus,
  Domain,
  McpReadonlyCollectIntelResponse,
  McpReadonlyCustomCallResponse,
  OpsOverview,
  RunLedgerItem,
  OpsAutoConfigResponse,
  MemoryItem,
  McpReadonlySearchResponse,
  McpReadonlyStatus,
  McpReadonlySyncMetricsResponse,
  OrchestratorControl,
  OrchestratorControlResponse,
  OrchestratorControlRollbackResponse,
  OrchestratorControlVersionsResponse,
  OrchestratorSelfUpgradeResponse,
  ChiefEvolutionResponse,
  CleanupDuplicateTasksResponse,
  PipelineTask,
  PublishHistoryImportItem,
  PublishHistoryImportResult,
  PublishIdentityImportItem,
  PublishIdentityImportResult,
  PublishFeedbackSummaryItem,
  PendingStrategyItem,
  RequiredConfirmation,
  StrategyVersion,
  TaskPlanPreview,
  FeatureChecklist,
  HotPostLlmAnalyzeResponse,
  IntelAnalysisResponse,
  RebuildXhsContentResponse,
  RuntimeContext,
  SopCurrentView,
  SopRollbackResponse,
  SopSnapshotListResponse,
  SubAgentRunRecord,
  SystemReadiness,
  Task,
  TaskFeedbackDetail,
  ToolExecutionResult,
  KbAiJobRecord,
  KbAssetRecord,
  KbCaseRecord,
  KbEntityTagRecord,
  KbIngestionLogRecord,
  KbOctopusImportResult,
  KbRpaCleanResultRecord,
  KbRpaRawPayloadRecord,
  KbRpaReplayResponse,
  KbRpaTaskRunRecord,
  KbRpaTaskTemplateRecord,
  KbReviewRecord,
  KbTagRecord,
  KbTopicRecommendResponse,
  KbTopicRecord,
  KbUserNeedRecord,
  ReviewActionResponse,
  AgentConfigView,
  ContentMethodView,
  ExecutionRouteStatusView,
  LoopExecutionView,
} from "@/lib/types";

export const DEFAULT_DOMAIN_SLUG = process.env.NEXT_PUBLIC_DEFAULT_DOMAIN_SLUG?.trim() || "japan_immigration";

import type { BrowserConnection, BrowserJob, BrowserMode, BrowserCapture } from "@/lib/browser-bridge";

export function getBrowserConnection(accountId: string): Promise<BrowserConnection> {
  return fetchJson(`/api/browser-bridge/accounts/${encodeURIComponent(accountId)}/status`);
}

export function createBrowserPairing(accountId: string, domainSlug: string, mode: BrowserMode = "background_text"): Promise<{ code: string; expires_in: number }> {
  return fetchJson(`/api/browser-bridge/accounts/${encodeURIComponent(accountId)}/pairing`, {
    method: "POST", body: JSON.stringify({ domain_slug: domainSlug, source_kind: "hotspot", mode })
  });
}

export function startBrowserCollection(accountId: string, domainSlug: string): Promise<BrowserJob> {
  return fetchJson(`/api/browser-bridge/accounts/${encodeURIComponent(accountId)}/jobs`, {
    method: "POST", body: JSON.stringify({ domain_slug: domainSlug, source_kind: "hotspot" })
  });
}

export function controlBrowserJob(accountId: string, jobId: string, action: "stop" | "resume"): Promise<BrowserJob> {
  return fetchJson(`/api/browser-bridge/accounts/${encodeURIComponent(accountId)}/jobs/${encodeURIComponent(jobId)}/${action}`, { method: "POST" });
}

export function disconnectBrowser(accountId: string): Promise<{ connected: boolean }> {
  return fetchJson(`/api/browser-bridge/accounts/${encodeURIComponent(accountId)}/disconnect`, { method: "POST" });
}

export function getBrowserCapture(accountId: string, itemId: string): Promise<BrowserCapture> {
  return fetchJson(`/api/browser-bridge/accounts/${encodeURIComponent(accountId)}/items/${encodeURIComponent(itemId)}/capture`);
}

export async function getBrowserMedia(accountId: string, itemId: string, mediaId: string, signal?: AbortSignal): Promise<Blob> {
  const controller = new AbortController();
  const cancel = () => controller.abort();
  if (signal?.aborted) cancel();
  signal?.addEventListener("abort",cancel,{once:true});
  const timer = setTimeout(cancel,20000);
  try {
    const response = await fetch(`${resolveApiBase()}/api/browser-bridge/accounts/${encodeURIComponent(accountId)}/items/${encodeURIComponent(itemId)}/media/${encodeURIComponent(mediaId)}`,{headers:DEFAULT_HEADERS,signal:controller.signal});
    if (!response.ok) throw new Error(parseApiErrorMessage(await response.text()));
    const blob = await response.blob();
    if (!blob.type.startsWith("image/") || blob.size > 10*1024*1024) throw new Error("图片类型或大小不符合要求");
    return blob;
  } finally { clearTimeout(timer); signal?.removeEventListener("abort",cancel); }
}

const DEFAULT_HEADERS = {
  "Content-Type": "application/json",
  "x-user-id": "demo-reviewer",
  "x-user-role": "admin"
};

function resolveApiBase(): string {
  const envBase = process.env.NEXT_PUBLIC_API_BASE_URL?.trim();
  const fallback = "http://127.0.0.1:8000";
  if (typeof window === "undefined") {
    return envBase || fallback;
  }

  const pageHost = window.location.hostname;
  if (!envBase) {
    if (pageHost === "127.0.0.1" || pageHost === "localhost") {
      return `http://${pageHost}:8000`;
    }
    return fallback;
  }

  try {
    const url = new URL(envBase);
    const isLoopbackPage = pageHost === "127.0.0.1" || pageHost === "localhost";
    const isLoopbackApi = url.hostname === "127.0.0.1" || url.hostname === "localhost";
    if (isLoopbackPage && isLoopbackApi && pageHost !== url.hostname) {
      url.hostname = pageHost;
      return url.toString().replace(/\/$/, "");
    }
    return envBase;
  } catch {
    return envBase || fallback;
  }
}

function clampInt(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, Math.floor(value)));
}

function parseApiErrorMessage(raw: string): string {
  const text = (raw || "").trim();
  if (!text) return "请求失败，请稍后重试。";
  try {
    const parsed = JSON.parse(text) as Record<string, unknown>;
    const error = parsed.error;
    if (error && typeof error === "object" && "message" in error && typeof error.message === "string") {
      return error.message.trim() || "请求失败，请稍后重试。";
    }
    const detail = parsed.detail;
    if (typeof detail === "string" && detail.trim()) return detail.trim();
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as Record<string, unknown>;
      const msg = String(first?.msg || "").trim();
      const loc = Array.isArray(first?.loc) ? (first.loc as unknown[]).map(String).join(".") : "";
      if (msg && loc) {
        return `${loc} 参数错误：${msg}`;
      }
      if (msg) return msg;
    }
  } catch {
    // ignore json parse errors
  }
  return text.length > 240 ? `${text.slice(0, 240)}...` : text;
}


async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const apiBase = resolveApiBase();
  const isRead = ["GET", "HEAD"].includes((init?.method || "GET").toUpperCase());
  const controller = new AbortController();
  const cancel = () => controller.abort();
  if (init?.signal?.aborted) cancel();
  init?.signal?.addEventListener("abort", cancel, { once: true });
  let timer: ReturnType<typeof setTimeout>;
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      reject(new Error(isRead ? "请求超时，请稍后重试。" : "请求超时，结果尚未确认，请先检查任务状态，勿重复提交。"));
      controller.abort();
    }, isRead ? 20_000 : 240_000);
  });
  try {
    const request = async (): Promise<T> => {
      let response: Response;
      try {
        response = await fetch(`${apiBase}${path}`, {
          ...init,
          signal: controller.signal,
          headers: {
            ...DEFAULT_HEADERS,
            ...(init?.headers ?? {})
          },
          cache: "no-store"
        });
      } catch {
        throw new Error(`无法连接到 API 服务（${apiBase}），请确认前端/API 已启动且地址一致。`);
      }
      if (!response.ok) {
        const raw = await response.text();
        throw new Error(parseApiErrorMessage(raw));
      }
      return (await response.json()) as T;
    };
    return await Promise.race([request(), deadline]);
  } finally {
    clearTimeout(timer!);
    init?.signal?.removeEventListener("abort", cancel);
  }
}


export async function getPendingTasks(channel?: string): Promise<Task[]> {
  const rows = await getPendingPipelineTasks({ channel });
  return rows.map((row) => ({
    id: row.id,
    domain_id: row.domain_id,
    channel: row.channel as Task["channel"],
    status: row.status,
    title: String(row.payload_jsonb?.title || "") || null,
    body: String(row.payload_jsonb?.body || "") || null,
    utm_code: String(row.publish_jsonb?.utm_code || "") || null,
    form_id: String(row.publish_jsonb?.form_id || "") || null,
    leads_count: Number(row.metrics_jsonb?.leads_count || 0),
    meta_jsonb: row.payload_jsonb ?? {},
    published_at: row.published_at
  }));
}

export type ContentWorkflowScope = {
  account_id: string;
  domain_slug: string;
};

export type ManualPublicationInput = ContentWorkflowScope & {
  published_url: string;
  published_at: string;
  confirmed: boolean;
  correction_reason?: string;
};

export type ObservationMetric = "views" | "likes" | "collects" | "comments_count" | "shares" | "followers_delta";

export type TaskObservationInput = ContentWorkflowScope & {
  values: Partial<Record<ObservationMetric, number | null>>;
  observed_at: string;
  provenance: string;
};

export type TaskObservationResult = PipelineTask & { operation_warning?: string };

export type CaptureKnowledgeResult = {
  status: string;
  linked: number;
  items: Array<{
    status: string;
    case_ids: string[];
    asset_ids: string[];
    linked_at: string;
    source_url: string;
    warnings: string[];
  }>;
  failures: Array<{ item_id: string; message: string }>;
};

export type CaptureCommentRetryResult = {
  status: string;
  capture: BrowserCapture;
  capture_status: string;
  comments_status: string;
  database_status: string;
  local_saved: boolean;
  warnings: string[];
};

export function registerManualPublication(
  taskId: string, input: ManualPublicationInput, correction = false
): Promise<PipelineTask> {
  return fetchJson(`/api/pipeline/tasks/${encodeURIComponent(taskId)}/manual-publication`, {
    method: correction ? "PATCH" : "POST", body: JSON.stringify(input)
  });
}

export function syncTaskFeedback(taskId: string, accountId: string, domainSlug: string): Promise<TaskObservationResult> {
  return fetchJson(`/api/pipeline/tasks/${encodeURIComponent(taskId)}/feedback/sync`, {
    method: "POST", body: JSON.stringify({ account_id: accountId, domain_slug: domainSlug })
  });
}

export function saveTaskObservation(taskId: string, input: TaskObservationInput): Promise<TaskObservationResult> {
  return fetchJson(`/api/pipeline/tasks/${encodeURIComponent(taskId)}/feedback/observations`, {
    method: "POST", body: JSON.stringify(input)
  });
}

export function reconcileCaptureKnowledge(
  accountId: string, domainSlug: string, itemIds?: string[]
): Promise<CaptureKnowledgeResult> {
  return fetchJson(`/api/ops/accounts/${encodeURIComponent(accountId)}/collection/reconcile-knowledge`, {
    method: "POST", body: JSON.stringify({ domain_slug: domainSlug, item_ids: itemIds })
  });
}

export function retryCaptureComments(accountId: string, itemId: string): Promise<CaptureCommentRetryResult> {
  return fetchJson(`/api/ops/accounts/${encodeURIComponent(accountId)}/browser/captures/${encodeURIComponent(itemId)}/retry-comments`, {
    method: "POST"
  });
}

export async function uploadDraftImage(taskId: string, accountId: string, file: File): Promise<PipelineTask> {
  if (!file.size) throw new Error("请选择非空图片文件");
  if (file.size > 10 * 1024 * 1024) throw new Error("每张图片不能超过10MiB");
  return fetchJson(`/api/pipeline/tasks/${encodeURIComponent(taskId)}/images?account_id=${encodeURIComponent(accountId)}`, {
    method: "POST", headers: { "Content-Type": file.type || "application/octet-stream" }, body: file
  });
}

export async function getDraftImage(taskId: string, accountId: string, imageId: string, signal?: AbortSignal): Promise<Blob> {
  const controller = new AbortController();
  const cancel = () => controller.abort();
  if (signal?.aborted) cancel();
  signal?.addEventListener("abort", cancel, { once: true });
  let timer: ReturnType<typeof setTimeout>;
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      reject(new Error("请求超时，请稍后重试。"));
      cancel();
    }, 20_000);
  });
  try {
    const read = async (): Promise<Blob> => {
      const response = await fetch(`${resolveApiBase()}/api/pipeline/tasks/${encodeURIComponent(taskId)}/images/${encodeURIComponent(imageId)}?account_id=${encodeURIComponent(accountId)}`, {
        headers: DEFAULT_HEADERS, signal: controller.signal, cache: "no-store"
      });
      controller.signal.throwIfAborted();
      if (!response.ok) throw new Error(parseApiErrorMessage(await response.text()));
      const blob = await response.blob();
      controller.signal.throwIfAborted();
      if (!blob.type.startsWith("image/") || !blob.size || blob.size > 10 * 1024 * 1024) {
        throw new Error("图片类型或大小不符合要求");
      }
      return blob;
    };
    return await Promise.race([read(), deadline]);
  } finally {
    clearTimeout(timer!);
    signal?.removeEventListener("abort", cancel);
  }
}

export function detachDraftImage(taskId: string, accountId: string, imageId: string): Promise<PipelineTask> {
  return fetchJson(`/api/pipeline/tasks/${encodeURIComponent(taskId)}/images/${encodeURIComponent(imageId)}?account_id=${encodeURIComponent(accountId)}`, {
    method: "DELETE"
  });
}

export async function getPipelineTasks(input?: {
  status?: string;
  channel?: string;
  domainSlug?: string;
  accountId?: string;
  limit?: number;
}): Promise<PipelineTask[]> {
  const query = new URLSearchParams();
  if (input?.status) query.set("status", input.status);
  if (input?.channel) query.set("channel", input.channel);
  if (input?.domainSlug) query.set("domain_slug", input.domainSlug);
  if (input?.accountId) query.set("account_id", input.accountId);
  query.set("limit", String(clampInt(input?.limit ?? 100, 1, 200)));
  const data = await fetchJson<{ items: PipelineTask[] }>(`/api/pipeline/tasks?${query.toString()}`);
  return data.items;
}

export async function getPendingPipelineTasks(input?: {
  channel?: string;
  accountId?: string;
}): Promise<PipelineTask[]> {
  return getPipelineTasks({ status: "pending_review", channel: input?.channel, accountId: input?.accountId, limit: 100 });
}

export async function getPipelineTask(id: string): Promise<PipelineTask> {
  return fetchJson<PipelineTask>(`/api/pipeline/tasks/${id}`);
}

export async function updatePipelineTask(id: string, input: { title: string; body: string }): Promise<PipelineTask> {
  return fetchJson<PipelineTask>(`/api/pipeline/tasks/${id}`, {
    method: "PATCH",
    body: JSON.stringify(input)
  });
}

export async function createPipelineTask(input: {
  domain_slug: string;
  channel?: string;
  content_type?: string;
  account_id?: string;
  intent_jsonb?: Record<string, unknown>;
  payload_jsonb?: Record<string, unknown>;
  scheduled_at?: string;
}): Promise<PipelineTask> {
  return fetchJson<PipelineTask>("/api/pipeline/tasks", {
    method: "POST",
    body: JSON.stringify(input)
  });
}

export async function approvePipelineTask(
  id: string,
  input?: {
    title?: string;
    body?: string;
  }
): Promise<PipelineTask> {
  return fetchJson<PipelineTask>(`/api/pipeline/tasks/${id}/approve`, {
    method: "POST",
    body: input ? JSON.stringify(input) : undefined
  });
}

export async function rejectPipelineTask(id: string, reason: string): Promise<PipelineTask> {
  return fetchJson<PipelineTask>(`/api/pipeline/tasks/${id}/reject`, {
    method: "POST",
    body: JSON.stringify({ reason })
  });
}

export async function runPipelineTask(id: string): Promise<void> {
  await fetchJson(`/api/pipeline/tasks/${id}/run`, { method: "POST" });
}

export async function getTasks(): Promise<Task[]> {
  const rows = await getPipelineTasks({ limit: 100 });
  return rows.map((row) => ({
    id: row.id,
    domain_id: row.domain_id,
    channel: row.channel as Task["channel"],
    status: row.status,
    title: String(row.payload_jsonb?.title || "") || null,
    body: String(row.payload_jsonb?.body || "") || null,
    utm_code: String(row.publish_jsonb?.utm_code || "") || null,
    form_id: String(row.publish_jsonb?.form_id || "") || null,
    leads_count: Number(row.metrics_jsonb?.leads_count || 0),
    meta_jsonb: row.payload_jsonb ?? {},
    published_at: row.published_at
  }));
}

export async function getTask(id: string): Promise<Task> {
  const row = await getPipelineTask(id);
  return {
    id: row.id,
    domain_id: row.domain_id,
    channel: row.channel as Task["channel"],
    status: row.status,
    title: String(row.payload_jsonb?.title || "") || null,
    body: String(row.payload_jsonb?.body || "") || null,
    utm_code: String(row.publish_jsonb?.utm_code || "") || null,
    form_id: String(row.publish_jsonb?.form_id || "") || null,
    leads_count: Number(row.metrics_jsonb?.leads_count || 0),
    meta_jsonb: row.payload_jsonb ?? {},
    published_at: row.published_at
  };
}

export async function getTaskFeedback(id: string): Promise<TaskFeedbackDetail> {
  return fetchJson<TaskFeedbackDetail>(`/api/pipeline/tasks/${id}/feedback`);
}

export async function getTaskAudits(id: string): Promise<AuditLog[]> {
  const data = await fetchJson<{ items: AuditLog[] }>(`/api/pipeline/tasks/${id}/audits`);
  return data.items;
}

export async function approveTask(
  id: string,
  input?: {
    title?: string;
    body?: string;
  }
): Promise<void> {
  await approvePipelineTask(id, input);
}

export async function rejectTask(id: string, reason: string): Promise<void> {
  await rejectPipelineTask(id, reason);
}

export async function runTask(id: string): Promise<void> {
  await runPipelineTask(id);
}

export async function getStrategyVersions(domainSlug = DEFAULT_DOMAIN_SLUG): Promise<StrategyVersion[]> {
  const data = await fetchJson<{ items: StrategyVersion[] }>(
    `/api/domains/${domainSlug}/strategy/versions`
  );
  return data.items;
}

export async function getDomains(): Promise<Domain[]> {
  const data = await fetchJson<{ items: Domain[] }>("/api/domains");
  return data.items;
}

export async function getDomain(domainSlug = DEFAULT_DOMAIN_SLUG): Promise<Domain> {
  return fetchJson<Domain>(`/api/domains/${domainSlug}`);
}

export async function createStrategyVersion(input: {
  domainSlug: string;
  reason: string;
  prompt_jsonb: Record<string, unknown>;
}): Promise<void> {
  await fetchJson(`/api/domains/${input.domainSlug}/strategy/versions`, {
    method: "POST",
    body: JSON.stringify({ reason: input.reason, prompt_jsonb: input.prompt_jsonb })
  });
}

export async function getAuditLogs(): Promise<AuditLog[]> {
  const data = await fetchJson<{ items: AuditLog[] }>("/api/audit-logs?limit=100");
  return data.items;
}

export async function getAuditLogsByTarget(input: {
  targetType: string;
  targetId?: string;
  limit?: number;
}): Promise<AuditLog[]> {
  const query = new URLSearchParams({
    target_type: input.targetType,
    limit: String(clampInt(input.limit ?? 100, 1, 200))
  });
  if (input.targetId) query.set("target_id", input.targetId);
  const data = await fetchJson<{ items: AuditLog[] }>(`/api/audit-logs?${query.toString()}`);
  return data.items;
}

export async function triggerReflection(domainSlug = DEFAULT_DOMAIN_SLUG): Promise<{
  status: string;
  [key: string]: unknown;
}> {
  return fetchJson(`/api/scheduler/reflect?domain_slug=${encodeURIComponent(domainSlug)}`, {
    method: "POST"
  });
}

export async function triggerDailyRun(domainSlug = DEFAULT_DOMAIN_SLUG): Promise<{
  status: string;
  [key: string]: unknown;
}> {
  return fetchJson(`/api/scheduler/daily-run?domain_slug=${encodeURIComponent(domainSlug)}`, {
    method: "POST"
  });
}

export async function triggerDailyViewpointRun(domainSlug = DEFAULT_DOMAIN_SLUG): Promise<{
  status: string;
  [key: string]: unknown;
}> {
  return fetchJson(`/api/scheduler/daily-viewpoint-run?domain_slug=${encodeURIComponent(domainSlug)}`, {
    method: "POST"
  });
}

export async function getDailyOpsStatus(
  domainSlug = DEFAULT_DOMAIN_SLUG,
  flow: "full" | "viewpoint" = "full"
): Promise<DailyOpsStatus> {
  return fetchJson(
    `/api/scheduler/daily-status?domain_slug=${encodeURIComponent(domainSlug)}&flow=${encodeURIComponent(flow)}`
  );
}

export async function getDailyOpsReports(
  domainSlug = DEFAULT_DOMAIN_SLUG,
  limit = 10,
  flow: "full" | "viewpoint" = "full"
): Promise<DailyOpsReport[]> {
  const safeLimit = clampInt(limit, 1, 200);
  const query = new URLSearchParams({
    domain_slug: domainSlug,
    limit: String(safeLimit),
    flow
  });
  const data = await fetchJson<{ items: DailyOpsReport[] }>(`/api/scheduler/daily-reports?${query.toString()}`);
  return data.items;
}

export async function getSystemReadiness(): Promise<SystemReadiness> {
  return fetchJson<SystemReadiness>("/api/system/readiness");
}

export async function getFeatureChecklist(): Promise<FeatureChecklist> {
  return fetchJson<FeatureChecklist>("/api/system/feature-checklist");
}

export async function getOpsOverview(
  domainSlug = DEFAULT_DOMAIN_SLUG,
  limit = 20,
  accountId?: string
): Promise<OpsOverview> {
  const safeLimit = clampInt(limit, 5, 100);
  const query = new URLSearchParams({
    domain_slug: domainSlug,
    limit: String(safeLimit)
  });
  if (accountId) query.set("account_id", accountId);
  return fetchJson<OpsOverview>(`/api/ops/overview?${query.toString()}`);
}

export async function getRuntimeContext(domainSlug = DEFAULT_DOMAIN_SLUG): Promise<RuntimeContext> {
  const query = new URLSearchParams({
    domain_slug: domainSlug
  });
  return fetchJson<RuntimeContext>(`/api/ops/runtime-context?${query.toString()}`);
}

export async function getRunLedger(
  domainSlug = DEFAULT_DOMAIN_SLUG,
  limit = 20,
  accountId?: string
): Promise<RunLedgerItem[]> {
  const query = new URLSearchParams({
    domain_slug: domainSlug,
    limit: String(clampInt(limit, 1, 100))
  });
  if (accountId) query.set("account_id", accountId);
  const data = await fetchJson<{ status: string; items: RunLedgerItem[] }>(`/api/ops/run-ledger?${query.toString()}`);
  return data.items || [];
}

export async function getMemoryItems(input?: {
  domainSlug?: string;
  status?: "active" | "pending" | "deprecated";
  accountId?: string;
  includeGlobal?: boolean;
  limit?: number;
}): Promise<MemoryItem[]> {
  const query = new URLSearchParams({
    domain_slug: input?.domainSlug || DEFAULT_DOMAIN_SLUG,
    limit: String(clampInt(input?.limit ?? 60, 1, 200)),
  });
  if (input?.status) query.set("status", input.status);
  if (input?.accountId) query.set("account_id", input.accountId);
  if (typeof input?.includeGlobal === "boolean") query.set("include_global", input.includeGlobal ? "true" : "false");
  const data = await fetchJson<{ status: string; items: MemoryItem[] }>(`/api/ops/memory-items?${query.toString()}`);
  return data.items || [];
}

export async function updateMemoryItemStatus(
  itemId: string,
  status: "active" | "pending" | "deprecated"
): Promise<MemoryItem> {
  const data = await fetchJson<{ status: string; item: MemoryItem }>(`/api/ops/memory-items/${itemId}`, {
    method: "PATCH",
    body: JSON.stringify({ status }),
  });
  return data.item;
}

export async function getPendingStrategyItems(input?: {
  domainSlug?: string;
  accountId?: string;
  limit?: number;
}): Promise<PendingStrategyItem[]> {
  const query = new URLSearchParams({
    domain_slug: input?.domainSlug || DEFAULT_DOMAIN_SLUG,
    limit: String(clampInt(input?.limit ?? 20, 1, 100)),
  });
  if (input?.accountId) query.set("account_id", input.accountId);
  const data = await fetchJson<{ status: string; items: PendingStrategyItem[] }>(`/api/ops/pending-strategy-items?${query.toString()}`);
  return data.items || [];
}

export async function applyPendingStrategyItemChange(
  itemId: string,
  input: {
    target: "collection_plan" | "feedback_plan" | "publish_preferences" | string;
    accountId?: string;
    domainSlug?: string;
    reason?: string;
    proposalFingerprint?: string;
  }
): Promise<{
  status: string;
  item_id: string;
  account_id: string;
  account_name: string;
  target: string;
  reason: string;
  before_summary: string;
  after_summary: string;
  memory_status: string;
}> {
  return fetchJson(`/api/ops/pending-strategy-items/${itemId}/apply`, {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input.domainSlug || DEFAULT_DOMAIN_SLUG,
      account_id: input.accountId || "",
      target: input.target,
      proposal_fingerprint: input.proposalFingerprint || "",
      reason: input.reason || "从前端确认并应用待确认建议",
    }),
  });
}

export function collectionResultNotice(result: {status:string; message?:string; collected?:number; inserted?:number; warnings?:string[]}): string {
  if (!["ok", "success", "completed", "partial"].includes(result.status)) throw new Error("采集未完成");
  const message = result.message || `采集完成：获取 ${result.collected || 0} 条，新增 ${result.inserted || 0} 条。`;
  return [message, ...(result.warnings || [])].join(" ");
}

export async function runAccountCollection(input: {
  account_id: string;
  domain_slug?: string;
  source_kind?: "hotspot" | "viewpoint";
}): Promise<{
  status: string;
  message?: string;
  warnings?: string[];
  local_saved?: number;
  images_saved?: number;
  domain_slug: string;
  account_id: string;
  account_name?: string;
  source_kind: string;
  collected: number;
  inserted: number;
  collection_plan?: Record<string, unknown>;
  preview?: Array<Record<string, unknown>>;
}> {
  return fetchJson(`/api/ops/accounts/${input.account_id}/collect`, {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input.domain_slug || DEFAULT_DOMAIN_SLUG,
      source_kind: input.source_kind || "hotspot",
    }),
  });
}

export async function runPlaywrightTool(input: {
  account_id: string;
  tool_name: string;
  params?: Record<string, unknown>;
}): Promise<ToolExecutionResult> {
  return fetchJson<ToolExecutionResult>("/api/ops/playwright-tools/run", {
    method: "POST",
    body: JSON.stringify({
      account_id: input.account_id,
      tool_name: input.tool_name,
      params: input.params ?? {},
    }),
  });
}

export async function reconcilePublishFeedback(input?: {
  domainSlug?: string;
  accountId?: string;
  limit?: number;
}): Promise<Record<string, unknown>> {
  return fetchJson<Record<string, unknown>>("/api/ops/publish-feedback/reconcile", {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input?.domainSlug || DEFAULT_DOMAIN_SLUG,
      account_id: input?.accountId || "",
      limit: clampInt(input?.limit ?? 20, 1, 50),
    }),
  });
}

export async function importPublishFeedbackIdentities(input: {
  domainSlug?: string;
  accountId: string;
  items: PublishIdentityImportItem[];
  limitRecent?: number;
  triggerReconcile?: boolean;
}): Promise<PublishIdentityImportResult> {
  return fetchJson<PublishIdentityImportResult>("/api/ops/publish-feedback/import-identities", {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input.domainSlug || DEFAULT_DOMAIN_SLUG,
      account_id: input.accountId,
      items: input.items || [],
      limit_recent: clampInt(input.limitRecent ?? 60, 1, 300),
      trigger_reconcile: input.triggerReconcile !== false,
    }),
  });
}

export async function importPublishFeedbackHistory(input: {
  domainSlug?: string;
  accountId: string;
  items: PublishHistoryImportItem[];
  triggerReconcile?: boolean;
  createTaskIfMissing?: boolean;
  writePendingMemory?: boolean;
  writeGlobalMemory?: boolean;
}): Promise<PublishHistoryImportResult> {
  return fetchJson<PublishHistoryImportResult>("/api/ops/publish-feedback/import-history", {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input.domainSlug || DEFAULT_DOMAIN_SLUG,
      account_id: input.accountId,
      items: input.items || [],
      trigger_reconcile: input.triggerReconcile !== false,
      create_task_if_missing: input.createTaskIfMissing !== false,
      write_pending_memory: input.writePendingMemory !== false,
      write_global_memory: input.writeGlobalMemory === true,
    }),
  });
}

export async function getPublishFeedbackSummary(input?: {
  domainSlug?: string;
  accountId?: string;
  limit?: number;
}): Promise<PublishFeedbackSummaryItem[]> {
  const query = new URLSearchParams();
  query.set("domain_slug", input?.domainSlug || DEFAULT_DOMAIN_SLUG);
  if (input?.accountId) query.set("account_id", input.accountId);
  query.set("limit", String(clampInt(input?.limit ?? 20, 1, 100)));
  const data = await fetchJson<{ status: string; items: PublishFeedbackSummaryItem[] }>(
    `/api/ops/publish-feedback/summary?${query.toString()}`
  );
  return data.items;
}

export async function submitOpsAnalysisFeedback(input: {
  pipeline_task_id: string;
  judgement: string;
  notes?: string;
  confidence?: number;
  decision?: string;
}): Promise<{
  status: string;
  [key: string]: unknown;
}> {
  return fetchJson("/api/ops/analysis-feedback", {
    method: "POST",
    body: JSON.stringify(input)
  });
}

export async function getChannelAccounts(input?: {
  channel?: string;
  limit?: number;
}): Promise<ChannelAccount[]> {
  const query = new URLSearchParams();
  if (input?.channel) query.set("channel", input.channel);
  query.set("limit", String(clampInt(input?.limit ?? 200, 1, 200)));
  const data = await fetchJson<{ items: ChannelAccount[] }>(`/api/accounts?${query.toString()}`);
  return data.items;
}

export async function createChannelAccount(input: {
  channel: string;
  account_name: string;
  account_handle?: string;
  login_mode: "storage_state" | "user_data_dir" | "cookies_json" | "credential";
  storage_state_path?: string;
  user_data_dir?: string;
  cookies_json?: string;
  login_username?: string;
  login_password?: string;
  publish_selector?: string;
  is_active?: boolean;
  tags?: string[];
  config_jsonb?: Record<string, unknown>;
  notes?: string;
}): Promise<ChannelAccount> {
  return fetchJson<ChannelAccount>("/api/accounts", {
    method: "POST",
    body: JSON.stringify(input)
  });
}

export async function updateChannelAccount(
  id: string,
  input: {
    account_name?: string;
    account_handle?: string;
    login_mode?: "storage_state" | "user_data_dir" | "cookies_json" | "credential";
    storage_state_path?: string;
    user_data_dir?: string;
    cookies_json?: string;
    login_username?: string;
    login_password?: string;
    publish_selector?: string;
    is_active?: boolean;
    tags?: string[];
    config_jsonb?: Record<string, unknown>;
    notes?: string;
  }
): Promise<ChannelAccount> {
  return fetchJson<ChannelAccount>(`/api/accounts/${id}`, {
    method: "PATCH",
    body: JSON.stringify(input)
  });
}

export async function deleteChannelAccount(id: string): Promise<void> {
  await fetchJson(`/api/accounts/${id}`, { method: "DELETE" });
}

export async function verifyChannelAccountLogin(id: string): Promise<{
  status: string;
  check: Record<string, unknown>;
  account: ChannelAccount;
}> {
  return fetchJson(`/api/accounts/${id}/verify-login`, {
    method: "POST"
  });
}

export async function getChannelAccountStrategy(id: string): Promise<{
  status: string;
  account_id: string;
  account_name: string;
  strategy_profile: ChannelAccountStrategyProfile;
  collection_plan?: AccountCollectionPlan;
  feedback_plan?: AccountFeedbackPlan;
}> {
  return fetchJson(`/api/accounts/${id}/strategy`);
}

export async function updateChannelAccountStrategy(
  id: string,
  input: {
    strategy_profile: Record<string, unknown>;
    feedback_plan?: Record<string, unknown>;
    reason?: string;
  }
): Promise<{
  status: string;
  account: ChannelAccount;
  strategy_profile: ChannelAccountStrategyProfile;
  feedback_plan?: AccountFeedbackPlan;
}> {
  return fetchJson(`/api/accounts/${id}/strategy`, {
    method: "PUT",
    body: JSON.stringify({
      strategy_profile: input.strategy_profile,
      feedback_plan: input.feedback_plan ?? {},
      reason: input.reason ?? "更新账号策略"
    })
  });
}

export async function getAccountRuntime(id: string): Promise<AccountRuntimeContext> {
  return fetchJson<AccountRuntimeContext>(`/api/accounts/${id}/runtime`);
}

export async function updateChannelAccountCollectionPlan(
  id: string,
  input: {
    collection_plan: AccountCollectionPlan | Record<string, unknown>;
    reason?: string;
  }
): Promise<{
  status: string;
  account: ChannelAccount;
  collection_plan: AccountCollectionPlan;
}> {
  return fetchJson(`/api/accounts/${id}/collection-plan`, {
    method: "PUT",
    body: JSON.stringify({
      collection_plan: input.collection_plan,
      reason: input.reason ?? "更新采集计划",
    }),
  });
}

export async function commitAccountOnboarding(
  id: string,
  input: {
    domain_slug?: string;
    primary_goal: string;
    persona_name: string;
    ip_positioning: string;
    tone_style?: string;
    cta_style?: string;
    audience?: string[];
    pain_points?: string[];
    content_pillars?: string[];
    forbidden_claims?: string[];
    focus_keywords?: string[];
    hotspot_queries?: string[];
    viewpoint_queries?: string[];
    publish_constraints?: string[];
    posts_per_day?: number;
    publish_time_slots?: string[];
    min_case_per_day?: number;
    min_asset_per_day?: number;
    checkpoints_hours?: number[];
    reason?: string;
    lock_onboarding?: boolean;
  }
): Promise<AccountOnboardingCommitResponse> {
  return fetchJson<AccountOnboardingCommitResponse>(`/api/accounts/${id}/onboarding/commit`, {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input.domain_slug ?? DEFAULT_DOMAIN_SLUG,
      primary_goal: input.primary_goal,
      persona_name: input.persona_name,
      ip_positioning: input.ip_positioning,
      tone_style: input.tone_style ?? "",
      cta_style: input.cta_style ?? "",
      audience: (input.audience || []).slice(0, 20),
      pain_points: (input.pain_points || []).slice(0, 20),
      content_pillars: (input.content_pillars || []).slice(0, 20),
      forbidden_claims: (input.forbidden_claims || []).slice(0, 30),
      focus_keywords: (input.focus_keywords || []).slice(0, 40),
      hotspot_queries: (input.hotspot_queries || []).slice(0, 40),
      viewpoint_queries: (input.viewpoint_queries || []).slice(0, 40),
      publish_constraints: (input.publish_constraints || []).slice(0, 30),
      posts_per_day: clampInt(input.posts_per_day ?? 1, 1, 10),
      publish_time_slots: (input.publish_time_slots || ["10:00", "20:00"]).slice(0, 8),
      min_case_per_day: clampInt(input.min_case_per_day ?? 3, 0, 300),
      min_asset_per_day: clampInt(input.min_asset_per_day ?? 3, 0, 300),
      checkpoints_hours: (input.checkpoints_hours || [1, 3, 24]).slice(0, 8),
      reason: input.reason ?? "commit single account onboarding",
      lock_onboarding: input.lock_onboarding !== false,
    }),
  });
}

export async function getAccountLoopStatus(
  id: string,
  input?: { domainSlug?: string }
): Promise<AccountLoopStatusResponse> {
  const query = new URLSearchParams({
    domain_slug: input?.domainSlug ?? DEFAULT_DOMAIN_SLUG,
  });
  return fetchJson<AccountLoopStatusResponse>(`/api/accounts/${id}/loop/status?${query.toString()}`);
}

export async function getAccountSopCurrent(
  id: string,
  input?: { domainSlug?: string }
): Promise<SopCurrentView> {
  const query = new URLSearchParams({
    domain_slug: input?.domainSlug ?? DEFAULT_DOMAIN_SLUG,
  });
  return fetchJson<SopCurrentView>(`/api/accounts/${id}/sop/current?${query.toString()}`);
}

export async function getAccountSopSnapshots(
  id: string,
  input?: { domainSlug?: string; limit?: number }
): Promise<SopSnapshotListResponse> {
  const query = new URLSearchParams({
    domain_slug: input?.domainSlug ?? DEFAULT_DOMAIN_SLUG,
    limit: String(clampInt(input?.limit ?? 10, 1, 50)),
  });
  return fetchJson<SopSnapshotListResponse>(`/api/accounts/${id}/sop/snapshots?${query.toString()}`);
}

export async function getAccountAgentConfig(
  id: string,
  input?: { domainSlug?: string }
): Promise<AgentConfigView> {
  const query = new URLSearchParams({
    domain_slug: input?.domainSlug ?? DEFAULT_DOMAIN_SLUG,
  });
  return fetchJson<AgentConfigView>(`/api/accounts/${id}/agent-config?${query.toString()}`);
}

export async function getAccountLoopExecutionView(
  id: string,
  input?: { domainSlug?: string }
): Promise<LoopExecutionView> {
  const query = new URLSearchParams({
    domain_slug: input?.domainSlug ?? DEFAULT_DOMAIN_SLUG,
  });
  return fetchJson<LoopExecutionView>(`/api/accounts/${id}/loop-execution-view?${query.toString()}`);
}

export async function getAccountContentMethodView(
  id: string,
  input?: { domainSlug?: string }
): Promise<ContentMethodView> {
  const query = new URLSearchParams({
    domain_slug: input?.domainSlug ?? DEFAULT_DOMAIN_SLUG,
  });
  return fetchJson<ContentMethodView>(`/api/accounts/${id}/content-method-view?${query.toString()}`);
}

export async function getExecutionRouteStatus(input?: {
  accountId?: string;
  domainSlug?: string;
}): Promise<ExecutionRouteStatusView> {
  const query = new URLSearchParams({
    account_id: input?.accountId ?? "",
    domain_slug: input?.domainSlug ?? DEFAULT_DOMAIN_SLUG,
  });
  return fetchJson<ExecutionRouteStatusView>(`/api/execution/route-status?${query.toString()}`);
}

export async function rollbackAccountSopSnapshot(
  id: string,
  input: { domainSlug?: string; version: number; reason?: string }
): Promise<SopRollbackResponse> {
  return fetchJson<SopRollbackResponse>(`/api/accounts/${id}/sop/rollback`, {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input.domainSlug ?? DEFAULT_DOMAIN_SLUG,
      version: Math.max(1, Math.floor(input.version)),
      reason: (input.reason || "manual sop rollback").slice(0, 500),
    }),
  });
}

export async function getSubagentRuns(input?: {
  domainSlug?: string;
  accountId?: string;
  status?: string;
  subagent?: string;
  limit?: number;
}): Promise<SubAgentRunRecord[]> {
  const query = new URLSearchParams({
    domain_slug: input?.domainSlug ?? DEFAULT_DOMAIN_SLUG,
    account_id: input?.accountId ?? "",
    status_filter: input?.status ?? "",
    subagent: input?.subagent ?? "",
    limit: String(clampInt(input?.limit ?? 100, 1, 300)),
  });
  const data = await fetchJson<{ status: string; items: SubAgentRunRecord[] }>(`/api/ops/subagent-runs?${query.toString()}`);
  return data.items || [];
}

export async function runAccountLoopDaily(
  id: string,
  input?: {
    domain_slug?: string;
    flow?: "full" | "viewpoint" | string;
    run_key?: string;
    force?: boolean;
  }
): Promise<AccountLoopRunDailyResponse> {
  return fetchJson<AccountLoopRunDailyResponse>(`/api/accounts/${id}/loop/run-daily`, {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input?.domain_slug ?? DEFAULT_DOMAIN_SLUG,
      flow: input?.flow ?? "full",
      run_key: input?.run_key ?? "",
      force: input?.force === true,
    }),
  });
}

export async function runAccountReviewAction(
  id: string,
  input: {
    domain_slug?: string;
    pipeline_task_id: string;
    action: "approve" | "reject";
    reason?: string;
    title?: string;
    body?: string;
    write_memory?: boolean;
    write_review?: boolean;
  }
): Promise<ReviewActionResponse> {
  return fetchJson<ReviewActionResponse>(`/api/accounts/${id}/loop/review-action`, {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input.domain_slug ?? DEFAULT_DOMAIN_SLUG,
      pipeline_task_id: input.pipeline_task_id,
      action: input.action,
      reason: input.reason ?? "",
      title: input.title ?? undefined,
      body: input.body ?? undefined,
      write_memory: input.write_memory !== false,
      write_review: input.write_review !== false,
    }),
  });
}

export async function resetChannelAccountAuth(id: string): Promise<{
  status: string;
  account_id: string;
  cleared_fields: string[];
  next_step_hint: string;
}> {
  return fetchJson(`/api/accounts/${id}/auth/reset`, { method: "POST" });
}

export async function launchChannelAccountLogin(
  id: string,
  input?: { wait_seconds?: number; url?: string }
): Promise<AccountLoginBootstrap> {
  return fetchJson<AccountLoginBootstrap>(`/api/accounts/${id}/login/bootstrap`, {
    method: "POST",
    body: JSON.stringify({
      wait_seconds: input?.wait_seconds ?? 90,
      url: input?.url ?? undefined,
    }),
  });
}

export async function getAccountPromptVersions(
  id: string,
  input?: { domainSlug?: string; agentName?: string; limit?: number }
): Promise<AccountPromptVersion[]> {
  const query = new URLSearchParams();
  if (input?.domainSlug) query.set("domain_slug", input.domainSlug);
  if (input?.agentName) query.set("agent_name", input.agentName);
  query.set("limit", String(clampInt(input?.limit ?? 50, 1, 200)));
  const data = await fetchJson<{ items: AccountPromptVersion[] }>(`/api/accounts/${id}/prompt-versions?${query.toString()}`);
  return data.items;
}

export async function createAccountPromptVersion(
  id: string,
  input: {
    domain_slug?: string;
    agent_name: string;
    version: string;
    system_prompt: string;
    status?: string;
    source?: string;
    reason?: string;
    evidence_jsonb?: Record<string, unknown>;
  }
): Promise<AccountPromptVersion> {
  return fetchJson<AccountPromptVersion>(`/api/accounts/${id}/prompt-versions`, {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input.domain_slug ?? DEFAULT_DOMAIN_SLUG,
      agent_name: input.agent_name,
      version: input.version,
      system_prompt: input.system_prompt,
      status: input.status ?? "draft",
      source: input.source ?? "manual",
      reason: input.reason ?? "新增提示词版本",
      evidence_jsonb: input.evidence_jsonb ?? {},
    }),
  });
}

export async function rollbackAccountPromptVersion(
  id: string,
  promptVersionId: string,
  reason = "回滚到上一版"
): Promise<AccountPromptVersion> {
  return fetchJson<AccountPromptVersion>(`/api/accounts/${id}/prompt-versions/${promptVersionId}/rollback`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

export async function activateAccountPromptVersion(
  id: string,
  promptVersionId: string,
  reason = "设为当前激活版本"
): Promise<AccountPromptVersion> {
  return fetchJson<AccountPromptVersion>(`/api/accounts/${id}/prompt-versions/${promptVersionId}/activate`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

export async function getCoachBrief(input?: {
  domain_slug?: string;
  account_id?: string;
}): Promise<CoachBrief> {
  const query = new URLSearchParams({
    domain_slug: input?.domain_slug ?? DEFAULT_DOMAIN_SLUG,
    account_id: input?.account_id ?? "",
  });
  return fetchJson<CoachBrief>(`/api/coach/brief?${query.toString()}`);
}

export async function chatWithCoach(input: {
  message: string;
  domain_slug?: string;
  account_id?: string;
  conversation_history?: Array<{ role: "user" | "assistant"; content: string }>;
}): Promise<CoachChatResponse> {
  return fetchJson<CoachChatResponse>("/api/coach/chat", {
    method: "POST",
    body: JSON.stringify({
      message: input.message ?? "",
      domain_slug: input.domain_slug ?? DEFAULT_DOMAIN_SLUG,
      account_id: input.account_id ?? "",
      conversation_history: input.conversation_history ?? []
    })
  });
}

export async function confirmCoachAction(input: {
  action_id: string;
  domain_slug?: string;
  account_id?: string;
  confirm?: boolean;
}): Promise<CoachConfirmActionResponse> {
  return fetchJson<CoachConfirmActionResponse>("/api/coach/confirm-action", {
    method: "POST",
    body: JSON.stringify({
      action_id: input.action_id,
      domain_slug: input.domain_slug ?? DEFAULT_DOMAIN_SLUG,
      account_id: input.account_id ?? "",
      confirm: input.confirm ?? true
    })
  });
}

export async function confirmTaskPlan(input: {
  task_plan: TaskPlanPreview | Record<string, unknown>;
  confirm_text?: string;
  second_confirm_text?: string;
}): Promise<{
  status: string;
  error_code?: string;
  message?: string;
  task_plan?: TaskPlanPreview;
  required_confirmations?: RequiredConfirmation[];
}> {
  return fetchJson("/api/ops/task-plan/confirm", {
    method: "POST",
    body: JSON.stringify({
      task_plan: input.task_plan,
      confirm_text: input.confirm_text ?? "确认执行",
      second_confirm_text: input.second_confirm_text ?? ""
    })
  });
}

export async function runTaskPlan(input: {
  task_plan: TaskPlanPreview | Record<string, unknown>;
  domain_slug?: string;
  account_id?: string;
  triggered_by?: string;
}): Promise<{
  status: string;
  error_code?: string;
  message?: string;
  task_plan?: TaskPlanPreview;
  executed?: Array<Record<string, unknown>>;
  execution_envelopes?: Array<Record<string, unknown>>;
  warnings?: string[];
  source_paths?: string[];
}> {
  return fetchJson("/api/ops/task-plan/run", {
    method: "POST",
    body: JSON.stringify({
      task_plan: input.task_plan,
      domain_slug: input.domain_slug ?? DEFAULT_DOMAIN_SLUG,
      account_id: input.account_id ?? "",
      triggered_by: input.triggered_by ?? "ui:assistant_confirmed_plan"
    })
  });
}

export async function getCrawlerTemplate(domainSlug = DEFAULT_DOMAIN_SLUG): Promise<{
  status: string;
  domain_slug: string;
  crawler_template: Record<string, unknown>;
  template_example: Record<string, unknown>;
}> {
  return fetchJson(`/api/ops/crawler-template?domain_slug=${encodeURIComponent(domainSlug)}`);
}

export async function updateCrawlerTemplate(input: {
  domain_slug?: string;
  crawler_template: Record<string, unknown>;
  reason?: string;
}): Promise<{
  status: string;
  domain_slug: string;
  crawler_template: Record<string, unknown>;
}> {
  return fetchJson("/api/ops/crawler-template", {
    method: "PUT",
    body: JSON.stringify({
      domain_slug: input.domain_slug ?? DEFAULT_DOMAIN_SLUG,
      crawler_template: input.crawler_template,
      reason: input.reason ?? "更新采集模板"
    })
  });
}

export async function analyzeIntel(input?: {
  domain_slug?: string;
  account_id?: string;
  limit?: number;
  custom_logic?: string;
}): Promise<IntelAnalysisResponse> {
  const safeLimit = clampInt(input?.limit ?? 30, 5, 200);
  return fetchJson("/api/ops/analyze-intel", {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input?.domain_slug ?? DEFAULT_DOMAIN_SLUG,
      account_id: input?.account_id ?? "",
      limit: safeLimit,
      custom_logic: input?.custom_logic ?? ""
    })
  });
}

export async function analyzeHotPostsWithLlm(input: {
  items: Array<{ title: string; body?: string; source_url?: string }>;
  analysis_prompt?: string;
  account_id?: string;
  domain_slug?: string;
}): Promise<HotPostLlmAnalyzeResponse> {
  const items = (input.items || []).slice(0, 20).map((item) => ({
    title: String(item.title || ""),
    body: String(item.body || ""),
    source_url: String(item.source_url || "")
  }));
  return fetchJson<HotPostLlmAnalyzeResponse>("/api/ops/analyze-hot-posts", {
    method: "POST",
    body: JSON.stringify({
      items,
      analysis_prompt: input.analysis_prompt ?? "",
      account_id: input.account_id ?? "",
      domain_slug: input.domain_slug ?? DEFAULT_DOMAIN_SLUG
    })
  });
}

export async function rebuildXhsContent(input: {
  competitor_analysis: Array<Record<string, unknown>>;
  ip_positioning: string;
  weekly_keywords: string[];
  tone_style: string;
  strategy_prompt?: string;
  copy_prompt?: string;
  account_id?: string;
  domain_slug?: string;
}): Promise<RebuildXhsContentResponse> {
  return fetchJson<RebuildXhsContentResponse>("/api/ops/rebuild-xhs-content", {
    method: "POST",
    body: JSON.stringify({
      competitor_analysis: (input.competitor_analysis || []).slice(0, 30),
      ip_positioning: input.ip_positioning || "",
      weekly_keywords: (input.weekly_keywords || []).slice(0, 20),
      tone_style: input.tone_style || "",
      strategy_prompt: input.strategy_prompt || "",
      copy_prompt: input.copy_prompt || "",
      account_id: input.account_id ?? "",
      domain_slug: input.domain_slug ?? DEFAULT_DOMAIN_SLUG
    })
  });
}

export async function getOrchestratorControl(domain_slug = DEFAULT_DOMAIN_SLUG): Promise<OrchestratorControlResponse> {
  const query = new URLSearchParams({ domain_slug });
  return fetchJson<OrchestratorControlResponse>(`/api/ops/orchestrator-control?${query.toString()}`);
}

export async function getOrchestratorControlVersions(input?: {
  domain_slug?: string;
  limit?: number;
}): Promise<OrchestratorControlVersionsResponse> {
  const query = new URLSearchParams({
    domain_slug: input?.domain_slug ?? DEFAULT_DOMAIN_SLUG,
    limit: String(clampInt(input?.limit ?? 20, 1, 60)),
  });
  return fetchJson<OrchestratorControlVersionsResponse>(`/api/ops/orchestrator-control/versions?${query.toString()}`);
}

export async function rollbackOrchestratorControl(input: {
  version: string;
  domain_slug?: string;
  reason?: string;
}): Promise<OrchestratorControlRollbackResponse> {
  return fetchJson<OrchestratorControlRollbackResponse>("/api/ops/orchestrator-control/rollback", {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input.domain_slug ?? DEFAULT_DOMAIN_SLUG,
      version: input.version,
      reason: input.reason ?? "manual rollback from ui"
    })
  });
}

export async function updateOrchestratorControl(input: {
  domain_slug?: string;
  control_jsonb: OrchestratorControl;
  reason?: string;
}): Promise<OrchestratorControlResponse> {
  return fetchJson<OrchestratorControlResponse>("/api/ops/orchestrator-control", {
    method: "PUT",
    body: JSON.stringify({
      domain_slug: input.domain_slug ?? DEFAULT_DOMAIN_SLUG,
      control_jsonb: input.control_jsonb,
      reason: input.reason ?? "更新总教练配置"
    })
  });
}

export async function runOrchestratorSelfUpgrade(input?: {
  domain_slug?: string;
  apply?: boolean;
  note?: string;
}): Promise<OrchestratorSelfUpgradeResponse> {
  return fetchJson<OrchestratorSelfUpgradeResponse>("/api/ops/orchestrator-self-upgrade", {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input?.domain_slug ?? DEFAULT_DOMAIN_SLUG,
      apply: Boolean(input?.apply),
      note: input?.note ?? ""
    })
  });
}

export async function runChiefEvolution(input?: {
  domain_slug?: string;
  account_id?: string;
  milestone_goal?: string;
  competitor_limit?: number;
  performance_limit?: number;
  apply?: boolean;
}): Promise<ChiefEvolutionResponse> {
  return fetchJson<ChiefEvolutionResponse>("/api/ops/chief-evolution", {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input?.domain_slug ?? DEFAULT_DOMAIN_SLUG,
      account_id: input?.account_id ?? "",
      milestone_goal: input?.milestone_goal ?? "",
      competitor_limit: clampInt(input?.competitor_limit ?? 60, 10, 300),
      performance_limit: clampInt(input?.performance_limit ?? 60, 10, 300),
      apply: Boolean(input?.apply),
    })
  });
}

export async function cleanupDuplicateTasks(input?: {
  domain_slug?: string;
  limit?: number;
  include_review?: boolean;
  dry_run?: boolean;
}): Promise<CleanupDuplicateTasksResponse> {
  const query = new URLSearchParams({
    domain_slug: input?.domain_slug ?? DEFAULT_DOMAIN_SLUG,
    limit: String(clampInt(input?.limit ?? 500, 50, 1200)),
    include_review: String(input?.include_review ?? true),
    dry_run: String(Boolean(input?.dry_run)),
  });
  return fetchJson<CleanupDuplicateTasksResponse>(`/api/ops/cleanup-duplicate-tasks?${query.toString()}`, {
    method: "POST",
  });
}

export async function getOpsAutoConfig(input?: {
  domain_slug?: string;
  account_id?: string;
  channel?: "xiaohongshu" | "wechat_mp" | "douyin" | "video";
  limit?: number;
  custom_logic?: string;
}): Promise<OpsAutoConfigResponse> {
  const safeLimit = clampInt(input?.limit ?? 50, 10, 200);
  return fetchJson("/api/ops/auto-config", {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input?.domain_slug ?? DEFAULT_DOMAIN_SLUG,
      account_id: input?.account_id ?? "",
      channel: input?.channel ?? "xiaohongshu",
      limit: safeLimit,
      custom_logic: input?.custom_logic ?? ""
    })
  });
}

export async function getMcpReadonlyStatus(): Promise<McpReadonlyStatus> {
  return fetchJson<McpReadonlyStatus>("/api/ops/mcp-readonly/status");
}

export async function searchWithMcpReadonly(input: {
  query: string;
  limit?: number;
  source_kind?: "hotspot" | "viewpoint";
}): Promise<McpReadonlySearchResponse> {
  const safeLimit = clampInt(input.limit ?? 8, 1, 50);
  return fetchJson<McpReadonlySearchResponse>("/api/ops/mcp-readonly/search", {
    method: "POST",
    body: JSON.stringify({
      query: input.query,
      limit: safeLimit,
      source_kind: input.source_kind ?? "hotspot"
    })
  });
}

export async function syncMcpReadonlyMetrics(input?: {
  domain_slug?: string;
  limit?: number;
}): Promise<McpReadonlySyncMetricsResponse> {
  const safeLimit = clampInt(input?.limit ?? 10, 1, 50);
  return fetchJson<McpReadonlySyncMetricsResponse>("/api/ops/mcp-readonly/sync-metrics", {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input?.domain_slug ?? DEFAULT_DOMAIN_SLUG,
      limit: safeLimit
    })
  });
}

export async function collectIntelWithMcpReadonly(input?: {
  domain_slug?: string;
  query?: string;
  account_id?: string;
  limit?: number;
  include_home?: boolean;
  include_search?: boolean;
  include_detail_metrics?: boolean;
  persist?: boolean;
  tool_profile?: Record<string, unknown>;
}): Promise<McpReadonlyCollectIntelResponse> {
  const safeLimit = clampInt(input?.limit ?? 12, 1, 100);
  return fetchJson<McpReadonlyCollectIntelResponse>("/api/ops/mcp-readonly/collect-intel", {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input?.domain_slug ?? DEFAULT_DOMAIN_SLUG,
      query: input?.query ?? "日本移民",
      account_id: input?.account_id ?? "",
      limit: safeLimit,
      include_home: input?.include_home ?? true,
      include_search: input?.include_search ?? true,
      include_detail_metrics: input?.include_detail_metrics ?? false,
      persist: input?.persist ?? true,
      tool_profile: input?.tool_profile ?? {}
    })
  });
}

export async function customCallMcpReadonly(input: {
  tool_name: string;
  arguments?: Record<string, unknown>;
  source_kind?: "hotspot" | "viewpoint" | "custom";
  limit?: number;
}): Promise<McpReadonlyCustomCallResponse> {
  return fetchJson<McpReadonlyCustomCallResponse>("/api/ops/mcp-readonly/custom-call", {
    method: "POST",
    body: JSON.stringify({
      tool_name: input.tool_name,
      arguments: input.arguments ?? {},
      source_kind: input.source_kind ?? "custom",
      limit: clampInt(input.limit ?? 20, 1, 100),
    }),
  });
}

export async function getKbCases(input?: {
  domain_slug?: string;
  account_id?: string;
  limit?: number;
}): Promise<KbCaseRecord[]> {
  const query = new URLSearchParams({
    domain_slug: input?.domain_slug ?? DEFAULT_DOMAIN_SLUG,
    account_id: input?.account_id ?? "",
    limit: String(clampInt(input?.limit ?? 50, 1, 200))
  });
  const data = await fetchJson<{ items: KbCaseRecord[] }>(`/api/kb/cases?${query.toString()}`);
  return data.items;
}

export async function createKbCase(input: {
  domain_slug?: string;
  account_id?: string;
  platform?: string;
  author?: string;
  url?: string;
  title: string;
  content?: string;
  metrics?: Record<string, unknown>;
}): Promise<KbCaseRecord> {
  return fetchJson<KbCaseRecord>("/api/kb/cases", {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input.domain_slug ?? DEFAULT_DOMAIN_SLUG,
      account_id: input.account_id ?? "",
      platform: input.platform ?? "xiaohongshu",
      author: input.author ?? "",
      url: input.url ?? "",
      title: input.title,
      content: input.content ?? "",
      metrics: input.metrics ?? {}
    })
  });
}

export async function patchKbCase(id: string, patch: Record<string, unknown>): Promise<KbCaseRecord> {
  return fetchJson<KbCaseRecord>(`/api/kb/cases/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch)
  });
}

export async function getKbAssets(input?: {
  domain_slug?: string;
  account_id?: string;
  type?: string;
  limit?: number;
}): Promise<KbAssetRecord[]> {
  const query = new URLSearchParams({
    domain_slug: input?.domain_slug ?? DEFAULT_DOMAIN_SLUG,
    account_id: input?.account_id ?? "",
    type: input?.type ?? "",
    limit: String(clampInt(input?.limit ?? 50, 1, 200))
  });
  const data = await fetchJson<{ items: KbAssetRecord[] }>(`/api/kb/assets?${query.toString()}`);
  return data.items;
}

export async function createKbAsset(input: {
  domain_slug?: string;
  account_id?: string;
  type?: string;
  content: string;
  source?: string;
  usable_scene?: string;
  is_verified?: boolean;
  summary?: string;
}): Promise<KbAssetRecord> {
  return fetchJson<KbAssetRecord>("/api/kb/assets", {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input.domain_slug ?? DEFAULT_DOMAIN_SLUG,
      account_id: input.account_id ?? "",
      type: input.type ?? "insight",
      content: input.content,
      source: input.source ?? "",
      usable_scene: input.usable_scene ?? "",
      is_verified: input.is_verified ?? false,
      summary: input.summary ?? ""
    })
  });
}

export async function patchKbAsset(id: string, patch: Record<string, unknown>): Promise<KbAssetRecord> {
  return fetchJson<KbAssetRecord>(`/api/kb/assets/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch)
  });
}

export async function getKbUserNeeds(input?: {
  domain_slug?: string;
  account_id?: string;
  demand_type?: string;
  emotion?: string;
  limit?: number;
}): Promise<KbUserNeedRecord[]> {
  const query = new URLSearchParams({
    domain_slug: input?.domain_slug ?? DEFAULT_DOMAIN_SLUG,
    account_id: input?.account_id ?? "",
    demand_type: input?.demand_type ?? "",
    emotion: input?.emotion ?? "",
    limit: String(clampInt(input?.limit ?? 50, 1, 200))
  });
  const data = await fetchJson<{ items: KbUserNeedRecord[] }>(`/api/kb/user-needs?${query.toString()}`);
  return data.items;
}

export async function createKbUserNeed(input: {
  domain_slug?: string;
  account_id?: string;
  user_type?: string;
  original_text: string;
  scenario?: string;
  demand_type?: string;
  emotion?: string;
  real_problem?: string;
}): Promise<KbUserNeedRecord> {
  return fetchJson<KbUserNeedRecord>("/api/kb/user-needs", {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input.domain_slug ?? DEFAULT_DOMAIN_SLUG,
      account_id: input.account_id ?? "",
      user_type: input.user_type ?? "",
      original_text: input.original_text,
      scenario: input.scenario ?? "",
      demand_type: input.demand_type ?? "",
      emotion: input.emotion ?? "",
      real_problem: input.real_problem ?? ""
    })
  });
}

export async function patchKbUserNeed(id: string, patch: Record<string, unknown>): Promise<KbUserNeedRecord> {
  return fetchJson<KbUserNeedRecord>(`/api/kb/user-needs/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch)
  });
}

export async function getKbTopics(input?: {
  domain_slug?: string;
  account_id?: string;
  status?: string;
  limit?: number;
}): Promise<KbTopicRecord[]> {
  const query = new URLSearchParams({
    domain_slug: input?.domain_slug ?? DEFAULT_DOMAIN_SLUG,
    account_id: input?.account_id ?? "",
    status: input?.status ?? "",
    limit: String(clampInt(input?.limit ?? 50, 1, 200))
  });
  const data = await fetchJson<{ items: KbTopicRecord[] }>(`/api/kb/topics?${query.toString()}`);
  return data.items;
}

export async function createKbTopic(input: {
  domain_slug?: string;
  account_id?: string;
  title: string;
  topic_description?: string;
  target_user?: string;
  platform?: string;
  structure_type?: string;
  status?: string;
  reason?: string;
}): Promise<KbTopicRecord> {
  return fetchJson<KbTopicRecord>("/api/kb/topics", {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input.domain_slug ?? DEFAULT_DOMAIN_SLUG,
      account_id: input.account_id ?? "",
      title: input.title,
      topic_description: input.topic_description ?? "",
      target_user: input.target_user ?? "",
      platform: input.platform ?? "xiaohongshu",
      structure_type: input.structure_type ?? "",
      status: input.status ?? "todo",
      reason: input.reason ?? ""
    })
  });
}

export async function patchKbTopic(id: string, patch: Record<string, unknown>): Promise<KbTopicRecord> {
  return fetchJson<KbTopicRecord>(`/api/kb/topics/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch)
  });
}

export async function getKbReviews(input?: {
  domain_slug?: string;
  account_id?: string;
  limit?: number;
}): Promise<KbReviewRecord[]> {
  const query = new URLSearchParams({
    domain_slug: input?.domain_slug ?? DEFAULT_DOMAIN_SLUG,
    account_id: input?.account_id ?? "",
    limit: String(clampInt(input?.limit ?? 50, 1, 200))
  });
  const data = await fetchJson<{ items: KbReviewRecord[] }>(`/api/kb/reviews?${query.toString()}`);
  return data.items;
}

export async function createKbReview(input: {
  domain_slug?: string;
  account_id?: string;
  topic_id?: string;
  pipeline_task_id?: string;
  content_item_ref?: string;
  platform?: string;
  metrics?: Record<string, unknown>;
  success_points?: string;
  failure_points?: string;
  improvement?: string;
  summary_text?: string;
}): Promise<KbReviewRecord> {
  return fetchJson<KbReviewRecord>("/api/kb/reviews", {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input.domain_slug ?? DEFAULT_DOMAIN_SLUG,
      account_id: input.account_id ?? "",
      topic_id: input.topic_id ?? "",
      pipeline_task_id: input.pipeline_task_id ?? "",
      content_item_ref: input.content_item_ref ?? "",
      platform: input.platform ?? "xiaohongshu",
      metrics: input.metrics ?? {},
      success_points: input.success_points ?? "",
      failure_points: input.failure_points ?? "",
      improvement: input.improvement ?? "",
      summary_text: input.summary_text ?? ""
    })
  });
}

export async function patchKbReview(id: string, patch: Record<string, unknown>): Promise<KbReviewRecord> {
  return fetchJson<KbReviewRecord>(`/api/kb/reviews/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch)
  });
}

export async function getKbTags(input?: {
  domain_slug?: string;
  account_id?: string;
  category?: string;
  status?: string;
  limit?: number;
}): Promise<KbTagRecord[]> {
  const query = new URLSearchParams({
    domain_slug: input?.domain_slug ?? DEFAULT_DOMAIN_SLUG,
    account_id: input?.account_id ?? "",
    category: input?.category ?? "",
    status: input?.status ?? "",
    limit: String(clampInt(input?.limit ?? 200, 1, 500))
  });
  const data = await fetchJson<{ items: KbTagRecord[] }>(`/api/kb/tags?${query.toString()}`);
  return data.items;
}

export async function createKbTag(input: {
  domain_slug?: string;
  account_id?: string;
  name: string;
  category?: string;
  status?: string;
}): Promise<KbTagRecord> {
  return fetchJson<KbTagRecord>("/api/kb/tags", {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input.domain_slug ?? DEFAULT_DOMAIN_SLUG,
      account_id: input.account_id ?? "",
      name: input.name,
      category: input.category ?? "general",
      status: input.status ?? "active"
    })
  });
}

export async function patchKbTag(id: string, patch: Record<string, unknown>): Promise<KbTagRecord> {
  return fetchJson<KbTagRecord>(`/api/kb/tags/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch)
  });
}

export async function getKbEntityTags(input?: {
  domain_slug?: string;
  entity_type?: string;
  entity_id?: string;
  limit?: number;
}): Promise<KbEntityTagRecord[]> {
  const query = new URLSearchParams({
    domain_slug: input?.domain_slug ?? DEFAULT_DOMAIN_SLUG,
    entity_type: input?.entity_type ?? "",
    entity_id: input?.entity_id ?? "",
    limit: String(clampInt(input?.limit ?? 200, 1, 500))
  });
  const data = await fetchJson<{ items: KbEntityTagRecord[] }>(`/api/kb/entity-tags?${query.toString()}`);
  return data.items;
}

export async function createKbEntityTag(input: {
  domain_slug?: string;
  account_id?: string;
  entity_type: string;
  entity_id: string;
  tag_id: string;
}): Promise<KbEntityTagRecord> {
  return fetchJson<KbEntityTagRecord>("/api/kb/entity-tags", {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input.domain_slug ?? DEFAULT_DOMAIN_SLUG,
      account_id: input.account_id ?? "",
      entity_type: input.entity_type,
      entity_id: input.entity_id,
      tag_id: input.tag_id
    })
  });
}

export async function patchKbEntityTag(id: string, patch: Record<string, unknown>): Promise<KbEntityTagRecord> {
  return fetchJson<KbEntityTagRecord>(`/api/kb/entity-tags/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch)
  });
}

export async function getKbIngestionLogs(input?: {
  domain_slug?: string;
  entity_type?: string;
  limit?: number;
}): Promise<KbIngestionLogRecord[]> {
  const query = new URLSearchParams({
    domain_slug: input?.domain_slug ?? DEFAULT_DOMAIN_SLUG,
    entity_type: input?.entity_type ?? "",
    limit: String(clampInt(input?.limit ?? 100, 1, 300))
  });
  const data = await fetchJson<{ items: KbIngestionLogRecord[] }>(`/api/kb/ingestion-logs?${query.toString()}`);
  return data.items;
}

export async function getKbAiJobs(input?: {
  domain_slug?: string;
  status?: string;
  limit?: number;
}): Promise<KbAiJobRecord[]> {
  const query = new URLSearchParams({
    domain_slug: input?.domain_slug ?? DEFAULT_DOMAIN_SLUG,
    status: input?.status ?? "",
    limit: String(clampInt(input?.limit ?? 100, 1, 300))
  });
  const data = await fetchJson<{ items: KbAiJobRecord[] }>(`/api/kb/ai-jobs?${query.toString()}`);
  return data.items;
}

export async function importKbOctopus(input: {
  domain_slug?: string;
  account_id?: string;
  source?: string;
  source_run_id?: string;
  entity_type: "case" | "asset" | "user_need" | "review";
  items: Array<Record<string, unknown>>;
}): Promise<KbOctopusImportResult> {
  return fetchJson<KbOctopusImportResult>("/api/kb/import/octopus", {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input.domain_slug ?? DEFAULT_DOMAIN_SLUG,
      account_id: input.account_id ?? "",
      source: input.source ?? "octopus",
      source_run_id: input.source_run_id ?? "",
      entity_type: input.entity_type,
      items: (input.items || []).slice(0, 500)
    })
  });
}

export async function recommendKbTopics(input: {
  domain_slug?: string;
  account_id?: string;
  case_ids?: string[];
  asset_ids?: string[];
  need_ids?: string[];
  limit?: number;
  platform?: string;
}): Promise<KbTopicRecommendResponse> {
  return fetchJson<KbTopicRecommendResponse>("/api/kb/topics/recommend", {
    method: "POST",
    body: JSON.stringify({
      domain_slug: input.domain_slug ?? DEFAULT_DOMAIN_SLUG,
      account_id: input.account_id ?? "",
      case_ids: (input.case_ids || []).slice(0, 100),
      asset_ids: (input.asset_ids || []).slice(0, 100),
      need_ids: (input.need_ids || []).slice(0, 100),
      limit: clampInt(input.limit ?? 10, 1, 30),
      platform: input.platform ?? "xiaohongshu"
    })
  });
}

export async function getKbRpaTaskTemplates(input?: {
  domain_slug?: string;
  active_only?: boolean;
  limit?: number;
}): Promise<KbRpaTaskTemplateRecord[]> {
  const query = new URLSearchParams({
    domain_slug: input?.domain_slug ?? DEFAULT_DOMAIN_SLUG,
    active_only: String(input?.active_only ?? true),
    limit: String(clampInt(input?.limit ?? 200, 1, 500)),
  });
  const data = await fetchJson<{ items: KbRpaTaskTemplateRecord[] }>(`/api/kb/rpa/task-templates?${query.toString()}`);
  return data.items;
}

export async function getKbRpaTaskRuns(input?: {
  domain_slug?: string;
  status?: string;
  task_type?: string;
  entity_type?: string;
  limit?: number;
}): Promise<KbRpaTaskRunRecord[]> {
  const query = new URLSearchParams({
    domain_slug: input?.domain_slug ?? DEFAULT_DOMAIN_SLUG,
    status: input?.status ?? "",
    task_type: input?.task_type ?? "",
    entity_type: input?.entity_type ?? "",
    limit: String(clampInt(input?.limit ?? 120, 1, 500)),
  });
  const data = await fetchJson<{ items: KbRpaTaskRunRecord[] }>(`/api/kb/rpa/task-runs?${query.toString()}`);
  return data.items;
}

export async function getKbRpaRawPayloads(input?: {
  domain_slug?: string;
  run_id?: string;
  status?: string;
  limit?: number;
}): Promise<KbRpaRawPayloadRecord[]> {
  const query = new URLSearchParams({
    domain_slug: input?.domain_slug ?? DEFAULT_DOMAIN_SLUG,
    run_id: input?.run_id ?? "",
    status: input?.status ?? "",
    limit: String(clampInt(input?.limit ?? 200, 1, 1000)),
  });
  const data = await fetchJson<{ items: KbRpaRawPayloadRecord[] }>(`/api/kb/rpa/raw-payloads?${query.toString()}`);
  return data.items;
}

export async function getKbRpaCleanResults(input?: {
  domain_slug?: string;
  run_id?: string;
  accepted?: boolean;
  reject_code?: string;
  limit?: number;
}): Promise<KbRpaCleanResultRecord[]> {
  const query = new URLSearchParams({
    domain_slug: input?.domain_slug ?? DEFAULT_DOMAIN_SLUG,
    run_id: input?.run_id ?? "",
    accepted: typeof input?.accepted === "boolean" ? String(input.accepted) : "",
    reject_code: input?.reject_code ?? "",
    limit: String(clampInt(input?.limit ?? 200, 1, 1000)),
  });
  const data = await fetchJson<{ items: KbRpaCleanResultRecord[] }>(`/api/kb/rpa/clean-results?${query.toString()}`);
  return data.items;
}

export async function replayKbRpaRun(runId: string): Promise<KbRpaReplayResponse> {
  return fetchJson<KbRpaReplayResponse>(`/api/kb/rpa/runs/${runId}/replay`, {
    method: "POST",
  });
}






