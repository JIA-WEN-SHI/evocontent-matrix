const encoder = new TextEncoder();

function toHex(bytes: ArrayBuffer): string {
  return Array.from(new Uint8Array(bytes))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

export async function hmacSha256Hex(secret: string, payload: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, encoder.encode(payload));
  return toHex(signature);
}

export function isTimestampFresh(ts: string, maxSkewSeconds = 300): boolean {
  const value = Number(ts);
  if (!Number.isFinite(value)) return false;
  const nowSeconds = Math.floor(Date.now() / 1000);
  return Math.abs(nowSeconds - value) <= maxSkewSeconds;
}

export async function verifyTimestampedSignature(
  secret: string,
  timestamp: string,
  rawBody: string,
  signature: string,
): Promise<boolean> {
  const canonical = `${timestamp}.${rawBody}`;
  const expected = await hmacSha256Hex(secret, canonical);
  return expected === (signature || "").trim().toLowerCase();
}
