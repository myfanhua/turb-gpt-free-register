import { randomUUID } from "node:crypto";
import type { Account } from "../shared/types.js";

function extras(parts: string[]): Pick<Account, "gptPassword" | "twofaSecret"> {
  const result = { gptPassword: "", twofaSecret: "" };
  for (const value of parts) {
    const [rawKey, ...rest] = value.split("=");
    const key = rawKey.trim().toLowerCase();
    const content = rest.join("=").trim();
    if (["gpt_password", "chatgpt_password"].includes(key)) result.gptPassword = content;
    if (["2fa", "twofa", "totp", "twofa_secret"].includes(key)) result.twofaSecret = content;
  }
  return result;
}

export function parseAccountLine(line: string): Account {
  const parts = line.split("----").map((part) => part.trim());
  if (parts.length < 4) throw new Error("格式应为 email----password----client_id----refresh_token");
  const [email, emailPassword, clientId, refreshToken] = parts;
  if (!email.includes("@") || !clientId || !refreshToken) throw new Error("邮箱、client_id 或 refresh_token 无效");
  const now = new Date().toISOString();
  return {
    id: randomUUID(),
    email,
    emailPassword,
    clientId,
    refreshToken,
    ...extras(parts.slice(4)),
    accessToken: "",
    sessionJson: "",
    storageStateJson: "",
    sessionExpires: "",
    sessionUpdatedAt: "",
    sessionValid: null,
    trialEligible: null,
    trialMessage: "",
    health: "unknown",
    healthDetail: "",
    accountType: "unknown",
    accountTypeDetail: "",
    status: "pending",
    statusText: "待注册",
    lastError: "",
    createdAt: now,
    updatedAt: now,
  };
}
