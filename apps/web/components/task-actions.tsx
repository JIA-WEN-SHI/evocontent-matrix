"use client";

import { useEffect, useRef, useState } from "react";
import { Check, Loader2, Play, Save, X } from "lucide-react";
import { approvePipelineTask, getPipelineTask, rejectPipelineTask, runPipelineTask, updatePipelineTask } from "@/lib/api";
import type { PipelineTask } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { toUserFacingError } from "@/lib/user-facing-errors";
import { TaskWorkflow } from "@/components/task-workflow";

type Action = "approve" | "reject" | "run";

export function TaskActions({ task: initialTask, onUpdated }: {
  task: PipelineTask;
  onUpdated?: (task: PipelineTask) => void;
}) {
  const [task, setTask] = useState(initialTask);
  const [title, setTitle] = useState(String(initialTask.payload_jsonb?.title || initialTask.payload_jsonb?.topic || ""));
  const [body, setBody] = useState(String(initialTask.payload_jsonb?.full_body || initialTask.payload_jsonb?.body || initialTask.payload_jsonb?.content || ""));
  const [reason, setReason] = useState("");
  const [confirm, setConfirm] = useState<Action | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [runSubmitted, setRunSubmitted] = useState(false);
  const lock = useRef(false);
  const editable = !task.published_at && ["queued", "intel_ready", "pending_review", "review_rejected", "approved", "publish_failed"].includes(task.status);
  const reviewable = task.status === "pending_review" && !task.published_at;
  const generating = ["queued", "intel_ready", "review_rejected"].includes(task.status);
  const runnable = !task.published_at && generating;
  const valid = Boolean(title.trim() && body.trim());
  const dirty = title !== String(task.payload_jsonb?.title || task.payload_jsonb?.topic || "") ||
    body !== String(task.payload_jsonb?.full_body || task.payload_jsonb?.body || task.payload_jsonb?.content || "");

  useEffect(() => {
    if (busy || dirty) return;
    setTask(initialTask);
    setTitle(String(initialTask.payload_jsonb?.title || initialTask.payload_jsonb?.topic || ""));
    setBody(String(initialTask.payload_jsonb?.full_body || initialTask.payload_jsonb?.body || initialTask.payload_jsonb?.content || ""));
  }, [initialTask, busy, dirty]);

  function accept(updated: PipelineTask) {
    setTask(updated);
    setTitle(String(updated.payload_jsonb?.title || updated.payload_jsonb?.topic || ""));
    setBody(String(updated.payload_jsonb?.full_body || updated.payload_jsonb?.body || updated.payload_jsonb?.content || ""));
  }

  async function execute(action: Action | "save") {
    if (lock.current || runSubmitted) return;
    if (action === "reject" && reason.trim().length < 2) {
      setError("请填写至少两个字的驳回原因。");
      return;
    }
    if (action !== "reject" && !(action === "run" && generating) && !valid) {
      setError("标题和正文不能为空。");
      return;
    }
    lock.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    let updated = task;
    try {
      if (action === "save") {
        updated = await updatePipelineTask(task.id, { title: title.trim(), body: body.trim() });
        accept(updated);
      }
      if (action === "approve") {
        updated = await approvePipelineTask(task.id, { title: title.trim(), body: body.trim() });
        accept(updated);
        setNotice("草稿已通过审核，请自行发布。");
      }
      if (action === "reject") {
        updated = await rejectPipelineTask(task.id, reason.trim());
        accept(updated);
        setNotice("已驳回并保存原因。");
      }
      if (action === "run" && generating) {
        await runPipelineTask(task.id);
        setRunSubmitted(true);
        setNotice("执行请求已提交，请刷新状态查看结果。");
        try {
          updated = await getPipelineTask(task.id);
          accept(updated);
          if (updated.status === "pending_review") setNotice("草稿已生成，等待审核。");
        } catch {
          setNotice("执行请求已提交，但状态暂未读取成功。请刷新状态，勿重复提交。");
        }
      }
      if (action === "save") setNotice("草稿已保存。");
      setConfirm(null);
      onUpdated?.(updated);
    } catch (err) {
      if (updated !== task) onUpdated?.(updated);
      if (action === "run") setRunSubmitted(true);
      setError(toUserFacingError(err, "操作失败，请刷新状态后重试；编辑内容已保留。"));
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }

  async function refreshStatus() {
    if (lock.current) return;
    if (dirty && !window.confirm("刷新将重新读取草稿，放弃尚未保存的编辑？")) return;
    lock.current = true;
    setBusy(true);
    setError("");
    try {
      const updated = await getPipelineTask(task.id);
      accept(updated);
      setRunSubmitted(false);
      setConfirm(null);
      setNotice("已读取最新状态。");
      onUpdated?.(updated);
    } catch (err) {
      setError(toUserFacingError(err, "读取任务状态失败，请稍后重试。"));
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }

  return (
    <section aria-label="任务操作" className="min-w-0 space-y-3 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
        <span>任务状态：{task.status}</span>
        <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void refreshStatus()}>刷新状态</Button>
      </div>
      {error ? <p role="alert" className="text-sm text-red-400">{error}</p> : null}
      {notice ? <p role="status" className="text-sm text-emerald-400">{notice}</p> : null}
      {editable ? (
        <fieldset disabled={busy || runSubmitted} className="min-w-0 space-y-3 disabled:opacity-60">
          <label className="block text-sm">草稿标题
            <input aria-label="草稿标题" value={title} onChange={(event) => setTitle(event.target.value)} className="mt-1 w-full rounded border border-current/25 bg-transparent p-2" />
          </label>
          <label className="block text-sm">草稿正文
            <textarea aria-label="草稿正文" value={body} onChange={(event) => setBody(event.target.value)} rows={7} className="mt-1 w-full rounded border border-current/25 bg-transparent p-2" />
          </label>
          <div className="flex flex-wrap gap-2">
            <Button type="button" size="sm" variant="outline" disabled={!valid || !dirty} onClick={() => void execute("save")}><Save className="mr-1 h-4 w-4" />保存草稿</Button>
            {reviewable ? <>
              <Button type="button" size="sm" disabled={!valid} onClick={() => setConfirm("approve")}><Check className="mr-1 h-4 w-4" />审核通过</Button>
              <Button type="button" size="sm" variant="outline" onClick={() => setConfirm("reject")}><X className="mr-1 h-4 w-4" />驳回</Button>
            </> : null}
            {runnable ? <Button type="button" size="sm" disabled={dirty} onClick={() => setConfirm("run")}><Play className="mr-1 h-4 w-4" />生成草稿</Button> : null}
          </div>
          {runnable && dirty ? <p className="text-sm opacity-70">请先保存修改，保存后需要重新审核。</p> : null}
          {confirm ? (
            <div role="group" aria-label="确认任务操作" className="space-y-2 rounded border border-amber-400/40 p-3 text-sm">
              <p>{confirm === "reject" ? "确认驳回这篇内容？" : confirm === "approve" ? "确认保存当前内容并通过审核？" : "确认运行任务生成草稿？"}</p>
              {confirm === "reject" ? <textarea aria-label="驳回原因" placeholder="驳回原因（必填）" value={reason} onChange={(event) => setReason(event.target.value)} className="w-full rounded border border-current/25 bg-transparent p-2" /> : null}
              <div className="flex gap-2">
                <Button type="button" size="sm" disabled={confirm === "reject" && reason.trim().length < 2} onClick={() => void execute(confirm)}>确认操作</Button>
                <Button type="button" size="sm" variant="outline" onClick={() => setConfirm(null)}>取消</Button>
              </div>
            </div>
          ) : null}
        </fieldset>
      ) : <p className="text-sm opacity-70">此状态不可编辑或审核。</p>}
      <TaskWorkflow key={task.id} task={task} disabled={busy || dirty || runSubmitted} onBusyChange={setBusy} onUpdated={(updated) => { accept(updated); onUpdated?.(updated); }} />
      {busy ? <p role="status" className="flex items-center gap-2 text-sm"><Loader2 className="h-4 w-4 animate-spin" />正在处理...</p> : null}
    </section>
  );
}
