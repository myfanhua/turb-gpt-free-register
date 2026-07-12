import type { Account } from "../shared/types.js";

export interface TrialResult {
  eligible: boolean | null;
  valid: boolean | null;
  message: string;
}

export async function checkTrial(accounts: Account[], endpoint: string): Promise<Map<string, TrialResult>> {
  const withToken = accounts.filter((account) => account.accessToken);
  const output = new Map<string, TrialResult>();
  for (const account of accounts) {
    if (!account.accessToken) output.set(account.id, { eligible: null, valid: null, message: "缺少 Access Token" });
  }
  for (let offset = 0; offset < withToken.length; offset += 20) {
    const batch = withToken.slice(offset, offset + 20);
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ tokens: batch.map((account) => account.accessToken) }),
      signal: AbortSignal.timeout(90_000),
    });
    if (!response.ok) throw new Error(`资格检测接口失败: HTTP ${response.status} ${(await response.text()).slice(0, 200)}`);
    const payload = await response.json() as { results?: Array<Record<string, unknown>> };
    if (!Array.isArray(payload.results) || payload.results.length !== batch.length) throw new Error("资格检测结果数量不匹配");
    payload.results.forEach((item, index) => {
      const eligible = typeof item.eligible === "boolean"
        ? item.eligible
        : typeof item.trial_available === "boolean"
          ? item.trial_available
          : typeof item.trial === "boolean" ? item.trial : null;
      const valid = typeof item.valid === "boolean" ? item.valid : null;
      output.set(batch[index].id, {
        eligible,
        valid,
        message: String(item.message ?? item.reason ?? ""),
      });
    });
  }
  return output;
}
