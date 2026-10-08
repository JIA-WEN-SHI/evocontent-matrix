"use client";

import { cloneElement, useId, useRef, useState, type ReactElement, type ReactNode } from "react";
import { Loader2, Save } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { createAccountPromptVersion, createChannelAccount, updateChannelAccount, updateChannelAccountStrategy } from "@/lib/api";
import type { AccountFeedbackPlan, AccountPromptVersion, ChannelAccount, ChannelAccountStrategyProfile } from "@/lib/types";
import { toUserFacingError } from "@/lib/user-facing-errors";

const selectClass = "h-10 w-full min-w-0 rounded-lg border border-border bg-background px-3 text-sm";
const splitList = (value: string) => [...new Set(value.split(/[,，\n]/).map(item => item.trim()).filter(Boolean))];
class ValidationError extends Error {}

function Field({ label, children }: { label: string; children: ReactElement<{ id?: string }> }) {
  const id = useId();
  return <div className="grid min-w-0 gap-2 text-sm"><label htmlFor={id}>{label}</label>{cloneElement(children, { id })}</div>;
}

function EditorDialog({ title, children, onClose, onSave, submitLabel = "保存" }: {
  title: string; children: ReactNode; onClose: () => void; onSave: () => Promise<void>; submitLabel?: string;
}) {
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <Dialog open onOpenChange={open => { if (!open && !savingRef.current) onClose(); }}>
      <DialogContent aria-describedby={undefined} className="max-h-[90dvh] w-[calc(100%-2rem)] max-w-2xl overflow-y-auto">
        <DialogHeader><DialogTitle>{title}</DialogTitle></DialogHeader>
        <form className="mt-4 space-y-4" onSubmit={async event => {
          event.preventDefault();
          if (savingRef.current) return;
          savingRef.current = true;
          setSaving(true);
          setError(null);
          try { await onSave(); onClose(); }
          catch (err) { setError(err instanceof ValidationError ? err.message : toUserFacingError(err, "保存失败，请稍后重试。")); }
          finally { savingRef.current = false; setSaving(false); }
        }}>
          <fieldset disabled={saving} className="grid min-w-0 gap-4">{children}</fieldset>
          {error ? <p role="alert" className="text-sm text-rose-400">{error}</p> : null}
          <DialogFooter>
            <Button type="button" variant="outline" disabled={saving} onClick={onClose}>取消</Button>
            <Button type="submit" disabled={saving}>
              {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Save className="mr-2 h-4 w-4" />}
              {saving ? "保存中..." : submitLabel}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function AccountEditor({ account, onClose, onSaved }: {
  account: ChannelAccount | null; onClose: () => void; onSaved: (account: ChannelAccount) => void;
}) {
  const [name, setName] = useState(account?.account_name || "");
  const [channel, setChannel] = useState(account?.channel || "xiaohongshu");
  const [handle, setHandle] = useState(account?.account_handle || "");
  const [mode, setMode] = useState<ChannelAccount["login_mode"]>(account?.login_mode || "storage_state");
  const [statePath, setStatePath] = useState(account?.storage_state_path || "");
  const [userDir, setUserDir] = useState(account?.user_data_dir || "");
  const [cookies, setCookies] = useState(account?.cookies_json || "");
  const [username, setUsername] = useState(account?.login_username || "");
  const [password, setPassword] = useState("");
  const [tags, setTags] = useState((account?.tags || []).join(", "));
  const [notes, setNotes] = useState(account?.notes || "");
  const [active, setActive] = useState(account?.is_active ?? true);

  async function save() {
    if (!name.trim()) throw new ValidationError("请输入账号名称。");
    if (mode === "credential" && (!username.trim() || (!password.trim() && !account?.has_login_password))) {
      throw new ValidationError("请填写登录用户名和密码。");
    }
    if (mode === "cookies_json" && cookies.trim()) {
      try {
        const parsed: unknown = JSON.parse(cookies);
        if (!Array.isArray(parsed)) throw new Error();
      } catch { throw new ValidationError("登录 Cookies 必须是有效的 JSON 数组。"); }
    }
    const payload = {
      account_name: name.trim(), account_handle: handle.trim(), login_mode: mode,
      tags: splitList(tags), notes: notes.trim(), is_active: active,
      ...(mode === "storage_state" ? { storage_state_path: statePath.trim() } : {}),
      ...(mode === "user_data_dir" ? { user_data_dir: userDir.trim() } : {}),
      ...(mode === "cookies_json" ? { cookies_json: cookies.trim() } : {}),
      ...(mode === "credential" ? { login_username: username.trim(), ...(password.trim() ? { login_password: password } : {}) } : {}),
    };
    const saved = account ? await updateChannelAccount(account.id, payload) : await createChannelAccount({ ...payload, channel });
    onSaved(saved);
  }

  return (
    <EditorDialog title={account ? "编辑账号" : "新增账号"} onClose={onClose} onSave={save}>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="账号名称"><Input value={name} maxLength={120} required onChange={e => setName(e.target.value)} /></Field>
        <Field label="渠道"><select className={selectClass} value={channel} disabled={!!account} onChange={e => setChannel(e.target.value as ChannelAccount["channel"])}>
          <option value="xiaohongshu">小红书</option><option value="wechat_mp">公众号</option><option value="douyin">抖音</option><option value="video">视频号</option>
        </select></Field>
        <Field label="账号标识"><Input value={handle} maxLength={120} onChange={e => setHandle(e.target.value)} /></Field>
        <Field label="登录方式"><select className={selectClass} value={mode} onChange={e => setMode(e.target.value as ChannelAccount["login_mode"])}>
          <option value="storage_state">浏览器登录态文件</option><option value="user_data_dir">浏览器用户目录</option><option value="cookies_json">Cookies</option><option value="credential">账号密码</option>
        </select></Field>
      </div>
      {mode === "storage_state" ? <Field label="登录态文件路径"><Input value={statePath} maxLength={1000} onChange={e => setStatePath(e.target.value)} /></Field> : null}
      {mode === "user_data_dir" ? <Field label="浏览器用户目录"><Input value={userDir} maxLength={1000} onChange={e => setUserDir(e.target.value)} /></Field> : null}
      {mode === "cookies_json" ? <Field label="登录 Cookies"><Textarea value={cookies} maxLength={200000} spellCheck={false} onChange={e => setCookies(e.target.value)} /></Field> : null}
      {mode === "credential" ? <div className="grid gap-4 sm:grid-cols-2">
        <Field label="登录用户名"><Input value={username} maxLength={200} autoComplete="username" required onChange={e => setUsername(e.target.value)} /></Field>
        <Field label={account?.has_login_password ? "新密码（留空保留原密码）" : "登录密码"}><Input type="password" value={password} maxLength={500} autoComplete="new-password" required={!account?.has_login_password} onChange={e => setPassword(e.target.value)} /></Field>
      </div> : null}
      <Field label="标签"><Input value={tags} onChange={e => setTags(e.target.value)} /></Field>
      <Field label="备注"><Textarea value={notes} maxLength={4000} onChange={e => setNotes(e.target.value)} /></Field>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={active} onChange={e => setActive(e.target.checked)} />启用账号</label>
    </EditorDialog>
  );
}

const profileFields = [
  ["persona_name", "人设名称"], ["ip_positioning", "IP 定位"], ["primary_goal", "主要目标"],
  ["tone_style", "语气风格"], ["cta_style", "行动引导风格"],
] as const;
const listFields = [
  ["audience", "目标受众"], ["pain_points", "核心痛点"], ["content_pillars", "内容支柱"],
  ["forbidden_claims", "禁用承诺"], ["publish_constraints", "发布约束"], ["focus_keywords", "重点关键词"],
] as const;
const projectFields = [
  ["name", "项目名称"], ["industry", "所属行业"], ["source_url", "官方来源"], ["summary", "技术与用途"],
  ["advantages", "项目优势"], ["input", "输入材料"], ["output", "输出结果"],
  ["technology_keywords", "技术关键词"], ["affected_role", "受影响的职业"],
  ["imagined_scene", "设想的职业画面"],
] as const;

export function AccountStrategyEditor({ account, profile, feedback, onClose, onSaved }: {
  account: ChannelAccount; profile: Partial<ChannelAccountStrategyProfile>; feedback?: AccountFeedbackPlan;
  onClose: () => void; onSaved: (account: ChannelAccount) => void;
}) {
  const [fields, setFields] = useState<Record<string, string>>(() => Object.fromEntries([
    ...profileFields.map(([key]) => [key, profile[key] || ""]),
    ...listFields.map(([key]) => [key, (profile[key] || []).join("\n")]),
  ]));
  const [reason, setReason] = useState("更新账号定位与策略");
  const [contentBranch, setContentBranch] = useState(String(profile.prompt_overrides?.content_branch || "tutorial"));
  const storedProject = (profile.prompt_overrides?.project_reference || {}) as Record<string, unknown>;
  const [project, setProject] = useState<Record<string, string>>(() => Object.fromEntries(projectFields.map(([key]) =>
    [key, key === "technology_keywords" && Array.isArray(storedProject[key]) ? storedProject[key].join("、") : String(storedProject[key] || "")])));
  const [sourceConfirmed, setSourceConfirmed] = useState(Boolean(storedProject.checked_at));
  return (
    <EditorDialog title="编辑定位与策略" onClose={onClose} onSave={async () => {
      if (reason.trim().length < 2) throw new ValidationError("请填写至少两个字的变更原因。");
      if (contentBranch === "project_observer") {
        if (!sourceConfirmed || projectFields.some(([key]) => !project[key].trim())) {
          throw new ValidationError("请填写项目资料并核对官方来源。");
        }
        let source: URL;
        try { source = new URL(project.source_url); } catch { throw new ValidationError("官方来源必须是有效的 HTTPS 链接。"); }
        if (source.protocol !== "https:" || source.username || source.password) throw new ValidationError("官方来源必须是有效的 HTTPS 链接。");
      }
      const nextProfile = { ...profile,
        ...Object.fromEntries(profileFields.map(([key]) => [key, fields[key].trim()])),
        ...Object.fromEntries(listFields.map(([key]) => [key, splitList(fields[key])])),
        prompt_overrides: { ...profile.prompt_overrides, content_branch: contentBranch,
          ...(contentBranch === "project_observer" ? { project_reference: {
            ...storedProject,
            ...(project.name !== storedProject.name || project.source_url !== storedProject.source_url ? { headline: "", source_notes: "" } : {}),
            ...Object.fromEntries(projectFields.filter(([key]) => key !== "technology_keywords").map(([key]) => [key, project[key].trim()])),
            technology_keywords: [...new Set(project.technology_keywords.split(/[,，、\n]/).map(word => word.trim()).filter(Boolean))],
            checked_at: projectFields.every(([key]) => project[key] === (key === "technology_keywords" && Array.isArray(storedProject[key]) ? storedProject[key].join("、") : String(storedProject[key] || "")))
              ? storedProject.checked_at || new Date().toISOString() : new Date().toISOString(),
          }} : {}),
        },
      };
      const result = await updateChannelAccountStrategy(account.id, { strategy_profile: nextProfile, feedback_plan: feedback, reason: reason.trim() });
      onSaved(result.account);
    }}>
      <Field label="内容分支"><select className={selectClass} value={contentBranch} onChange={e => setContentBranch(e.target.value)}>
        <option value="tutorial">实用教程（原有结构）</option>
        <option value="project_observer">AI 项目观察·共情短文</option>
      </select></Field>
      {contentBranch === "project_observer" ? <div className="grid gap-4 border-b border-border pb-4 sm:grid-cols-2">
        {projectFields.map(([key, label]) => <Field key={key} label={label}><Input value={project[key]} maxLength={key === "summary" || key === "advantages" ? 1000 : 500}
          onChange={e => { setProject(prev => ({ ...prev, [key]: e.target.value,
            ...((key === "name" || key === "source_url") && e.target.value !== String(storedProject[key] || "") ? { affected_role: "", imagined_scene: "" } : {}),
          })); setSourceConfirmed(false); }} /></Field>)}
        <label className="flex items-start gap-2 text-sm sm:col-span-2"><input type="checkbox" checked={sourceConfirmed} onChange={e => setSourceConfirmed(e.target.checked)} />我已核对官方来源中的项目能力</label>
      </div> : null}
      <div className="grid gap-4 sm:grid-cols-2">
        {profileFields.map(([key, label]) => <Field key={key} label={label}><Input value={fields[key]} onChange={e => setFields(prev => ({ ...prev, [key]: e.target.value }))} /></Field>)}
        {listFields.map(([key, label]) => <Field key={key} label={`${label}（每行一项）`}><Textarea value={fields[key]} onChange={e => setFields(prev => ({ ...prev, [key]: e.target.value }))} /></Field>)}
      </div>
      <Field label="变更原因"><Input required minLength={2} maxLength={4000} value={reason} onChange={e => setReason(e.target.value)} /></Field>
    </EditorDialog>
  );
}

const agents = [["coach", "总教练"], ["collector_agent", "采集环节"], ["analysis_agent", "分析环节"], ["copy_agent", "文案环节"], ["review_agent", "复盘环节"]];

export function AccountPromptEditor({ accountId, domainSlug, source, onClose, onSaved }: {
  accountId: string; domainSlug: string; source: AccountPromptVersion | null; onClose: () => void; onSaved: () => void;
}) {
  const [agent, setAgent] = useState(source?.agent_name || "copy_agent");
  const [version, setVersion] = useState("");
  const [prompt, setPrompt] = useState(source?.system_prompt || "");
  const [reason, setReason] = useState("人工更新提示词");
  return (
    <EditorDialog title={source ? "编辑为新版本" : "新增提示词版本"} onClose={onClose} submitLabel="保存草稿" onSave={async () => {
      if (!version.trim() || !prompt.trim()) throw new ValidationError("请填写版本名称和提示词全文。");
      if (reason.trim().length < 2) throw new ValidationError("请填写至少两个字的变更原因。");
      await createAccountPromptVersion(accountId, { domain_slug: domainSlug, agent_name: agent, version: version.trim(), system_prompt: prompt.trim(), status: "draft", source: "manual", reason: reason.trim() });
      onSaved();
    }}>
      <Field label="所属环节"><select className={selectClass} value={agent} onChange={e => setAgent(e.target.value)}>
        {agents.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
        {source && !agents.some(([key]) => key === source.agent_name) ? <option value={source.agent_name}>{source.agent_name}</option> : null}
      </select></Field>
      <Field label="版本名称"><Input required maxLength={120} value={version} onChange={e => setVersion(e.target.value)} /></Field>
      <Field label="提示词全文"><Textarea className="min-h-64" required maxLength={100000} value={prompt} onChange={e => setPrompt(e.target.value)} /></Field>
      <Field label="变更原因"><Input required minLength={2} maxLength={4000} value={reason} onChange={e => setReason(e.target.value)} /></Field>
    </EditorDialog>
  );
}
