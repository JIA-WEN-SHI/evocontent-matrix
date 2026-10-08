"use client";

import Link from "next/link";
import { use, useEffect, useState } from "react";

import { FoldableSection } from "@/components/foldable-section";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { getPipelineTask, getTaskAudits, getTaskFeedback } from "@/lib/api";
import type { AuditLog, PipelineTask, TaskFeedbackDetail } from "@/lib/types";
import { TaskActions } from "@/components/task-actions";
import { toUserFacingError } from "@/lib/user-facing-errors";

type Props = { params: Promise<{ id: string }> };

type UnknownRecord = Record<string, unknown>;

function readString(value: unknown, fallback = ""): string {
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}

function readNumber(value: unknown, fallback = 0): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

function readRecord(value: unknown): UnknownRecord {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as UnknownRecord) : {};
}

function readRecordArray(value: unknown): UnknownRecord[] {
  return Array.isArray(value)
    ? value.filter((item): item is UnknownRecord => Boolean(item) && typeof item === "object" && !Array.isArray(item))
    : [];
}

function toTextList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item ?? "").trim()).filter(Boolean);
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) return "暂无";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function shortText(value: unknown, max = 140): string {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  if (!text) return "暂无";
  return text.length > max ? `${text.slice(0, max)}...` : text;
}

function channelLabel(channel: string): string {
  const map: Record<string, string> = {
    xiaohongshu: "小红书",
    wechat_mp: "公众号",
    douyin: "抖音",
    video: "视频",
  };
  return map[channel] ?? channel ?? "未知渠道";
}

function statusLabel(status: string): string {
  const map: Record<string, string> = {
    queued: "待执行",
    intel_ready: "已拿到输入",
    drafting: "生成中",
    pending_review: "待审核",
    approved: "已通过审核",
    review_rejected: "已打回",
    publishing: "发布中",
    published: "已发布",
    publish_failed: "发布失败",
    metrics_ready: "已回收数据",
    reflecting: "复盘中",
    reflection_failed: "复盘失败",
    done: "已完成",
  };
  return map[status] ?? status ?? "未知状态";
}

function stageLabel(stage: string): string {
  const map: Record<string, string> = {
    feedback_pending: "等待反馈",
    feedback_collecting: "回收数据中",
    feedback_ready: "可复盘",
    reflecting: "复盘中",
    done: "已完成",
  };
  return map[stage] ?? stage ?? "暂无阶段";
}

function feedbackStateLabel(state: string): string {
  const map: Record<string, string> = {
    pending_identity: "等待帖子身份确认",
    pending_metrics: "等待真实指标",
    synthetic_preview_only: "仅有演示指标",
    metrics_ready: "指标已到齐",
    retro_ready: "可进入复盘",
    retro_done: "复盘已完成",
  };
  return map[state] ?? state ?? "暂无反馈状态";
}

function checkpointStatusLabel(status: "done" | "overdue" | "waiting"): string {
  if (status === "done") return "已完成";
  if (status === "overdue") return "已超时";
  return "等待中";
}

function metricCards(taskMetrics: UnknownRecord, feedbackDetail: TaskFeedbackDetail | null) {
  const feedbackAnalysis = readRecord(feedbackDetail?.feedback_analysis);
  const currentMetrics = readRecord(feedbackAnalysis.current_metrics);
  const postMetrics = readRecord(taskMetrics.post_metrics);
  const metrics = Object.keys(currentMetrics).length > 0 ? currentMetrics : Object.keys(postMetrics).length > 0 ? postMetrics : taskMetrics;
  function counter(value: unknown): number | string {
    const number = readNumber(value, NaN);
    return Number.isSafeInteger(number) && number >= 0 ? number : "未知";
  }

  return [
    { label: "点赞", value: counter(metrics.likes) },
    { label: "收藏", value: counter(metrics.collects) },
    { label: "评论", value: counter(metrics.comments_count) },
    { label: "分享", value: counter(metrics.shares) },
  ];
}

function metricModeLabel(mode: string): string {
  const map: Record<string, string> = {
    pending: "暂未拿到真实反馈",
    synthetic: "演示指标",
    observed: "真实观测指标",
    real: "真实观测指标",
  };
  return map[mode] ?? mode ?? "未标记";
}

function confidenceLabel(value: unknown): string {
  const num = readNumber(value, 0);
  if (!num) return "未标记";
  return `${Math.round(num * 100)}%`;
}

function renderBulletList(items: string[], emptyText: string) {
  if (items.length === 0) {
    return <p className="text-sm text-muted-foreground">{emptyText}</p>;
  }
  return (
    <ul className="space-y-2 text-sm leading-6 text-foreground/90">
      {items.map((item) => (
        <li key={item} className="rounded-lg border border-border/50 bg-background/40 px-3 py-2">
          {item}
        </li>
      ))}
    </ul>
  );
}

function renderKeyValueRows(record: UnknownRecord, emptyText: string) {
  const rows = Object.entries(record).filter(([, value]) => value !== null && value !== undefined && String(value).trim() !== "");
  if (rows.length === 0) {
    return <p className="text-sm text-muted-foreground">{emptyText}</p>;
  }
  return (
    <div className="grid gap-3 md:grid-cols-2">
      {rows.map(([key, value]) => (
        <div key={key} className="rounded-xl border border-border/50 bg-background/40 p-3">
          <p className="text-xs text-muted-foreground">{key}</p>
          <p className="mt-1 text-sm leading-6 text-foreground/90">{typeof value === "object" ? JSON.stringify(value) : String(value)}</p>
        </div>
      ))}
    </div>
  );
}

export default function TaskDetailPage({ params }: Props) {
  const { id } = use(params);
  const [task, setTask] = useState<PipelineTask | null>(null);
  const [audits, setAudits] = useState<AuditLog[]>([]);
  const [feedbackDetail, setFeedbackDetail] = useState<TaskFeedbackDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [warnings, setWarnings] = useState<string[]>([]);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    setWarnings([]);
    setTask(null);
    setAudits([]);
    setFeedbackDetail(null);
    void getPipelineTask(id).then((loaded) => {
      if (!cancelled) {
        setTask(loaded);
        setLoading(false);
      }
    }).catch((err: unknown) => {
      if (cancelled) return;
      const message = err instanceof Error ? err.message : "";
      const missing = (err && typeof err === "object" && "status" in err && err.status === 404) || /not found|不存在/i.test(message);
      setError(missing ? "未找到这篇任务，可能已被删除或链接无效。" : toUserFacingError(err, "任务加载失败，请稍后重试。"));
      setLoading(false);
    });
    void getTaskAudits(id).then((rows) => {
      if (!cancelled) setAudits(rows);
    }).catch(() => {
      if (!cancelled) setWarnings((items) => [...items, "执行轨迹暂时加载失败。"]);
    });
    void getTaskFeedback(id).then((detail) => {
      if (!cancelled) setFeedbackDetail(detail);
    }).catch(() => {
      if (!cancelled) setWarnings((items) => [...items, "反馈暂时不可用，当前展示任务中已记录的数据。"]);
    });
    return () => { cancelled = true; };
  }, [id, retry]);

  if (loading) return <p role="status" className="p-6">正在加载任务...</p>;
  if (!task) return (
    <div className="space-y-4 p-6">
      <h1 className="text-xl font-semibold">任务暂不可用</h1>
      <p role="alert">{error}</p>
      <button type="button" onClick={() => setRetry((value) => value + 1)} className="rounded border px-3 py-2">重新加载</button>
      <Link href="/legacy-flow" className="ml-4 text-primary hover:underline">返回流程执行</Link>
    </div>
  );

  const intent = readRecord(task.intent_jsonb);
  const payload = readRecord(task.payload_jsonb);
  const review = readRecord(task.review_jsonb);
  const publishMeta = readRecord(task.publish_jsonb);
  const metricsMeta = readRecord(task.metrics_jsonb);
  const analysis = readRecord(payload.analysis_jsonb);
  const isProjectObserver = payload.content_branch === "project_observer" || intent.content_branch === "project_observer";
  const optimizationHint = readRecord(analysis.optimization_hint || intent.optimization_hint);
  const publishResult = readRecord(publishMeta.last_result);
  const publishIdentity = readRecord(publishMeta.identity);

  const title = readString(payload.title, readString(intent.title, "未命名帖子"));
  const body = readString(payload.full_body, readString(payload.body, readString(payload.content, "暂无正文内容。")));
  const topic = readString(payload.topic, readString(intent.topic, "暂无主题"));
  const ctaQuestion = readString(payload.cta_question, readString(intent.cta_question, ""));
  const hashtags = Array.from(
    new Set([...toTextList(payload.tags), ...toTextList(payload.hashtags), ...toTextList(payload.topics)]),
  ).slice(0, 8);

  const logicLines = toTextList(analysis.logic);
  const structureHints = toTextList(analysis.structure_points);
  const titleReasons = toTextList(analysis.title_reasoning);
  const bodyReasons = toTextList(analysis.body_reasoning);
  const referenceItems = readRecordArray(analysis.reference_items);

  const bestTitle = readString(optimizationHint.best_title, "");
  const bestHint = readString(optimizationHint.best_hint, "");

  const publishedUrl = readString(
    publishResult.published_url,
    readString(publishIdentity.published_url, readString(publishMeta.published_url, "")),
  );
  const publishError = readString(publishResult.error, "");
  const publishMethod = readString(publishResult.publish_method, readString(publishMeta.publish_method, "未标记"));
  const publishAttempt = readNumber(publishResult.attempt, 0);

  const approvedBy = readString(review.approved_by, readString(review.auto_approved_by, ""));
  const approvedAt = readString(review.approved_at, readString(review.auto_approved_at, ""));
  const rejectedBy = readString(review.rejected_by, "");
  const rejectedAt = readString(review.rejected_at, "");
  const rejectionReason = readString(review.rejection_reason, "");
  const reviewNotes = readString(review.review_notes, "");

  const identity = readRecord(feedbackDetail?.identity);
  const feedbackAnalysis = readRecord(feedbackDetail?.feedback_analysis);
  const ces = readRecord(feedbackAnalysis.ces);
  const attribution = readRecord(feedbackAnalysis.attribution);
  const ee = readRecord(feedbackAnalysis.ee);
  const featureWeights = readRecord(feedbackAnalysis.feature_weights);
  const decisionSummary = readRecord(feedbackAnalysis.decision_summary);
  const promptTargets = readRecordArray(feedbackAnalysis.prompt_upgrade_targets);

  const checkpoints = Array.isArray(feedbackDetail?.schedule_hours) ? feedbackDetail!.schedule_hours : [];
  const completedCheckpoints = new Set(Array.isArray(feedbackDetail?.completed_hours) ? feedbackDetail!.completed_hours.map(Number) : []);
  const publishedAtForCheckpoint = feedbackDetail?.published_at ? new Date(feedbackDetail.published_at) : null;
  const checkpointTimeline =
    publishedAtForCheckpoint && !Number.isNaN(publishedAtForCheckpoint.getTime())
      ? checkpoints.map((hour) => {
          const dueAt = new Date(publishedAtForCheckpoint.getTime() + hour * 60 * 60 * 1000);
          const done = completedCheckpoints.has(hour);
          const overdue = !done && dueAt.getTime() <= Date.now();
          return {
            hour,
            dueAt: dueAt.toISOString(),
            status: done ? "done" : overdue ? "overdue" : "waiting",
          } as const;
        })
      : [];

  const resultMetrics = metricCards(metricsMeta, feedbackDetail);
  const currentMetrics = readRecord(feedbackAnalysis.current_metrics);
  const decisionWorked = toTextList(decisionSummary.what_worked);
  const decisionFailed = toTextList(decisionSummary.what_failed);
  const decisionNextActions = toTextList(decisionSummary.next_actions);
  const eeMix = readRecord(ee.next_mix);

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">单篇帖子详情</h1>
          <p className="text-sm text-muted-foreground">
            先看完整帖子，再看写作逻辑、发布状态、真实反馈和下一轮优化方向。
          </p>
        </div>
        <Link href="/legacy-flow" className="text-sm text-primary hover:underline">
          返回流程执行与手动修复
        </Link>
      </div>

      {warnings.length ? <div role="status" className="space-y-2 text-sm text-amber-500">
        {warnings.map((warning) => <p key={warning}>{warning}</p>)}
        <button type="button" onClick={() => setRetry((value) => value + 1)} className="underline">重新加载附加数据</button>
      </div> : null}
      <TaskActions key={task.id} task={task} onUpdated={setTask} />

      <Card>
        <CardHeader>
          <CardTitle>完整帖子</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="secondary">{channelLabel(task.channel)}</Badge>
            <Badge>{statusLabel(task.status)}</Badge>
            <Badge variant="outline">主题：{topic}</Badge>
            <Badge variant="outline">阶段：{stageLabel(feedbackDetail?.stage || task.stage || "")}</Badge>
            {feedbackDetail ? <Badge variant="outline">反馈：{feedbackStateLabel(feedbackDetail.feedback_state)}</Badge> : null}
          </div>

          <div className="rounded-2xl border border-border/60 bg-background/70 p-4">
            <p className="text-xs text-muted-foreground">标题</p>
            <p className="mt-2 text-2xl font-semibold leading-tight">{title}</p>
          </div>

          <div className="rounded-2xl border border-border/60 bg-background/70 p-4">
            <p className="text-xs text-muted-foreground">正文</p>
            <div className="mt-3 whitespace-pre-wrap text-sm leading-7 text-foreground/90">{body}</div>
          </div>

          <div className="grid gap-4 lg:grid-cols-[1.15fr_0.85fr]">
            <div className="rounded-2xl border border-border/60 bg-background/70 p-4">
              <p className="text-xs text-muted-foreground">标签与互动引导</p>
              <div className="mt-3 flex flex-wrap gap-2">
                {hashtags.length > 0 ? (
                  hashtags.map((tag) => (
                    <Badge key={tag} variant="outline">
                      {tag.startsWith("#") ? tag : `#${tag}`}
                    </Badge>
                  ))
                ) : (
                  <span className="text-sm text-muted-foreground">暂无标签</span>
                )}
              </div>
              <p className="mt-4 text-sm leading-6 text-muted-foreground">
                互动引导：{ctaQuestion || (isProjectObserver ? "无" : "当前未单独配置 CTA，默认使用正文最后一段的提问式收口。")}
              </p>
            </div>

            <div className="rounded-2xl border border-border/60 bg-background/70 p-4 text-sm text-muted-foreground">
              <p>创建时间：{formatDateTime(task.created_at)}</p>
              <p className="mt-1">最近更新时间：{formatDateTime(task.updated_at)}</p>
              <p className="mt-1">发布时间：{formatDateTime(task.published_at)}</p>
              <p className="mt-1">发布方式：{publishMethod}</p>
              {publishedUrl ? (
                <a href={publishedUrl} target="_blank" rel="noreferrer" className="mt-3 inline-block text-primary hover:underline">
                  查看发布链接
                </a>
              ) : null}
            </div>
          </div>
        </CardContent>
      </Card>

      <div className="grid gap-6 xl:grid-cols-[1.15fr_0.85fr]">
        <div className="min-w-0 space-y-6">
          <FoldableSection
            title="为什么这样写"
            summary="把标题逻辑、正文结构、最佳写法提示和真实参考样本放在一起，便于人工判断当前文案是否合理。"
            defaultOpen
          >
            <div className="space-y-5">
              <div className="grid gap-4 md:grid-cols-2">
                <div className="rounded-2xl border border-border/60 bg-background/70 p-4">
                  <p className="text-sm font-medium">标题写法依据</p>
                  <div className="mt-3">{renderBulletList(titleReasons, "当前没有单独沉淀标题原因，建议补齐标题策略说明。")}</div>
                </div>
                <div className="rounded-2xl border border-border/60 bg-background/70 p-4">
                  <p className="text-sm font-medium">正文写法依据</p>
                  <div className="mt-3">{renderBulletList(bodyReasons, "当前没有单独沉淀正文原因，建议补齐正文结构说明。")}</div>
                </div>
              </div>

              <div className="grid gap-4 md:grid-cols-2">
                <div className="rounded-2xl border border-border/60 bg-background/70 p-4">
                  <p className="text-sm font-medium">本轮写作逻辑</p>
                  <div className="mt-3">{renderBulletList(logicLines, "当前没有记录写作逻辑链。")}</div>
                </div>
                <div className="rounded-2xl border border-border/60 bg-background/70 p-4">
                  <p className="text-sm font-medium">结构提醒</p>
                  <div className="mt-3">{renderBulletList(structureHints, "当前没有记录结构提醒。")}</div>
                </div>
              </div>

              <div className="rounded-2xl border border-border/60 bg-background/70 p-4">
                <p className="text-sm font-medium">系统给出的更优提示</p>
                <div className="mt-3 grid gap-4 md:grid-cols-2">
                  <div>
                    <p className="text-xs text-muted-foreground">建议标题</p>
                    <p className="mt-1 text-sm leading-6 text-foreground/90">{bestTitle || "当前没有额外建议标题。"}</p>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">优化提醒</p>
                    <p className="mt-1 text-sm leading-6 text-foreground/90">{bestHint || "当前没有额外优化提醒。"}</p>
                  </div>
                </div>
              </div>

              <div className="rounded-2xl border border-border/60 bg-background/70 p-4">
                <p className="text-sm font-medium">参考样本</p>
                {referenceItems.length > 0 ? (
                  <div className="mt-3 overflow-x-auto">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>来源</TableHead>
                          <TableHead>抓取内容</TableHead>
                          <TableHead>采集时间</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {referenceItems.slice(0, 8).map((item, index) => {
                          const rawText = readString(item.raw_text, readString(item.title, "未命名样本"));
                          const sourceType = readString(item.source_type, "未标记来源");
                          const sourceUrl = readString(item.source_url, "");
                          return (
                            <TableRow key={`${sourceType}-${index}`}>
                              <TableCell className="align-top">
                                <div className="space-y-1">
                                  <p className="font-medium">{sourceType}</p>
                                  {sourceUrl ? (
                                    <a href={sourceUrl} target="_blank" rel="noreferrer" className="text-xs text-primary hover:underline">
                                      打开原始链接
                                    </a>
                                  ) : null}
                                </div>
                              </TableCell>
                              <TableCell className="align-top text-sm text-muted-foreground">{shortText(rawText, 120)}</TableCell>
                              <TableCell className="align-top text-sm text-muted-foreground">{formatDateTime(readString(item.captured_at, ""))}</TableCell>
                            </TableRow>
                          );
                        })}
                      </TableBody>
                    </Table>
                  </div>
                ) : (
                  <p className="mt-3 text-sm text-muted-foreground">当前没有附带参考样本，建议让分析环节补齐样本证据。</p>
                )}
              </div>
            </div>
          </FoldableSection>

          <FoldableSection
            title="发布与审核状态"
            summary="这里回答三个问题：有没有过审、有没有真正发出去、如果没发出去是卡在哪一步。"
            defaultOpen
          >
            <div className="grid gap-4 md:grid-cols-2">
              <div className="rounded-2xl border border-border/60 bg-background/70 p-4">
                <p className="text-sm font-medium">审核结果</p>
                <div className="mt-3 space-y-2 text-sm text-muted-foreground">
                  <p>通过人：{approvedBy || "暂无"}</p>
                  <p>通过时间：{formatDateTime(approvedAt)}</p>
                  <p>打回人：{rejectedBy || "暂无"}</p>
                  <p>打回时间：{formatDateTime(rejectedAt)}</p>
                </div>
                {rejectionReason ? (
                  <div className="mt-3 rounded-xl border border-rose-400/30 bg-rose-500/10 p-3 text-sm text-rose-100">
                    打回原因：{rejectionReason}
                  </div>
                ) : null}
                {reviewNotes ? (
                  <div className="mt-3 rounded-xl border border-border/50 bg-background/40 p-3 text-sm text-foreground/90">
                    审核备注：{reviewNotes}
                  </div>
                ) : null}
              </div>

              <div className="rounded-2xl border border-border/60 bg-background/70 p-4">
                <p className="text-sm font-medium">发布结果</p>
                <div className="mt-3 space-y-2 text-sm text-muted-foreground">
                  <p>发布状态：{statusLabel(readString(publishResult.status, task.status))}</p>
                  <p>执行方式：{publishMethod}</p>
                  <p>尝试次数：{publishAttempt || 0}</p>
                  <p>帖子身份：{readString(identity.feed_id, readString(identity.remote_post_id, "待确认"))}</p>
                </div>
                {publishError ? (
                  <div className="mt-3 rounded-xl border border-amber-400/30 bg-amber-500/10 p-3 text-sm text-amber-100">
                    当前卡点：{publishError}
                  </div>
                ) : null}
                {publishedUrl ? (
                  <a href={publishedUrl} target="_blank" rel="noreferrer" className="mt-3 inline-block text-sm text-primary hover:underline">
                    查看实际帖子链接
                  </a>
                ) : null}
              </div>
            </div>
          </FoldableSection>
        </div>

        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>当前结果</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-3 grid-cols-2">
                {resultMetrics.map((item) => (
                  <div key={item.label} className="rounded-xl border border-border/60 bg-background/70 p-3">
                    <p className="text-xs text-muted-foreground">{item.label}</p>
                    <p className="mt-1 text-xl font-semibold">{item.value}</p>
                  </div>
                ))}
              </div>
              <div className="rounded-xl border border-border/60 bg-background/70 p-4 text-sm text-muted-foreground">
                <p>反馈阶段：{feedbackDetail ? feedbackStateLabel(feedbackDetail.feedback_state) : "暂无反馈记录"}</p>
                <p className="mt-1">指标口径：{metricModeLabel(readString(feedbackAnalysis.metrics_mode, "pending"))}</p>
                <p className="mt-1">下一次反馈时间：{formatDateTime(feedbackDetail?.next_feedback_at)}</p>
                <p className="mt-1">上一次反馈时间：{formatDateTime(feedbackDetail?.last_feedback_at)}</p>
              </div>
            </CardContent>
          </Card>

          <FoldableSection
            title="复盘结论"
            summary="这里不是只看指标，而是告诉你本轮哪里有效、哪里失效、下一轮该怎么改。"
            defaultOpen
          >
            <div className="space-y-4">
              <div className="grid gap-4 md:grid-cols-2">
                <div className="rounded-2xl border border-border/60 bg-background/70 p-4">
                  <p className="text-sm font-medium">CES 结论</p>
                  <div className="mt-3 space-y-2 text-sm text-muted-foreground">
                    <p>综合得分：{readNumber(ces.score, 0) || "暂无"}</p>
                    <p>主要瓶颈：{readString(attribution.primary_bottleneck, "暂无")}</p>
                    <p>探索/利用模式：{readString(ee.mode, "暂无")}</p>
                    <p>建议配比：{Object.keys(eeMix).length > 0 ? JSON.stringify(eeMix) : "暂无"}</p>
                  </div>
                </div>
                <div className="rounded-2xl border border-border/60 bg-background/70 p-4">
                  <p className="text-sm font-medium">决策摘要</p>
                  <div className="mt-3 space-y-3">
                    <div>
                      <p className="text-xs text-muted-foreground">有效点</p>
                      <div className="mt-2">{renderBulletList(decisionWorked, "当前没有记录有效点。")}</div>
                    </div>
                    <div>
                      <p className="text-xs text-muted-foreground">失效点</p>
                      <div className="mt-2">{renderBulletList(decisionFailed, "当前没有记录失效点。")}</div>
                    </div>
                    <div>
                      <p className="text-xs text-muted-foreground">下一轮动作</p>
                      <div className="mt-2">{renderBulletList(decisionNextActions, "当前没有生成下一轮动作。")}</div>
                    </div>
                  </div>
                </div>
              </div>

              <div className="rounded-2xl border border-border/60 bg-background/70 p-4">
                <p className="text-sm font-medium">反馈检查点</p>
                {checkpointTimeline.length > 0 ? (
                  <div className="mt-3 overflow-x-auto">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>检查点</TableHead>
                          <TableHead>应到时间</TableHead>
                          <TableHead>状态</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {checkpointTimeline.map((item) => (
                          <TableRow key={item.hour}>
                            <TableCell>{item.hour} 小时</TableCell>
                            <TableCell>{formatDateTime(item.dueAt)}</TableCell>
                            <TableCell>{checkpointStatusLabel(item.status)}</TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                ) : (
                  <p className="mt-3 text-sm text-muted-foreground">当前没有可展示的反馈检查点。</p>
                )}
              </div>

              <div className="rounded-2xl border border-border/60 bg-background/70 p-4">
                <p className="text-sm font-medium">特征权重与提示词升级方向</p>
                <div className="mt-3 space-y-4">
                  <div>
                    <p className="text-xs text-muted-foreground">特征权重</p>
                    <div className="mt-2">{renderKeyValueRows(featureWeights, "当前没有沉淀特征权重。")}</div>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">提示词升级目标</p>
                    {promptTargets.length > 0 ? (
                      <div className="mt-2 space-y-3">
                        {promptTargets.map((item, index) => (
                          <div key={index} className="rounded-xl border border-border/50 bg-background/40 p-3">
                            <p className="text-sm font-medium">{readString(item.title, `升级目标 ${index + 1}`)}</p>
                            <p className="mt-1 text-sm text-muted-foreground">{readString(item.reason, "暂无原因说明")}</p>
                            <p className="mt-2 text-xs text-muted-foreground">置信度：{confidenceLabel(item.confidence)}</p>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <p className="mt-2 text-sm text-muted-foreground">当前没有生成提示词升级目标。</p>
                    )}
                  </div>
                </div>
              </div>
            </div>
          </FoldableSection>
        </div>
      </div>

      <FoldableSection
        title="原始执行轨迹"
        summary="保留人工排查需要的信息，但只展示用户能理解的动作和变化，不暴露内部错误码。"
      >
        {audits.length > 0 ? (
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>时间</TableHead>
                  <TableHead>动作</TableHead>
                  <TableHead>执行人</TableHead>
                  <TableHead>变化摘要</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {audits.map((audit: AuditLog) => (
                  <TableRow key={audit.id}>
                    <TableCell>{formatDateTime(audit.created_at)}</TableCell>
                    <TableCell>{audit.action}</TableCell>
                    <TableCell>{audit.actor}</TableCell>
                    <TableCell className="max-w-[560px] text-sm text-muted-foreground">
                      {shortText(JSON.stringify(audit.diff_jsonb || {}), 220)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">当前没有可展示的执行轨迹。</p>
        )}
      </FoldableSection>

      {Object.keys(currentMetrics).length > 0 ? (
        <FoldableSection title="当前观测指标明细" summary="保留当前时点的真实观测值，方便人工判断本轮内容是否值得继续放大。">
          {renderKeyValueRows(currentMetrics, "暂无当前观测指标。")}
        </FoldableSection>
      ) : null}
    </div>
  );
}
