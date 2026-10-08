"use client";

import { useEffect, useRef, useState } from "react";
import { Check, Copy, Download, Link2, Play, Square, Unplug } from "lucide-react";
import { controlBrowserJob, createBrowserPairing, disconnectBrowser, getBrowserConnection, startBrowserCollection } from "@/lib/api";
import { browserJobLabel, isBrowserJobActive, type BrowserConnection, type BrowserMode } from "@/lib/browser-bridge";

export function BrowserConnectionPanel({ accountId, domainSlug, onImported }: {
  accountId: string; domainSlug: string; onImported?: () => void;
}) {
  const [connection, setConnection] = useState<BrowserConnection | null>(null);
  const [pairing, setPairing] = useState<{ code: string; expires: number } | null>(null);
  const [error, setError] = useState("");
  const [connectionError, setConnectionError] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [mode,setMode] = useState<BrowserMode>("background_text");
  const lock = useRef(false);
  const reportedJob = useRef("");
  const callback = useRef(onImported);
  callback.current = onImported;

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    setConnection(null); setPairing(null); setError(""); setConnectionError(""); reportedJob.current = "";
    async function update() {
      try {
        const value = await getBrowserConnection(accountId);
        if (!alive) return;
        setConnection(value);
        setConnectionError("");
        if (value.mode) setMode(value.mode);
        setPairing(current => value.connected || (current && current.expires <= Date.now()) ? null : current);
        const job = value.job;
        if (job && !isBrowserJobActive(job) && (job.inserted > 0 || (job.updated || 0) > 0) && reportedJob.current !== job.id) {
          reportedJob.current = job.id;
          callback.current?.();
        }
      } catch (e) {
        if (alive) { setConnectionError(e instanceof Error ? e.message : "无法检查浏览器连接"); setConnection(null); }
      } finally {
        if (alive) timer = setTimeout(update, 2000);
      }
    }
    if (accountId) void update();
    return () => { alive = false; clearTimeout(timer); };
  }, [accountId, domainSlug]);

  async function action(work: () => Promise<void>) {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError("");
    try { await work(); }
    catch (e) { setError(e instanceof Error ? e.message : "操作结果无法确认，请核对任务状态"); }
    finally {
      try { setConnection(await getBrowserConnection(accountId)); } catch { /* Preserve the write's error. */ }
      lock.current = false; setBusy(false);
    }
  }
  const job = connection?.job;
  const active = isBrowserJobActive(job);
  const button = "inline-flex h-8 items-center justify-center gap-1 rounded border border-cyan-300/35 px-2 text-xs disabled:opacity-40";
  return <section aria-label="Chrome 页面采集" className="mt-3 min-w-0 border-t border-cyan-300/25 pt-3 text-cyan-100">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="text-sm font-semibold">Chrome 页面采集</h3>
      <span className="text-xs" role="status">{connection?.connected ? "已连接" : "未连接"}{job ? ` · ${browserJobLabel(job.status)}` : ""}{connection?.connected && (connection.protocol_version || 1) < 2 ? " · 插件需更新" : ""}</span>
    </div>
    <div className="mt-2 flex flex-wrap gap-2">
      <div role="group" aria-label="采集模式" className="flex flex-wrap gap-1">
        {([['background_text','后台文本'],['foreground_visual','前台视觉']] as const).map(([value,label])=><button key={value} type="button" aria-pressed={mode===value} disabled={busy || !!connection?.connected || !!pairing} onClick={()=>setMode(value)} className={`${button} ${mode===value?'bg-emerald-500/20 border-emerald-300/50':''}`}>{label}</button>)}
      </div>
      {!connection?.connected ? <button type="button" className={button} disabled={busy || !accountId} onClick={() => void action(async () => {
        const result = await createBrowserPairing(accountId, domainSlug, mode); setCopied(false);
        setPairing({ code: result.code, expires: Date.now() + result.expires_in * 1000 });
      })}><Link2 size={14} />连接 Chrome</button> : <>
        <button type="button" className={button} disabled={busy || active} onClick={() => void action(async () => {
          const result = await startBrowserCollection(accountId, domainSlug);
          setConnection(current => current ? { ...current, job: result } : current);
        })}><Play size={14} />采集</button>
        {job?.status === "awaiting_user" ? <button type="button" className={button} disabled={busy} onClick={() => void action(async () => { await controlBrowserJob(accountId, job.id, "resume"); })}><Play size={14} />继续</button> : null}
      </>}
      {active && job ? <button type="button" className={button} disabled={busy} title="停止当前采集" aria-label="停止当前采集" onClick={() => void action(async () => { await controlBrowserJob(accountId, job.id, "stop"); })}><Square size={14} /></button> : null}
      {connection?.connected ? <button type="button" className={button} disabled={busy} title="断开 Chrome" aria-label="断开 Chrome" onClick={() => void action(async () => { await disconnectBrowser(accountId); })}><Unplug size={14} /></button> : null}
      <a href="/chrome-bridge.zip?v=1.1.1&build=diagnostics" download className={button} title="下载 Chrome 插件 1.1.1" aria-label="下载 Chrome 插件"><Download size={14} /></a>
    </div>
    {pairing ? <div className="mt-2 flex min-w-0 items-center gap-2">
      <label className="shrink-0 text-xs" htmlFor="chrome-pairing">配对码</label>
      <input id="chrome-pairing" readOnly value={pairing.code} className="min-w-0 flex-1 rounded border border-cyan-300/30 bg-transparent p-1.5 text-xs" />
      <button type="button" className={button} title="复制配对码" aria-label="复制配对码" onClick={() => void navigator.clipboard.writeText(pairing.code).then(() => setCopied(true)).catch(() => setError("无法复制，请选中配对码"))}>{copied ? <Check size={14} /> : <Copy size={14} />}</button>
    </div> : null}
    {job ? <div className="mt-2 min-w-0 text-xs">
      <p className="break-words" role="status">{job.message}</p>
      <p className="mt-1 tabular-nums">已采集 {job.collected} · 新增 {job.inserted} · 补充 {job.updated || 0} · 重复 {job.duplicates}</p>
      {job.events?.length ? <ul className="mt-1 max-h-28 overflow-y-auto text-cyan-200/70">{job.events.slice(-5).map((event, index) => <li key={`${job.id}-${index}`} className="break-words">{event}</li>)}</ul> : null}
    </div> : null}
    {error ? <p role="alert" className="mt-2 break-words text-xs text-rose-300">{error}</p> : null}
    {connectionError ? <p role="alert" className="mt-2 break-words text-xs text-rose-300">{connectionError}</p> : null}
  </section>;
}
