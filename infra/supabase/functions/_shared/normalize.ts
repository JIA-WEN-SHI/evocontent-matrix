export type EntityType = "case" | "asset" | "user_need" | "review";

export type CleanDecision = {
  accepted: boolean;
  reject_code: string;
  reject_reason: string;
  normalized_item: Record<string, unknown>;
};

function asString(input: unknown, maxLen: number): string {
  const value = String(input ?? "").trim();
  if (!value) return "";
  return value.slice(0, maxLen);
}

function normalizeMetrics(input: unknown): { ok: boolean; value: Record<string, unknown> } {
  if (!input) return { ok: true, value: {} };
  if (typeof input !== "object" || Array.isArray(input)) return { ok: false, value: {} };
  return { ok: true, value: input as Record<string, unknown> };
}

function normalizeTags(input: unknown): { ok: boolean; value: string[] } {
  if (!input) return { ok: true, value: [] };
  if (!Array.isArray(input)) return { ok: false, value: [] };
  const tags = input
    .map((x) => asString(x, 80))
    .filter(Boolean)
    .slice(0, 30);
  return { ok: true, value: tags };
}

function isHttpUrl(value: string): boolean {
  if (!value) return true;
  try {
    const parsed = new URL(value);
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch {
    return false;
  }
}

export function normalizeItem(
  item: Record<string, unknown>,
  index: number,
  entityType: EntityType,
  sourceRunId: string,
): CleanDecision {
  const title = asString(item.title, 500);
  const content = asString(item.content, 20000);
  const url = asString(item.url, 2000);
  const author = asString(item.author, 200);
  const platform = asString(item.platform || "xiaohongshu", 80) || "xiaohongshu";
  const capturedAt = asString(item.captured_at, 80);
  const raw = typeof item.raw === "object" && item.raw && !Array.isArray(item.raw) ? item.raw as Record<string, unknown> : {};
  const sourceRef = asString(item.source_ref, 1000) || url || `octopus:${sourceRunId}:${index + 1}`;

  const metrics = normalizeMetrics(item.metrics);
  if (!metrics.ok) {
    return {
      accepted: false,
      reject_code: "metrics_not_object",
      reject_reason: "metrics must be a JSON object",
      normalized_item: {},
    };
  }
  const tags = normalizeTags(item.tags);
  if (!tags.ok) {
    return {
      accepted: false,
      reject_code: "tags_not_array",
      reject_reason: "tags must be an array",
      normalized_item: {},
    };
  }
  if (!title) {
    return {
      accepted: false,
      reject_code: "missing_title",
      reject_reason: "title is required",
      normalized_item: {},
    };
  }
  if (!sourceRef) {
    return {
      accepted: false,
      reject_code: "missing_source_ref",
      reject_reason: "source_ref is required",
      normalized_item: {},
    };
  }
  if (!isHttpUrl(url)) {
    return {
      accepted: false,
      reject_code: "invalid_url",
      reject_reason: "url must be http(s)",
      normalized_item: {},
    };
  }

  if (entityType === "asset" || entityType === "user_need") {
    if (!content) {
      return {
        accepted: false,
        reject_code: "empty_content",
        reject_reason: `${entityType} requires non-empty content`,
        normalized_item: {},
      };
    }
  } else if (!content && !url) {
    return {
      accepted: false,
      reject_code: "empty_content_url",
      reject_reason: `${entityType} requires content or url`,
      normalized_item: {},
    };
  }

  return {
    accepted: true,
    reject_code: "",
    reject_reason: "",
    normalized_item: {
      title,
      content,
      url,
      author,
      platform,
      metrics: metrics.value,
      tags: tags.value,
      captured_at: capturedAt,
      raw,
      source_ref: sourceRef,
    },
  };
}
