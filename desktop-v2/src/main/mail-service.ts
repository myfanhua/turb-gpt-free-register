import { ImapFlow } from "imapflow";
import { simpleParser, type ParsedMail } from "mailparser";
import type { Account, RecentEmail } from "../shared/types.js";

const codePattern = /(?<!\d)(\d{6})(?!\d)/;
const imapScope = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access";
const tokenSpecs = [
  { name: "LIVE", url: "https://login.live.com/oauth20_token.srf" },
  { name: "LIVE+scope", url: "https://login.live.com/oauth20_token.srf", scope: imapScope },
  { name: "V1-COMMON", url: "https://login.microsoftonline.com/common/oauth2/token", resource: "https://outlook.office.com/" },
  { name: "V1-CONSUMERS", url: "https://login.microsoftonline.com/consumers/oauth2/token", resource: "https://outlook.office.com/" },
  { name: "CONSUMERS", url: "https://login.microsoftonline.com/consumers/oauth2/v2.0/token", scope: imapScope },
  { name: "CONSUMERS-noscope", url: "https://login.microsoftonline.com/consumers/oauth2/v2.0/token" },
  { name: "COMMON", url: "https://login.microsoftonline.com/common/oauth2/v2.0/token", scope: imapScope },
  { name: "COMMON-noscope", url: "https://login.microsoftonline.com/common/oauth2/v2.0/token" },
] as const;

type MailTransport = "imap" | "rest";
type TokenResult = { accessToken: string; expiresAt: number; endpoint: string };
type MailboxOptions = { preferRest?: boolean };
type RestMessage = {
  Id?: string;
  Subject?: string;
  BodyPreview?: string;
  Body?: { Content?: string; ContentType?: string };
  ReceivedDateTime?: string;
  From?: { EmailAddress?: { Address?: string; Name?: string } };
};

const tokenCache = new Map<string, TokenResult>();
const transportCache = new Map<string, MailTransport>();

function createImapClient(account: Account, accessToken: string): ImapFlow {
  return new ImapFlow({
    host: "outlook.office365.com",
    port: 993,
    secure: true,
    logger: false,
    auth: { user: account.email, accessToken },
    connectionTimeout: 15_000,
    greetingTimeout: 15_000,
    socketTimeout: 30_000,
  });
}

async function* accessTokens(account: Account): AsyncGenerator<TokenResult> {
  if (!account.clientId || !account.refreshToken) throw new Error("该账号缺少邮箱 client_id 或 refresh_token");
  const cached = tokenCache.get(account.id);
  const attempted = new Set<string>();
  let yielded = false;
  if (cached && cached.expiresAt > Date.now() + 60_000) {
    attempted.add(cached.endpoint);
    yielded = true;
    yield cached;
  }
  const errors: string[] = [];
  for (const spec of tokenSpecs) {
    if (attempted.has(spec.name)) continue;
    const body = new URLSearchParams({
      client_id: account.clientId,
      refresh_token: account.refreshToken,
      grant_type: "refresh_token",
    });
    if ("scope" in spec && spec.scope) body.set("scope", spec.scope);
    if ("resource" in spec && spec.resource) body.set("resource", spec.resource);
    try {
      const response = await fetch(spec.url, {
        method: "POST",
        headers: { accept: "application/json" },
        body,
        signal: AbortSignal.timeout(15_000),
      });
      const payload = await response.json() as { access_token?: string; expires_in?: number; error?: string; error_description?: string };
      if (!response.ok || !payload.access_token) {
        errors.push(`${spec.name}: ${payload.error_description || payload.error || `HTTP ${response.status}`}`);
        continue;
      }
      const result = {
        accessToken: payload.access_token,
        expiresAt: Date.now() + Math.max(300, Number(payload.expires_in) || 3600) * 1000,
        endpoint: spec.name,
      };
      tokenCache.set(account.id, result);
      yielded = true;
      yield result;
    } catch (error) {
      errors.push(`${spec.name}: ${(error as Error).message}`);
    }
  }
  if (!yielded) throw new Error(`邮箱 Token 刷新失败: ${errors.join(" | ") || "没有端点返回 access_token"}`);
}

async function withMailbox<T>(
  account: Account,
  readImap: (client: ImapFlow) => Promise<T>,
  readRest: (accessToken: string) => Promise<T>,
  options: MailboxOptions = {},
): Promise<T> {
  const errors: string[] = [];
  for await (const token of accessTokens(account)) {
    const preferred = transportCache.get(account.id) || (options.preferRest ? "rest" : undefined);
    let restAttempted = false;
    if (preferred === "rest") {
      restAttempted = true;
      try {
        return await readRest(token.accessToken);
      } catch (error) {
        errors.push(`${token.endpoint}/REST: ${(error as Error).message}`);
        transportCache.delete(account.id);
      }
    }
    const client = createImapClient(account, token.accessToken);
    try {
      await client.connect();
      const result = await readImap(client);
      transportCache.set(account.id, "imap");
      return result;
    } catch (error) {
      errors.push(`${token.endpoint}/IMAP: ${imapError(error)}`);
    } finally {
      await client.logout().catch(() => undefined);
    }
    if (!restAttempted) {
      try {
        const result = await readRest(token.accessToken);
        transportCache.set(account.id, "rest");
        return result;
      } catch (error) {
        errors.push(`${token.endpoint}/REST: ${(error as Error).message}`);
      }
    }
    tokenCache.delete(account.id);
  }
  throw new Error(`邮箱读取失败: ${errors.join(" | ") || "没有可用的邮箱访问方式"}`);
}

function imapError(error: unknown): string {
  const value = error as Error & { responseText?: string; response?: string };
  return String(value.responseText || value.response || value.message || error).slice(0, 300);
}

async function fetchRestFolder(accessToken: string, folder: "inbox" | "junkemail", limit: number): Promise<RestMessage[]> {
  const query = new URLSearchParams({
    "$top": String(Math.max(1, Math.min(100, limit))),
    "$orderby": "ReceivedDateTime desc",
    "$select": "Id,Subject,BodyPreview,Body,ReceivedDateTime,From",
  });
  const response = await fetch(`https://outlook.office.com/api/v2.0/me/mailfolders/${folder}/messages?${query}`, {
    headers: { authorization: `Bearer ${accessToken}`, accept: "application/json" },
    signal: AbortSignal.timeout(12_000),
  });
  const text = await response.text();
  if (!response.ok) throw new Error(`Outlook REST ${folder} HTTP ${response.status}: ${text.slice(0, 180)}`);
  const payload = JSON.parse(text) as { value?: RestMessage[] };
  return Array.isArray(payload.value) ? payload.value : [];
}

async function fetchRestMessages(accessToken: string, limit: number, includeJunk = true): Promise<RestMessage[]> {
  const folders: Array<"inbox" | "junkemail"> = includeJunk ? ["inbox", "junkemail"] : ["inbox"];
  const results = await Promise.all(folders.map(async (folder) => {
    try { return await fetchRestFolder(accessToken, folder, limit); }
    catch (error) {
      if (folder === "junkemail") return [];
      throw error;
    }
  }));
  return results.flat().sort((a, b) => String(b.ReceivedDateTime || "").localeCompare(String(a.ReceivedDateTime || "")));
}

function restMessageText(message: RestMessage): string {
  return [message.Subject, message.BodyPreview, htmlToText(message.Body?.Content || "")].filter(Boolean).join("\n");
}

function restSender(message: RestMessage): string {
  const sender = message.From?.EmailAddress;
  return [sender?.Name, sender?.Address].filter(Boolean).join(" <") + (sender?.Name && sender?.Address ? ">" : "");
}

function htmlToText(value: string): string {
  return value
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;|&apos;/gi, "'")
    .replace(/\s+/g, " ")
    .trim();
}

function isOpenAiMail(from: string, text: string): boolean {
  return /openai|chatgpt/i.test(`${from}\n${text}`);
}

async function imapMessages(client: ImapFlow, after: Date, limit: number, includeJunk = true): Promise<ParsedMail[]> {
  const output: ParsedMail[] = [];
  const folders = includeJunk ? ["INBOX", "Junk", "Junk Email"] : ["INBOX"];
  for (const folder of folders) {
    let lock;
    try { lock = await client.getMailboxLock(folder); }
    catch { continue; }
    try {
      const ids = await client.search({ since: after });
      if (!ids || !ids.length) continue;
      for await (const message of client.fetch(ids.slice(-limit).reverse(), { source: true, internalDate: true })) {
        if (!message.source || (message.internalDate && message.internalDate < after)) continue;
        output.push(await simpleParser(message.source) as ParsedMail);
      }
    } finally {
      lock.release();
    }
  }
  return output;
}

async function imapRecentMessages(client: ImapFlow, limit: number): Promise<ParsedMail[]> {
  const output: ParsedMail[] = [];
  for (const folder of ["INBOX", "Junk", "Junk Email"]) {
    let lock;
    try { lock = await client.getMailboxLock(folder); }
    catch { continue; }
    try {
      const total = client.mailbox && typeof client.mailbox !== "boolean" ? client.mailbox.exists : 0;
      if (!total) continue;
      const start = Math.max(1, total - Math.max(1, limit) + 1);
      for await (const message of client.fetch(`${start}:*`, { source: true }, { uid: false })) {
        if (message.source) output.push(await simpleParser(message.source) as ParsedMail);
      }
    } finally {
      lock.release();
    }
  }
  return output;
}

async function findLatestCode(account: Account, after: Date): Promise<string> {
  return withMailbox(account, async (client) => {
    const messages = await imapMessages(client, after, 30, true);
    for (const parsed of messages) {
      const from = parsed.from?.text || "";
      const text = `${parsed.subject || ""}\n${parsed.text || ""}\n${htmlToText(String(parsed.html || ""))}`;
      if (!isOpenAiMail(from, text)) continue;
      const match = text.match(codePattern);
      if (match) return match[1];
    }
    return "";
  }, async (accessToken) => {
    const messages = await fetchRestMessages(accessToken, 30, true);
    for (const message of messages) {
      if (new Date(message.ReceivedDateTime || 0) < after) continue;
      const text = restMessageText(message);
      if (!isOpenAiMail(restSender(message), text)) continue;
      const match = text.match(codePattern);
      if (match) return match[1];
    }
    return "";
  }, { preferRest: true });
}

export async function fetchLatestEmailCode(account: Account, after = new Date(Date.now() - 30 * 60 * 1000)): Promise<string> {
  const code = await findLatestCode(account, after);
  if (!code) throw new Error("最近 30 分钟未找到 OpenAI 邮箱验证码");
  return code;
}

export async function waitForEmailCode(account: Account, after: Date, signal: AbortSignal): Promise<string> {
  const deadline = Date.now() + 180_000;
  while (Date.now() < deadline) {
    if (signal.aborted) throw new Error("任务已停止");
    const code = await findLatestCode(account, after);
    if (code) return code;
    await new Promise((resolve) => setTimeout(resolve, 2500));
  }
  throw new Error("等待邮箱验证码超时");
}

export async function detectDeactivationMail(account: Account, after = new Date(Date.now() - 45 * 24 * 60 * 60 * 1000)): Promise<string> {
  return withMailbox(account, async (client) => {
    const messages = await imapMessages(client, after, 100, true);
    for (const parsed of messages) {
      const from = parsed.from?.text || "";
      const text = `${parsed.subject || ""}\n${parsed.text || ""}`;
      if (isOpenAiMail(from, text) && isDeactivationText(text)) return (parsed.subject || "检测到账号停用通知").slice(0, 180);
    }
    return "";
  }, async (accessToken) => {
    for (const message of await fetchRestMessages(accessToken, 100, true)) {
      const text = restMessageText(message);
      if (isOpenAiMail(restSender(message), text) && isDeactivationText(text)) return (message.Subject || "检测到账号停用通知").slice(0, 180);
    }
    return "";
  }, { preferRest: true });
}

function isDeactivationText(value: string): boolean {
  return /deleted or deactivated|account[_ -]deactivated|account (?:was )?suspended|账号.{0,8}(?:停用|封禁|删除)/i.test(value);
}

export async function listRecentEmails(account: Account, limit = 20): Promise<RecentEmail[]> {
  return withMailbox(account, async (client) => {
    const messages = await imapRecentMessages(client, Math.max(1, limit));
    return messages.map((parsed, index) => ({
      id: String(parsed.messageId || index),
      subject: parsed.subject || "(无主题)",
      from: parsed.from?.text || "",
      date: new Date(parsed.date || Date.now()).toISOString(),
      text: String(parsed.text || htmlToText(String(parsed.html || ""))).trim().slice(0, 50_000),
    })).sort((a, b) => b.date.localeCompare(a.date)).slice(0, limit);
  }, async (accessToken) => (await fetchRestMessages(accessToken, Math.max(1, limit), true)).map((message) => ({
    id: String(message.Id || ""),
    subject: message.Subject || "(无主题)",
    from: restSender(message),
    date: new Date(message.ReceivedDateTime || Date.now()).toISOString(),
    text: restMessageText(message).replace(`${message.Subject || ""}\n`, "").trim().slice(0, 50_000),
  })).slice(0, limit), { preferRest: true });
}

export const mailInternals = { htmlToText, isOpenAiMail, restMessageText };
