"use client";

import type { ComponentType } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { BarChart3, LayoutDashboard, LockKeyhole, Workflow } from "lucide-react";

import { cn } from "@/lib/utils";

type MenuItem = {
  href: string;
  label: string;
  desc: string;
  icon: ComponentType<{ className?: string }>;
};

const coreMenus: MenuItem[] = [
  {
    href: "/",
    label: "\u4e3b\u5c4f\u95ed\u73af",
    desc: "\u603b\u6559\u7ec3\u5bf9\u8bdd\u3001\u4eca\u65e5\u52a8\u4f5c\u3001\u4e3b\u6d41\u7a0b\u5165\u53e3",
    icon: LayoutDashboard,
  },
  {
    href: "/dashboard",
    label: "\u6570\u636e\u603b\u89c8",
    desc: "\u56fe\u6807\u5361\u7247\u3001\u8d8b\u52bf\u5206\u6790\u3001\u7ed3\u679c\u8868\u73b0",
    icon: BarChart3,
  },
  {
    href: "/legacy-flow",
    label: "\u6d41\u7a0b\u6267\u884c\u4e0e\u624b\u52a8\u4fee\u590d",
    desc: "\u91c7\u96c6\u3001\u5206\u6790\u3001\u5199\u7a3f\u3001\u5ba1\u6838\u3001\u53d1\u5e03\u3001\u590d\u76d8",
    icon: Workflow,
  },
  {
    href: "/accounts",
    label: "\u8d26\u53f7\u7ba1\u7406",
    desc: "\u8d26\u53f7\u914d\u7f6e\u3001\u6a21\u578b\u914d\u7f6e\u3001SOP \u4e0e\u65b9\u6cd5\u5e93",
    icon: LockKeyhole,
  },
];

function MenuLink({ menu, pathname }: { menu: MenuItem; pathname: string }) {
  const isActive = pathname === menu.href || (menu.href !== "/" && pathname.startsWith(`${menu.href}/`));
  const Icon = menu.icon;

  return (
    <Link
      href={menu.href}
      className={cn(
        "block rounded-xl border px-3 py-2.5 transition-all",
        isActive
          ? "border-cyan-300/55 bg-cyan-400/18 text-cyan-100 shadow-[0_0_22px_rgba(56,189,248,0.24)]"
          : "border-cyan-900/45 bg-slate-950/30 text-cyan-100/85 hover:border-cyan-300/45 hover:bg-cyan-900/25 hover:text-cyan-100",
      )}
    >
      <div className="flex items-start gap-3">
        <Icon className={cn("mt-0.5 h-4 w-4 shrink-0", isActive ? "text-cyan-100" : "text-cyan-300/80")} />
        <div className="min-w-0">
          <p className="text-sm font-medium">{menu.label}</p>
          <p className={cn("mt-0.5 text-xs", isActive ? "text-cyan-100/80" : "text-cyan-300/65")}>{menu.desc}</p>
        </div>
      </div>
    </Link>
  );
}

export default function Sidebar() {
  const pathname = usePathname();
  const currentStep = pathname.startsWith("/accounts")
    ? "\u8d26\u53f7\u6cbb\u7406"
    : pathname.startsWith("/dashboard")
      ? "\u6570\u636e\u603b\u89c8"
      : pathname.startsWith("/legacy-flow")
        ? "\u6d41\u7a0b\u6267\u884c\u4e0e\u624b\u52a8\u4fee\u590d"
        : "\u4e3b\u5c4f\u95ed\u73af";

  return (
    <aside className="fixed inset-y-0 left-0 z-30 hidden w-64 border-r border-cyan-400/20 bg-[#031426] backdrop-blur-xl md:flex md:flex-col">
      <div className="border-b border-cyan-400/18 px-5 py-5">
        <p className="text-[11px] tracking-[0.22em] text-cyan-300/78">NEUROLAB CONTROL</p>
        <h1 className="mt-1 text-[22px] font-semibold text-cyan-50">{"\u6570\u5b57\u5458\u5de5\u4e2d\u53f0"}</h1>
      </div>

      <nav className="flex-1 overflow-y-auto px-3 py-4">
        <div className="space-y-1">
          <p className="px-3 text-[11px] font-medium tracking-[0.08em] text-cyan-300/70">{"\u6838\u5fc3\u5165\u53e3"}</p>
          {coreMenus.map((menu) => (
            <MenuLink key={menu.href} menu={menu} pathname={pathname} />
          ))}
        </div>
      </nav>

      <div className="border-t border-cyan-400/18 px-5 py-4 text-xs text-cyan-300/74">
        <p>{"V3 \u5355\u8d26\u53f7\u95ed\u73af"}</p>
        <p className="mt-1 text-cyan-100/80">{currentStep}</p>
      </div>
    </aside>
  );
}
