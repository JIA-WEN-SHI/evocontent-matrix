"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { Check, KeyRound, Loader2, Pencil, Play, Plus, RefreshCcw, ShieldCheck } from "lucide-react";

import { AccountEditor, AccountPromptEditor, AccountStrategyEditor } from "@/components/accounts/account-editors";
import { KnowledgeWorkspace } from "@/components/KnowledgeWorkspace";
import { FoldableSection } from "@/components/foldable-section";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  DEFAULT_DOMAIN_SLUG,
  applyPendingStrategyItemChange,
  activateAccountPromptVersion,
  getAccountAgentConfig,
  getAccountContentMethodView,
  getAccountLoopExecutionView,
  getAccountLoopStatus,
  getAccountPromptVersions,
  getAccountRuntime,
  getAccountSopCurrent,
  getAccountSopSnapshots,
  getChannelAccounts,
  getChannelAccountStrategy,
  getExecutionRouteStatus,
  getPendingStrategyItems,
  launchChannelAccountLogin,
  resetChannelAccountAuth,
  runAccountLoopDaily,
  updateChannelAccount,
  verifyChannelAccountLogin,
} from "@/lib/api";
import { toUserFacingError } from "@/lib/user-facing-errors";
import type {
  AccountPromptVersion,
  AccountRuntimeContext,
  AgentConfigNodeView,
  AgentConfigView,
  ChannelAccount,
  ChannelAccountStrategyProfile,
  ContentMethodView,
  ExecutionRouteStatusView,
  LoopExecutionView,
  PendingStrategyItem,
  SopCurrentView,
  SopSnapshotRecord,
} from "@/lib/types";

const DOMAIN_SLUG = DEFAULT_DOMAIN_SLUG;

type StrategyBundle = Awaited<ReturnType<typeof getChannelAccountStrategy>> | null;

type SummaryCard = {
  label: string;
  value: string;
  hint: string;
};

function zh(value: string): string {
  return value;
}

function formatTime(value?: string | null): string {
  if (!value) return zh("\u6682\u65e0");
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", { hour12: false });
}

function shortText(value: string | null | undefined, max = 120): string {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (!text) return zh("\u6682\u65e0");
  return text.length > max ? `${text.slice(0, max)}...` : text;
}

function channelLabel(channel: string): string {
  const map: Record<string, string> = {
    xiaohongshu: zh("\u5c0f\u7ea2\u4e66"),
    wechat_mp: zh("\u516c\u4f17\u53f7"),
    douyin: zh("\u6296\u97f3"),
    video: zh("\u89c6\u9891\u53f7"),
  };
  return map[channel] ?? channel ?? zh("\u672a\u77e5\u6e20\u9053");
}

function loginModeLabel(mode: string): string {
  const map: Record<string, string> = {
    storage_state: zh("\u6d4f\u89c8\u5668\u767b\u5f55\u6001\u6587\u4ef6"),
    user_data_dir: zh("\u6d4f\u89c8\u5668\u7528\u6237\u76ee\u5f55"),
    credential: zh("\u8d26\u53f7\u5bc6\u7801"),
    cookies_json: "Cookies",
  };
  return map[mode] ?? mode ?? zh("\u5176\u4ed6\u65b9\u5f0f");
}

function statusLabel(status: string | null): string {
  const map: Record<string, string> = {
    ok: zh("\u53ef\u7528"),
    warning: zh("\u9700\u5173\u6ce8"),
    failed: zh("\u5931\u8d25"),
  };
  return map[String(status || "")] ?? zh("\u672a\u6821\u9a8c");
}

function promptSourceLabel(source: string): string {
  const map: Record<string, string> = {
    system: zh("\u7cfb\u7edf\u9ed8\u8ba4"),
    manual: zh("\u4eba\u5de5\u7ef4\u62a4"),
    reflection: zh("\u590d\u76d8\u751f\u6210"),
    strategy: zh("\u7b56\u7565\u751f\u6210"),
  };
  return map[source] ?? source ?? zh("\u672a\u6807\u8bb0");
}

function changeTargetLabel(target: string): string {
  const map: Record<string, string> = {
    collection_plan: zh("\u91c7\u96c6\u7b56\u7565"),
    feedback_plan: zh("\u590d\u76d8\u7b56\u7565"),
    publish_preferences: zh("\u53d1\u5e03\u504f\u597d"),
  };
  return map[target] ?? target ?? zh("\u672a\u6807\u8bb0\u76ee\u6807");
}

function agentKeyLabel(key: string): string {
  const map: Record<string, string> = {
    coach: zh("\u603b\u6559\u7ec3"),
    collector_agent: zh("\u91c7\u96c6\u73af\u8282"),
    analysis_agent: zh("\u5206\u6790\u73af\u8282"),
    copy_agent: zh("\u6587\u6848\u73af\u8282"),
    review_agent: zh("\u590d\u76d8\u73af\u8282"),
  };
  return map[key] ?? key;
}

function routeStatusLabel(route?: ExecutionRouteStatusView | null): string {
  if (!route) return zh("\u672a\u52a0\u8f7d");
  return route.user_facing_status || zh("\u672a\u914d\u7f6e");
}

function joinList(items?: string[] | null, fallback = zh("\u6682\u65e0")): string {
  return items && items.length > 0 ? items.join(" / ") : fallback;
}

function buildSummaryCards(input: {
  account: ChannelAccount | null;
  strategyProfile: Partial<ChannelAccountStrategyProfile> | null;
  loopView: LoopExecutionView | null;
  routeView: ExecutionRouteStatusView | null;
  sopCurrent: SopCurrentView | null;
  promptVersions: AccountPromptVersion[];
  pendingItems: PendingStrategyItem[];
}): SummaryCard[] {
  const activeVersions = input.promptVersions.filter((item) => item.status === "active");
  return [
    {
      label: zh("\u5f53\u524d\u8d26\u53f7"),
      value: input.account?.account_name || zh("\u672a\u9009\u62e9"),
      hint: input.account ? channelLabel(input.account.channel) : zh("\u8bf7\u5148\u9009\u62e9\u8d26\u53f7"),
    },
    {
      label: zh("\u8d26\u53f7\u5b9a\u4f4d"),
      value: input.strategyProfile?.persona_name || zh("\u672a\u914d\u7f6e"),
      hint: input.strategyProfile?.primary_goal || zh("\u672a\u914d\u7f6e\u4e3b\u76ee\u6807"),
    },
    {
      label: zh("\u95ed\u73af\u9636\u6bb5"),
      value: input.loopView?.current_stage || zh("\u672a\u52a0\u8f7d"),
      hint: input.loopView ? `${zh("\u5b8c\u6210\u5ea6")} ${input.loopView.completion_score}%` : zh("\u7b49\u5f85\u95ed\u73af\u6267\u884c\u6570\u636e"),
    },
    {
      label: zh("\u6267\u884c\u8def\u7531"),
      value: routeStatusLabel(input.routeView),
      hint: input.routeView?.primary_route || zh("\u5c1a\u672a\u914d\u7f6e\u4e3b\u6267\u884c\u8def\u7531"),
    },
    {
      label: zh("\u5f53\u524d SOP"),
      value: input.sopCurrent?.sop_latest ? `v${input.sopCurrent.sop_latest.version}` : zh("\u672a\u751f\u6210"),
      hint: input.sopCurrent?.sop_latest ? formatTime(input.sopCurrent.sop_latest.captured_at) : zh("\u6682\u65e0\u5feb\u7167"),
    },
    {
      label: zh("\u6fc0\u6d3b\u63d0\u793a\u8bcd"),
      value: String(activeVersions.length),
      hint: `${zh("\u603b\u8ba1")} ${input.promptVersions.length} ${zh("\u4e2a\u7248\u672c")}`,
    },
    {
      label: zh("\u5f85\u786e\u8ba4\u7b56\u7565"),
      value: String(input.pendingItems.length),
      hint: input.pendingItems[0]?.title || zh("\u5f53\u524d\u6ca1\u6709\u5f85\u786e\u8ba4\u52a8\u4f5c"),
    },
    {
      label: zh("\u767b\u5f55\u72b6\u6001"),
      value: statusLabel(input.account?.last_login_check_status || null),
      hint: formatTime(input.account?.last_login_check_at),
    },
  ];
}

function BulletCard({ title, items }: { title: string; items: string[] }) {
  return (
    <div className="rounded-xl border border-border/60 bg-background/70 p-4">
      <p className="text-sm font-semibold">{title}</p>
      <div className="mt-3 flex flex-wrap gap-2">
        {items.length > 0 ? (
          items.map((item) => (
            <Badge key={`${title}-${item}`} variant="outline">
              {item}
            </Badge>
          ))
        ) : (
          <span className="text-sm text-muted-foreground">{zh("\u6682\u65e0")}</span>
        )}
      </div>
    </div>
  );
}

function AgentNodeCard({ node }: { node: AgentConfigNodeView }) {
  return (
    <div className="rounded-xl border border-border/60 bg-background/70 p-4">
      <div className="flex items-center justify-between gap-2">
        <div>
          <p className="text-sm font-semibold">{node.label}</p>
          <p className="text-xs text-muted-foreground">{agentKeyLabel(node.agent_key)}</p>
        </div>
        <Badge variant="outline">{node.model_name || zh("\u672a\u914d\u7f6e\u6a21\u578b")}</Badge>
      </div>

      <p className="mt-3 text-sm text-muted-foreground">{node.role_summary}</p>

      <div className="mt-4 grid gap-3 md:grid-cols-2">
        <div className="rounded-lg border border-border/50 bg-muted/10 p-3 text-xs text-muted-foreground">
          <p>{zh("\u6e29\u5ea6")}：{node.temperature ?? zh("\u672a\u63a5\u5165")}</p>
          <p className="mt-1">{zh("\u63d0\u793a\u8bcd\u6765\u6e90")}：{node.prompt_source || zh("\u672a\u914d\u7f6e")}</p>
          <p className="mt-1">{zh("\u63d0\u793a\u8bcd\u7248\u672c")}：{node.prompt_version || zh("\u672a\u6fc0\u6d3b")}</p>
          <p className="mt-1">{zh("\u4e0a\u6e38\u8f93\u5165")}：{joinList(node.upstream_inputs)}</p>
          <p className="mt-1">{zh("\u4e0b\u6e38\u8f93\u51fa")}：{joinList(node.downstream_outputs)}</p>
        </div>
        <div className="rounded-lg border border-border/50 bg-muted/10 p-3 text-xs text-muted-foreground">
          <p>{zh("\u53ef\u7528\u5de5\u5177")}：{joinList(node.allowed_tools)}</p>
          <p className="mt-1">{zh("\u5f15\u7528\u89c4\u5219\u5e93")}：{joinList(node.knowledge_sources)}</p>
          <p className="mt-1">{zh("\u8865\u5145\u8bf4\u660e")}：{joinList(node.notes)}</p>
        </div>
      </div>

      <div className="mt-3 rounded-lg border border-border/50 bg-muted/10 p-3 text-xs text-muted-foreground">
        <p className="mb-1 font-medium text-foreground">{zh("\u63d0\u793a\u8bcd\u7247\u6bb5")}</p>
        <p className="whitespace-pre-wrap">{node.prompt_preview || zh("\u6682\u65e0\u63d0\u793a\u8bcd\u9884\u89c8")}</p>
      </div>
    </div>
  );
}

export default function AccountsPage() {
  return <Suspense fallback={<p className="text-sm text-muted-foreground">正在加载账号...</p>}><AccountsWorkspace /></Suspense>;
}

function AccountsWorkspace() {
  const panel = useSearchParams().get("panel");
  const selectedIdRef = useRef<string | null>(null);
  const detailRequestRef = useRef(0);
  const busyRef = useRef(new Set<string>());
  const [accountEditor, setAccountEditor] = useState<{ account: ChannelAccount | null } | null>(null);
  const [strategyEditor, setStrategyEditor] = useState(false);
  const [promptEditor, setPromptEditor] = useState<{ source: AccountPromptVersion | null } | null>(null);
  const [knowledgeScope, setKnowledgeScope] = useState("account");
  const [rows, setRows] = useState<ChannelAccount[]>([]);
  const [selectedAccountId, setSelectedAccountId] = useState<string | null>(null);
  const [strategy, setStrategy] = useState<StrategyBundle>(null);
  const [runtimeContext, setRuntimeContext] = useState<AccountRuntimeContext | null>(null);
  const [loopStatus, setLoopStatus] = useState<Awaited<ReturnType<typeof getAccountLoopStatus>> | null>(null);
  const [loopExecutionView, setLoopExecutionView] = useState<LoopExecutionView | null>(null);
  const [sopCurrent, setSopCurrent] = useState<SopCurrentView | null>(null);
  const [sopSnapshots, setSopSnapshots] = useState<SopSnapshotRecord[]>([]);
  const [agentConfigView, setAgentConfigView] = useState<AgentConfigView | null>(null);
  const [contentMethodView, setContentMethodView] = useState<ContentMethodView | null>(null);
  const [executionRouteView, setExecutionRouteView] = useState<ExecutionRouteStatusView | null>(null);
  const [promptVersions, setPromptVersions] = useState<AccountPromptVersion[]>([]);
  const [pendingStrategyItems, setPendingStrategyItems] = useState<PendingStrategyItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busyMap, setBusyMap] = useState<Record<string, boolean>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const selectedAccount = useMemo(
    () => rows.find((item) => item.id === selectedAccountId) || null,
    [rows, selectedAccountId],
  );

  const strategyProfile = (strategy?.strategy_profile || null) as Partial<ChannelAccountStrategyProfile> | null;
  const collectionPlan = strategy?.collection_plan || runtimeContext?.collection_plan || null;
  const feedbackPlan = strategy?.feedback_plan || null;
  const latestTask = runtimeContext?.latest?.task as Record<string, unknown> | undefined;
  const latestRun = runtimeContext?.latest?.run as Record<string, unknown> | undefined;

  const coachNode = useMemo(
    () => (agentConfigView?.nodes || []).find((item) => item.agent_key === "coach") || null,
    [agentConfigView],
  );

  const subAgents = useMemo(
    () =>
      (agentConfigView?.nodes || []).filter((item) =>
        ["collector_agent", "analysis_agent", "copy_agent", "review_agent"].includes(item.agent_key),
      ),
    [agentConfigView],
  );

  const cards = useMemo(
    () =>
      buildSummaryCards({
        account: selectedAccount,
        strategyProfile,
        loopView: loopExecutionView,
        routeView: executionRouteView,
        sopCurrent,
        promptVersions,
        pendingItems: pendingStrategyItems,
      }),
    [selectedAccount, strategyProfile, loopExecutionView, executionRouteView, sopCurrent, promptVersions, pendingStrategyItems],
  );

  function clearDetails() {
    setStrategy(null);
    setRuntimeContext(null);
    setLoopStatus(null);
    setLoopExecutionView(null);
    setSopCurrent(null);
    setSopSnapshots([]);
    setAgentConfigView(null);
    setContentMethodView(null);
    setExecutionRouteView(null);
    setPromptVersions([]);
    setPendingStrategyItems([]);
  }

  async function loadAccountDetails(accountId: string) {
    if (selectedIdRef.current !== accountId) return;
    const requestId = ++detailRequestRef.current;
    clearDetails();
    setDetailLoading(true);
    setError(null);
    try {
      const [
        strategyData,
        runtimeData,
        loopData,
        loopExecutionData,
        sopCurrentData,
        sopSnapshotData,
        agentConfigData,
        contentMethodData,
        routeData,
        promptVersionData,
        pendingItems,
      ] = await Promise.allSettled([
        getChannelAccountStrategy(accountId),
        getAccountRuntime(accountId),
        getAccountLoopStatus(accountId, { domainSlug: DOMAIN_SLUG }),
        getAccountLoopExecutionView(accountId, { domainSlug: DOMAIN_SLUG }),
        getAccountSopCurrent(accountId, { domainSlug: DOMAIN_SLUG }),
        getAccountSopSnapshots(accountId, { domainSlug: DOMAIN_SLUG, limit: 10 }),
        getAccountAgentConfig(accountId, { domainSlug: DOMAIN_SLUG }),
        getAccountContentMethodView(accountId, { domainSlug: DOMAIN_SLUG }),
        getExecutionRouteStatus({ accountId, domainSlug: DOMAIN_SLUG }),
        getAccountPromptVersions(accountId, { domainSlug: DOMAIN_SLUG, limit: 20 }),
        getPendingStrategyItems({ accountId, domainSlug: DOMAIN_SLUG, limit: 12 }),
      ]);

      if (requestId !== detailRequestRef.current || selectedIdRef.current !== accountId) return;
      const value = <T,>(result: PromiseSettledResult<T>): T | null => result.status === "fulfilled" ? result.value : null;
      setStrategy(value(strategyData));
      setRuntimeContext(value(runtimeData));
      setLoopStatus(value(loopData));
      setLoopExecutionView(value(loopExecutionData));
      setSopCurrent(value(sopCurrentData));
      setSopSnapshots(value(sopSnapshotData)?.items || []);
      setAgentConfigView(value(agentConfigData));
      setContentMethodView(value(contentMethodData));
      setExecutionRouteView(value(routeData));
      setPromptVersions(value(promptVersionData) || []);
      setPendingStrategyItems(value(pendingItems) || []);
      if ([strategyData, runtimeData, loopData, loopExecutionData, sopCurrentData, sopSnapshotData, agentConfigData, contentMethodData, routeData, promptVersionData, pendingItems].some(result => result.status === "rejected")) {
        setError("部分账号信息加载失败，已显示可用内容。请稍后刷新。");
      }
    } catch (err) {
      if (requestId !== detailRequestRef.current || selectedIdRef.current !== accountId) return;
      clearDetails();
      setError(toUserFacingError(err, zh("\u52a0\u8f7d\u8d26\u53f7\u6cbb\u7406\u4fe1\u606f\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u5237\u65b0\u3002")));
    } finally {
      if (requestId === detailRequestRef.current && selectedIdRef.current === accountId) setDetailLoading(false);
    }
  }

  async function loadAccounts() {
    setLoading(true);
    setError(null);
    try {
      const items = await getChannelAccounts({ limit: 200 });
      setRows(items);
      const fallbackId =
        selectedIdRef.current && items.some((item) => item.id === selectedIdRef.current)
          ? selectedIdRef.current
          : items.find((item) => item.is_active)?.id || items[0]?.id || null;
      selectedIdRef.current = fallbackId;
      setSelectedAccountId(fallbackId);
      setLoading(false);
      if (fallbackId) {
        await loadAccountDetails(fallbackId);
      } else {
        ++detailRequestRef.current;
        setDetailLoading(false);
        clearDetails();
      }
    } catch (err) {
      setError(toUserFacingError(err, zh("\u52a0\u8f7d\u8d26\u53f7\u5217\u8868\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u5237\u65b0\u3002")));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadAccounts();
    return () => { ++detailRequestRef.current; };
  }, []);

  useEffect(() => {
    if (!panel || loading || detailLoading) return;
    document.getElementById(panel)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [panel, loading, detailLoading, selectedAccountId]);

  async function selectAccount(accountId: string) {
    selectedIdRef.current = accountId;
    setStrategyEditor(false);
    setPromptEditor(null);
    setSelectedAccountId(accountId);
    await loadAccountDetails(accountId);
  }

  function accountSaved(account: ChannelAccount, select = false) {
    setRows(prev => prev.some(item => item.id === account.id) ? prev.map(item => item.id === account.id ? account : item) : [...prev, account]);
    setMessage("账号设置已保存。");
    if (select) {
      selectedIdRef.current = account.id;
      setSelectedAccountId(account.id);
    }
    void loadAccountDetails(account.id);
  }

  async function withBusy(key: string, fn: () => Promise<void>) {
    if (busyRef.current.has(key)) return;
    busyRef.current.add(key);
    setBusyMap((prev) => ({ ...prev, [key]: true }));
    setMessage(null);
    setError(null);
    try {
      await fn();
    } catch (err) {
      setError(toUserFacingError(err, zh("\u6267\u884c\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002")));
    } finally {
      setBusyMap((prev) => ({ ...prev, [key]: false }));
      busyRef.current.delete(key);
    }
  }

  return (
    <div className="min-w-0 space-y-6 [overflow-wrap:anywhere]">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-2xl font-semibold">{zh("\u8d26\u53f7\u7ba1\u7406")}</h2>
          <p className="text-sm text-muted-foreground">
            {zh("\u8fd9\u91cc\u53ea\u5c55\u793a\u7528\u6237\u771f\u6b63\u9700\u8981\u6cbb\u7406\u7684\u5185\u5bb9\uff1a\u8d26\u53f7\u5b9a\u4f4d\u3001\u5b8c\u6574 SOP\u30014 \u4e2a\u5b50 Agent\u3001\u6267\u884c\u8def\u7531\u3001\u63d0\u793a\u8bcd\u7248\u672c\u548c\u5f85\u786e\u8ba4\u7b56\u7565\u3002")}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button onClick={() => setAccountEditor({ account: null })} disabled={loading}>
            <Plus className="mr-2 h-4 w-4" />新增账号
          </Button>
          <Link href="/" className="inline-flex h-10 items-center rounded-md border border-border/60 px-4 text-sm hover:bg-muted/10">
            {zh("\u8fd4\u56de\u4e3b\u5c4f")}
          </Link>
          <Button variant="outline" onClick={() => void loadAccounts()} disabled={loading || detailLoading}>
            <RefreshCcw className="mr-2 h-4 w-4" />
            {zh("\u5237\u65b0")}
          </Button>
        </div>
      </div>

      {error ? <div role="alert" className="rounded-xl border border-rose-400/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">{error}</div> : null}
      {message ? <div role="status" className="rounded-xl border border-emerald-400/30 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-100">{message}</div> : null}

      {accountEditor ? <AccountEditor account={accountEditor.account} onClose={() => setAccountEditor(null)} onSaved={account => accountSaved(account, !accountEditor.account)} /> : null}
      {strategyEditor && selectedAccount && strategyProfile ? <AccountStrategyEditor account={selectedAccount} profile={strategyProfile} feedback={strategy?.feedback_plan} onClose={() => setStrategyEditor(false)} onSaved={account => accountSaved(account)} /> : null}
      {promptEditor && selectedAccount ? <AccountPromptEditor accountId={selectedAccount.id} domainSlug={DOMAIN_SLUG} source={promptEditor.source} onClose={() => setPromptEditor(null)} onSaved={() => { setMessage("提示词草稿已保存。"); void loadAccountDetails(selectedAccount.id); }} /> : null}

      <Card>
        <CardHeader>
          <CardTitle>{zh("\u8d26\u53f7\u9009\u62e9")}</CardTitle>
        </CardHeader>
        <CardContent>
          {loading ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              {zh("\u6b63\u5728\u52a0\u8f7d\u8d26\u53f7...")}
            </div>
          ) : rows.length === 0 ? (
            <div className="rounded-xl border border-border/60 bg-background/70 p-4 text-sm text-muted-foreground">
              {"当前还没有账号配置。"}
            </div>
          ) : (
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
              {rows.map((row) => {
                const isSelected = row.id === selectedAccountId;
                const persona = (row.config_jsonb?.strategy_profile as Record<string, unknown> | undefined)?.persona_name;
                return (
                  <button
                    key={row.id}
                    type="button"
                    onClick={() => void selectAccount(row.id)}
                    className={`rounded-xl border p-4 text-left transition ${
                      isSelected ? "border-primary/60 bg-primary/5" : "border-border/60 bg-background/70 hover:bg-muted/10"
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <p className="font-semibold">{row.account_name}</p>
                      <Badge variant={row.is_active ? "default" : "outline"}>
                        {row.is_active ? zh("\u542f\u7528\u4e2d") : zh("\u5df2\u505c\u7528")}
                      </Badge>
                    </div>
                    <p className="mt-2 text-xs text-muted-foreground">
                      {channelLabel(row.channel)} / {loginModeLabel(row.login_mode)}
                    </p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {zh("\u767b\u5f55\uff1a")}{statusLabel(row.last_login_check_status)}
                    </p>
                    <p className="mt-2 text-sm text-muted-foreground">{shortText(String(persona || ""), 42)}</p>
                  </button>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>

      {selectedAccount ? (
        <>
          <div className="grid gap-3 md:grid-cols-4 xl:grid-cols-8">
            {cards.map((item) => (
              <div key={item.label} className="rounded-xl border border-border/60 bg-background/80 p-3">
                <p className="text-xs text-muted-foreground">{item.label}</p>
                <p className="mt-1 text-sm font-semibold">{shortText(item.value, 26)}</p>
                <p className="mt-1 text-xs text-muted-foreground">{shortText(item.hint, 44)}</p>
              </div>
            ))}
          </div>

          <div id="security" className="flex scroll-mt-6 flex-wrap items-center gap-2">
            <Button variant="outline" onClick={() => setAccountEditor({ account: selectedAccount })} disabled={loading || !!busyMap[`active-${selectedAccount.id}`]}>
              <Pencil className="mr-2 h-4 w-4" />编辑账号
            </Button>
            <label className="flex items-center gap-2 px-2 text-sm">
              <input type="checkbox" role="switch" aria-label="账号启用" aria-checked={selectedAccount.is_active} checked={selectedAccount.is_active}
                disabled={loading || !!busyMap[`active-${selectedAccount.id}`]}
                onChange={event => {
                  const active = event.target.checked;
                  void withBusy(`active-${selectedAccount.id}`, async () => {
                    const updated = await updateChannelAccount(selectedAccount.id, { is_active: active });
                    setRows(prev => prev.map(item => item.id === updated.id ? updated : item));
                    setMessage(active ? "账号已启用。" : "账号已停用。");
                  });
                }} />
              {busyMap[`active-${selectedAccount.id}`] ? "保存中..." : "启用账号"}
            </label>
            <Button
              variant="outline"
              disabled={!!busyMap.verify}
              onClick={() =>
                void withBusy("verify", async () => {
                  await verifyChannelAccountLogin(selectedAccount.id);
                  setMessage(zh("\u5df2\u5b8c\u6210\u767b\u5f55\u72b6\u6001\u6821\u9a8c\u3002"));
                  await loadAccountDetails(selectedAccount.id);
                  await loadAccounts();
                })
              }
            >
              <ShieldCheck className="mr-2 h-4 w-4" />
              {busyMap.verify ? zh("\u6821\u9a8c\u4e2d...") : zh("\u6821\u9a8c\u767b\u5f55")}
            </Button>

            <Button
              variant="outline"
              disabled={!!busyMap.login}
              onClick={() =>
                void withBusy("login", async () => {
                  await launchChannelAccountLogin(selectedAccount.id);
                  setMessage(zh("\u5df2\u6253\u5f00\u767b\u5f55\u5f15\u5bfc\u7a97\u53e3\uff0c\u8bf7\u5728\u6d4f\u89c8\u5668\u5b8c\u6210\u767b\u5f55\u3002"));
                })
              }
            >
              <KeyRound className="mr-2 h-4 w-4" />
              {busyMap.login ? zh("\u6253\u5f00\u4e2d...") : zh("\u6253\u5f00\u767b\u5f55")}
            </Button>

            <Button
              variant="outline"
              disabled={!!busyMap.reset}
              onClick={() =>
                void withBusy("reset", async () => {
                  await resetChannelAccountAuth(selectedAccount.id);
                  setMessage(zh("\u5df2\u6e05\u7406\u5f53\u524d\u767b\u5f55\u6001\uff0c\u8bf7\u91cd\u65b0\u767b\u5f55\u3002"));
                  await loadAccountDetails(selectedAccount.id);
                })
              }
            >
              {busyMap.reset ? zh("\u91cd\u7f6e\u4e2d...") : zh("\u91cd\u7f6e\u767b\u5f55\u6001")}
            </Button>

            <Button
              disabled={!!busyMap.runloop || !selectedAccount.is_active}
              onClick={() =>
                void withBusy("runloop", async () => {
                  await runAccountLoopDaily(selectedAccount.id, { domain_slug: DOMAIN_SLUG, flow: "full", force: true });
                  setMessage(zh("\u5df2\u89e6\u53d1\u8be5\u8d26\u53f7\u7684\u4eca\u65e5\u95ed\u73af\u3002"));
                  await loadAccountDetails(selectedAccount.id);
                })
              }
            >
              <Play className="mr-2 h-4 w-4" />
              {busyMap.runloop ? zh("\u6267\u884c\u4e2d...") : zh("\u8fd0\u884c\u4eca\u65e5\u95ed\u73af")}
            </Button>
          </div>

          {detailLoading ? (
            <div className="rounded-xl border border-border/60 bg-background/70 p-4 text-sm text-muted-foreground">
              {zh("\u6b63\u5728\u52a0\u8f7d\u8d26\u53f7\u6cbb\u7406\u89c6\u56fe...")}
            </div>
          ) : (
            <div key={`${selectedAccount.id}-${panel || ""}`} className="space-y-6">
              <div id="profile" className="scroll-mt-6">
              <FoldableSection
                title={zh("\u8d26\u53f7\u57fa\u7840\u4e0e\u5b9a\u4f4d")}
                defaultOpen
                actions={<Button size="sm" variant="outline" disabled={!strategy || strategy.status === "degraded"} onClick={() => setStrategyEditor(true)}><Pencil className="mr-2 h-4 w-4" />编辑定位与策略</Button>}
                summary={zh("\u5148\u770b\u8fd9\u4e2a\u8d26\u53f7\u662f\u8c01\u3001\u505a\u4ec0\u4e48\u3001\u5bf9\u8c01\u8bf4\u3001\u4e0d\u80fd\u8bf4\u4ec0\u4e48\u3002")}
              >
                <div className="grid gap-4 xl:grid-cols-[1.1fr_0.9fr]">
                  <div className="space-y-4">
                    <div className="rounded-xl border border-border/60 bg-background/70 p-4">
                      <p className="text-sm font-semibold">{zh("\u8d26\u53f7\u57fa\u7840")}</p>
                      <div className="mt-3 space-y-1 text-sm text-muted-foreground">
                        <p>{zh("\u8d26\u53f7\u540d\u79f0\uff1a")}{selectedAccount.account_name}</p>
                        <p>{zh("\u8d26\u53f7\u6807\u8bc6\uff1a")}{selectedAccount.account_handle || zh("\u672a\u914d\u7f6e")}</p>
                        <p>{zh("\u6e20\u9053\uff1a")}{channelLabel(selectedAccount.channel)}</p>
                        <p>{zh("\u767b\u5f55\u65b9\u5f0f\uff1a")}{loginModeLabel(selectedAccount.login_mode)}</p>
                        <p>{zh("\u767b\u5f55\u72b6\u6001\uff1a")}{statusLabel(selectedAccount.last_login_check_status)}</p>
                        <p>{zh("\u5907\u6ce8\uff1a")}{selectedAccount.notes || zh("\u6682\u65e0")}</p>
                      </div>
                    </div>

                    <div className="rounded-xl border border-border/60 bg-background/70 p-4">
                      <p className="text-sm font-semibold">{zh("\u5b9a\u4f4d\u4e0e\u76ee\u6807")}</p>
                      <div className="mt-3 space-y-2 text-sm text-muted-foreground">
                        <p>{zh("\u4eba\u8bbe\uff1a")}{strategyProfile?.persona_name || zh("\u672a\u914d\u7f6e")}</p>
                        <p>{zh("IP \u5b9a\u4f4d\uff1a")}{strategyProfile?.ip_positioning || zh("\u672a\u914d\u7f6e")}</p>
                        <p>{zh("\u4e3b\u76ee\u6807\uff1a")}{strategyProfile?.primary_goal || zh("\u672a\u914d\u7f6e")}</p>
                        <p>{zh("\u8bed\u6c14\u98ce\u683c\uff1a")}{strategyProfile?.tone_style || zh("\u672a\u914d\u7f6e")}</p>
                        <p>{zh("CTA \u98ce\u683c\uff1a")}{strategyProfile?.cta_style || zh("\u672a\u914d\u7f6e")}</p>
                      </div>
                    </div>
                  </div>

                  <div className="space-y-4">
                    <BulletCard title={zh("\u76ee\u6807\u53d7\u4f17")} items={strategyProfile?.audience || []} />
                    <BulletCard title={zh("\u6838\u5fc3\u75db\u70b9")} items={strategyProfile?.pain_points || []} />
                    <BulletCard title={zh("\u5185\u5bb9\u652f\u67f1")} items={strategyProfile?.content_pillars || []} />
                    <BulletCard title={zh("\u7981\u7528\u627f\u8bfa")} items={strategyProfile?.forbidden_claims || []} />
                  </div>
                </div>
              </FoldableSection>

              </div>
              <FoldableSection
                title={zh("\u5b8c\u6574 SOP \u89c6\u56fe")}
                defaultOpen
                summary={zh("\u8fd9\u91cc\u770b\u5b8c\u6574\u65b9\u6cd5\u94fe\uff1a\u91c7\u96c6\u4e86\u4ec0\u4e48\u3001\u600e\u4e48\u5206\u6790\u3001\u600e\u4e48\u5199\u3001\u600e\u4e48\u590d\u76d8\u3001\u672c\u8f6e\u6539\u4e86\u4ec0\u4e48\u3002")}
              >
                <div className="space-y-4">
                  <div className="rounded-xl border border-border/60 bg-background/70 p-4">
                    <p className="text-sm font-semibold">{zh("\u5f53\u524d\u751f\u6548 SOP")}</p>
                    <p className="mt-2 text-sm text-muted-foreground">
                      {sopCurrent?.sop_latest
                        ? `v${sopCurrent.sop_latest.version} / ${sopCurrent.sop_latest.reason || zh("\u672a\u5199\u53d8\u66f4\u539f\u56e0")}`
                        : zh("\u6682\u672a\u751f\u6210\u7ed3\u6784\u5316 SOP \u5feb\u7167")}
                    </p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {sopCurrent?.sop_latest
                        ? `${zh("\u66f4\u65b0\u65f6\u95f4\uff1a")}${formatTime(sopCurrent.sop_latest.captured_at)} / ${zh("\u53d8\u66f4\u5b57\u6bb5\uff1a")}${(sopCurrent.sop_latest.changed_fields || []).join(" / ") || zh("\u65e0")}`
                        : zh("\u5efa\u8bae\u81f3\u5c11\u8dd1\u5b8c\u4e00\u8f6e\u91c7\u96c6\u3001\u5206\u6790\u3001\u6587\u6848\u3001\u590d\u76d8\u540e\u518d\u56fa\u5316\u4e3a SOP \u5feb\u7167\u3002")}
                    </p>
                  </div>

                  <div className="grid gap-4 xl:grid-cols-2">
                    <div className="rounded-xl border border-border/60 bg-background/70 p-4">
                      <p className="text-sm font-semibold">{zh("\u672c\u8f6e\u95ed\u73af\u8bf4\u660e")}</p>
                      <div className="mt-3 space-y-2 text-sm text-muted-foreground">
                        <p>{zh("\u5f53\u524d\u9636\u6bb5\uff1a")}{loopExecutionView?.current_stage || loopStatus?.loop.stage || zh("\u672a\u52a0\u8f7d")}</p>
                        <p>{zh("\u95ed\u73af\u5b8c\u6210\u5ea6\uff1a")}{loopExecutionView?.completion_score ?? loopStatus?.loop.completion_score ?? 0}%</p>
                        <p>{zh("\u6700\u8fd1\u4efb\u52a1\uff1a")}{shortText(String(latestTask?.title || latestTask?.topic || ""), 60)}</p>
                        <p>{zh("\u6700\u8fd1\u8fd0\u884c\uff1a")}{shortText(String(latestRun?.status || zh("\u6682\u65e0")), 60)}</p>
                        <p>{zh("\u5f53\u524d\u963b\u585e\uff1a")}{(loopStatus?.loop.blockers || []).join(" / ") || zh("\u65e0")}</p>
                      </div>
                    </div>

                    <div className="rounded-xl border border-border/60 bg-background/70 p-4">
                      <p className="text-sm font-semibold">{zh("\u590d\u76d8\u53e3\u5f84")}</p>
                      <div className="mt-3 space-y-2 text-sm text-muted-foreground">
                        <p>{zh("\u68c0\u67e5\u70b9\uff1a")}{feedbackPlan?.checkpoints_hours?.map((item: number) => `${item}h`).join(" -> ") || zh("\u672a\u914d\u7f6e")}</p>
                        <p>{feedbackPlan?.require_real_metrics_for_upgrade ? zh("\u53ea\u5141\u8bb8\u771f\u5b9e\u6307\u6807\u9a71\u52a8\u6b63\u5f0f\u5347\u7ea7") : zh("\u5141\u8bb8\u4e34\u65f6\u6307\u6807\u5148\u505a\u5185\u90e8\u9884\u89c8")}</p>
                        <p>{feedbackPlan?.synthetic_preview_enabled ? zh("\u7f3a\u771f\u5b9e\u6570\u636e\u65f6\u5141\u8bb8\u6f14\u793a\u9884\u89c8") : zh("\u7f3a\u771f\u5b9e\u6570\u636e\u65f6\u4e0d\u5c55\u793a\u9884\u89c8\u6307\u6807")}</p>
                        <p>{feedbackPlan?.auto_retro_after_last_checkpoint ? zh("\u6700\u540e\u4e00\u4e2a\u68c0\u67e5\u70b9\u540e\u81ea\u52a8\u8fdb\u5165\u590d\u76d8") : zh("\u6700\u540e\u4e00\u4e2a\u68c0\u67e5\u70b9\u540e\u7b49\u5f85\u4eba\u5de5\u89e6\u53d1\u590d\u76d8")}</p>
                      </div>
                    </div>
                  </div>

                  <div className="grid gap-4 xl:grid-cols-2">
                    <div className="rounded-xl border border-border/60 bg-background/70 p-4">
                      <p className="text-sm font-semibold">{zh("\u91c7\u96c6\u65b9\u6cd5")}</p>
                      <div className="mt-3 space-y-2 text-sm text-muted-foreground">
                        <p>{zh("\u91c7\u96c6\u6a21\u5f0f\uff1a")}{String(collectionPlan?.mode || zh("\u672a\u914d\u7f6e"))}</p>
                        <p>{zh("\u8bf4\u660e\uff1a")}{String(collectionPlan?.notes || zh("\u6682\u65e0\u8bf4\u660e"))}</p>
                        <div className="mt-2 space-y-2">
                          {Array.isArray(collectionPlan?.steps) && collectionPlan.steps.length > 0 ? (
                            collectionPlan.steps.map((step: Record<string, unknown>, index: number) => (
                              <div key={`${String(step.tool || "step")}-${index}`} className="rounded-lg border border-border/50 bg-muted/10 p-3">
                                <p>{index + 1}. {String(step.label || step.tool || zh("\u672a\u547d\u540d\u6b65\u9aa4"))}</p>
                                <p className="mt-1 text-xs">
                                  {zh("\u8303\u56f4\uff1a")}{step.scope === "account" ? zh("\u8d26\u53f7") : zh("\u516c\u5171")}
                                  {" / "}
                                  {zh("\u6570\u91cf\uff1a")}{String(step.limit || 0)}
                                  {step.query ? ` / ${zh("\u5173\u952e\u8bcd\uff1a")}${String(step.query)}` : ""}
                                </p>
                                <p className="mt-1 text-xs">{String(step.reason || zh("\u6682\u65e0\u8bf4\u660e"))}</p>
                              </div>
                            ))
                          ) : (
                            <div className="rounded-lg border border-border/50 bg-muted/10 p-3 text-xs">
                              {zh("\u5f53\u524d\u6ca1\u6709\u914d\u7f6e\u91c7\u96c6\u6b65\u9aa4\u3002")}
                            </div>
                          )}
                        </div>
                      </div>
                    </div>

                    <div className="rounded-xl border border-border/60 bg-background/70 p-4">
                      <p className="text-sm font-semibold">{zh("\u672c\u8f6e\u6267\u884c\u6b65\u9aa4")}</p>
                      <div className="mt-3 grid gap-3">
                        {(loopExecutionView?.steps || []).length > 0 ? (
                          (loopExecutionView?.steps || []).map((step) => (
                            <div key={step.step_key} className="rounded-xl border border-border/60 bg-background/70 p-4">
                              <div className="flex items-center justify-between gap-2">
                                <p className="text-sm font-semibold">{step.label}</p>
                                <Badge variant="outline">{step.status}</Badge>
                              </div>
                              <p className="mt-2 text-sm text-muted-foreground">{step.summary}</p>
                              <div className="mt-3 space-y-1 text-xs text-muted-foreground">
                                {step.method_summary.slice(0, 3).map((line) => (
                                  <p key={`${step.step_key}-m-${line}`}>{zh("\u65b9\u6cd5\uff1a")}{line}</p>
                                ))}
                                {step.evidence_summary.slice(0, 2).map((line) => (
                                  <p key={`${step.step_key}-e-${line}`}>{zh("\u4f9d\u636e\uff1a")}{line}</p>
                                ))}
                                {step.result_summary.slice(0, 2).map((line) => (
                                  <p key={`${step.step_key}-r-${line}`}>{zh("\u7ed3\u679c\uff1a")}{line}</p>
                                ))}
                              </div>
                            </div>
                          ))
                        ) : (
                          <div className="rounded-lg border border-border/50 bg-muted/10 p-3 text-sm text-muted-foreground">
                            {zh("\u5f53\u524d\u8fd8\u6ca1\u6709\u53ef\u5c55\u793a\u7684\u6267\u884c\u6b65\u9aa4\u3002")}
                          </div>
                        )}
                      </div>
                    </div>
                  </div>

                  <div className="rounded-xl border border-border/60 bg-background/70 p-4">
                    <p className="text-sm font-semibold">{zh("\u65b9\u6cd5\u8bba\u8865\u5145")}</p>
                    <div className="mt-3 grid gap-3 xl:grid-cols-2">
                      {(contentMethodView?.sections || []).length > 0 ? (
                        (contentMethodView?.sections || []).map((section) => (
                          <div key={section.section_key} className="rounded-lg border border-border/50 bg-muted/10 p-3">
                            <p className="font-medium text-foreground">{section.title}</p>
                            <p className="mt-1 text-sm text-muted-foreground">{section.summary}</p>
                            <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
                              {section.bullets.slice(0, 4).map((line) => (
                                <li key={line}>- {line}</li>
                              ))}
                            </ul>
                          </div>
                        ))
                      ) : (
                        <div className="rounded-lg border border-border/50 bg-muted/10 p-3 text-sm text-muted-foreground">
                          {zh("\u5f53\u524d\u8fd8\u6ca1\u6709\u7ed3\u6784\u5316\u65b9\u6cd5\u8bba\u89c6\u56fe\u3002")}
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              </FoldableSection>

              <div id="settings" className="scroll-mt-6">
              <FoldableSection
                title={zh("\u6a21\u578b\u4e0e\u5b50 Agent \u914d\u7f6e")}
                defaultOpen
                summary={zh("\u8fd9\u91cc\u56de\u7b54 4 \u4e2a\u95ee\u9898\uff1a\u8c01\u8d1f\u8d23\u3001\u7528\u4ec0\u4e48\u6a21\u578b\u3001\u63d0\u793a\u8bcd\u4ece\u54ea\u6765\u3001\u8c03\u7528\u54ea\u4e9b\u5de5\u5177\u3002")}
              >
                <div className="space-y-4">
                  {coachNode ? (
                    <div>
                      <p className="mb-3 text-sm font-semibold">{zh("\u4e3b\u5165\u53e3\u603b\u6559\u7ec3")}</p>
                      <AgentNodeCard node={coachNode} />
                    </div>
                  ) : null}

                  <div>
                    <p className="mb-3 text-sm font-semibold">{zh("4 \u4e2a\u72ec\u7acb\u5b50 Agent")}</p>
                    <div className="grid gap-4 xl:grid-cols-2">
                      {subAgents.length > 0 ? (
                        subAgents.map((node) => <AgentNodeCard key={node.agent_key} node={node} />)
                      ) : (
                        <div className="rounded-lg border border-border/50 bg-muted/10 p-3 text-sm text-muted-foreground xl:col-span-2">
                          {zh("\u5f53\u524d\u8fd8\u6ca1\u6709\u53ef\u5c55\u793a\u7684\u5b50 Agent \u914d\u7f6e\u3002")}
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              </FoldableSection>

              </div>
              <FoldableSection
                title={zh("\u6267\u884c\u8def\u7531\u4e0e\u8fd0\u884c\u72b6\u6001")}
                defaultOpen
                summary={zh("\u7528\u6237\u53ea\u9700\u8981\u77e5\u9053\u73b0\u5728\u8d70\u54ea\u6761\u6267\u884c\u8def\u5f84\u3001\u4e3a\u4ec0\u4e48\u5207\u6362\u3001\u5f53\u524d\u8fd0\u884c\u662f\u5426\u6b63\u5e38\u3002")}
              >
                <div className="grid gap-4 xl:grid-cols-2">
                  <div className="rounded-xl border border-border/60 bg-background/70 p-4 text-sm text-muted-foreground">
                    <p className="font-semibold text-foreground">{zh("\u6267\u884c\u8def\u7531")}</p>
                    <p className="mt-2">{zh("\u5f53\u524d\u72b6\u6001\uff1a")}{routeStatusLabel(executionRouteView)}</p>
                    <p className="mt-1">{zh("\u4e3b\u6267\u884c\u8def\u5f84\uff1a")}{executionRouteView?.primary_route || zh("\u672a\u914d\u7f6e")}</p>
                    <p className="mt-1">{zh("\u5907\u7528\u8def\u5f84\uff1a")}{executionRouteView?.backup_route || zh("\u672a\u914d\u7f6e")}</p>
                    <p className="mt-1">{zh("\u53ea\u8bfb\u6865\u63a5\uff1a")}{executionRouteView?.readonly_bridge || zh("\u672a\u914d\u7f6e")}</p>
                    <div className="mt-3 rounded-lg border border-border/50 bg-muted/10 p-3 text-xs">
                      {(executionRouteView?.switch_rules || []).length > 0 ? (
                        executionRouteView?.switch_rules.map((line) => <p key={line}>- {line}</p>)
                      ) : (
                        <p>{zh("\u5f53\u524d\u6ca1\u6709\u5207\u6362\u89c4\u5219\u8bf4\u660e\u3002")}</p>
                      )}
                    </div>
                  </div>

                  <div className="rounded-xl border border-border/60 bg-background/70 p-4 text-sm text-muted-foreground">
                    <p className="font-semibold text-foreground">{zh("\u8fd0\u884c\u4e0a\u4e0b\u6587")}</p>
                    <p className="mt-2">{zh("\u6267\u884c\u72b6\u6001\uff1a")}{runtimeContext?.execution.status || zh("\u672a\u52a0\u8f7d")}</p>
                    <p className="mt-1">{zh("\u662f\u5426\u7e41\u5fd9\uff1a")}{runtimeContext?.execution.busy ? zh("\u662f") : zh("\u5426")}</p>
                    <p className="mt-1">{zh("\u5f53\u524d\u52a8\u4f5c\uff1a")}{String(runtimeContext?.execution.current_action || zh("\u6682\u65e0"))}</p>
                    <p className="mt-1">{zh("\u6700\u8fd1\u4f7f\u7528\uff1a")}{formatTime(runtimeContext?.execution.last_used_at as string | null | undefined)}</p>
                    <p className="mt-1">{zh("\u6700\u8fd1\u5f02\u5e38\uff1a")}{shortText(String(runtimeContext?.execution.last_error || ""), 80)}</p>
                  </div>
                </div>
              </FoldableSection>

              <div id="strategy" className="scroll-mt-6">
              <FoldableSection
                title={zh("\u63d0\u793a\u8bcd\u7248\u672c\u4e0e\u7b56\u7565\u52a8\u4f5c")}
                defaultOpen
                actions={<Button size="sm" variant="outline" onClick={() => setPromptEditor({ source: null })}><Plus className="mr-2 h-4 w-4" />新增提示词版本</Button>}
                summary={zh("\u8fd9\u91cc\u770b\u5f53\u524d\u6709\u54ea\u4e9b\u7248\u672c\u5728\u7528\uff0c\u4ee5\u53ca\u672c\u8f6e\u6709\u54ea\u4e9b\u5f85\u786e\u8ba4\u7684\u7b56\u7565\u53d8\u66f4\u3002")}
              >
                <div className="grid gap-4 xl:grid-cols-[1fr_1fr]">
                  <div className="space-y-3">
                    <div className="rounded-xl border border-border/60 bg-background/70 p-4">
                      <p className="text-sm font-semibold">{zh("\u63d0\u793a\u8bcd\u7248\u672c")}</p>
                      <div className="mt-3 space-y-3">
                        {promptVersions.length === 0 ? (
                          <p className="text-sm text-muted-foreground">{zh("\u5f53\u524d\u6ca1\u6709\u53ef\u5c55\u793a\u7684\u63d0\u793a\u8bcd\u7248\u672c\u3002")}</p>
                        ) : (
                          promptVersions.map((item) => (
                            <div key={item.id} className="rounded-lg border border-border/50 bg-muted/10 p-3">
                              <div className="flex items-center justify-between gap-2">
                                <p className="font-medium text-foreground">{agentKeyLabel(item.agent_name)}</p>
                                <Badge variant={item.status === "active" ? "default" : "outline"}>{({ active: "当前生效", draft: "草稿", archived: "已归档", rolled_back: "已回退" } as Record<string, string>)[item.status] || item.status}</Badge>
                              </div>
                              <p className="mt-1 text-xs text-muted-foreground">
                                {zh("\u7248\u672c\uff1a")}{item.version}
                                {" / "}
                                {zh("\u6765\u6e90\uff1a")}{promptSourceLabel(item.source)}
                                {" / "}
                                {zh("\u66f4\u65b0\u65f6\u95f4\uff1a")}{formatTime(item.updated_at)}
                              </p>
                              <p className="mt-2 text-sm text-muted-foreground">{shortText(item.reason || zh("\u6682\u65e0\u7248\u672c\u8bf4\u660e"), 90)}</p>
                              <details className="mt-2 text-sm"><summary className="cursor-pointer">提示词全文</summary><p className="mt-2 whitespace-pre-wrap break-words text-muted-foreground">{item.system_prompt}</p></details>
                              <div className="mt-3 flex flex-wrap gap-2">
                                <Button size="sm" variant="outline" disabled={!!busyMap.prompts} onClick={() => setPromptEditor({ source: item })}><Pencil className="mr-2 h-4 w-4" />编辑为新版本</Button>
                                {item.status !== "active" ? <Button size="sm" variant="outline" disabled={!!busyMap.prompts} onClick={() => void withBusy("prompts", async () => {
                                  await activateAccountPromptVersion(selectedAccount.id, item.id);
                                  setMessage("提示词版本已生效。");
                                  await loadAccountDetails(selectedAccount.id);
                                })}>{busyMap.prompts ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Check className="mr-2 h-4 w-4" />}设为当前版本</Button> : null}
                              </div>
                            </div>
                          ))
                        )}
                      </div>
                    </div>

                    <div className="rounded-xl border border-border/60 bg-background/70 p-4">
                      <p className="text-sm font-semibold">{zh("SOP \u5386\u53f2\u5feb\u7167")}</p>
                      <div className="mt-3 space-y-3">
                        {sopSnapshots.length === 0 ? (
                          <p className="text-sm text-muted-foreground">{zh("\u6682\u65e0\u5386\u53f2\u5feb\u7167\u3002")}</p>
                        ) : (
                          sopSnapshots.map((item) => (
                            <div key={`${item.version}-${item.captured_at}`} className="rounded-lg border border-border/50 bg-muted/10 p-3 text-sm text-muted-foreground">
                              <p className="font-medium text-foreground">{`v${item.version} / ${item.reason || zh("\u672a\u5199\u539f\u56e0")}`}</p>
                              <p className="mt-1">{zh("\u65f6\u95f4\uff1a")}{formatTime(item.captured_at)}</p>
                              <p className="mt-1">{zh("\u53d8\u66f4\u5b57\u6bb5\uff1a")}{(item.changed_fields || []).join(" / ") || zh("\u65e0")}</p>
                            </div>
                          ))
                        )}
                      </div>
                    </div>
                  </div>

                  <div className="rounded-xl border border-border/60 bg-background/70 p-4">
                    <p className="text-sm font-semibold">{zh("\u5f85\u786e\u8ba4\u7b56\u7565\u52a8\u4f5c")}</p>
                    <div className="mt-3 space-y-3">
                      {pendingStrategyItems.length === 0 ? (
                        <p className="text-sm text-muted-foreground">{zh("\u5f53\u524d\u6ca1\u6709\u5f85\u786e\u8ba4\u7b56\u7565\u52a8\u4f5c\u3002")}</p>
                      ) : (
                        pendingStrategyItems.map((item) => (
                          <div key={item.id} className="rounded-lg border border-border/50 bg-muted/10 p-3">
                            <div className="flex items-center justify-between gap-2">
                              <p className="font-medium text-foreground">{item.title}</p>
                              <Badge variant="outline">{`${zh("\u7f6e\u4fe1\u5ea6")} ${item.confidence.toFixed(2)}`}</Badge>
                            </div>
                            <p className="mt-2 text-sm text-muted-foreground">{shortText(item.content, 160)}</p>
                            {item.proposed_changes?.[0] ? (
                              <div className="mt-2 rounded-lg border border-border/50 bg-background/50 p-3 text-xs text-muted-foreground">
                                <p>{zh("\u76ee\u6807\uff1a")}{changeTargetLabel(item.proposed_changes[0].target)}</p>
                                <p className="mt-1">{zh("\u539f\u56e0\uff1a")}{item.proposed_changes[0].reason}</p>
                                <p className="mt-1">{zh("\u6539\u540e\uff1a")}{item.proposed_changes[0].after_summary}</p>
                              </div>
                            ) : null}
                            {item.proposed_changes?.[0] ? (
                              <div className="mt-3 flex gap-2">
                                <Button
                                  size="sm"
                                  disabled={!!busyMap[`apply-${item.id}`]}
                                  onClick={() =>
                                    void withBusy(`apply-${item.id}`, async () => {
                                      await applyPendingStrategyItemChange(item.id, {
                                        accountId: selectedAccount.id,
                                        domainSlug: DOMAIN_SLUG,
                                        target: item.proposed_changes?.[0]?.target || "collection_plan",
                                      });
                                      setMessage(`${zh("\u5df2\u5e94\u7528\u7b56\u7565\u52a8\u4f5c\uff1a")}${item.title}`);
                                      await loadAccountDetails(selectedAccount.id);
                                    })
                                  }
                                >
                                  {zh("\u5e94\u7528\u4fee\u6539")}
                                </Button>
                              </div>
                            ) : null}
                          </div>
                        ))
                      )}
                    </div>
                  </div>
                </div>
              </FoldableSection>
              </div>
            </div>
          )}
        </>
      ) : null}
      <div id="knowledge" className="min-w-0 scroll-mt-6 space-y-4 border-t border-border/60 pt-6">
        <label className="flex flex-wrap items-center gap-2 text-sm">
          知识范围
          <select aria-label="知识范围" className="h-10 rounded-lg border border-border bg-background px-3" value={selectedAccount ? knowledgeScope : "all"} onChange={event => setKnowledgeScope(event.target.value)}>
            {selectedAccount ? <option value="account">当前账号</option> : null}
            <option value="all">当前领域全部账号</option>
          </select>
        </label>
        <KnowledgeWorkspace accountId={knowledgeScope === "account" ? selectedAccount?.id : undefined} domainSlug={DOMAIN_SLUG} />
      </div>
    </div>
  );
}
