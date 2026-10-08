import { jsonResponse, errorResponse } from "../_shared/http.ts";
import { getAdminClient } from "../_shared/supabase.ts";
import { hmacSha256Hex } from "../_shared/signature.ts";

type DispatchBody = {
  instruction_id: string;
  domain_slug: string;
  task_type: string;
  entity_type: "case" | "asset" | "user_need" | "review";
  params?: Record<string, unknown>;
  source_run_id?: string;
  account_id?: string;
};

function normalizeBody(input: unknown): DispatchBody {
  const body = (typeof input === "object" && input ? input : {}) as Record<string, unknown>;
  return {
    instruction_id: String(body.instruction_id || "").trim(),
    domain_slug: String(body.domain_slug || "japan_immigration").trim(),
    task_type: String(body.task_type || "").trim(),
    entity_type: String(body.entity_type || "").trim() as DispatchBody["entity_type"],
    params: typeof body.params === "object" && body.params && !Array.isArray(body.params)
      ? body.params as Record<string, unknown>
      : {},
    source_run_id: String(body.source_run_id || "").trim(),
    account_id: String(body.account_id || "").trim(),
  };
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return { raw: text };
  }
}

function resolveUrl(base: string, endpoint: string): string {
  if (endpoint.startsWith("http://") || endpoint.startsWith("https://")) return endpoint;
  return `${base.replace(/\/+$/, "")}/${endpoint.replace(/^\/+/, "")}`;
}

Deno.serve(async (req) => {
  if (req.method !== "POST") {
    return errorResponse(405, "Method not allowed");
  }
  try {
    const payload = normalizeBody(await req.json());
    if (!payload.instruction_id || !payload.task_type || !payload.entity_type) {
      return errorResponse(422, "Missing instruction_id/task_type/entity_type");
    }
    const supabase = getAdminClient();

    const domainResp = await supabase
      .from("domains")
      .select("id,slug")
      .eq("slug", payload.domain_slug)
      .limit(1)
      .maybeSingle();
    if (domainResp.error || !domainResp.data) {
      return errorResponse(404, "domain not found");
    }
    const domainId = String(domainResp.data.id);

    const existedResp = await supabase
      .from("rpa_task_runs")
      .select("*")
      .eq("domain_id", domainId)
      .eq("instruction_id", payload.instruction_id)
      .is("deleted_at", null)
      .limit(1)
      .maybeSingle();
    if (!existedResp.error && existedResp.data) {
      return jsonResponse({
        status: "exists",
        run: existedResp.data,
      });
    }

    const tplResp = await supabase
      .from("rpa_task_templates")
      .select("*")
      .eq("domain_id", domainId)
      .eq("task_type", payload.task_type)
      .eq("is_active", true)
      .is("deleted_at", null)
      .limit(1)
      .maybeSingle();
    if (tplResp.error || !tplResp.data) {
      return errorResponse(422, "task template not found or inactive");
    }
    if (String(tplResp.data.entity_type) !== payload.entity_type) {
      return errorResponse(422, "entity_type does not match template");
    }

    const sourceRunId = payload.source_run_id || payload.instruction_id;
    const runInsert = await supabase
      .from("rpa_task_runs")
      .insert({
        domain_id: domainId,
        account_id: payload.account_id || null,
        instruction_id: payload.instruction_id,
        task_type: payload.task_type,
        entity_type: payload.entity_type,
        source_run_id: sourceRunId,
        status: "dispatching",
        created_by: "edge:rpa-dispatch",
      })
      .select("*")
      .single();
    if (runInsert.error || !runInsert.data) {
      return errorResponse(500, runInsert.error?.message || "failed to create run");
    }
    const run = runInsert.data;

    const baseUrl = Deno.env.get("OCTOPUS_API_BASE_URL") || "";
    if (!baseUrl) {
      await supabase
        .from("rpa_task_runs")
        .update({
          status: "failed",
          error_message: "Missing OCTOPUS_API_BASE_URL",
          updated_at: new Date().toISOString(),
        })
        .eq("id", run.id);
      return errorResponse(500, "Missing OCTOPUS_API_BASE_URL");
    }

    const callbackUrl = Deno.env.get("OCTOPUS_CALLBACK_URL") || "";
    const endpoint = String(tplResp.data.octopus_endpoint || "/openapi/flows/run");
    const dispatchUrl = resolveUrl(baseUrl, endpoint);
    const requestPayload = {
      flow_id: tplResp.data.octopus_flow_id,
      task_type: payload.task_type,
      entity_type: payload.entity_type,
      instruction_id: payload.instruction_id,
      source_run_id: sourceRunId,
      callback_url: callbackUrl,
      params: payload.params || {},
    };
    const rawBody = JSON.stringify(requestPayload);

    const headers: Record<string, string> = {
      "content-type": "application/json",
    };
    const apiKey = Deno.env.get("OCTOPUS_API_KEY") || "";
    if (apiKey) headers.authorization = `Bearer ${apiKey}`;
    const signSecret = Deno.env.get("OCTOPUS_API_SECRET") || "";
    if (signSecret) {
      headers["x-signature"] = await hmacSha256Hex(signSecret, rawBody);
    }

    const octopusResp = await fetch(dispatchUrl, {
      method: "POST",
      headers,
      body: rawBody,
    });
    const respText = await octopusResp.text();
    const parsed = safeJson(respText) as Record<string, unknown>;
    const octopusRunId = String(parsed.run_id || parsed.task_id || parsed.id || "");

    if (!octopusResp.ok) {
      await supabase
        .from("rpa_task_runs")
        .update({
          status: "failed",
          octopus_request: requestPayload,
          octopus_response: parsed,
          error_message: `octopus http ${octopusResp.status}`,
          updated_at: new Date().toISOString(),
        })
        .eq("id", run.id);
      return errorResponse(502, "octopus dispatch failed", { code: octopusResp.status, response: parsed });
    }

    const status = octopusRunId ? "dispatched" : "failed";
    await supabase
      .from("rpa_task_runs")
      .update({
        status,
        octopus_run_id: octopusRunId,
        octopus_request: requestPayload,
        octopus_response: parsed,
        error_message: octopusRunId ? null : "missing octopus run id",
        updated_at: new Date().toISOString(),
      })
      .eq("id", run.id);

    if (!octopusRunId) {
      return errorResponse(502, "octopus response missing run id", { response: parsed });
    }

    return jsonResponse({
      status: "ok",
      run_id: run.id,
      octopus_run_id: octopusRunId,
      source_run_id: sourceRunId,
      template: {
        task_type: tplResp.data.task_type,
        entity_type: tplResp.data.entity_type,
        octopus_flow_id: tplResp.data.octopus_flow_id,
      },
    });
  } catch (error) {
    return errorResponse(500, error instanceof Error ? error.message : "Unexpected error");
  }
});
