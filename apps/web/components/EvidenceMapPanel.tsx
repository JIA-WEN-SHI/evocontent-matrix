"use client";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

type EvidenceMapPanelProps = {
  analysis?: Record<string, unknown> | null;
  compact?: boolean;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function asStringList(value: unknown, limit = 8): string[] {
  if (!Array.isArray(value)) return [];
  const rows: string[] = [];
  for (const item of value) {
    const text = String(item ?? "").trim();
    if (!text) continue;
    rows.push(text);
    if (rows.length >= limit) break;
  }
  return rows;
}

function short(text: string, max = 120): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max)}...`;
}

export function EvidenceMapPanel({ analysis, compact = false }: EvidenceMapPanelProps) {
  const evidenceMap = isRecord(analysis) && isRecord(analysis.evidence_map) ? analysis.evidence_map : null;
  if (!evidenceMap) return null;

  const titleLogic = isRecord(evidenceMap.title_logic) ? evidenceMap.title_logic : {};
  const sections = Array.isArray(evidenceMap.sections) ? evidenceMap.sections : [];
  const references = Array.isArray(evidenceMap.references) ? evidenceMap.references : [];

  const titleTags = asStringList(titleLogic.tags, 6);
  const titleReasons = asStringList(titleLogic.reasons, 4);
  const shownSections = compact ? sections.slice(0, 2) : sections.slice(0, 6);

  const refMap = new Map<string, Record<string, unknown>>();
  for (const raw of references) {
    if (!isRecord(raw)) continue;
    const key = String(raw.ref_id ?? "").trim();
    if (!key) continue;
    refMap.set(key, raw);
  }

  return (
    <Card className="border-border/70">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">证据映射</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-xs">
        <div className="space-y-2">
          <p className="text-muted-foreground">标题逻辑</p>
          <div className="flex flex-wrap gap-1">
            {titleTags.length > 0 ? (
              titleTags.map((tag) => (
                <Badge key={tag} variant="outline" className="px-1.5 py-0 text-[11px]">
                  {tag}
                </Badge>
              ))
            ) : (
              <span className="text-muted-foreground">无</span>
            )}
          </div>
          {titleReasons.length > 0 ? (
            <p className="text-muted-foreground">{titleReasons.join("；")}</p>
          ) : null}
        </div>

        <div className="space-y-2">
          <p className="text-muted-foreground">段落 {"->"} 证据</p>
          {shownSections.length === 0 ? (
            <p className="text-muted-foreground">暂无段落映射</p>
          ) : (
            shownSections.map((raw, idx) => {
              const row = isRecord(raw) ? raw : {};
              const sectionId = String(row.section_id ?? `p${idx + 1}`);
              const preview = short(String(row.preview ?? ""));
              const why = short(String(row.why_this_section ?? ""), 80);
              const refs = asStringList(row.evidence_refs, 3);
              return (
                <div key={`${sectionId}-${idx}`} className="rounded-md border border-border/60 p-2">
                  <p className="font-medium">{sectionId}</p>
                  <p className="mt-1 text-muted-foreground">{preview || "无正文片段"}</p>
                  {refs.length > 0 ? (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {refs.map((refId) => (
                        <Badge key={refId} variant="secondary" className="px-1.5 py-0 text-[11px]">
                          {refId}
                        </Badge>
                      ))}
                    </div>
                  ) : null}
                  {why ? <p className="mt-1 text-muted-foreground">理由：{why}</p> : null}
                </div>
              );
            })
          )}
        </div>

        {!compact ? (
          <div className="space-y-2">
            <p className="text-muted-foreground">来源列表</p>
            {refMap.size === 0 ? (
              <p className="text-muted-foreground">暂无来源</p>
            ) : (
              Array.from(refMap.entries())
                .slice(0, 6)
                .map(([refId, ref]) => {
                  const sourceType = String(ref.source_type ?? "");
                  const sourceUrl = String(ref.source_url ?? "");
                  const rawText = short(String(ref.raw_text ?? ""), 90);
                  return (
                    <div key={refId} className="rounded-md border border-border/60 p-2">
                      <p className="font-medium">
                        {refId} · {sourceType || "unknown"}
                      </p>
                      <p className="mt-1 text-muted-foreground">{rawText || "无摘要"}</p>
                      {sourceUrl ? (
                        <a href={sourceUrl} target="_blank" rel="noreferrer" className="mt-1 inline-block text-primary hover:underline">
                          打开来源
                        </a>
                      ) : null}
                    </div>
                  );
                })
            )}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
