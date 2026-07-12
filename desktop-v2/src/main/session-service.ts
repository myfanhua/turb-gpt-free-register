import { request } from "playwright-core";
import type { Account } from "../shared/types.js";
import { CHATGPT_BASE_URL, detectDeactivationMessage } from "./auth-helpers.js";
import { prepareBrowserProxy } from "./proxy-chain-server.js";

export interface SessionCheckResult {
  valid: boolean | null;
  deactivated: boolean;
  detail: string;
}

export async function checkAccountSession(account: Account, localProxy = "", dynamicProxy = ""): Promise<SessionCheckResult> {
  if (!account.accessToken) return { valid: false, deactivated: false, detail: "缺少 Access Token" };
  const proxyRoute = await prepareBrowserProxy(localProxy, dynamicProxy);
  const context = await request.newContext({
    baseURL: CHATGPT_BASE_URL,
    proxy: proxyRoute.proxy,
    extraHTTPHeaders: {
      accept: "application/json",
      authorization: `Bearer ${account.accessToken}`,
      origin: CHATGPT_BASE_URL,
      referer: `${CHATGPT_BASE_URL}/`,
    },
  });
  const endpoints = [
    "/backend-api/accounts/check/v4-2023-04-27",
    "/backend-api/me",
    "/backend-api/models",
  ];
  const errors: string[] = [];
  let unauthorized = 0;
  try {
    for (const endpoint of endpoints) {
      try {
        const response = await context.get(endpoint, { timeout: 30_000 });
        const text = await response.text();
        const deactivation = detectDeactivationMessage(text);
        if (deactivation) {
          return { valid: false, deactivated: true, detail: deactivation };
        }
        if (response.ok()) return { valid: true, deactivated: false, detail: `${endpoint} -> HTTP ${response.status()}` };
        if (response.status() === 401) unauthorized += 1;
        errors.push(`${endpoint}: HTTP ${response.status()} ${text.slice(0, 120)}`);
      } catch (error) {
        errors.push(`${endpoint}: ${(error as Error).message}`);
      }
    }
  } finally {
    await context.dispose();
    await proxyRoute.close();
  }
  if (unauthorized === endpoints.length) return { valid: false, deactivated: false, detail: "所有 Session 检测接口均返回 HTTP 401" };
  return { valid: null, deactivated: false, detail: errors.slice(-3).join(" | ") || "未获得检测响应" };
}
