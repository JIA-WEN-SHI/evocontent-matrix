"use client";

import { usePathname } from "next/navigation";
import Sidebar from "@/components/Sidebar";
import AppHeader from "@/components/AppHeader";
import GlobalAssistantDock from "@/components/GlobalAssistantDock";
import ConnectionStatus from "@/components/ConnectionStatus";

export default function AdminShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const isLanding = pathname === "/";
  if (isLanding) {
    return <div className="min-h-screen bg-slate-950"><ConnectionStatus />{children}</div>;
  }
  return (
    <div className="relative min-h-screen text-foreground">
      <div className="pointer-events-none fixed inset-0 -z-10" />
      <Sidebar />
      <div className="min-h-screen md:ml-64">
        <AppHeader />
        <ConnectionStatus />
        <main className="px-4 py-5 md:px-6">{children}</main>
      </div>
      <GlobalAssistantDock />
    </div>
  );
}
