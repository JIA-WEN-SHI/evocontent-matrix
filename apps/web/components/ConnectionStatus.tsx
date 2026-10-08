"use client";

import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";
import { getSystemReadiness } from "@/lib/api";

export default function ConnectionStatus() {
  const [message, setMessage] = useState("");
  const [checking, setChecking] = useState(false);
  const check = useCallback(async () => {
    if (new URLSearchParams(window.location.search).get("demo") === "1") return;
    setChecking(true);
    try {
      const result = await getSystemReadiness();
      setMessage(result.database?.status === "unavailable"
        ? "数据服务连接不可用，当前列表可能不完整，保存和执行暂不可用。"
        : !result.core_ready
          ? "数据服务尚未准备完成，部分功能暂不可用。"
          : !result.agent_health.reachable
            ? "执行服务暂时无法连接，现有数据仍可查看。"
            : "");
    } catch {
      setMessage("暂时无法连接后台服务，请检查服务状态后重试。");
    } finally {
      setChecking(false);
    }
  }, []);

  useEffect(() => { void check(); }, [check]);

  if (!message) return null;
  return (
    <div role="status" className="flex items-start gap-2 border-b border-amber-400/40 bg-amber-950 px-4 py-3 text-sm text-amber-100">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
      <p className="min-w-0 flex-1">{message}</p>
      <button type="button" title="重新检查连接" aria-label="重新检查连接" disabled={checking}
        onClick={() => void check()} className="shrink-0 rounded p-1 hover:bg-amber-900 disabled:opacity-50">
        <RefreshCw className={`h-4 w-4 ${checking ? "animate-spin" : ""}`} />
      </button>
    </div>
  );
}
