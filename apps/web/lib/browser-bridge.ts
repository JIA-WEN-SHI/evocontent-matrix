export type BrowserJob = {
  id: string;
  account_id: string;
  domain_slug: string;
  status: string;
  message: string;
  collected: number;
  inserted: number;
  duplicates: number;
  updated?: number;
  mode?: BrowserMode;
  events: string[];
};

export type BrowserConnection = {
  connected: boolean;
  mode?: BrowserMode | null;
  protocol_version?: number;
  account_id: string;
  domain_slug: string | null;
  tab_id: number | null;
  job: BrowserJob | null;
};

export type BrowserMode = "background_text" | "foreground_visual";
export type BrowserCapture = {
  title: string; source_url: string; body_text: string; summary?: string | null;
  summary_status: string; body_status: string; comments_status: string; images_status: string;
  author_name: string; tags: string[]; captured_at: string; truncation_reasons: string[];
  comments: Array<{ key: string; parent_key: string | null; text: string; author_name: string; observed_time: string; truncated: boolean }>;
  images: Array<{ id?: string; index: number; status: string; message?: string; bytes?: number; last_attempt?: {message:string} }>;
};

export function isBrowserJobActive(job?: Pick<BrowserJob, "status"> | null): boolean {
  return !!job && ["queued", "running", "awaiting_user"].includes(job.status);
}

export function browserJobLabel(status: string): string {
  const labels: Record<string, string> = { queued: "等待启动", running: "采集中", awaiting_user: "已暂停",
    completed: "采集完成", partial: "部分完成", cancelled: "已停止", failed: "采集失败", model_unavailable: "模型不可用" };
  return labels[status] || "状态待确认";
}
