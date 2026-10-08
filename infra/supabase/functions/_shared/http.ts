export function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
    },
  });
}

export function errorResponse(status: number, message: string, extra: Record<string, unknown> = {}): Response {
  return jsonResponse(
    {
      status: "error",
      message,
      ...extra,
    },
    status,
  );
}
