export function toUserFacingError(error: unknown, fallback: string): string {
  const raw = error instanceof Error ? String(error.message || "").trim() : "";
  const message = raw.toLowerCase();

  if (!raw) return fallback;
  if (message.includes("无法连接到 api 服务")) {
    return raw;
  }
  if (message.includes("missing authentication headers") || message.includes("401")) {
    return "当前登录态不可用，请刷新页面后重试。";
  }
  if (
    message.includes("503") ||
    message.includes("temporarily unavailable") ||
    message.includes("unexpected eof") ||
    message.includes("ssl:") ||
    message.includes("timeout") ||
    message.includes("network")
  ) {
    return "后台数据暂时不可用，页面已切换为降级展示，请稍后刷新。";
  }
  return fallback;
}
