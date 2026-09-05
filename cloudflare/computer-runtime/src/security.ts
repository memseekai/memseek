const HEX_64 = /^[0-9a-f]{64}$/;

export function safeAbsolutePath(path: string, roots: readonly string[]): boolean {
  if (!path.startsWith("/") || path === "/" || path.includes("\0")) return false;
  const parts = path.split("/");
  if (parts.includes("..") || parts.includes(".")) return false;
  return roots.some((root) => path === root || path.startsWith(`${root}/`));
}

export function safeRelativePath(path: string): boolean {
  if (!path || path.startsWith("/") || path.includes("\0")) return false;
  const parts = path.split("/");
  return !parts.includes("..") && !parts.includes(".") && !parts.includes("");
}

export function shellQuote(value: string): string {
  if (/^[A-Za-z0-9_\-+=:,./@%]+$/.test(value)) return value;
  return `'${value.replaceAll("'", "'\\''")}'`;
}

export async function verifySignedBody(
  body: Uint8Array,
  headers: Headers,
  secret: string,
  nowSeconds = Math.floor(Date.now() / 1000),
): Promise<boolean> {
  const timestamp = headers.get("x-memseek-timestamp");
  const signature = headers.get("x-memseek-signature");
  if (!timestamp || !signature || !HEX_64.test(signature)) return false;
  const parsed = Number(timestamp);
  if (!Number.isInteger(parsed) || Math.abs(nowSeconds - parsed) > 300) return false;
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["verify"],
  );
  const message = new Uint8Array(new TextEncoder().encode(`${timestamp}.`).length + body.length);
  message.set(new TextEncoder().encode(`${timestamp}.`));
  message.set(body, new TextEncoder().encode(`${timestamp}.`).length);
  const bytes = Uint8Array.from(signature.match(/.{2}/g) ?? [], (value) => Number.parseInt(value, 16));
  return crypto.subtle.verify(
    "HMAC",
    key,
    bytes.buffer as ArrayBuffer,
    message.buffer as ArrayBuffer,
  );
}

export async function sha256(value: string | Uint8Array): Promise<string> {
  const bytes = typeof value === "string" ? new TextEncoder().encode(value) : value;
  const digest = new Uint8Array(
    await crypto.subtle.digest("SHA-256", bytes.buffer as ArrayBuffer),
  );
  return Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join("");
}
