import { acceptCleanResults } from "../_shared/accept.ts";
import { jsonResponse, errorResponse } from "../_shared/http.ts";
import { normalizeItem, type EntityType } from "../_shared/normalize.ts";
import { isTimestampFresh, verifyTimestampedSignature } from "../_shared/signature.ts";
import { getAdminClient } from "../_shared/supabase.ts";

type CallbackBody = {
  run_id?: string;
  task_id?: string;
  source_run_id?: string;
  result_items?: Array<Record<string, unknown>>;
};

function parseBody(input: unknown): CallbackBody {
  if (!input || typeof input !== "object" || Array.isArray(input)) return {};
  return input as CallbackBody;
}

function nowIso(): string {
  return new Date().toISOString();
}

const FINAL_RUN_STATUSES = new Set(["accepted", "partial_failed", "rejected", "failed", "canceled"]);

Deno.serve(async (req) => {
  if (req.method !== "POST") return errorResponse(405, "Method not allowed");

  const rawBody = await req.text();
  const secret = Deno.env.get("OCTOPUS_CALLBACK_SECRET") || "";
  const signature = req.headers.get("x-signature") || "";
  const timestamp = req.headers.get("x-timestamp") || "";
  if (!secret) return errorResponse(500, "Missing OCTOPUS_CALLBACK_SECRET");
  if (!timestamp || !signature) return errorResponse(401, "Missing callback signature headers");
  if (!isTimestampFresh(timestamp, 300)) return errorResponse(401, "Callback timestamp expired");
  const ok = await verifyTimestampedSignature(secret, timestamp, rawBody, signature);
  if (!ok) return errorResponse(401, "Invalid callback signature");

  let parsed: CallbackBody;
  try {
    parsed = parseBody(JSON.parse(rawBody));
  } catch {
    return errorResponse(422, "Invalid JSON body");
  }

  const octopusRunId = String(parsed.run_id || parsed.task_id || "").trim();
  if (!octopusRunId) return errorResponse(422, "Missing run_id");
  const resultItems = Array.isArray(parsed.result_items) ? parsed.result_items : [];

  try {
    const supabase = getAdminClient();
    const runResp = await supabase
      .from("rpa_task_runs")
      .select("*")
      .eq("octopus_run_id", octopusRunId)
      .is("deleted_at", null)
      .limit(1)
      .maybeSingle();
    if (runResp.error || !runResp.data) {
      return errorResponse(404, "rpa run not found");
    }
    const run = runResp.data;
    const runId = String(run.id);

    if (FINAL_RUN_STATUSES.has(String(run.status || ""))) {
      return jsonResponse({
        status: "ok",
        run_id: runId,
        octopus_run_id: octopusRunId,
        accepted_count: Number(run.accepted_count || 0),
        rejected_count: Number(run.rejected_count || 0),
        duplicate_count: Number(run.duplicate_count || 0),
        note: "already finalized",
      });
    }

    await supabase
      .from("rpa_task_runs")
      .update({
        status: "callback_received",
        callback_payload: parsed,
        updated_at: nowIso(),
      })
      .eq("id", runId);

    const entityType = String(run.entity_type || "") as EntityType;
    const sourceRunId = String(parsed.source_run_id || run.source_run_id || octopusRunId);
    await supabase
      .from("rpa_task_runs")
      .update({
        source_run_id: sourceRunId,
        status: "cleaning",
        updated_at: nowIso(),
      })
      .eq("id", runId);

    for (let idx = 0; idx < resultItems.length; idx += 1) {
      const item = resultItems[idx];
      const rawRef = String(item?.source_ref || item?.url || `${octopusRunId}:${idx + 1}`);
      const rawResp = await supabase
        .from("rpa_raw_payloads")
        .insert({
          domain_id: run.domain_id,
          account_id: run.account_id,
          run_id: runId,
          source_run_id: sourceRunId,
          entity_type: entityType,
          raw_ref: rawRef,
          raw_item: item,
          status: "received",
        })
        .select("id")
        .single();
      if (rawResp.error || !rawResp.data) continue;

      const decision = normalizeItem(item || {}, idx, entityType, sourceRunId);
      await supabase
        .from("rpa_raw_payloads")
        .update({
          status: decision.accepted ? "normalized" : "invalid",
          reject_code: decision.reject_code,
          reject_reason: decision.reject_reason,
        })
        .eq("id", rawResp.data.id);

      await supabase
        .from("rpa_clean_results")
        .insert({
          domain_id: run.domain_id,
          account_id: run.account_id,
          run_id: runId,
          raw_payload_id: rawResp.data.id,
          entity_type: entityType,
          normalized_item: decision.normalized_item,
          accepted: decision.accepted,
          reject_code: decision.reject_code,
          reject_reason: decision.reject_reason,
        });
    }

    const accept = await acceptCleanResults(supabase, runId, "edge:octopus-callback", "edge_octopus");
    return jsonResponse({
      status: "ok",
      run_id: runId,
      octopus_run_id: octopusRunId,
      ...accept,
    });
  } catch (error) {
    return errorResponse(500, error instanceof Error ? error.message : "Unexpected error");
  }
});
