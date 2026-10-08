"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { ArrowUpRight, CircleDot } from "lucide-react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { DEFAULT_DOMAIN_SLUG, getAuditLogs, getOpsOverview, getPipelineTasks, getRuntimeContext } from "@/lib/api";
import { toUserFacingError } from "@/lib/user-facing-errors";
import type { AuditLog, OpsOverview, PipelineTask, RuntimeContext } from "@/lib/types";

type TrendItem = {
  day: string;
  drafted: number;
  published: number;
};

function dayKey(date: Date): string {
  return date.toISOString().slice(0, 10);
}

function dayLabel(date: Date): string {
  return `${date.getMonth() + 1}/${date.getDate()}`;
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
    queued: "排队中",
    intel_ready: "输入已就绪",
    drafting: "生成中",
    pending_review: "待审核",
    approved: "已通过审核",
    review_rejected: "已打回",
    publishing: "发布中",
    published: "已发布",
    publish_failed: "发布失败",
    metrics_ready: "指标已回收",
    reflecting: "复盘中",
    reflection_failed: "复盘失败",
    done: "已完成",
  };
  return map[status] ?? status ?? "未知状态";
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) return "暂无";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function shortText(value: string | null | undefined, max = 80): string {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (!text) return "暂无";
  return text.length > max ? `${text.slice(0, max)}...` : text;
}

function sourceTypeLabel(sourceType: string): string {
  const key = (sourceType || "").toLowerCase();
  if (key.includes("xiaohongshu") && key.includes("feed")) return "小红书首页流";
  if (key.includes("xiaohongshu")) return "小红书搜索";
  if (key.includes("google")) return "Google 新闻";
  if (key.includes("viewpoint")) return "观点采集";
  return sourceType || "未知来源";
}

function scoreTask(task: PipelineTask): number {
  const metrics = task.metrics_jsonb || {};
  const likes = Number(metrics.likes || 0);
  const collects = Number(metrics.collects || 0);
  const comments = Number(metrics.comments_count || 0);
  const shares = Number(metrics.shares || 0);
  return likes + collects * 2 + comments * 2 + shares * 2;
}

function taskTitle(task: PipelineTask): string {
  return String(task.payload_jsonb?.title || task.intent_jsonb?.title || task.payload_jsonb?.topic || "未命名内容");
}

export default function DashboardPage() {
  const [tasks, setTasks] = useState<PipelineTask[]>([]);
  const [audits, setAudits] = useState<AuditLog[]>([]);
  const [overview, setOverview] = useState<OpsOverview | null>(null);
  const [runtimeContext, setRuntimeContext] = useState<RuntimeContext | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function loadAll() {
    setLoading(true);
    setError(null);
    try {
      const results = await Promise.allSettled([
        getPipelineTasks({ domainSlug: DEFAULT_DOMAIN_SLUG, limit: 200 }),
        getAuditLogs(),
        getOpsOverview(DEFAULT_DOMAIN_SLUG, 20),
        getRuntimeContext(DEFAULT_DOMAIN_SLUG),
      ]);
      const [taskRows, auditRows, ops, runtime] = results;
      if (taskRows.status === "fulfilled") setTasks(taskRows.value);
      if (auditRows.status === "fulfilled") setAudits(auditRows.value);
      if (ops.status === "fulfilled") setOverview(ops.value);
      if (runtime.status === "fulfilled") setRuntimeContext(runtime.value);
      if (results.some((result) => result.status === "rejected")) {
        setError("部分数据暂时无法加载，已显示可用结果，请稍后刷新。");
      }
    } catch (err) {
      setError(toUserFacingError(err, "加载数据总览失败，请稍后刷新。"));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadAll();
  }, []);

  const metrics = useMemo(() => {
    const now = new Date();
    const sevenDaysAgo = new Date(now);
    sevenDaysAgo.setDate(now.getDate() - 6);
    const thirtyDaysAgo = new Date(now);
    thirtyDaysAgo.setDate(now.getDate() - 29);

    const pendingReviews = tasks.filter((task) => task.status === "pending_review").length;
    const published7d = tasks.filter(
      (task) => task.published_at && new Date(task.published_at) >= sevenDaysAgo && new Date(task.published_at) <= now,
    ).length;
    const done7d = tasks.filter(
      (task) => task.status === "done" && task.updated_at && new Date(task.updated_at) >= sevenDaysAgo && new Date(task.updated_at) <= now,
    ).length;
    const reflectionEligible7d = tasks.filter(
      (task) =>
        ["published", "metrics_ready", "reflecting", "reflection_failed", "done"].includes(task.status) &&
        task.updated_at &&
        new Date(task.updated_at) >= sevenDaysAgo &&
        new Date(task.updated_at) <= now,
    ).length;
    const reflectionWinRate = reflectionEligible7d > 0 ? Math.round((done7d / reflectionEligible7d) * 100) : 0;

    const tasks30d = tasks.filter(
      (task) => new Date(task.created_at) >= thirtyDaysAgo && new Date(task.created_at) <= now,
    ).length;

    return {
      pendingReviews,
      published7d,
      reflectionWinRate,
      tasks30d,
    };
  }, [tasks]);

  const chartData = useMemo<TrendItem[]>(() => {
    const now = new Date();
    const start = new Date(now);
    start.setDate(now.getDate() - 29);

    const draftedMap = new Map<string, number>();
    const publishedMap = new Map<string, number>();

    for (const task of tasks) {
      const createdAt = new Date(task.created_at);
      if (createdAt >= start && createdAt <= now) {
        const key = dayKey(createdAt);
        draftedMap.set(key, (draftedMap.get(key) ?? 0) + 1);
      }
      if (task.published_at) {
        const publishedAt = new Date(task.published_at);
        if (publishedAt >= start && publishedAt <= now) {
          const key = dayKey(publishedAt);
          publishedMap.set(key, (publishedMap.get(key) ?? 0) + 1);
        }
      }
    }

    const series: TrendItem[] = [];
    for (let offset = 0; offset < 30; offset += 1) {
      const date = new Date(start);
      date.setDate(start.getDate() + offset);
      const key = dayKey(date);
      series.push({
        day: dayLabel(date),
        drafted: draftedMap.get(key) ?? 0,
        published: publishedMap.get(key) ?? 0,
      });
    }
    return series;
  }, [tasks]);

  const recentPipelineEvents = useMemo(() => {
    return audits
      .filter((log) => log.target_type === "pipeline_task" && log.action.startsWith("pipeline."))
      .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
      .slice(0, 8)
      .map((log) => {
        const time = new Date(log.created_at).toLocaleString("zh-CN", { hour12: false });
        return `${time} · ${log.action}`;
      });
  }, [audits]);

  const sourceStats = useMemo(() => {
    const counter = new Map<string, number>();
    for (const item of overview?.reference_items ?? []) {
      const label = sourceTypeLabel(item.source_type);
      counter.set(label, (counter.get(label) ?? 0) + 1);
    }
    return Array.from(counter.entries())
      .map(([label, count]) => ({ label, count }))
      .sort((a, b) => b.count - a.count)
      .slice(0, 8);
  }, [overview]);

  const recentPublished = useMemo(() => {
    return tasks
      .filter((task) => ["published", "done"].includes(task.status))
      .sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime())
      .slice(0, 6);
  }, [tasks]);

  const topPerformers = useMemo(() => {
    return tasks
      .filter((task) => ["published", "done", "metrics_ready", "reflecting"].includes(task.status))
      .sort((a, b) => scoreTask(b) - scoreTask(a))
      .slice(0, 5);
  }, [tasks]);

  const needsAttention = useMemo(() => {
    return tasks
      .filter((task) => ["pending_review", "publish_failed", "reflection_failed", "review_rejected"].includes(task.status))
      .sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime())
      .slice(0, 5);
  }, [tasks]);

  const stats = [
    { label: "30天任务量", value: String(metrics.tasks30d), trend: "任务生成总数" },
    { label: "待审核数量", value: String(metrics.pendingReviews), trend: "人工审核队列" },
    { label: "7天已发布", value: String(metrics.published7d), trend: "渠道发布成功" },
    { label: "7天复盘完成率", value: `${metrics.reflectionWinRate}%`, trend: "已完成复盘 / 可复盘任务" },
  ];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-2xl font-semibold">数据总览</h2>
          <p className="text-sm text-muted-foreground">
            这里只看结果：整体表现、趋势、来源分布、最近表现好坏，不做执行和编辑。
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Link href="/" className="inline-flex h-10 items-center rounded-md border border-border/60 px-4 text-sm hover:bg-muted/10">
            返回主屏
          </Link>
          <Button onClick={() => void loadAll()} variant="outline">
            刷新总览
          </Button>
        </div>
      </div>

      {error ? (
        <div className="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">{error}</div>
      ) : null}

      {loading ? (
        <div className="rounded-lg border border-border/70 bg-card p-6 text-sm text-muted-foreground">正在加载总览...</div>
      ) : null}

      <section className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">当前账号</CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">
            {runtimeContext?.publish_account.account_name || "未配置"}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">最近采集</CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">
            {runtimeContext?.latest.last_xhs_intel_at ? formatDateTime(runtimeContext.latest.last_xhs_intel_at) : "暂无"}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">最近发布</CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">
            {runtimeContext?.latest.last_published_at ? formatDateTime(runtimeContext.latest.last_published_at) : "暂无"}
          </CardContent>
        </Card>
      </section>

      <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        {stats.map((item) => (
          <Card key={item.label} className="overflow-hidden">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">{item.label}</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="flex items-end justify-between gap-2">
                <p className="text-4xl font-bold tracking-tight">{item.value}</p>
                <Badge variant="secondary" className="gap-1">
                  <ArrowUpRight className="h-3.5 w-3.5" />
                  {item.trend}
                </Badge>
              </div>
            </CardContent>
          </Card>
        ))}
      </section>

      <section className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>近30天草稿与发布趋势</CardTitle>
          </CardHeader>
          <CardContent className="h-[320px]">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={chartData} margin={{ top: 10, right: 12, left: 0, bottom: 0 }}>
                <defs>
                  <linearGradient id="draftedGradient" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#0e7490" stopOpacity={0.35} />
                    <stop offset="95%" stopColor="#0e7490" stopOpacity={0.05} />
                  </linearGradient>
                  <linearGradient id="publishedGradient" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#c27803" stopOpacity={0.3} />
                    <stop offset="95%" stopColor="#c27803" stopOpacity={0.04} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                <XAxis dataKey="day" tick={{ fontSize: 12 }} />
                <YAxis tick={{ fontSize: 12 }} />
                <Tooltip />
                <Legend />
                <Area
                  type="monotone"
                  dataKey="drafted"
                  name="草稿生成量"
                  stroke="#0e7490"
                  fillOpacity={1}
                  fill="url(#draftedGradient)"
                  strokeWidth={2}
                />
                <Area
                  type="monotone"
                  dataKey="published"
                  name="发布量"
                  stroke="#c27803"
                  fillOpacity={1}
                  fill="url(#publishedGradient)"
                  strokeWidth={2}
                />
              </AreaChart>
            </ResponsiveContainer>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>最近流程事件</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {recentPipelineEvents.length === 0 ? (
              <p className="text-sm text-muted-foreground">暂无流程事件。</p>
            ) : (
              recentPipelineEvents.map((item) => (
                <div key={item} className="rounded-md border border-border/70 bg-card p-3">
                  <div className="flex items-start gap-2">
                    <CircleDot className="mt-1 h-3.5 w-3.5 text-emerald-500" />
                    <p className="text-sm leading-6">{item}</p>
                  </div>
                </div>
              ))
            )}
          </CardContent>
        </Card>
      </section>

      <section className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle>渠道表现</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {!overview ? (
              <p className="text-sm text-muted-foreground">暂无数据。</p>
            ) : Object.keys(overview.channel_stats).length === 0 ? (
              <p className="text-sm text-muted-foreground">暂无渠道结果。</p>
            ) : (
              Object.entries(overview.channel_stats).map(([channel, item]) => (
                <div key={channel} className="rounded-md border border-border/70 p-3">
                  <p className="text-sm font-medium">{channelLabel(channel)}</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    总任务 {item.total} | 待审核 {item.pending_review} | 已发布 {item.published} | 已完成 {item.done}
                  </p>
                  <p className="mt-1 text-xs text-muted-foreground">失败 {item.failed} | 7天发布 {item.published_7d}</p>
                </div>
              ))
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>采集来源分布</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {sourceStats.length === 0 ? (
              <p className="text-sm text-muted-foreground">暂无来源数据。</p>
            ) : (
              sourceStats.map((item) => (
                <div key={item.label} className="rounded-md border border-border/70 p-3">
                  <p className="text-sm font-medium">{item.label}</p>
                  <p className="mt-1 text-xs text-muted-foreground">{item.count} 条可用输入</p>
                </div>
              ))
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>最近已发布</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {recentPublished.length === 0 ? (
              <p className="text-sm text-muted-foreground">最近还没有已发布内容。</p>
            ) : (
              recentPublished.map((task) => (
                <Link key={task.id} href={`/tasks/${task.id}`} className="block rounded-md border border-border/70 p-3 transition hover:border-primary/50 hover:bg-muted/10">
                  <p className="text-sm font-medium">{shortText(taskTitle(task), 56)}</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {channelLabel(task.channel)} · {statusLabel(task.status)} · {formatDateTime(task.published_at || task.updated_at)}
                  </p>
                </Link>
              ))
            )}
          </CardContent>
        </Card>
      </section>

      <section className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>最近表现较好的内容</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {topPerformers.length === 0 ? (
              <p className="text-sm text-muted-foreground">暂无可比较的结果数据。</p>
            ) : (
              topPerformers.map((task) => {
                const metrics = task.metrics_jsonb || {};
                return (
                  <Link key={task.id} href={`/tasks/${task.id}`} className="block rounded-md border border-border/70 p-3 transition hover:border-primary/50 hover:bg-muted/10">
                    <p className="text-sm font-medium">{shortText(taskTitle(task), 64)}</p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      点赞 {Number(metrics.likes || 0)} · 收藏 {Number(metrics.collects || 0)} · 评论 {Number(metrics.comments_count || 0)}
                    </p>
                  </Link>
                );
              })
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>当前需要关注的内容</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {needsAttention.length === 0 ? (
              <p className="text-sm text-muted-foreground">当前没有需要特别关注的异常内容。</p>
            ) : (
              needsAttention.map((task) => (
                <Link key={task.id} href={`/tasks/${task.id}`} className="block rounded-md border border-border/70 p-3 transition hover:border-primary/50 hover:bg-muted/10">
                  <p className="text-sm font-medium">{shortText(taskTitle(task), 64)}</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {statusLabel(task.status)} · {channelLabel(task.channel)} · {formatDateTime(task.updated_at)}
                  </p>
                </Link>
              ))
            )}
          </CardContent>
        </Card>
      </section>
    </div>
  );
}
