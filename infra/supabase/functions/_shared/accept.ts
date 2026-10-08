import type { SupabaseClient } from "npm:@supabase/supabase-js@2";
import type { EntityType } from "./normalize.ts";

type AcceptResult = {
  status: string;
  ingestion_log_id: string;
  entity_type: EntityType;
  received: number;
  accepted_count: number;
  rejected_count: number;
  duplicate_count: number;
  inserted_ids: string[];
};

type RunRow = {
  id: string;
  domain_id: string;
  account_id: string | null;
  entity_type: EntityType;
  source_run_id: string;
};

function nowIso(): string {
  return new Date().toISOString();
}

function tableName(entityType: EntityType): string {
  if (entityType === "case") return "cases";
  if (entityType === "asset") return "assets";
  if (entityType === "user_need") return "user_needs";
  return "reviews";
}

function toPayload(
  run: RunRow,
  entityType: EntityType,
  normalized: Record<string, unknown>,
  sourceType: string,
  createdBy: string,
): Record<string, unknown> {
  const title = String(normalized.title || "");
  const content = String(normalized.content || "");
  const url = String(normalized.url || "");
  const author = String(normalized.author || "");
  const platform = String(normalized.platform || "xiaohongshu");
  const metrics = typeof normalized.metrics === "object" && normalized.metrics && !Array.isArray(normalized.metrics)
    ? normalized.metrics as Record<string, unknown>
    : {};
  const sourceRef = String(normalized.source_ref || "");
  if (entityType === "case") {
    return {
      domain_id: run.domain_id,
      account_id: run.account_id,
      platform,
      author,
      url,
      title,
      content,
      metrics,
      hook: "",
      structure: "",
      analysis: "",
      source_type: sourceType,
      source_ref: sourceRef,
      created_by: createdBy,
    };
  }
  if (entityType === "asset") {
    return {
      domain_id: run.domain_id,
      account_id: run.account_id,
      type: "insight",
      content: content || title,
      source: url || sourceType,
      usable_scene: "",
      is_verified: false,
      summary: title,
      source_type: sourceType,
      source_ref: sourceRef,
      created_by: createdBy,
    };
  }
  if (entityType === "user_need") {
    return {
      domain_id: run.domain_id,
      account_id: run.account_id,
      user_type: "",
      original_text: content || title,
      scenario: "",
      demand_type: "",
      emotion: "",
      real_problem: title,
      source_type: sourceType,
      source_ref: sourceRef,
      created_by: createdBy,
    };
  }
  return {
    domain_id: run.domain_id,
    account_id: run.account_id,
    topic_id: null,
    pipeline_task_id: null,
    content_item_ref: url,
    platform,
    metrics,
    success_points: "",
    failure_points: "",
    improvement: "",
    summary_text: title,
    source_type: sourceType,
    source_ref: sourceRef,
    created_by: createdBy,
  };
}

async function markRun(
  supabase: SupabaseClient,
  runId: string,
  payload: Record<string, unknown>,
): Promise<void> {
  await supabase
    .from("rpa_task_runs")
    .update({ ...payload, updated_at: nowIso() })
    .eq("id", runId);
}

export async function acceptCleanResults(
  supabase: SupabaseClient,
  runId: string,
  createdBy: string,
  sourceType = "edge_octopus",
): Promise<AcceptResult> {
  const runResp = await supabase
    .from("rpa_task_runs")
    .select("id,domain_id,account_id,entity_type,source_run_id,accepted_count,rejected_count,duplicate_count,status")
    .eq("id", runId)
    .is("deleted_at", null)
    .limit(1)
    .maybeSingle();
  if (runResp.error || !runResp.data) {
    throw new Error("rpa run not found");
  }
  const run = runResp.data as RunRow & {
    accepted_count: number;
    rejected_count: number;
    duplicate_count: number;
    status: string;
  };

  const cleanResp = await supabase
    .from("rpa_clean_results")
    .select("id,normalized_item,accepted,inserted_entity_id,duplicate,reject_code,reject_reason")
    .eq("run_id", runId)
    .order("created_at", { ascending: true });
  if (cleanResp.error) {
    throw new Error(cleanResp.error.message);
  }
  const cleanRows = cleanResp.data || [];
  const pendingRows = cleanRows.filter((row) => row.accepted === true && !row.inserted_entity_id);
  if (!pendingRows.length) {
    return {
      status: run.status || "accepted",
      ingestion_log_id: "",
      entity_type: run.entity_type,
      received: cleanRows.length,
      accepted_count: Number(run.accepted_count || 0),
      rejected_count: Number(run.rejected_count || 0),
      duplicate_count: Number(run.duplicate_count || 0),
      inserted_ids: [],
    };
  }

  const ingestionResp = await supabase
    .from("ingestion_logs")
    .insert({
      domain_id: run.domain_id,
      account_id: run.account_id,
      source: sourceType,
      source_run_id: run.source_run_id || `run:${runId}`,
      entity_type: run.entity_type,
      status: "processing",
      request_payload: {
        run_id: runId,
        source: sourceType,
      },
      normalized_count: pendingRows.length,
      success_count: 0,
      failed_count: 0,
      created_by: createdBy,
    })
    .select("id")
    .single();
  if (ingestionResp.error || !ingestionResp.data) {
    throw new Error(ingestionResp.error?.message || "failed to create ingestion log");
  }
  const ingestionLogId = String(ingestionResp.data.id);

  const targetTable = tableName(run.entity_type);
  let acceptedCount = 0;
  let rejectedCount = 0;
  let duplicateCount = 0;
  const insertedIds: string[] = [];

  for (const row of pendingRows) {
    const cleanId = String(row.id);
    const normalized = typeof row.normalized_item === "object" && row.normalized_item && !Array.isArray(row.normalized_item)
      ? row.normalized_item as Record<string, unknown>
      : {};
    const sourceRef = String(normalized.source_ref || "");
    const title = String(normalized.title || "");
    try {
      const existingResp = await supabase
        .from(targetTable)
        .select("id")
        .eq("domain_id", run.domain_id)
        .eq("source_ref", sourceRef)
        .eq("title", title)
        .is("deleted_at", null)
        .limit(1)
        .maybeSingle();
      if (!existingResp.error && existingResp.data?.id) {
        duplicateCount += 1;
        await supabase
          .from("rpa_clean_results")
          .update({
            duplicate: true,
            inserted_entity_id: existingResp.data.id,
            updated_at: nowIso(),
          })
          .eq("id", cleanId);
        continue;
      }

      const payload = toPayload(run, run.entity_type, normalized, sourceType, createdBy);
      const inserted = await supabase.from(targetTable).insert(payload).select("id").single();
      if (inserted.error || !inserted.data?.id) {
        throw new Error(inserted.error?.message || "insert failed");
      }
      acceptedCount += 1;
      insertedIds.push(String(inserted.data.id));
      await supabase
        .from("rpa_clean_results")
        .update({
          duplicate: false,
          inserted_entity_id: inserted.data.id,
          reject_code: "",
          reject_reason: "",
          updated_at: nowIso(),
        })
        .eq("id", cleanId);
    } catch (error) {
      rejectedCount += 1;
      await supabase
        .from("rpa_clean_results")
        .update({
          accepted: false,
          reject_code: "insert_failed",
          reject_reason: error instanceof Error ? error.message : "insert failed",
          updated_at: nowIso(),
        })
        .eq("id", cleanId);
    }
  }

  let finalStatus = "success";
  if (rejectedCount > 0 && (acceptedCount > 0 || duplicateCount > 0)) finalStatus = "partial_failed";
  if (rejectedCount > 0 && acceptedCount === 0 && duplicateCount === 0) finalStatus = "failed";

  await supabase
    .from("ingestion_logs")
    .update({
      status: finalStatus,
      success_count: acceptedCount,
      failed_count: rejectedCount,
      updated_at: nowIso(),
      error_message: rejectedCount > 0 ? "accept stage has rejected rows" : null,
    })
    .eq("id", ingestionLogId);

  const runStatus = rejectedCount > 0
    ? (acceptedCount > 0 || duplicateCount > 0 ? "partial_failed" : "rejected")
    : "accepted";
  await markRun(supabase, runId, {
    status: runStatus,
    accepted_count: acceptedCount,
    rejected_count: rejectedCount,
    duplicate_count: duplicateCount,
  });

  return {
    status: runStatus,
    ingestion_log_id: ingestionLogId,
    entity_type: run.entity_type,
    received: pendingRows.length,
    accepted_count: acceptedCount,
    rejected_count: rejectedCount,
    duplicate_count: duplicateCount,
    inserted_ids: insertedIds,
  };
}
