"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { Loader2, RefreshCw, Play, CheckCircle2, XCircle, Route, Bot } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  DEFAULT_DOMAIN_SLUG,
  approvePipelineTask,
  applyPendingStrategyItemChange,
  getAccountAgentConfig,
  getAccountContentMethodView,
  getAccountLoopExecutionView,
  getAccountSopCurrent,
  getExecutionRouteStatus,
  getOpsOverview,
  getPendingStrategyItems,
  getPipelineTasks,
  getPublishFeedbackSummary,
  getRuntimeContext,
  getSubagentRuns,
  runAccountLoopDaily,
  rejectPipelineTask,
  updateMemoryItemStatus,
} from "@/lib/api";
import { toUserFacingError } from "@/lib/user-facing-errors";
import type {
  AgentConfigView,
  ContentMethodView,
  ExecutionRouteStatusView,
  LoopExecutionView,
  OpsOverview,
  PendingStrategyItem,
  PipelineTask,
  PublishFeedbackSummaryItem,
  RuntimeContext,
  SopCurrentView,
  SubAgentRunRecord,
} from "@/lib/types";

const DOMAIN_SLUG = DEFAULT_DOMAIN_SLUG;

function fmtTime(value?: string | null): string {
  if (!value) return "暂无";
  try {
    return new Date(value).toLocaleString("zh-CN", { hour12: false });
  } catch {
    return value;
  }
}

function shortText(value: unknown, max = 88): string {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  if (!text) return "暂无";
  return text.length > max ? `${text.slice(0, max)}...` : text;
}

function agentLabel(actor: string): string {
  const map: Record<string, string> = {
    collector_agent: "采集环节",
    analysis_agent: "分析环节",
    copy_agent: "文案环节",
    review_agent: "复盘环节",
    coach: "总教练",
    coach_agent: "总教练",
    assistant: "执行协调层",
  };
  return map[actor] || actor || "未命名执行单元";
}

function stageStatusLabel(status: string): string {
  const map: Record<string, string> = {
    queued: "排队中",
    waiting: "等待中",
    pending: "待处理",
    running: "执行中",
    success: "已完成",
    completed: "已完成",
    published: "已发布",
    done: "已完成",
    pending_review: "待审核",
    review: "审核中",
    drafting: "写稿中",
    reflecting: "复盘中",
    failed: "失败",
    blocked: "已阻塞",
    canceled: "已取消",
  };
  return map[status] ?? status ?? "未知状态";
}

function actionLabel(actionType: string, source: string): string {
  const key = actionType || source;
  const map: Record<string, string> = {
    collect_intel: "采集真实输入",
    reconcile_feedback: "回收发布反馈",
    draft_content: "生成草稿",
    review_content: "审核整理",
    run_daily_loop: "运行今日闭环",
    confirm_apply_change: "确认策略变更",
    collect: "采集内容",
    analysis: "分析样本",
    copy: "生成帖子",
    review: "生成复盘",
  };
  return map[key] || key || "未命名动作";
}

function runSummary(item: SubAgentRunRecord): string {
  const result = item.result_jsonb || {};
  const summary =
    String(
      result.summary ||
        result.message ||
        result.result ||
        result.status ||
        item.error_message ||
        "",
    ).trim();
  if (summary) return shortText(summary, 120);
  if (item.status === "success") return "本次动作已执行完成。";
  if (item.status === "running") return "当前正在处理中。";
  if (item.status === "failed") return "本次动作执行失败，需人工查看。";
  return "暂无执行摘要。";
}

function subRunHint(item: SubAgentRunRecord | null | undefined): string {
  if (!item) return "暂无运行记录";
  return `${agentLabel(item.actor)} · ${stageStatusLabel(item.status)}`;
}

function strategySourceLabel(item: PendingStrategyItem): string {
  const source = String(item.source || "").toLowerCase();
  if (source.includes("review")) return "来自复盘结论";
  if (source.includes("reflection")) return "来自系统复盘";
  if (source.includes("memory")) return "来自沉淀记忆";
  if (source.includes("daily")) return "来自今日闭环";
  return "来自策略分析";
}

function affectedAgentLabel(value: string): string {
  const map: Record<string, string> = {
    collector_agent: "采集环节",
    analysis_agent: "分析环节",
    copy_agent: "文案环节",
    review_agent: "复盘环节",
    memory: "策略沉淀",
    strategy: "策略层",
    draft_writer: "文案环节",
  };
  return map[value] || value || "未标记";
}

function taskTitle(task: PipelineTask): string {
  const payload = task.payload_jsonb || {};
  const intent = task.intent_jsonb || {};
  return String(payload.title || intent.title || payload.topic || intent.topic || "未命名任务");
}

function taskBody(task: PipelineTask): string {
  const payload = task.payload_jsonb || {};
  const review = task.review_jsonb || {};
  return String(payload.body || review.body || payload.content || "");
}

function statusTone(status: string): string {
  if (["done", "published", "success", "completed"].includes(status)) return "border-emerald-400/40 bg-emerald-400/10 text-emerald-100";
  if (["attention", "pending_review", "review", "drafting"].includes(status)) return "border-amber-300/40 bg-amber-400/10 text-amber-50";
  if (["failed", "error", "blocked"].includes(status)) return "border-rose-400/40 bg-rose-400/10 text-rose-100";
  if (["running", "publishing", "reflecting"].includes(status)) return "border-cyan-300/40 bg-cyan-400/10 text-cyan-100";
  return "border-border/60 bg-background text-foreground";
}

export default function LegacyFlowWorkspace() {
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const [runtime, setRuntime] = useState<RuntimeContext | null>(null);
  const [overview, setOverview] = useState<OpsOverview | null>(null);
  const [loopView, setLoopView] = useState<LoopExecutionView | null>(null);
  const [methodView, setMethodView] = useState<ContentMethodView | null>(null);
  const [agentConfig, setAgentConfig] = useState<AgentConfigView | null>(null);
  const [sopCurrent, setSopCurrent] = useState<SopCurrentView | null>(null);
  const [routeStatus, setRouteStatus] = useState<ExecutionRouteStatusView | null>(null);
  const [tasks, setTasks] = useState<PipelineTask[]>([]);
  const [pendingStrategy, setPendingStrategy] = useState<PendingStrategyItem[]>([]);
  const [feedback, setFeedback] = useState<PublishFeedbackSummaryItem[]>([]);
  const [subagentRuns, setSubagentRuns] = useState<SubAgentRunRecord[]>([]);

  const accountId = runtime?.publish_account.account_id || overview?.account_id || "";

  async function loadAll() {
    setLoading(true);
    setError("");
    try {
      const [runtimeData, overviewData] = await Promise.all([
        getRuntimeContext(DOMAIN_SLUG),
        getOpsOverview(DOMAIN_SLUG, 12),
      ]);
      setRuntime(runtimeData);
      setOverview(overviewData);

      const effectiveAccountId = runtimeData.publish_account.account_id || overviewData.account_id || "";
      if (!effectiveAccountId) {
        setTasks([]);
        setLoopView(null);
        setMethodView(null);
        setAgentConfig(null);
        setSopCurrent(null);
        setRouteStatus(null);
        setPendingStrategy([]);
        setFeedback([]);
        setSubagentRuns([]);
        return;
      }

      setTasks(await getPipelineTasks({ accountId: effectiveAccountId, domainSlug: DOMAIN_SLUG, limit: 40 }));

      const [loopData, methodData, agentConfigData, sopData, routeData, strategyItems, feedbackItems, runItems] = await Promise.all([
        getAccountLoopExecutionView(effectiveAccountId, { domainSlug: DOMAIN_SLUG }),
        getAccountContentMethodView(effectiveAccountId, { domainSlug: DOMAIN_SLUG }),
        getAccountAgentConfig(effectiveAccountId, { domainSlug: DOMAIN_SLUG }),
        getAccountSopCurrent(effectiveAccountId, { domainSlug: DOMAIN_SLUG }),
        getExecutionRouteStatus({ accountId: effectiveAccountId, domainSlug: DOMAIN_SLUG }),
        getPendingStrategyItems({ accountId: effectiveAccountId, limit: 8 }),
        getPublishFeedbackSummary({ accountId: effectiveAccountId, limit: 6 }),
        getSubagentRuns({ accountId: effectiveAccountId, domainSlug: DOMAIN_SLUG, limit: 12 }),
      ]);

      setLoopView(loopData);
      setMethodView(methodData);
      setAgentConfig(agentConfigData);
      setSopCurrent(sopData);
      setRouteStatus(routeData);
      setPendingStrategy(strategyItems);
      setFeedback(feedbackItems);
      setSubagentRuns(runItems);
    } catch (err) {
      setError(toUserFacingError(err, "加载流程页失败，请稍后刷新。"));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadAll();
  }, []);

  const pendingReviewTasks = useMemo(() => tasks.filter((task) => task.status === "pending_review").slice(0, 4), [tasks]);
  const latestTask = useMemo(() => tasks[0] || null, [tasks]);
  const latestPublished = useMemo(() => tasks.find((task) => task.status === "published" || task.status === "done") || null, [tasks]);

  async function handleRunDaily() {
    if (busy || !accountId || !window.confirm("确认运行今日内容流程？流程会按账号配置执行，草稿等待人工审核。")) return;
    setBusy(true);
    setNotice("");
    setError("");
    try {
      const result = await runAccountLoopDaily(accountId, { domain_slug: DOMAIN_SLUG, flow: "full", force: true });
      setNotice(result.gate_passed ? "已触发今日闭环执行。" : `未进入执行：${result.blocked_reason || "门禁未通过"}`);
      await loadAll();
    } catch (err) {
      setError(toUserFacingError(err, "触发闭环失败，请稍后重试。"));
    } finally {
      setBusy(false);
    }
  }

  async function handleApprove(taskId: string) {
    if (busy || !window.confirm("确认通过这篇内容的审核？")) return;
    setBusy(true);
    setNotice("");
    setError("");
    try {
      await approvePipelineTask(taskId);
      setNotice("草稿已通过审核，请自行发布。");
      await loadAll();
    } catch (err) {
      setError(toUserFacingError(err, "审核通过失败，请稍后重试。"));
    } finally {
      setBusy(false);
    }
  }

  async function handleReject(taskId: string) {
    if (busy) return;
    const reason = window.prompt("请填写驳回原因（至少两个字）：");
    if (reason === null) return;
    if (reason.trim().length < 2) {
      setError("请填写至少两个字的驳回原因。");
      return;
    }
    setBusy(true);
    setNotice("");
    setError("");
    try {
      await rejectPipelineTask(taskId, reason.trim());
      setNotice("已打回当前内容并记录原因，可在任务详情中修改或重新生成。");
      await loadAll();
    } catch (err) {
      setError(toUserFacingError(err, "打回失败，请稍后重试。"));
    } finally {
      setBusy(false);
    }
  }

  async function handleStrategy(item: PendingStrategyItem, apply: boolean) {
    if (busy || !accountId) return;
    const proposal = item.proposed_changes?.[0];
    if (apply && !proposal) return;
    const question = apply
      ? `确认应用「${item.title}」？\n目标：${proposal!.label}\n改动后：${proposal!.after_summary}`
      : `确认不采用「${item.title}」？`;
    if (!window.confirm(question)) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      if (apply) {
        await applyPendingStrategyItemChange(item.id, { accountId, domainSlug: DOMAIN_SLUG, target: proposal!.target, reason: "来自流程页确认" });
      } else {
        await updateMemoryItemStatus(item.id, "deprecated");
      }
      setPendingStrategy((items) => items.filter((row) => row.id !== item.id));
      setNotice(apply ? "策略变更已应用。" : "已记录不采用该建议。");
      await loadAll();
    } catch (err) {
      setError(toUserFacingError(err, "策略操作失败，请稍后重试。"));
    } finally {
      setBusy(false);
    }
  }

  const summaryCards = [
    { label: "当前账号", value: runtime?.publish_account.account_name || "未绑定", hint: runtime?.publish_account.auth_hint || "未配置账号身份" },
    { label: "当前阶段", value: loopView?.current_stage || runtime?.latest.last_draft_task_status || "空闲", hint: loopView ? `完成度 ${loopView.completion_score}%` : "等待执行" },
    { label: "执行路由", value: routeStatus?.user_facing_status || "未加载", hint: routeStatus ? "默认走主执行路径，异常时自动切到备用方案" : "" },
    { label: "SOP 当前版本", value: sopCurrent?.sop_latest ? `v${sopCurrent.sop_latest.version}` : "未生成", hint: sopCurrent?.sop_latest ? fmtTime(sopCurrent.sop_latest.captured_at) : "暂无快照" },
    { label: "待审核", value: String(pendingReviewTasks.length), hint: pendingReviewTasks.length > 0 ? "需人工确认" : "审核队列清空" },
    { label: "待确认策略", value: String(pendingStrategy.length), hint: pendingStrategy[0]?.title || "当前无待确认策略" },
    { label: "最近发布反馈", value: feedback[0]?.feedback_state || "暂无", hint: feedback[0]?.title || "等待真实反馈" },
    { label: "子 Agent 运行", value: String(subagentRuns.length), hint: subRunHint(subagentRuns[0]) },
  ];

  if (loading) {
    return (
      <div className="flex min-h-[420px] items-center justify-center rounded-2xl border border-border/60 bg-background/70">
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          正在加载流程执行页...
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-semibold">闭环总览</h2>
          <p className="text-sm text-muted-foreground">
            这里只保留用户主流程需要的信息：当前阶段、方法、结果、审核动作和策略确认。
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button type="button" variant="outline" onClick={() => void loadAll()} disabled={busy}>
            <RefreshCw className="mr-2 h-4 w-4" />刷新
          </Button>
          <Button type="button" onClick={() => void handleRunDaily()} disabled={busy || !accountId}>
            {busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Play className="mr-2 h-4 w-4" />}
            运行今日闭环
          </Button>
        </div>
      </div>

      {error ? <div role="alert" className="rounded-xl border border-rose-400/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">{error}</div> : null}
      {notice ? <div role="status" className="whitespace-pre-line rounded-xl border border-emerald-400/30 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-100">{notice}</div> : null}

      <div className="grid gap-3 md:grid-cols-4 xl:grid-cols-8">
        {summaryCards.map((item) => (
          <div key={item.label} className="rounded-xl border border-border/60 bg-background/80 p-3">
            <p className="text-xs text-muted-foreground">{item.label}</p>
            <p className="mt-1 text-sm font-semibold">{shortText(item.value, 26)}</p>
            <p className="mt-1 text-xs text-muted-foreground">{shortText(item.hint, 42)}</p>
          </div>
        ))}
      </div>

      <Card>
        <CardHeader>
          <CardTitle>全流程步骤视图</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 xl:grid-cols-5">
            {(loopView?.steps || []).map((step) => (
              <div key={step.step_key} className={`rounded-xl border p-4 ${statusTone(step.status)}`}>
                <div className="flex items-center justify-between gap-2">
                  <p className="text-base font-semibold">{step.label}</p>
                  <span className="rounded-full border border-current/30 px-2 py-0.5 text-[11px]">{stageStatusLabel(step.status)}</span>
                </div>
                <p className="mt-2 text-sm">{shortText(step.summary, 80)}</p>
                <div className="mt-3 space-y-2 text-xs opacity-90">
                  <div>
                    <p className="font-medium">方法</p>
                    <ul className="mt-1 space-y-1">
                      {step.method_summary.slice(0, 3).map((line) => <li key={line}>- {line}</li>)}
                    </ul>
                  </div>
                  <div>
                    <p className="font-medium">结果</p>
                    <ul className="mt-1 space-y-1">
                      {step.result_summary.slice(0, 3).map((line) => <li key={line}>- {line}</li>)}
                    </ul>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      <div className="grid gap-4 xl:grid-cols-[1.5fr_1fr]">
        <Card>
          <CardHeader>
            <CardTitle>当前结果预览</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="rounded-xl border border-border/60 bg-background/70 p-4">
              <p className="text-xs text-muted-foreground">最新草稿 / 任务</p>
              <p className="mt-1 text-lg font-semibold">{latestTask ? taskTitle(latestTask) : "暂无任务"}</p>
              <p className="mt-3 whitespace-pre-line text-sm leading-7 text-muted-foreground">
                {latestTask ? shortText(taskBody(latestTask), 360) : "当前还没有可预览的标题和正文。"}
              </p>
              {latestTask ? (
                <Link href={`/tasks/${latestTask.id}`} className="mt-3 inline-block text-sm text-primary hover:underline">
                  打开这篇内容的完整详情
                </Link>
              ) : null}
            </div>
            <div className="grid gap-3 md:grid-cols-2">
              <div className="rounded-xl border border-border/60 bg-background/70 p-4">
                <p className="text-xs text-muted-foreground">最近已发布</p>
                <p className="mt-1 font-semibold">{latestPublished ? taskTitle(latestPublished) : "暂无"}</p>
                <p className="mt-2 text-xs text-muted-foreground">{latestPublished ? fmtTime(latestPublished.published_at || latestPublished.updated_at) : "等待发布"}</p>
                {latestPublished ? (
                  <Link href={`/tasks/${latestPublished.id}`} className="mt-2 inline-block text-sm text-primary hover:underline">
                    查看发布结果与复盘
                  </Link>
                ) : null}
              </div>
              <div className="rounded-xl border border-border/60 bg-background/70 p-4">
                <p className="text-xs text-muted-foreground">最近反馈</p>
                <p className="mt-1 font-semibold">{feedback[0]?.title || "暂无反馈"}</p>
                <p className="mt-2 text-xs text-muted-foreground">{feedback[0] ? `${feedback[0].feedback_state} · ${feedback[0].primary_bottleneck || "待判断"}` : "等待指标回收"}</p>
              </div>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>待审核内容</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {pendingReviewTasks.length === 0 ? (
              <div className="rounded-xl border border-border/60 bg-background/70 p-4 text-sm text-muted-foreground">当前没有待审核内容。</div>
            ) : (
              pendingReviewTasks.map((task) => (
                <div key={task.id} className="rounded-xl border border-border/60 bg-background/70 p-4">
                  <p className="font-semibold">{taskTitle(task)}</p>
                  <p className="mt-2 text-sm text-muted-foreground">{shortText(taskBody(task), 120)}</p>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Button type="button" size="sm" onClick={() => void handleApprove(task.id)} disabled={busy}>
                      <CheckCircle2 className="mr-1 h-4 w-4" />通过
                    </Button>
                    <Button type="button" size="sm" variant="outline" onClick={() => void handleReject(task.id)} disabled={busy}>
                      <XCircle className="mr-1 h-4 w-4" />打回
                    </Button>
                    <Link href={`/tasks/${task.id}`} className="inline-flex h-9 items-center rounded-md border border-border/60 px-3 text-sm text-primary hover:bg-muted/10">
                      编辑与发布
                    </Link>
                  </div>
                </div>
              ))
            )}
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-[1.15fr_1fr]">
        <Card>
          <CardHeader>
            <CardTitle>当前生效 SOP 与内容方法</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="rounded-xl border border-border/60 bg-background/70 p-4 text-sm">
              <p className="text-xs text-muted-foreground">当前快照</p>
              <p className="mt-1 font-semibold">
                {sopCurrent?.sop_latest ? `v${sopCurrent.sop_latest.version} · ${sopCurrent.sop_latest.reason || "未写原因"}` : "暂未生成 SOP 快照"}
              </p>
              <p className="mt-2 text-xs text-muted-foreground">
                {sopCurrent?.sop_latest ? `更新时间：${fmtTime(sopCurrent.sop_latest.captured_at)} · 变更：${sopCurrent.sop_latest.changed_fields.join(" / ") || "无"}` : "建议先跑完一轮采集、分析、文案、复盘后生成结构化 SOP。"}
              </p>
            </div>
            {(methodView?.sections || []).map((section) => (
              <div key={section.section_key} className="rounded-xl border border-border/60 bg-background/70 p-4">
                <p className="font-semibold">{section.title}</p>
                <p className="mt-2 text-sm text-muted-foreground">{section.summary}</p>
                <div className="mt-3 grid gap-3 md:grid-cols-2">
                  <div>
                    <p className="text-xs font-medium text-muted-foreground">方法细则</p>
                    <ul className="mt-1 space-y-1 text-sm">
                      {section.bullets.slice(0, 5).map((line) => <li key={line}>- {line}</li>)}
                    </ul>
                  </div>
                  <div>
                    <p className="text-xs font-medium text-muted-foreground">证据来源</p>
                    <ul className="mt-1 space-y-1 text-sm">
                      {section.evidence_sources.slice(0, 5).map((line) => <li key={line}>- {line}</li>)}
                    </ul>
                  </div>
                </div>
              </div>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>待确认策略动作</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {pendingStrategy.length === 0 ? (
              <div className="rounded-xl border border-border/60 bg-background/70 p-4 text-sm text-muted-foreground">当前没有待确认策略动作。</div>
            ) : (
              pendingStrategy.map((item) => (
                <div key={item.id} className="rounded-xl border border-border/60 bg-background/70 p-4">
                  <p className="font-semibold">{item.title}</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {affectedAgentLabel(item.affected_agent)} · {strategySourceLabel(item)} · 置信度 {item.confidence.toFixed(2)}
                  </p>
                  <p className="mt-2 text-sm text-muted-foreground">{shortText(item.content, 160)}</p>
                  {item.proposed_changes?.[0] ? (
                    <div className="mt-2 rounded-lg border border-border/50 bg-muted/10 p-3 text-xs text-muted-foreground">
                      <p>目标：{item.proposed_changes[0].label}</p>
                      <p className="mt-1">原因：{item.proposed_changes[0].reason}</p>
                      <p className="mt-1">改后：{item.proposed_changes[0].after_summary}</p>
                    </div>
                  ) : null}
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Button type="button" size="sm" disabled={busy || !accountId || !item.proposed_changes?.length} onClick={() => void handleStrategy(item, true)}>
                      <CheckCircle2 className="mr-1 h-4 w-4" />同意并应用
                    </Button>
                    <Button type="button" size="sm" variant="outline" disabled={busy || !accountId} onClick={() => void handleStrategy(item, false)}>
                      <XCircle className="mr-1 h-4 w-4" />不采用
                    </Button>
                  </div>
                  {!item.proposed_changes?.length ? <p className="mt-2 text-xs text-muted-foreground">此建议暂无可直接应用的配置变更。</p> : null}
                </div>
              ))
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>大模型配置与协同分工</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Bot className="h-4 w-4" />
            主入口：总教练 · 协同方式：总教练依次调度采集、分析、文案、复盘四个环节
          </div>
          <div className="grid gap-3 xl:grid-cols-4">
            {(agentConfig?.nodes || []).map((node) => (
              <div key={node.agent_key} className="rounded-xl border border-border/60 bg-background/70 p-4">
                <p className="font-semibold">{node.label}</p>
                <p className="mt-1 text-xs text-muted-foreground">负责环节：{node.label}</p>
                <p className="mt-3 text-sm">{node.role_summary}</p>
                <div className="mt-3 space-y-1 text-xs text-muted-foreground">
                  <p>模型：{node.model_name || "未配置"}</p>
                  <p>温度：{node.temperature ?? "未接入"}</p>
                  <p>提示词来源：{node.prompt_source || "未配置"}</p>
                  <p>提示词版本：{node.prompt_version || "未激活"}</p>
                  <p>上游：{node.upstream_inputs.join(" / ") || "无"}</p>
                  <p>下游：{node.downstream_outputs.join(" / ") || "无"}</p>
                  <p>工具：{node.allowed_tools.join(" / ") || "无"}</p>
                  <p>规则库：{node.knowledge_sources.join(" / ") || "无"}</p>
                </div>
                <div className="mt-3 rounded-lg border border-border/50 bg-muted/10 p-3 text-xs text-muted-foreground">
                  {node.prompt_preview || "暂无提示词预览"}
                </div>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      <div className="grid gap-4 xl:grid-cols-[0.9fr_1.1fr]">
        <Card>
          <CardHeader>
            <CardTitle>执行策略状态</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <div className="rounded-xl border border-border/60 bg-background/70 p-4">
              <div className="flex items-center gap-2 font-semibold"><Route className="h-4 w-4" />{routeStatus?.user_facing_status || "未加载"}</div>
              <p className="mt-2 text-muted-foreground">当前主方案：已准备</p>
              <p className="mt-1 text-muted-foreground">备用方案：{routeStatus?.backup_route ? "已准备" : "未准备"}</p>
              <p className="mt-1 text-muted-foreground">只读采集桥接：{routeStatus?.readonly_bridge ? "已准备" : "未准备"}</p>
            </div>
            <div className="rounded-xl border border-border/60 bg-background/70 p-4">
              <p className="font-semibold">方案切换规则</p>
              <ul className="mt-2 space-y-1 text-muted-foreground">
                {(routeStatus?.switch_rules || []).map((line) => <li key={line}>- {line}</li>)}
              </ul>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>最近环节执行</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {subagentRuns.length === 0 ? (
              <div className="rounded-xl border border-border/60 bg-background/70 p-4 text-sm text-muted-foreground">暂无最近执行记录。</div>
            ) : (
              subagentRuns.map((item) => (
                <div key={item.id} className="rounded-xl border border-border/60 bg-background/70 p-4">
                  <div className="flex items-center justify-between gap-2">
                    <p className="font-semibold">{agentLabel(item.actor)}</p>
                    <span className={`rounded-full border px-2 py-0.5 text-[11px] ${statusTone(item.status)}`}>{stageStatusLabel(item.status)}</span>
                  </div>
                  <p className="mt-1 text-xs text-muted-foreground">动作：{actionLabel(item.action_type, item.source)}</p>
                  <p className="mt-2 text-sm text-muted-foreground">{runSummary(item)}</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    时间：{fmtTime(item.updated_at)}
                    {item.retryable ? " · 可重试" : ""}
                  </p>
                </div>
              ))
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}



