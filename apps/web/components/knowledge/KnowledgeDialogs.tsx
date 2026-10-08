"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { Loader2, Plus, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { createKbEntityTag, createKbTag, getKbEntityTags, getKbTags, importKbOctopus, patchKbEntityTag } from "@/lib/api";
import type { KbEntityTagRecord, KbOctopusImportResult, KbTagRecord, KbTopicRecord } from "@/lib/types";
import { fields, labels, makeDraft, metricLabels, parseOctopusJson, prepareImportItems, rowTitle, saveRow, type Kind, type KnowledgeRow, type Scope } from "./workspace-model";

export const selectClass = "h-9 w-full min-w-0 rounded-md border border-input bg-background px-2 text-sm";
export function errorText(error: unknown): string {
  if (error instanceof Error && /[\u3400-\u9fff]/.test(error.message)) return error.message;
  return "操作失败，请检查数据或服务连接后重试。";
}

export function KnowledgeDialog({ title, description, busy = false, close, children }: {
  title: string; description?: string; busy?: boolean; close: () => void; children: ReactNode;
}) {
  return <Dialog open onOpenChange={open => { if (!open && !busy) close(); }}>
    <DialogContent className="max-h-[90dvh] w-[calc(100%-2rem)] max-w-2xl overflow-y-auto" {...(!description ? { "aria-describedby": undefined } : {})}>
      <DialogTitle className="pr-6">{title}</DialogTitle>
      {description ? <DialogDescription className="mt-2 break-words">{description}</DialogDescription> : null}
      <div className="mt-4">{children}</div>
    </DialogContent>
  </Dialog>;
}

export function RecordEditor({ kind, row, scope, topics, topicsError, close, saved }: {
  kind: Kind; row?: KnowledgeRow; scope: Scope; topics: KbTopicRecord[]; topicsError?: string;
  close: () => void; saved: (row: KnowledgeRow) => void;
}) {
  const [draft, setDraft] = useState(() => makeDraft(kind, row));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const locked = useRef(false);
  const set = (key: string, value: string) => setDraft(current => ({ ...current, [key]: value }));
  return <KnowledgeDialog title={`${row ? "编辑" : "新增"}${labels[kind]}`} busy={busy} close={close}>
    <form onSubmit={async event => {
      event.preventDefault();
      if (locked.current) return;
      locked.current = true; setBusy(true); setError("");
      try { saved(await saveRow(kind, scope, draft, row)); }
      catch (err) { setError(errorText(err)); }
      finally { locked.current = false; setBusy(false); }
    }}>
      <fieldset disabled={busy} className="grid min-w-0 gap-4 sm:grid-cols-2">
        {fields[kind].map(field => <label key={field.key} className={`min-w-0 space-y-1 text-sm ${field.multiline ? "sm:col-span-2" : ""}`}>
          <span>{field.label}{field.required ? " *" : ""}</span>
          {field.checkbox ? <input className="ml-2 accent-cyan-500" type="checkbox" checked={draft[field.key] === "true"} onChange={e => set(field.key, String(e.target.checked))} />
            : field.options ? <select className={selectClass} value={draft[field.key]} onChange={e => set(field.key, e.target.value)}>
              {!Object.hasOwn(field.options, draft[field.key]) ? <option value={draft[field.key]}>{draft[field.key] || "请选择"}</option> : null}
              {Object.entries(field.options).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
            : field.multiline ? <Textarea rows={4} value={draft[field.key]} required={field.required} maxLength={field.max} onChange={e => set(field.key, e.target.value)} />
              : <Input value={draft[field.key]} required={field.required} maxLength={field.max} onChange={e => set(field.key, e.target.value)} />}
        </label>)}
        {kind === "review" ? <label className="min-w-0 space-y-1 text-sm sm:col-span-2">
          <span>关联选题</span>
          <select className={selectClass} disabled={!!topicsError} value={draft.topic_id} onChange={e => set("topic_id", e.target.value)}>
            <option value="">不关联选题</option>
            {draft.topic_id && !topics.some(topic => topic.id === draft.topic_id) ? <option value={draft.topic_id}>已关联选题（当前列表未包含）</option> : null}
            {topics.map(topic => <option key={topic.id} value={topic.id}>{topic.title}</option>)}
          </select>
          {topicsError ? <span className="text-destructive">选题暂不可用，已有关联会保留。</span> : null}
        </label> : null}
        {kind === "case" || kind === "review" ? <div className="grid grid-cols-2 gap-3 border-t pt-3 sm:col-span-2 sm:grid-cols-3">
          {Object.entries(metricLabels).map(([key, label]) => <label className="min-w-0 space-y-1 text-sm" key={key}>
            <span>{label}</span><Input type="number" min={0} max={Number.MAX_SAFE_INTEGER} step={1} value={draft[`metric_${key}`]} onChange={e => set(`metric_${key}`, e.target.value)} />
          </label>)}
        </div> : null}
      </fieldset>
      {error ? <p role="alert" className="mt-3 text-sm text-destructive">{error}</p> : null}
      <div className="mt-5 flex justify-end gap-2">
        <Button type="button" variant="outline" disabled={busy} onClick={close}>取消</Button>
        <Button disabled={busy} type="submit">{busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}保存</Button>
      </div>
    </form>
  </KnowledgeDialog>;
}

export function TagEditor({ kind, row, scope, close }: { kind: Kind; row: KnowledgeRow; scope: Scope; close: () => void }) {
  const [tags, setTags] = useState<KbTagRecord[]>([]);
  const [links, setLinks] = useState<KbEntityTagRecord[]>([]);
  const [removed, setRemoved] = useState<KbEntityTagRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [ready, setReady] = useState(false);
  const [limited, setLimited] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [name, setName] = useState("");
  const [category, setCategory] = useState("主题");
  const [reload, setReload] = useState(0);
  const locked = useRef(false);
  // A domain-wide list can include other accounts; only display this record's and global tags.
  const entityScope = { ...scope, account_id: row.account_id || "" };
  useEffect(() => {
    let alive = true;
    setLoading(true); setReady(false); setError("");
    Promise.all([
      getKbTags({ domain_slug: scope.domain_slug, limit: 200 }),
      getKbEntityTags({ domain_slug: scope.domain_slug, entity_type: kind, entity_id: row.id, limit: 500 }),
      row.account_id ? getKbTags({ domain_slug: scope.domain_slug, account_id: row.account_id, limit: 200 }) : Promise.resolve([]),
    ]).then(([allTags, allLinks, accountTags]) => {
      if (!alive) return;
      const unique = [...new Map([...allTags, ...accountTags].map(tag => [tag.id, tag])).values()];
      setTags(unique.filter(tag => !tag.deleted_at && (!tag.account_id || tag.account_id === row.account_id)));
      setLimited(allTags.length >= 200 || accountTags.length >= 200 || allLinks.length >= 500);
      setLinks(allLinks.filter(link => !link.deleted_at)); setReady(true);
    }).catch(err => { if (alive) setError(errorText(err)); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [scope.domain_slug, kind, row.id, row.account_id, reload]);

  async function run(action: () => Promise<void>) {
    if (locked.current) return;
    locked.current = true; setBusy(true); setError("");
    try { await action(); } catch (err) { setError(errorText(err)); }
    finally { locked.current = false; setBusy(false); }
  }
  return <KnowledgeDialog title="管理标签" description={rowTitle(row)} busy={busy} close={close}>
    {loading ? <p role="status" className="text-sm">正在加载标签…</p> : ready ? <div className="flex flex-wrap gap-3">
      {tags.filter(tag => tag.status === "active" || links.some(link => link.tag_id === tag.id)).map(tag => {
        const assigned = links.filter(link => link.tag_id === tag.id);
        return <label key={tag.id} className="flex max-w-full items-center gap-2 text-sm">
          <input type="checkbox" disabled={busy} checked={assigned.length > 0} onChange={e => {
            const checked = e.target.checked;
            void run(async () => {
              if (checked) {
                const previous = removed.find(link => link.tag_id === tag.id);
                const link = previous ? await patchKbEntityTag(previous.id, { deleted: false }) : await createKbEntityTag({ ...entityScope, entity_type: kind, entity_id: row.id, tag_id: tag.id });
                setLinks(current => [...current, link]); setRemoved(current => current.filter(item => item.id !== link.id));
              } else {
                for (const link of assigned) {
                  await patchKbEntityTag(link.id, { deleted: true });
                  setLinks(current => current.filter(item => item.id !== link.id));
                  setRemoved(current => [...current, link]);
                }
              }
            });
          }} />
          <span className="break-words">{tag.name}{tag.status !== "active" ? "（已停用）" : ""}</span>
        </label>;
      })}
      {!tags.length ? <p className="text-sm text-muted-foreground">暂无可用标签</p> : null}
    </div> : null}
    {links.filter(link => !tags.some(tag => tag.id === link.tag_id)).length ? <p className="mt-3 text-sm text-muted-foreground">部分已有标签未在当前标签列表中，将保留这些关联。</p> : null}
    <form className="mt-5 grid gap-3 border-t pt-4 sm:grid-cols-[1fr_1fr_auto]" onSubmit={event => {
      event.preventDefault();
      if (!name.trim() || !category.trim()) { setError("请填写标签名称和分类。"); return; }
      if (tags.some(tag => tag.name.toLocaleLowerCase() === name.trim().toLocaleLowerCase() && tag.category.toLocaleLowerCase() === category.trim().toLocaleLowerCase())) { setError("同分类下已存在此标签。"); return; }
      void run(async () => {
        const tag = await createKbTag({ ...entityScope, name: name.trim(), category: category.trim() });
        setTags(current => [...current, tag]); setName("");
      });
    }}>
      <label className="min-w-0 space-y-1 text-sm">标签名称<Input required maxLength={120} value={name} disabled={!ready || busy} onChange={e => setName(e.target.value)} /></label>
      <label className="min-w-0 space-y-1 text-sm">分类<Input required maxLength={120} value={category} disabled={!ready || busy} onChange={e => setCategory(e.target.value)} /></label>
      <Button type="submit" size="sm" className="self-end" disabled={!ready || busy}><Plus className="mr-1 h-4 w-4" />新增标签</Button>
    </form>
    {limited ? <p className="mt-3 text-xs text-muted-foreground">标签列表已达到接口上限，较早的标签可能未显示。</p> : null}
    {error ? <p role="alert" className="mt-3 text-sm text-destructive">{error}</p> : null}
    <div className="mt-4 flex justify-end gap-2">
      {!ready && !loading ? <Button variant="outline" onClick={() => setReload(value => value + 1)}>重试</Button> : null}
      <Button variant="outline" disabled={busy} onClick={close}>完成</Button>
    </div>
  </KnowledgeDialog>;
}

export function ImportDialog({ scope, close, imported }: { scope: Scope; close: () => void; imported: (kind: Kind) => void }) {
  const [text, setText] = useState("");
  const [kind, setKind] = useState<"case" | "asset" | "user_need" | "review">("case");
  const [busy, setBusy] = useState(false);
  const [reading, setReading] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<KbOctopusImportResult | null>(null);
  const locked = useRef(false);
  const fileVersion = useRef(0);
  let items: Array<Record<string, unknown>> = [];
  let validation = "";
  if (text.trim()) { try { items = parseOctopusJson(text); } catch (err) { validation = errorText(err); } }
  return <KnowledgeDialog title="八爪鱼 JSON 导入" busy={busy || reading} close={close}>
    <form onSubmit={async event => {
      event.preventDefault();
      if (locked.current || !items.length || validation || result) return;
      locked.current = true; setBusy(true); setError("");
      try {
        const prepared = await prepareImportItems(items, scope);
        const response = await importKbOctopus({ ...scope, entity_type: kind, items: prepared });
        setResult(response); imported(kind);
      }
      catch (err) { setError(errorText(err)); }
      finally { locked.current = false; setBusy(false); }
    }}>
      <fieldset disabled={busy || reading || !!result} className="space-y-4">
        <label className="block space-y-1 text-sm">导入到<select className={selectClass} value={kind} onChange={e => setKind(e.target.value as typeof kind)}>
          {(["case", "asset", "user_need", "review"] as const).map(value => <option key={value} value={value}>{labels[value]}</option>)}
        </select></label>
        <label className="block space-y-1 text-sm">JSON 文件（最大 5 MB）<Input type="file" accept=".json,application/json" onChange={async event => {
          const version = ++fileVersion.current;
          const file = event.target.files?.[0];
          setError(""); setText("");
          if (!file) return;
          if (file.size > 5 * 1024 * 1024) { setError("文件不能超过 5 MB。"); return; }
          setReading(true);
          try { const content = await file.text(); if (version === fileVersion.current) setText(content); }
          catch { setError("文件读取失败，请重新选择。"); }
          finally { if (version === fileVersion.current) setReading(false); }
        }} /></label>
        <label className="block space-y-1 text-sm">JSON 内容<Textarea rows={9} className="font-mono text-xs" value={text} maxLength={5 * 1024 * 1024} placeholder={'[{"title":"", "content":"", "url":"", "tags":[]} ]'} onChange={e => { setText(e.target.value); setError(""); }} /></label>
      </fieldset>
      {validation ? <p role="alert" className="mt-2 text-sm text-destructive">{validation}</p> : items.length ? <p role="status" className="mt-2 text-sm">已校验 {items.length} 条</p> : null}
      {error ? <p role="alert" className="mt-3 text-sm text-destructive">{error}</p> : null}
      {result ? <div role="status" className="mt-4 space-y-2 border-t pt-3 text-sm">
        <p>接收 {result.received} 条，成功 {result.success_count} 条，失败 {result.failed_count} 条。</p>
        {!!result.auto_assets_created_count && <p>同时生成素材 {result.auto_assets_created_count} 条。</p>}
        {result.failed_items.map((item, index) => <p key={index} className="break-words text-destructive">第 {typeof item.index === "number" ? item.index + 1 : index + 1} 条{item.title ? `（${String(item.title)}）` : ""}：{errorText(new Error(String(item.error || item.reason || "请检查该条数据后重新导入。")))}</p>)}
      </div> : null}
      <div className="mt-5 flex flex-wrap justify-end gap-2">
        <Button type="button" variant="outline" disabled={busy || reading} onClick={close}>{result ? "完成" : "取消"}</Button>
        {result ? <Button type="button" onClick={() => { setText(""); setResult(null); setError(""); }}>导入下一批</Button>
          : <Button type="submit" disabled={busy || reading || !items.length || !!validation}><Upload className="mr-2 h-4 w-4" />{busy ? "正在导入…" : `导入${items.length ? ` ${items.length} 条` : ""}`}</Button>}
      </div>
    </form>
  </KnowledgeDialog>;
}
