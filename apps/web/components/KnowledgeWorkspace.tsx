"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { CheckCheck, Loader2, Pencil, Plus, RefreshCw, Search, Sparkles, Tags, Trash2, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { DEFAULT_DOMAIN_SLUG, recommendKbTopics } from "@/lib/api";
import type { KbTopicRecord } from "@/lib/types";
import { errorText, ImportDialog, KnowledgeDialog, RecordEditor, selectClass, TagEditor } from "./knowledge/KnowledgeDialogs";
import { kinds, labels, listRows, patchRow, platforms, rowDetail, rowTitle, topicStatuses, type Kind, type KnowledgeRow, type Scope } from "./knowledge/workspace-model";

export type KnowledgeWorkspaceProps = { accountId?: string; domainSlug?: string };
type Rows = Record<Kind, KnowledgeRow[]>;
type Modal = { type: "edit" | "tags" | "delete"; kind: Kind; row?: KnowledgeRow } | { type: "import" | "recommend" };
const emptyRows = (): Rows => ({ case: [], asset: [], user_need: [], topic: [], review: [] });
const sourceKinds = ["case", "asset", "user_need"] as const;

export function KnowledgeWorkspace({ accountId, domainSlug = DEFAULT_DOMAIN_SLUG }: KnowledgeWorkspaceProps) {
  return <Workspace key={JSON.stringify([domainSlug, accountId || ""])} scope={{ domain_slug: domainSlug, account_id: accountId || "" }} />;
}

function Workspace({ scope }: { scope: Scope }) {
  const [active, setActive] = useState<Kind>("case");
  const [rows, setRows] = useState<Rows>(emptyRows);
  const [loading, setLoading] = useState<Partial<Record<Kind, boolean>>>({});
  const [errors, setErrors] = useState<Partial<Record<Kind, string>>>({});
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [selected, setSelected] = useState<Record<string, Kind>>({});
  const [modal, setModal] = useState<Modal | null>(null);
  const [notice, setNotice] = useState("");
  const [actionError, setActionError] = useState("");
  const [busy, setBusy] = useState(false);
  const locked = useRef(false);
  const mounted = useRef(true);
  const versions = useRef<Record<Kind, number>>({ case: 0, asset: 0, user_need: 0, topic: 0, review: 0 });
  const tabId = useId();
  const load = useCallback(async (kind: Kind) => {
    const version = ++versions.current[kind];
    setLoading(current => ({ ...current, [kind]: true }));
    setErrors(current => ({ ...current, [kind]: "" }));
    try {
      const response = await listRows[kind]({ domain_slug: scope.domain_slug, account_id: scope.account_id, limit: 200 });
      if (!mounted.current || versions.current[kind] !== version) return;
      const items = response.filter(row => !row.deleted_at && (!scope.account_id || row.account_id === scope.account_id));
      setRows(current => ({ ...current, [kind]: items }));
      setSelected(current => Object.fromEntries(Object.entries(current).filter(([id, selectedKind]) => selectedKind !== kind || items.some(row => row.id === id))));
    } catch (err) {
      if (mounted.current && versions.current[kind] === version) setErrors(current => ({ ...current, [kind]: errorText(err) }));
    } finally {
      if (mounted.current && versions.current[kind] === version) setLoading(current => ({ ...current, [kind]: false }));
    }
  }, [scope.domain_slug, scope.account_id]);
  useEffect(() => {
    mounted.current = true;
    kinds.forEach(kind => { void load(kind); });
    return () => { mounted.current = false; kinds.forEach(kind => { versions.current[kind] += 1; }); };
  }, [load]);

  function upsert(kind: Kind, row: KnowledgeRow) {
    if (!mounted.current) return;
    // Invalidate earlier reads so a late response cannot overwrite a successful edit.
    versions.current[kind] += 1;
    setLoading(current => ({ ...current, [kind]: false }));
    setErrors(current => ({ ...current, [kind]: "" }));
    setRows(current => ({ ...current, [kind]: row.deleted_at ? current[kind].filter(item => item.id !== row.id) : [row, ...current[kind].filter(item => item.id !== row.id)].slice(0, 200) }));
    if (row.deleted_at) setSelected(current => Object.fromEntries(Object.entries(current).filter(([id]) => id !== row.id)));
  }

  async function mutate(kind: Kind, row: KnowledgeRow, patch: Record<string, unknown>, message: string) {
    if (locked.current) return;
    locked.current = true; setBusy(true); setActionError(""); setNotice("");
    try {
      const result = await patchRow[kind](row.id, patch);
      if (mounted.current) { upsert(kind, result); setNotice(message); setModal(null); }
    } catch (err) { if (mounted.current) setActionError(errorText(err)); }
    finally { locked.current = false; if (mounted.current) setBusy(false); }
  }

  const visible = rows[active].filter(row => {
    const term = search.trim().toLocaleLowerCase();
    const matchesSearch = !term || `${rowTitle(row)} ${rowDetail(row)} ${"author" in row ? row.author : ""}`.toLocaleLowerCase().includes(term);
    const matchesStatus = !status || ("status" in row ? row.status === status : "is_verified" in row ? String(row.is_verified) === status : true);
    return matchesSearch && matchesStatus;
  });
  const sourceCount = Object.keys(selected).length;
  const activeIsSource = sourceKinds.some(kind => kind === active);
  const blocked = busy || !!loading[active] || !!errors[active];
  function open(next: Modal) { setActionError(""); setNotice(""); setModal(next); }

  return <section className="min-w-0 space-y-4" aria-label="知识工作台">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div><h2 className="text-base font-semibold">知识工作台</h2><p className="mt-1 text-xs text-muted-foreground">{scope.account_id ? "当前账户" : "当前领域 · 全部账户"}</p></div>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="outline" onClick={() => open({ type: "import" })}><Upload className="mr-1.5 h-4 w-4" />导入</Button>
        <Button size="sm" variant="outline" onClick={() => open({ type: "recommend" })}><Sparkles className="mr-1.5 h-4 w-4" />推荐选题{sourceCount ? ` (${sourceCount})` : ""}</Button>
        <Button size="sm" onClick={() => open({ type: "edit", kind: active })}><Plus className="mr-1.5 h-4 w-4" />新增{labels[active]}</Button>
      </div>
    </div>
    <div role="tablist" aria-label="知识分类" className="flex max-w-full overflow-x-auto border-b">
      {kinds.map((kind, index) => <button key={kind} id={`${tabId}-${kind}`} role="tab" type="button" aria-selected={active === kind} aria-controls={`${tabId}-panel`} tabIndex={active === kind ? 0 : -1}
        className={`shrink-0 border-b-2 px-4 py-2.5 text-sm ${active === kind ? "border-primary font-medium text-foreground" : "border-transparent text-muted-foreground hover:text-foreground"}`}
        onClick={() => { setActive(kind); setSearch(""); setStatus(""); }}
        onKeyDown={event => {
          const next = event.key === "ArrowRight" ? (index + 1) % kinds.length : event.key === "ArrowLeft" ? (index + kinds.length - 1) % kinds.length : event.key === "Home" ? 0 : event.key === "End" ? kinds.length - 1 : -1;
          if (next < 0) return;
          event.preventDefault(); setActive(kinds[next]); setSearch(""); setStatus("");
          document.getElementById(`${tabId}-${kinds[next]}`)?.focus();
        }}>{labels[kind]}{!loading[kind] && !errors[kind] ? <span className="ml-1.5 text-xs">{rows[kind].length}</span> : null}</button>)}
    </div>
    {notice ? <p role="status" className="text-sm text-emerald-600 dark:text-emerald-300">{notice}</p> : null}
    {actionError && modal?.type !== "delete" ? <p role="alert" className="text-sm text-destructive">{actionError}</p> : null}
    <div role="tabpanel" id={`${tabId}-panel`} aria-labelledby={`${tabId}-${active}`} className="min-w-0 space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-0 flex-1 sm:max-w-sm"><Search aria-hidden className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" /><Input aria-label={`搜索${labels[active]}`} className="h-9 pl-8" value={search} onChange={event => setSearch(event.target.value)} /></div>
        {active === "topic" || active === "asset" ? <select aria-label="筛选状态" className={`${selectClass} !w-auto max-w-full`} value={status} onChange={event => setStatus(event.target.value)}>
          <option value="">全部状态</option>
          {Object.entries(active === "topic" ? topicStatuses : { true: "已核实", false: "未核实" }).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select> : null}
        <Button size="sm" variant="ghost" className="w-9 shrink-0 !px-0" aria-label="刷新列表" title="刷新列表" disabled={!!loading[active] || busy} onClick={() => { void load(active); }}><RefreshCw className={`h-4 w-4 ${loading[active] ? "animate-spin" : ""}`} /></Button>
        <span className="text-xs text-muted-foreground">{visible.length} 条{rows[active].length >= 200 ? " · 最近 200 条" : ""}</span>
      </div>
      {errors[active] ? <div role="alert" className="flex flex-wrap items-center gap-3 py-5 text-sm"><span className="text-destructive">{errors[active]}</span><Button size="sm" variant="outline" onClick={() => { void load(active); }}>重新加载</Button></div>
        : loading[active] ? <p role="status" className="flex items-center gap-2 py-8 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" />正在加载{labels[active]}…</p>
          : !visible.length ? <p className="py-8 text-center text-sm text-muted-foreground">{search || status ? "没有匹配的记录" : `暂无${labels[active]}`}</p>
            : <div className="max-w-full overflow-x-auto"><table className="w-full min-w-[610px] table-fixed text-left text-sm">
              <thead className="border-y bg-muted/30 text-xs text-muted-foreground"><tr>
                {activeIsSource ? <th className="w-10 p-2"><span className="sr-only">选题来源</span></th> : null}
                <th className="px-3 py-2 font-medium">内容</th><th className="w-32 px-2 py-2 font-medium">状态 / 平台</th><th className="w-24 px-2 py-2 font-medium">更新日期</th><th className="w-32 px-2 py-2 font-medium">操作</th>
              </tr></thead>
              <tbody>{visible.map(row => <tr key={row.id} className="border-b align-top hover:bg-muted/20">
                {activeIsSource ? <td className="p-3"><input type="checkbox" aria-label={`作为选题来源：${rowTitle(row)}`} checked={!!selected[row.id]} disabled={blocked} onChange={event => {
                  const checked = event.target.checked;
                  setSelected(current => { const next = { ...current }; if (checked) next[row.id] = active; else delete next[row.id]; return next; });
                }} /></td> : null}
                <td className="min-w-0 px-3 py-3"><button type="button" className="line-clamp-2 break-words text-left font-medium hover:underline" onClick={() => open({ type: "edit", kind: active, row })}>{rowTitle(row)}</button><p className="mt-1 line-clamp-2 break-words text-xs text-muted-foreground">{rowDetail(row)}</p></td>
                <td className="px-2 py-3">{"status" in row ? <select aria-label={`选题状态：${rowTitle(row)}`} className={selectClass} value={row.status} disabled={blocked} onChange={event => { void mutate("topic", row, { status: event.target.value }, "选题状态已更新。"); }}>
                  {!Object.hasOwn(topicStatuses, row.status) ? <option value={row.status}>未知状态</option> : null}
                  {Object.entries(topicStatuses).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select> : "is_verified" in row ? <label className="flex items-center gap-1.5 text-xs"><input type="checkbox" checked={row.is_verified} disabled={blocked} onChange={event => { void mutate("asset", row, { is_verified: event.target.checked }, "素材核实状态已更新。"); }} />{row.is_verified ? "已核实" : "未核实"}</label>
                  : "platform" in row ? <span className="break-words text-xs">{platforms[row.platform as keyof typeof platforms] || row.platform || "未设置"}</span> : <span className="break-words text-xs">{"demand_type" in row ? row.demand_type || "未分类" : ""}</span>}</td>
                <td className="px-2 py-3 text-xs text-muted-foreground">{Number.isNaN(Date.parse(row.updated_at)) ? "未记录" : new Date(row.updated_at).toLocaleDateString("zh-CN")}</td>
                <td className="px-2 py-2"><div className="flex gap-0.5">
                  <Button size="sm" variant="ghost" className="w-9 !px-0" disabled={blocked} title="编辑" aria-label={`编辑${labels[active]}`} onClick={() => open({ type: "edit", kind: active, row })}><Pencil className="h-4 w-4" /></Button>
                  <Button size="sm" variant="ghost" className="w-9 !px-0" disabled={blocked} title="管理标签" aria-label="管理标签" onClick={() => open({ type: "tags", kind: active, row })}><Tags className="h-4 w-4" /></Button>
                  <Button size="sm" variant="ghost" className="w-9 !px-0" disabled={blocked} title="删除" aria-label={`删除${labels[active]}`} onClick={() => open({ type: "delete", kind: active, row })}><Trash2 className="h-4 w-4" /></Button>
                </div></td>
              </tr>)}</tbody>
            </table></div>}
    </div>
    {modal?.type === "edit" ? <RecordEditor scope={scope} kind={modal.kind} row={modal.row} topics={rows.topic as KbTopicRecord[]} topicsError={errors.topic || (loading.topic ? "正在加载" : "")} close={() => setModal(null)} saved={row => { upsert(modal.kind, row); setNotice(`${labels[modal.kind]}已保存。`); setModal(null); }} /> : null}
    {modal?.type === "tags" && modal.row ? <TagEditor scope={scope} kind={modal.kind} row={modal.row} close={() => setModal(null)} /> : null}
    {modal?.type === "delete" && modal.row ? <KnowledgeDialog title={`删除${labels[modal.kind]}`} description={rowTitle(modal.row)} busy={busy} close={() => setModal(null)}>
      <p className="text-sm">确认删除这条记录？删除后将从知识列表中移除。</p>
      {actionError ? <p role="alert" className="mt-3 text-sm text-destructive">{actionError}</p> : null}
      <div className="mt-5 flex justify-end gap-2"><Button variant="outline" disabled={busy} onClick={() => setModal(null)}>取消</Button><Button variant="destructive" disabled={busy} onClick={() => { if (modal.row) void mutate(modal.kind, modal.row, { deleted: true }, `${labels[modal.kind]}已删除。`); }}>{busy ? "正在删除…" : "确认删除"}</Button></div>
    </KnowledgeDialog> : null}
    {modal?.type === "import" ? <ImportDialog scope={scope} close={() => setModal(null)} imported={kind => { setActive(kind); setSearch(""); setStatus(""); void load(kind); if (kind === "case") void load("asset"); }} /> : null}
    {modal?.type === "recommend" ? <RecommendDialog scope={scope} rows={rows} selected={selected} loading={loading} errors={errors} retry={kind => { void load(kind); }} close={() => setModal(null)} done={message => {
      setNotice(message); setModal(null); setActive("topic"); setSearch(""); setStatus(""); void load("topic");
    }} /> : null}
  </section>;
}

function RecommendDialog({ scope, rows, selected, loading, errors, retry, close, done }: {
  scope: Scope; rows: Rows; selected: Record<string, Kind>; loading: Partial<Record<Kind, boolean>>;
  errors: Partial<Record<Kind, string>>; retry: (kind: Kind) => void; close: () => void; done: (message: string) => void;
}) {
  const [chosen, setChosen] = useState(selected);
  const [count, setCount] = useState("5");
  const [platform, setPlatform] = useState("xiaohongshu");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const locked = useRef(false);
  const sources = sourceKinds.map(kind => ({ kind, ids: rows[kind].filter(row => chosen[row.id] === kind).map(row => row.id) }));
  const total = sources.reduce((sum, source) => sum + source.ids.length, 0);
  const validCount = Number.isInteger(Number(count)) && Number(count) >= 1 && Number(count) <= 30;
  return <KnowledgeDialog title="推荐选题" busy={busy} close={close}>
    <form onSubmit={async event => {
      event.preventDefault();
      if (locked.current || !total || !validCount) return;
      if (sources.some(source => source.ids.length > 100)) { setError("每类来源最多选择 100 条。"); return; }
      if (sources.some(source => source.ids.length && (loading[source.kind] || errors[source.kind]))) { setError("选中的来源尚未加载成功，请重试。"); return; }
      locked.current = true; setBusy(true); setError("");
      try {
        const result = await recommendKbTopics({ ...scope, case_ids: sources[0].ids, asset_ids: sources[1].ids, need_ids: sources[2].ids, limit: Number(count), platform });
        done(`推荐完成：新增 ${result.created_count} 个选题，跳过 ${result.skipped_count} 个重复选题。`);
      } catch (err) { setError(errorText(err)); }
      finally { locked.current = false; setBusy(false); }
    }}>
      <fieldset disabled={busy} className="space-y-4">
        <div className="grid grid-cols-2 gap-3">
          <label className="min-w-0 space-y-1 text-sm">推荐数量<Input type="number" min={1} max={30} step={1} required value={count} onChange={e => setCount(e.target.value)} /></label>
          <label className="min-w-0 space-y-1 text-sm">平台<select className={selectClass} value={platform} onChange={e => setPlatform(e.target.value)}>{Object.entries(platforms).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        </div>
        {sourceKinds.map(kind => <div key={kind} className="space-y-2 border-t pt-3">
          <h3 className="text-sm font-medium">{labels[kind]}来源 · 已选 {sources.find(source => source.kind === kind)?.ids.length || 0}</h3>
          {loading[kind] ? <p className="text-xs text-muted-foreground">正在加载…</p> : errors[kind] ? <div className="text-sm"><span className="text-destructive">加载失败</span><Button type="button" variant="ghost" size="sm" onClick={() => retry(kind)}>重试</Button></div>
            : <div className="max-h-36 space-y-2 overflow-y-auto">
              {!rows[kind].length ? <p className="text-xs text-muted-foreground">暂无可用来源</p> : rows[kind].map(row => <label key={row.id} className="flex items-start gap-2 text-sm">
                <input type="checkbox" className="mt-1 shrink-0" checked={chosen[row.id] === kind} onChange={e => {
                  const checked = e.target.checked;
                  setChosen(current => { const next = { ...current }; if (checked) next[row.id] = kind; else delete next[row.id]; return next; });
                }} /><span className="line-clamp-2 break-words">{rowTitle(row)}</span>
              </label>)}
            </div>}
        </div>)}
      </fieldset>
      {error ? <p role="alert" className="mt-3 text-sm text-destructive">{error}</p> : null}
      <div className="mt-5 flex justify-end gap-2"><Button type="button" variant="outline" disabled={busy} onClick={close}>取消</Button><Button type="submit" disabled={busy || !total || !validCount}><CheckCheck className="mr-2 h-4 w-4" />{busy ? "正在推荐…" : `生成选题 (${total})`}</Button></div>
    </form>
  </KnowledgeDialog>;
}

export default KnowledgeWorkspace;
