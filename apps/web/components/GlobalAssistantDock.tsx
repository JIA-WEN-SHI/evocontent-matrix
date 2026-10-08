"use client";

import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Bot, ChevronDown, ChevronUp, Send, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { chatWithCoach } from "@/lib/api";
import { toUserFacingError } from "@/lib/user-facing-errors";

type DockRole = "user" | "assistant";

type DockNavLink = {
  href: string;
  label: string;
};

type DockMessage = {
  id: string;
  role: DockRole;
  content: string;
  navLinks?: DockNavLink[];
  createdAt: string;
};

const STORAGE_KEY = "global_coach_dock_v3";
const DEFAULT_NAV: DockNavLink[] = [
  { href: "/", label: "主屏闭环" },
  { href: "/dashboard", label: "数据总览" },
  { href: "/legacy-flow", label: "流程执行与手动修复" },
  { href: "/accounts", label: "账号管理" },
];

function nowIso(): string {
  return new Date().toISOString();
}

function uid(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function normalizeText(value: string): string {
  return (value || "").replace(/\s+/g, " ").trim();
}

function dedupeLinks(items: DockNavLink[]): DockNavLink[] {
  const seen = new Set<string>();
  const result: DockNavLink[] = [];
  for (const item of items) {
    const key = `${item.href}|${item.label}`;
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(item);
  }
  return result;
}

function linksByText(text: string): DockNavLink[] {
  const raw = normalizeText(text);
  const out: DockNavLink[] = [];
  if (!raw) return out;
  if (/(账号|登录|人设|定位|提示词|SOP|策略)/.test(raw)) out.push({ href: "/accounts", label: "去账号管理" });
  if (/(采集|热点|公共数据|账号数据)/.test(raw)) out.push({ href: "/?panel=public", label: "去主屏采集区" });
  if (/(审核|发布|草稿|待审)/.test(raw)) out.push({ href: "/?panel=publish", label: "去主屏草稿区" });
  if (/(复盘|分析|建议|方法论)/.test(raw)) out.push({ href: "/?panel=sop", label: "去主屏SOP区" });
  return out;
}

function toHistory(messages: DockMessage[]): Array<{ role: "user" | "assistant"; content: string }> {
  return messages
    .filter((m) => m.role === "user" || m.role === "assistant")
    .slice(-12)
    .map((m) => ({ role: m.role, content: m.content.slice(0, 1200) }));
}

function loadPersistedMessages(): DockMessage[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as DockMessage[];
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((x) => x && typeof x.content === "string" && (x.role === "user" || x.role === "assistant"));
  } catch {
    return [];
  }
}

export default function GlobalAssistantDock() {
  const pathname = usePathname();
  const listRef = useRef<HTMLDivElement | null>(null);
  const [open, setOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<DockMessage[]>([
    {
      id: uid("assistant"),
      role: "assistant",
      content: "我是总教练。你可以直接告诉我今天要推进采集、生成草稿、审核还是复盘。",
      navLinks: [{ href: "/", label: "去主屏闭环" }],
      createdAt: nowIso(),
    },
  ]);

  useEffect(() => {
    const persisted = loadPersistedMessages();
    if (persisted.length > 0) setMessages(persisted.slice(-80));
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(messages.slice(-80)));
  }, [messages]);

  useEffect(() => {
    if (!open || collapsed) return;
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, open, collapsed]);

  const quickLinks = useMemo(() => {
    return DEFAULT_NAV.map((item) => ({
      ...item,
      active: pathname === item.href || pathname.startsWith(`${item.href}/`),
    }));
  }, [pathname]);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const content = normalizeText(input);
    if (!content || busy) return;
    const nextMessages: DockMessage[] = [
      ...messages,
      { id: uid("user"), role: "user", content, createdAt: nowIso() },
    ];
    setMessages(nextMessages);
    setInput("");
    setBusy(true);
    try {
      const response = await chatWithCoach({
        message: content,
        conversation_history: toHistory(nextMessages),
      });
      const navLinks = dedupeLinks([
        ...linksByText(response.coach_reply || ""),
        ...(response.pending_actions?.length ? [{ href: "/", label: "去主屏确认动作" }] : []),
      ]).slice(0, 5);
      setMessages((prev) => [
        ...prev,
        {
          id: uid("assistant"),
          role: "assistant",
          content: response.coach_reply || "已收到。",
          navLinks: navLinks.length ? navLinks : [{ href: "/", label: "回到主屏继续" }],
          createdAt: nowIso(),
        },
      ]);
    } catch (err) {
      const msg = toUserFacingError(err, "当前暂时无法完成本次请求，请稍后重试。");
      setMessages((prev) => [
        ...prev,
        {
          id: uid("assistant"),
          role: "assistant",
          content: msg,
          navLinks: [{ href: "/", label: "回到主屏检查" }],
          createdAt: nowIso(),
        },
      ]);
    } finally {
      setBusy(false);
    }
  }

  function clearHistory() {
    setMessages([
      {
        id: uid("assistant"),
        role: "assistant",
        content: "会话已清空。继续告诉我今天要推进哪一步。",
        navLinks: [{ href: "/", label: "去主屏继续" }],
        createdAt: nowIso(),
      },
    ]);
  }

  return (
    <>
      {!open ? (
        <Button type="button" onClick={() => setOpen(true)} className="fixed bottom-5 right-5 z-40 h-12 rounded-full px-4 shadow-lg">
          <Bot className="mr-2 h-4 w-4" />
          总教练
        </Button>
      ) : null}

      {open ? (
        <div className="fixed bottom-5 right-5 z-40 w-[380px] max-w-[calc(100vw-24px)] rounded-xl border border-border/70 bg-card shadow-2xl">
          <div className="flex items-center justify-between border-b border-border/60 px-3 py-2">
            <div className="flex items-center gap-2">
              <div className="inline-flex h-8 w-8 items-center justify-center rounded-full bg-primary/10 text-primary">
                <Bot className="h-4 w-4" />
              </div>
              <div className="leading-tight">
                <p className="text-sm font-semibold">总教练</p>
                <p className="text-[11px] text-muted-foreground">全局常驻入口</p>
              </div>
            </div>
            <div className="flex items-center gap-1">
              <Button type="button" variant="ghost" size="sm" className="h-7 w-7 px-0" onClick={() => setCollapsed((v) => !v)}>
                {collapsed ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
              </Button>
              <Button type="button" variant="ghost" size="sm" className="h-7 w-7 px-0" onClick={() => setOpen(false)}>
                <X className="h-4 w-4" />
              </Button>
            </div>
          </div>

          {!collapsed ? (
            <>
              <div className="border-b border-border/60 px-3 py-2">
                <div className="flex flex-wrap gap-2">
                  {quickLinks.map((item) => (
                    <Link
                      key={item.href}
                      href={item.href}
                      className={`rounded-full border px-2 py-1 text-[11px] ${item.active ? "border-primary/60 bg-primary/10 text-primary" : "border-border/70 text-muted-foreground hover:border-primary/40 hover:text-foreground"}`}
                    >
                      {item.label}
                    </Link>
                  ))}
                </div>
              </div>

              <div ref={listRef} className="max-h-[340px] space-y-3 overflow-y-auto px-3 py-3">
                {messages.map((msg) => (
                  <div key={msg.id} className={`rounded-lg border px-3 py-2 ${msg.role === "assistant" ? "border-primary/20 bg-primary/5" : "border-border/70 bg-muted/20"}`}>
                    <p className="text-[11px] text-muted-foreground">{msg.role === "assistant" ? "教练" : "你"}</p>
                    <p className="mt-1 whitespace-pre-wrap break-words text-sm">{msg.content}</p>
                    {msg.navLinks?.length ? (
                      <div className="mt-2 flex flex-wrap gap-2">
                        {msg.navLinks.map((link) => (
                          <Link key={`${msg.id}-${link.href}-${link.label}`} href={link.href} className="rounded-full border border-border/70 px-2 py-1 text-[11px] text-muted-foreground hover:border-primary/40 hover:text-foreground">
                            {link.label}
                          </Link>
                        ))}
                      </div>
                    ) : null}
                  </div>
                ))}
              </div>

              <form onSubmit={onSubmit} className="border-t border-border/60 px-3 py-3">
                <div className="flex items-center gap-2">
                  <Input
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    placeholder="告诉教练你的目标，例如：先看今天该发什么"
                    disabled={busy}
                  />
                  <Button type="submit" size="sm" disabled={busy}>
                    <Send className="h-4 w-4" />
                  </Button>
                </div>
                <div className="mt-2 flex items-center justify-between">
                  <button type="button" onClick={clearHistory} className="text-[11px] text-muted-foreground hover:text-foreground">
                    清空会话
                  </button>
                  <p className="text-[11px] text-muted-foreground">内部由总教练协同 4 个执行环节</p>
                </div>
              </form>
            </>
          ) : null}
        </div>
      ) : null}
    </>
  );
}
