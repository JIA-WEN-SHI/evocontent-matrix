import Link from "next/link";
import LegacyFlowWorkspace from "@/components/legacy/LegacyFlowWorkspace";

export default function LegacyFlowPage() {
  return (
    <div className="min-h-screen bg-background">
      <div className="sticky top-0 z-20 border-b border-border/60 bg-background/92 px-4 py-3 backdrop-blur">
        <div className="mx-auto flex w-full max-w-[1600px] items-center justify-between gap-3">
          <div>
            <p className="text-sm font-semibold">流程执行与手动修复</p>
            <p className="text-xs text-muted-foreground">
              保留整轮闭环执行视图，用于查看进度、人工修正、定位问题和确认策略变更。
            </p>
          </div>
          <div className="flex items-center gap-2 text-xs">
            <Link className="rounded border border-border px-2 py-1 hover:bg-muted/40" href="/">
              返回主屏
            </Link>
            <Link className="rounded border border-border px-2 py-1 hover:bg-muted/40" href="/dashboard">
              数据总览
            </Link>
            <Link className="rounded border border-border px-2 py-1 hover:bg-muted/40" href="/accounts">
              账号管理
            </Link>
          </div>
        </div>
      </div>
      <LegacyFlowWorkspace />
    </div>
  );
}
