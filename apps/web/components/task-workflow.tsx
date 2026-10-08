"use client";

import { useEffect, useRef, useState } from "react";
import { Check, RefreshCw, Trash2, Upload } from "lucide-react";
import { DEFAULT_DOMAIN_SLUG, detachDraftImage, getDraftImage, registerManualPublication, saveTaskObservation, syncTaskFeedback, uploadDraftImage } from "@/lib/api";
import type { PipelineTask } from "@/lib/types";
import { toUserFacingError } from "@/lib/user-facing-errors";

const counters = [["views", "曝光"], ["likes", "点赞"], ["collects", "收藏"], ["comments_count", "评论"], ["shares", "分享"], ["followers_delta", "涨粉"]] as const;
const field = "mt-1 w-full rounded border border-current/25 bg-transparent p-2 text-sm";
const command = "inline-flex items-center gap-1 rounded border border-current/25 px-3 py-2 text-sm disabled:opacity-40";

function localTime(value?: string | null) {
  const date = value ? new Date(value) : new Date();
  if (!Number.isFinite(date.getTime())) return "";
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

function DraftImage({ taskId, accountId, imageId }: { taskId: string; accountId: string; imageId: string }) {
  const [url, setUrl] = useState("");
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    let objectUrl = "";
    setFailed(false);
    setUrl("");
    void getDraftImage(taskId, accountId, imageId, controller.signal).then((blob) => {
      if (!active) return;
      objectUrl = URL.createObjectURL(blob);
      setUrl(objectUrl);
    }).catch(() => { if (active) setFailed(true); });
    return () => { active = false; controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [taskId, accountId, imageId]);
  return url ? <img src={url} alt="草稿配图" className="aspect-square w-full object-contain" /> :
    <div className="flex aspect-square items-center justify-center text-xs opacity-60">{failed ? "图片读取失败" : "图片加载中"}</div>;
}

export function TaskWorkflow({ task, onUpdated, disabled = false, onBusyChange }: { task: PipelineTask; onUpdated: (task: PipelineTask) => void; disabled?: boolean; onBusyChange?: (busy: boolean) => void }) {
  const accountId = String(task.account_id || task.payload_jsonb?.channel_account_id || "");
  const identity = (task.publish_jsonb?.identity || {}) as Record<string, unknown>;
  const media = (task.payload_jsonb?.draft_media || {}) as Record<string, unknown>;
  const images = (Array.isArray(media.images) ? media.images : []).filter((image) => image && image.status === "attached") as { id: string }[];
  const metrics = (task.metrics_jsonb?.post_metrics || {}) as Record<string, unknown>;
  const draftAnalysis = (task.payload_jsonb?.analysis_jsonb || {}) as Record<string, unknown>;
  const branch = (draftAnalysis.content_branch || {}) as Record<string, unknown>;
  const projectSource = (branch.project_reference || {}) as Record<string, unknown>;
  const sourceUrl = String(projectSource.source_url || "");
  const editable = !task.published_at && ["queued", "intel_ready", "pending_review", "review_rejected", "approved", "publish_failed"].includes(task.status);
  const [url, setUrl] = useState(String(identity.published_url || ""));
  const [publishedAt, setPublishedAt] = useState(localTime(task.published_at));
  const [confirmed, setConfirmed] = useState(false);
  const [reason, setReason] = useState("");
  const [correction, setCorrection] = useState(false);
  const [observedAt, setObservedAt] = useState(localTime());
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const lock = useRef(false);
  const scope = useRef(task.id);
  scope.current = task.id;
  useEffect(() => { setUrl(String(identity.published_url || "")); setPublishedAt(localTime(task.published_at)); setConfirmed(false); }, [task.published_at, identity.published_url]);

  async function perform(operation: () => Promise<PipelineTask & { operation_warning?: string }>, success: string) {
    if (lock.current || disabled || !accountId) return;
    const submittedTask = task.id;
    lock.current = true; setBusy(true); setError(""); setNotice("");
    onBusyChange?.(true);
    try {
      const updated = await operation();
      if (scope.current !== submittedTask) return;
      onUpdated(updated);
      setNotice(updated.operation_warning || success);
      setConfirmed(false); setCorrection(false);
    } catch (err) {
      if (scope.current === submittedTask) setError(toUserFacingError(err, "操作未确认，请刷新任务核对；当前输入已保留。"));
    } finally { lock.current = false; if (scope.current === submittedTask) { setBusy(false); onBusyChange?.(false); } }
  }

  function register() {
    const timestamp = new Date(publishedAt);
    if (!confirmed || !Number.isFinite(timestamp.getTime()) || timestamp.getTime() > Date.now()) {
      setError("请确认归属，并填写实际发布时间，不能选择未来时间。"); return;
    }
    void perform(() => registerManualPublication(task.id, { account_id: accountId, domain_slug: DEFAULT_DOMAIN_SLUG,
      published_url: url.trim(), published_at: timestamp.toISOString(), confirmed, correction_reason: reason.trim() }, correction), "手动发布已登记，等待真实反馈。");
  }

  function saveObservation() {
    const parsed: Record<string, number> = {};
    for (const [key] of counters) {
      const raw = values[key]?.trim();
      if (!raw) continue;
      const number = Number(raw);
      if (!Number.isSafeInteger(number) || (key !== "followers_delta" && number < 0)) { setError("观测数值必须是整数，涨粉以外不能为负数。"); return; }
      parsed[key] = number;
    }
    const timestamp = new Date(observedAt);
    if (!Object.keys(parsed).length || !Number.isFinite(timestamp.getTime()) || timestamp.getTime() > Date.now()) { setError("请填写至少一项真实观测及观测时间。"); return; }
    void perform(() => saveTaskObservation(task.id, { account_id: accountId, domain_slug: DEFAULT_DOMAIN_SLUG,
      values: parsed, observed_at: timestamp.toISOString(), provenance: "用户从创作者后台补录" }), "观测已保存。");
  }

  if (!accountId) return null;
  return <section aria-label="发布与反馈" className="min-w-0 space-y-4 border-t border-current/15 pt-4">
    {error ? <p role="alert" className="text-sm text-red-400">{error}</p> : null}
    {notice ? <p role="status" className="text-sm text-emerald-400">{notice}</p> : null}
    {branch.id === "project_observer" ? <details className="min-w-0 border-b border-current/15 pb-3 text-sm">
      <summary className="cursor-pointer font-medium">{String(branch.name || "AI 项目观察·共情短文")} · v{String(branch.version || "1.0")}</summary>
      <div className="mt-3 space-y-3">
        <p className="text-xs opacity-70">职业反应：设想画面 · 人工审核</p>
        {sourceUrl.startsWith("https://") ? <a href={sourceUrl} target="_blank" rel="noreferrer" className="block break-all underline">项目来源</a> : null}
        <p className="text-xs opacity-60">来源核对记录：{projectSource.checked_at ? new Date(String(projectSource.checked_at)).toLocaleString("zh-CN") : "未记录"}</p>
        {projectSource.source_notes ? <p className="whitespace-pre-wrap text-xs opacity-70">来源备注：{String(projectSource.source_notes)}</p> : null}
        <div className="whitespace-pre-wrap break-words text-xs leading-6 opacity-75">{String(branch.rules || "")}</div>
      </div>
    </details> : null}
    {disabled ? <p className="text-sm opacity-60">请先保存正文修改，再操作配图或登记发布。</p> : null}
    <fieldset disabled={busy || disabled} className="min-w-0 space-y-4 disabled:opacity-60">
      <div className="space-y-2">
        <h3 className="text-sm font-semibold">草稿配图 <span className="font-normal opacity-60">{images.length}/9</span></h3>
        {!images.length ? <p className="text-sm opacity-60">暂无配图</p> : <div className="grid grid-cols-3 gap-2">
          {images.map((image) => <div key={image.id} className="min-w-0">
            <DraftImage taskId={task.id} accountId={accountId} imageId={image.id} />
            {editable ? <button type="button" title="移除配图" aria-label="移除配图" className={command} onClick={() => void perform(() => detachDraftImage(task.id, accountId, image.id), "配图已移除，原文件保留。请重新审核。")}><Trash2 className="h-4 w-4" /></button> : null}
          </div>)}
        </div>}
        {editable ? <label className="block text-sm"><span className="inline-flex items-center gap-1"><Upload className="h-4 w-4" />上传配图</span>
          <input type="file" aria-label="上传草稿配图" accept="image/png,image/jpeg,image/webp" disabled={images.length >= 9} className="mt-2 block max-w-full text-xs" onChange={(event) => {
            const file = event.target.files?.[0]; event.target.value = "";
            if (file) void perform(() => uploadDraftImage(task.id, accountId, file), "配图已保存，请重新审核。");
          }} />
        </label> : null}
      </div>
      {(task.status === "approved" && !task.published_at) || correction ? <div className="space-y-3 border-t border-current/15 pt-3">
        <h3 className="text-sm font-semibold">{correction ? "更正发布登记" : "登记手动发布"}</h3>
        <label className="block text-sm">发布链接<input type="url" required value={url} onChange={(e) => setUrl(e.target.value)} className={field} /></label>
        <label className="block text-sm">实际发布时间<input type="datetime-local" required value={publishedAt} onChange={(e) => setPublishedAt(e.target.value)} className={field} /></label>
        {correction ? <label className="block text-sm">纠错原因<input value={reason} onChange={(e) => setReason(e.target.value)} className={field} /></label> : null}
        <label className="flex items-start gap-2 text-sm"><input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} className="mt-1" />我确认这是本账号已自行发布的笔记</label>
        <button type="button" className={command} disabled={!confirmed || !url.trim() || (correction && reason.trim().length < 3)} onClick={register}><Check className="h-4 w-4" />确认登记</button>
      </div> : null}
      {task.published_at ? <div className="space-y-3 border-t border-current/15 pt-3">
        <h3 className="text-sm font-semibold">发布反馈</h3>
        <p className="break-all text-xs opacity-70">{String(identity.published_url || "发布链接未登记")}<br />发布于 {new Date(task.published_at).toLocaleString("zh-CN")}</p>
        {task.metrics_jsonb?.post_metrics_synced_at ? <p className="text-xs opacity-60">观测于 {new Date(String(task.metrics_jsonb.post_metrics_synced_at)).toLocaleString("zh-CN")}</p> : null}
        <div className="grid grid-cols-3 gap-2 text-xs">{counters.map(([key, label]) => <div key={key}>{label}<span className="ml-2 font-medium">{typeof metrics[key] === "number" ? String(metrics[key]) : "未知"}</span></div>)}</div>
        <div className="flex flex-wrap gap-2">
          <button type="button" className={command} onClick={() => void perform(() => syncTaskFeedback(task.id, accountId, DEFAULT_DOMAIN_SLUG), "真实反馈已保存。")}><RefreshCw className="h-4 w-4" />同步真实反馈</button>
          <button type="button" className={command} onClick={() => setCorrection(!correction)}>更正登记</button>
        </div>
        <details><summary className="cursor-pointer text-sm">补录观测</summary><div className="mt-3 space-y-3">
          <div className="grid grid-cols-2 gap-3">{counters.map(([key, label]) => <label key={key} className="text-xs">{label}<input type="number" step="1" min={key === "followers_delta" ? undefined : 0} placeholder="未知" value={values[key] || ""} onChange={(e) => setValues({ ...values, [key]: e.target.value })} className={field} /></label>)}</div>
          <label className="block text-sm">观测时间<input type="datetime-local" value={observedAt} onChange={(e) => setObservedAt(e.target.value)} className={field} /></label>
          <button type="button" className={command} onClick={saveObservation}><Check className="h-4 w-4" />保存观测</button>
        </div></details>
      </div> : null}
    </fieldset>
  </section>;
}
