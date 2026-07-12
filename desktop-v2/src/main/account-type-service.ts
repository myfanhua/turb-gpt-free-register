import type { AccountType } from "../shared/types.js";

export interface AccountTypeResult {
  accountType: AccountType;
  detail: string;
}

export function inferAccountTypeFromPayload(payload: unknown): AccountTypeResult {
  let foundFree = "";
  let foundPaid = "";
  const stack: unknown[] = [payload];
  while (stack.length) {
    const item = stack.pop();
    if (Array.isArray(item)) {
      stack.push(...item);
      continue;
    }
    if (!item || typeof item !== "object") continue;
    const values = Object.values(item as Record<string, unknown>);
    const lowerKeys = Object.fromEntries(Object.entries(item as Record<string, unknown>).map(([key, value]) => [key.toLowerCase(), value]));
    for (const key of ["is_paid_subscription_active", "has_active_subscription", "is_plus_user", "is_subscribed"]) {
      if (!(key in lowerKeys)) continue;
      const value = lowerKeys[key];
      if (value === true) foundPaid ||= `${key}=true`;
      if (value === false) foundFree ||= `${key}=false`;
    }
    for (const key of ["subscription_plan", "plan_type", "plan", "account_plan", "product_name", "sku", "name"]) {
      const value = lowerKeys[key];
      if (typeof value !== "string") continue;
      const text = value.toLowerCase();
      if (["team", "enterprise"].some((word) => text.includes(word))) return { accountType: "team", detail: `${key}=${value}` };
      if (["plus", "pro", "chatgptplusplan"].some((word) => text.includes(word))) return { accountType: "plus", detail: `${key}=${value}` };
      if (["free", "none", "no_plan"].some((word) => text.includes(word))) foundFree ||= `${key}=${value}`;
    }
    stack.push(...values);
  }
  if (foundPaid) return { accountType: "plus", detail: foundPaid };
  if (foundFree) return { accountType: "free", detail: foundFree };
  return { accountType: "unknown", detail: "unknown account type" };
}

export function inferAccountTypeFromAccessToken(accessToken: string): AccountTypeResult {
  const claims = decodeJwtPayload(accessToken);
  const auth = nestedRecord(claims, "https://api.openai.com/auth");
  const plan = firstNonEmpty(auth.chatgpt_plan_type, auth.plan_type, auth.account_plan, auth.subscription_plan);
  if (!plan) return { accountType: "unknown", detail: "access token has no plan claim" };
  return inferAccountTypeFromPayload({ plan_type: plan });
}

export function chatgptAccountIdFromAccessToken(accessToken: string): string {
  const claims = decodeJwtPayload(accessToken);
  const auth = nestedRecord(claims, "https://api.openai.com/auth");
  return firstNonEmpty(auth.chatgpt_account_id, auth.account_id);
}

function decodeJwtPayload(token: string): Record<string, unknown> {
  try {
    const part = token.split(".")[1] || "";
    if (!part) return {};
    const normalized = `${part.replace(/-/g, "+").replace(/_/g, "/")}${"=".repeat((4 - part.length % 4) % 4)}`;
    const text = Buffer.from(normalized, "base64").toString("utf8");
    const value = JSON.parse(text) as unknown;
    return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
  } catch {
    return {};
  }
}

function nestedRecord(root: Record<string, unknown>, key: string): Record<string, unknown> {
  const value = root[key];
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function firstNonEmpty(...values: unknown[]): string {
  for (const value of values) {
    const text = String(value || "").trim();
    if (text) return text;
  }
  return "";
}
