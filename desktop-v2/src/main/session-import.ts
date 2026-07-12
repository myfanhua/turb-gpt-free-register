export interface ParsedSessionImport {
  email: string;
  accessToken: string;
  sessionJson: string;
  storageStateJson: string;
  expires: string;
}

export function parseSessionImports(text: string, emailHint = ""): ParsedSessionImport[] {
  const raw = String(text || "").trim();
  if (!raw) throw new Error("请粘贴 Session JSON 或 Access Token");
  const items = parseItems(raw);
  const now = new Date();
  return items.map((item, index) => {
    const sourceText = typeof item === "string" ? item.trim() : JSON.stringify(item, null, 2);
    const accessToken = findAccessToken(item) || findAccessToken(sourceText);
    if (!accessToken) throw new Error(`第 ${index + 1} 项未解析到 accessToken`);
    const tokenPayload = decodeJwtPayload(accessToken);
    const hintedEmail = items.length === 1 ? emailHint.trim() : "";
    const email = hintedEmail || findEmail(item) || findEmail(tokenPayload) || placeholderEmail(now, index);
    if (!isEmail(email)) throw new Error(`第 ${index + 1} 项的邮箱格式无效`);
    const nestedSession = nestedValue(item, ["sessionJson", "session_json", "session"]);
    const storageState = nestedValue(item, ["storageStateJson", "storage_state_json", "storageState", "storage_state"]);
    const expires = findStringByKeys(item, ["expires", "expiresAt", "expires_at"])
      || (typeof tokenPayload.exp === "number" ? new Date(tokenPayload.exp * 1000).toISOString() : "");
    return {
      email,
      accessToken,
      sessionJson: serializeValue(nestedSession) || sourceText,
      storageStateJson: serializeValue(storageState),
      expires,
    };
  });
}

function parseItems(raw: string): unknown[] {
  try {
    const value = JSON.parse(raw) as unknown;
    return Array.isArray(value) ? value : [value];
  } catch {
    const lines = raw.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    if (lines.length > 1 && lines.every((line) => /^Bearer\s+\S+$/i.test(line) || looksLikeToken(line))) return lines;
    return [raw];
  }
}

function findAccessToken(value: unknown, depth = 0): string {
  if (depth > 8) return "";
  if (typeof value === "string") {
    const raw = value.trim();
    if (/^Bearer\s+/i.test(raw)) return raw.replace(/^Bearer\s+/i, "").trim();
    try { return findAccessToken(JSON.parse(raw), depth + 1); } catch { /* plain text */ }
    const labeled = raw.match(/"(?:accessToken|access_token)"\s*:\s*"([^"]+)"/i);
    if (labeled) return labeled[1].trim();
    const token = raw.match(/\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/);
    return token?.[0] || (looksLikeToken(raw) ? raw : "");
  }
  if (!value || typeof value !== "object") return "";
  if (Array.isArray(value)) {
    for (const item of value) { const found = findAccessToken(item, depth + 1); if (found) return found; }
    return "";
  }
  const record = value as Record<string, unknown>;
  for (const key of ["accessToken", "access_token"]) {
    const found = findAccessToken(record[key], depth + 1);
    if (found) return found;
  }
  for (const item of Object.values(record)) {
    const found = findAccessToken(item, depth + 1);
    if (found) return found;
  }
  return "";
}

function looksLikeToken(value: string): boolean {
  return value.length > 80 && !/\s/.test(value) && (value.split(".").length >= 3 || /^[A-Za-z0-9._~-]+$/.test(value));
}

function findEmail(value: unknown, depth = 0): string {
  if (depth > 8 || !value) return "";
  if (typeof value === "string") {
    if (isEmail(value.trim())) return value.trim();
    try { return findEmail(JSON.parse(value), depth + 1); } catch { return ""; }
  }
  if (Array.isArray(value)) {
    for (const item of value) { const found = findEmail(item, depth + 1); if (found) return found; }
    return "";
  }
  if (typeof value !== "object") return "";
  const record = value as Record<string, unknown>;
  for (const key of ["email", "emailAddress", "email_address", "preferred_username", "upn"]) {
    const candidate = String(record[key] || "").trim();
    if (isEmail(candidate)) return candidate;
  }
  for (const item of Object.values(record)) {
    const found = findEmail(item, depth + 1);
    if (found) return found;
  }
  return "";
}

function findStringByKeys(value: unknown, keys: string[], depth = 0): string {
  if (depth > 8 || !value) return "";
  if (typeof value === "string") {
    try { return findStringByKeys(JSON.parse(value), keys, depth + 1); } catch { return ""; }
  }
  if (Array.isArray(value)) {
    for (const item of value) { const found = findStringByKeys(item, keys, depth + 1); if (found) return found; }
    return "";
  }
  if (typeof value !== "object") return "";
  const record = value as Record<string, unknown>;
  for (const key of keys) {
    const candidate = record[key];
    if (typeof candidate === "string" && candidate.trim()) return candidate.trim();
  }
  for (const item of Object.values(record)) {
    const found = findStringByKeys(item, keys, depth + 1);
    if (found) return found;
  }
  return "";
}

function nestedValue(value: unknown, keys: string[]): unknown {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const record = value as Record<string, unknown>;
  for (const key of keys) if (record[key] !== undefined) return record[key];
  return undefined;
}

function serializeValue(value: unknown): string {
  if (value === undefined || value === null || value === "") return "";
  if (typeof value === "string") {
    try { return JSON.stringify(JSON.parse(value), null, 2); } catch { return value; }
  }
  return JSON.stringify(value, null, 2);
}

function decodeJwtPayload(token: string): Record<string, unknown> {
  const parts = token.split(".");
  if (parts.length < 2) return {};
  try {
    const value = JSON.parse(Buffer.from(parts[1], "base64url").toString("utf8")) as unknown;
    return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
  } catch {
    return {};
  }
}

function placeholderEmail(now: Date, index: number): string {
  const stamp = now.toISOString().replace(/\D/g, "").slice(0, 14);
  return `session-${stamp}-${index + 1}@import.local`;
}

function isEmail(value: string): boolean {
  return /^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$/i.test(value);
}

export const sessionImportInternals = { findAccessToken, findEmail, decodeJwtPayload };
