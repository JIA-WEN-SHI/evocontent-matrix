import { acceptCleanResults } from "../_shared/accept.ts";
import { errorResponse, jsonResponse } from "../_shared/http.ts";
import { getAdminClient } from "../_shared/supabase.ts";

type AcceptBody = {
  run_id?: string;
  source_type?: string;
};

Deno.serve(async (req) => {
  if (req.method !== "POST") return errorResponse(405, "Method not allowed");
  try {
    const body = (await req.json()) as AcceptBody;
    const runId = String(body?.run_id || "").trim();
    if (!runId) return errorResponse(422, "Missing run_id");
    const sourceType = String(body?.source_type || "edge_manual_accept").trim() || "edge_manual_accept";

    const supabase = getAdminClient();
    const result = await acceptCleanResults(supabase, runId, "edge:kb-accept", sourceType);
    return jsonResponse({
      status: "ok",
      run_id: runId,
      ...result,
    });
  } catch (error) {
    return errorResponse(500, error instanceof Error ? error.message : "Unexpected error");
  }
});
