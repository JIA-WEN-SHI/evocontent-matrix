"use client";

import { ReactNode, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";

type FoldableSectionProps = {
  title: string;
  summary?: string;
  defaultOpen?: boolean;
  actions?: ReactNode;
  className?: string;
  contentClassName?: string;
  children: ReactNode;
};

export function FoldableSection({
  title,
  summary,
  defaultOpen = false,
  actions,
  className,
  contentClassName,
  children,
}: FoldableSectionProps) {
  const [open, setOpen] = useState(defaultOpen);

  return (
    <section className={cn("rounded-3xl border border-border/70 bg-background/75", className)}>
      <div className="flex flex-col gap-3 px-5 py-4 md:flex-row md:items-start md:justify-between">
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="flex flex-1 items-start gap-3 text-left"
        >
          <span className="mt-0.5 text-muted-foreground">
            {open ? <ChevronDown className="h-5 w-5" /> : <ChevronRight className="h-5 w-5" />}
          </span>
          <span className="space-y-1">
            <span className="block text-sm font-semibold text-foreground">{title}</span>
            {summary ? (
              <span className="block text-xs leading-5 text-muted-foreground">{summary}</span>
            ) : null}
          </span>
        </button>
        {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
      </div>
      {open ? <div className={cn("border-t border-border/60 px-5 py-4", contentClassName)}>{children}</div> : null}
    </section>
  );
}
