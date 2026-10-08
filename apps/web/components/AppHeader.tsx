"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ChevronRight } from "lucide-react";

import ThemeToggle from "@/components/ThemeToggle";

const routeMap: Record<string, string> = {
  "": "主屏闭环",
  dashboard: "数据总览",
  "legacy-flow": "流程执行与手动修复",
  accounts: "账号管理",
  tasks: "帖子详情",
  employee: "流程执行与手动修复",
  settings: "账号管理",
  security: "安全设置",
};

function breadcrumbFromPath(pathname: string) {
  const parts = pathname.split("/").filter(Boolean);
  if (parts.length === 0) return ["控制台"];
  if (parts[0] === "tasks") return ["控制台", "帖子详情"];
  return ["控制台", ...parts.map((part) => routeMap[part] ?? part)];
}

export default function AppHeader() {
  const pathname = usePathname();
  const crumbs = breadcrumbFromPath(pathname);

  return (
    <header className="sticky top-0 z-20 border-b border-cyan-400/18 bg-[#031426] px-4 py-3 backdrop-blur-xl md:px-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-1 text-sm text-cyan-200/70">
          {crumbs.map((item, idx) => (
            <span key={`${item}-${idx}`} className="flex items-center">
              {idx > 0 && <ChevronRight className="mx-1 h-3.5 w-3.5 text-cyan-300/55" />}
              <span className={idx === crumbs.length - 1 ? "font-semibold text-cyan-50" : ""}>{item}</span>
            </span>
          ))}
        </div>

        <div className="flex items-center gap-3">
          <ThemeToggle />
          <div className="flex items-center gap-2 rounded-xl border border-cyan-300/35 bg-slate-950/60 px-2.5 py-1.5">
            <div className="h-7 w-7 rounded-full bg-gradient-to-br from-cyan-300 to-blue-500 shadow-[0_0_14px_rgba(56,189,248,0.5)]" />
            <div className="text-right leading-tight">
              <p className="text-xs text-cyan-300/70">当前用户</p>
              <p className="text-sm font-semibold text-cyan-50">超级管理员</p>
            </div>
          </div>
        </div>
      </div>

      <div className="mt-3 flex gap-2 overflow-auto md:hidden">
        <Link className="rounded-md border border-cyan-300/35 bg-slate-950/55 px-2 py-1 text-xs text-cyan-100" href="/">
          主屏
        </Link>
        <Link className="rounded-md border border-cyan-300/35 bg-slate-950/55 px-2 py-1 text-xs text-cyan-100" href="/accounts">
          账号
        </Link>
        <Link className="shrink-0 rounded-md border border-cyan-300/35 bg-slate-950/55 px-2 py-1 text-xs text-cyan-100" href="/dashboard">
          数据总览
        </Link>
        <Link className="shrink-0 rounded-md border border-cyan-300/35 bg-slate-950/55 px-2 py-1 text-xs text-cyan-100" href="/legacy-flow">
          流程执行
        </Link>
      </div>
    </header>
  );
}
