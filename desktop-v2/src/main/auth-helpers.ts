import { createHmac, randomBytes, randomUUID } from "node:crypto";
import type { BrowserContext, Locator, Page } from "playwright-core";
import type { SessionSnapshot } from "../shared/types.js";

export const CHATGPT_BASE_URL = "https://chatgpt.com";

export function detectDeactivationMessage(value: unknown): string {
  const text = typeof value === "string" ? value : JSON.stringify(value ?? "");
  return /account_deactivated|deleted or deactivated|account has been deactivated|account (?:was )?suspended|账号.{0,8}(?:停用|封禁|删除)/i.test(text)
    ? text.slice(0, 500)
    : "";
}

export async function createOpenAiAuthUrl(
  context: BrowserContext,
  screenHint: "signup" | "login",
  email: string,
  locale = "zh-CN",
): Promise<string> {
  let lastError = "";
  for (let attempt = 1; attempt <= 4; attempt += 1) {
    try {
      const csrfDevice = await getChatGptCsrfAndDevice(context, locale);
      const csrfResponse = await context.request.get(`${CHATGPT_BASE_URL}/api/auth/csrf`, {
        headers: { accept: "application/json", referer: `${CHATGPT_BASE_URL}/`, "accept-language": locale },
        timeout: 30_000,
      });
      if (!csrfResponse.ok()) throw new Error(`CSRF HTTP ${csrfResponse.status()}`);
      const csrfPayload = await csrfResponse.json() as { csrfToken?: string };
      const csrfToken = String(csrfDevice.csrfToken || csrfPayload.csrfToken || "").trim();
      if (!csrfToken) throw new Error("CSRF 响应缺少 csrfToken");
      const cookies = await context.cookies([CHATGPT_BASE_URL, "https://openai.com"]);
      const deviceId = csrfDevice.deviceId || cookies.find((cookie) => cookie.name === "oai-did")?.value || randomUUID();
      const query = new URLSearchParams({
        prompt: "login",
        "ext-oai-did": deviceId,
        auth_session_logging_id: randomUUID(),
        "ext-passkey-client-capabilities": "0111",
        screen_hint: screenHint,
        login_hint: email,
        locale,
      });
      const response = await context.request.post(`${CHATGPT_BASE_URL}/api/auth/signin/openai?${query}`, {
        form: { callbackUrl: `${CHATGPT_BASE_URL}/`, csrfToken, json: "true" },
        headers: { accept: "application/json", "accept-language": locale },
        timeout: 30_000,
      });
      const detail = await response.text();
      if (!response.ok()) throw new Error(`授权入口 HTTP ${response.status()} ${detail.slice(0, 200)}`);
      const payload = JSON.parse(detail) as { url?: string };
      if (!payload.url) throw new Error("授权入口响应缺少跳转 URL");
      return payload.url;
    } catch (error) {
      lastError = (error as Error).message;
      if (attempt < 4) await sleep(1500 * attempt);
    }
  }
  throw new Error(`ChatGPT ${screenHint === "signup" ? "注册" : "登录"}初始化连续失败: ${lastError}`);
}

async function getChatGptCsrfAndDevice(context: BrowserContext, locale: string): Promise<{ csrfToken: string; deviceId: string }> {
  const cookieValues = await readChatGptAuthCookies(context);
  if (cookieValues.csrfToken) return cookieValues;
  for (let attempt = 1; attempt <= 4; attempt += 1) {
    const response = await context.request.get(`${CHATGPT_BASE_URL}/api/auth/csrf`, {
      headers: { accept: "application/json", referer: `${CHATGPT_BASE_URL}/`, "accept-language": locale },
      timeout: 30_000,
    }).catch(() => null);
    if (response?.ok()) {
      const payload = await response.json() as { csrfToken?: string };
      const csrfToken = String(payload.csrfToken || "").trim();
      const latestCookies = await readChatGptAuthCookies(context);
      if (csrfToken || latestCookies.csrfToken) return { csrfToken: latestCookies.csrfToken || csrfToken, deviceId: latestCookies.deviceId || cookieValues.deviceId || randomUUID() };
    }
    if (attempt < 4) await sleep(1500 * attempt);
  }
  const latestCookies = await readChatGptAuthCookies(context);
  return { csrfToken: latestCookies.csrfToken, deviceId: latestCookies.deviceId || cookieValues.deviceId || randomUUID() };
}

async function readChatGptAuthCookies(context: BrowserContext): Promise<{ csrfToken: string; deviceId: string }> {
  const cookies = await context.cookies([CHATGPT_BASE_URL, "https://openai.com"]).catch(() => []);
  let csrfToken = "";
  let deviceId = "";
  for (const cookie of cookies) {
    if (cookie.name === "__Host-next-auth.csrf-token") csrfToken = decodeURIComponent(cookie.value || "").split("|")[0] || csrfToken;
    if (cookie.name === "oai-did") deviceId = cookie.value || deviceId;
  }
  return { csrfToken, deviceId };
}

export async function readSession(page: Page, context: BrowserContext): Promise<SessionSnapshot | null> {
  try {
    let session: Record<string, unknown> | null = null;
    const response = await context.request.get(`${CHATGPT_BASE_URL}/api/auth/session`, {
      headers: { accept: "application/json", referer: `${CHATGPT_BASE_URL}/` },
      timeout: 5000,
    }).catch(() => null);
    if (response?.ok()) session = await response.json().catch(() => null) as Record<string, unknown> | null;
    if (!session && isChatGptPageUrl(page.url())) {
      session = await page.evaluate(async () => {
        const response = await fetch("/api/auth/session", { credentials: "include" });
        if (!response.ok) return null;
        return response.json();
      }).catch(() => null) as Record<string, unknown> | null;
    }
    const accessToken = String(session?.accessToken || "").trim();
    if (!session || !accessToken) return null;
    return {
      accessToken,
      sessionJson: JSON.stringify(session, null, 2),
      storageStateJson: JSON.stringify(await context.storageState()),
      expires: String(session.expires || ""),
      capturedAt: new Date().toISOString(),
    };
  } catch {
    return null;
  }
}

function isChatGptPageUrl(value: string): boolean {
  try {
    return new URL(value).hostname.endsWith("chatgpt.com");
  } catch {
    return false;
  }
}

export function sessionPatch(snapshot: SessionSnapshot) {
  return {
    accessToken: snapshot.accessToken,
    sessionJson: snapshot.sessionJson,
    storageStateJson: snapshot.storageStateJson,
    sessionExpires: snapshot.expires,
    sessionUpdatedAt: snapshot.capturedAt,
    sessionValid: true as const,
  };
}

export async function visibleFirst(page: Page, selectors: string[]): Promise<Locator | null> {
  for (const selector of selectors) {
    const count = await page.locator(selector).count().catch(() => 0);
    for (let index = 0; index < count; index += 1) {
      const locator = page.locator(selector).nth(index);
      if (await locator.isVisible().catch(() => false)) return locator;
    }
  }
  return null;
}

export async function visibleAll(page: Page, selectors: string[]): Promise<Locator[]> {
  const output: Locator[] = [];
  const matches = page.locator(selectors.join(", "));
  const count = await matches.count().catch(() => 0);
  for (let index = 0; index < count; index += 1) {
    const locator = matches.nth(index);
    if (await locator.isVisible().catch(() => false)) output.push(locator);
  }
  return output;
}

export async function clickAction(page: Page, pattern: RegExp): Promise<boolean> {
  const controls = page.locator('button, [role="button"], a, input[type="submit"], input[type="button"]');
  const count = await controls.count().catch(() => 0);
  for (let index = 0; index < count; index += 1) {
    const control = controls.nth(index);
    if (!await control.isVisible().catch(() => false) || !await control.isEnabled().catch(() => false)) continue;
    const text = [
      await control.innerText().catch(() => ""),
      await control.getAttribute("aria-label").catch(() => "") || "",
      await control.getAttribute("title").catch(() => "") || "",
      await control.getAttribute("value").catch(() => "") || "",
    ].join(" ").replace(/\s+/g, " ").trim();
    if (!pattern.test(text)) continue;
    await control.scrollIntoViewIfNeeded().catch(() => undefined);
    await control.click({ timeout: 5000 }).catch(async () => control.click({ force: true, timeout: 5000 }));
    return true;
  }
  return clickDomAction(page, pattern);
}

export async function clickDomAction(page: Page, pattern: RegExp): Promise<boolean> {
  return page.evaluate(({ source, flags }) => {
    const actionPattern = new RegExp(source, flags.replace(/g/g, ""));
    const visible = (element: Element | null) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
    };
    const enabled = (element: Element) => {
      const input = element as HTMLButtonElement | HTMLInputElement;
      return input.disabled !== true && element.getAttribute("aria-disabled") !== "true";
    };
    const controls = Array.from(document.querySelectorAll<HTMLElement>('button, [role="button"], a, input[type="submit"], input[type="button"]'))
      .filter((element) => visible(element) && enabled(element));
    const scored = controls.flatMap((element) => {
      const text = [
        element.textContent || "",
        element.getAttribute("aria-label") || "",
        element.getAttribute("title") || "",
        (element as HTMLInputElement).value || "",
      ].join(" ").replace(/\s+/g, " ").trim();
      if (!actionPattern.test(text)) return [];
      const rect = element.getBoundingClientRect();
      return [{ element, textLength: text.length, area: rect.width * rect.height }];
    }).sort((a, b) => a.textLength - b.textLength || a.area - b.area);
    const target = scored[0]?.element;
    if (!target) return false;
    target.scrollIntoView({ block: "center", inline: "center" });
    target.click();
    return true;
  }, { source: pattern.source, flags: pattern.flags }).catch(() => false);
}

export async function clickPrimaryFormAction(page: Page, pattern: RegExp): Promise<boolean> {
  return page.evaluate(({ source, flags }) => {
    const actionPattern = new RegExp(source, flags.replace(/g/g, ""));
    const visible = (element: Element | null) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
    };
    const enabled = (element: Element) => {
      const input = element as HTMLButtonElement | HTMLInputElement;
      return input.disabled !== true && element.getAttribute("aria-disabled") !== "true";
    };
    const fields = Array.from(document.querySelectorAll<HTMLInputElement>('input[type="password"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[name*="code" i]'))
      .filter(visible);
    const form = fields.at(-1)?.closest("form");
    const scope: ParentNode = form || document;
    const controls = Array.from(scope.querySelectorAll<HTMLElement>('button, [role="button"], input[type="submit"], input[type="button"]'))
      .filter((element) => visible(element) && enabled(element));
    const target = controls.find((element) => {
      const text = [
        element.textContent || "",
        element.getAttribute("aria-label") || "",
        element.getAttribute("title") || "",
        (element as HTMLInputElement).value || "",
      ].join(" ").replace(/\s+/g, " ").trim();
      return actionPattern.test(text);
    }) || controls.find((element) => (element as HTMLInputElement).type === "submit" || (element as HTMLButtonElement).type === "submit");
    if (!target) return false;
    target.scrollIntoView({ block: "center", inline: "center" });
    target.click();
    return true;
  }, { source: pattern.source, flags: pattern.flags }).catch(() => false);
}

export async function clickLoginConfirmationIfVisible(page: Page): Promise<boolean> {
  if ((await passwordInputs(page)).length || (await otpInputs(page)).length) return false;
  const text = await pageText(page, 3000);
  const looksLikeConfirmation = /approve login|approve sign-in|approve sign in|confirm login|confirm sign-in|confirm sign in|verify it'?s you|is this you|new sign-?in|ip address|location|device|security check|确认登录|确认是你|是你本人|登录确认|批准登录|允许登录|承認|本人確認|ログインを承認/i.test(text);
  if (!looksLikeConfirmation) return false;
  return clickAction(page, /approve|confirm|continue|yes|allow|this was me|verify|批准|确认|继续|允许|是我|验证|承認|確認|続行|はい/i);
}

export async function selectAuthenticatorIfVisible(page: Page): Promise<boolean> {
  if ((await otpInputs(page)).length) return false;
  const text = await pageText(page, 3000);
  if (!/authenticator|verification app|two-factor|two factor|2fa|mfa|身份验证器|认证器|多因素|認証アプリ|二段階|二要素/i.test(text)) return false;
  if (await clickAction(page, /authenticator app|authenticator|verification app|身份验证器|认证器|認証アプリ/i)) return true;
  return page.evaluate(() => {
    const terms = ["authenticator app", "authenticator", "verification app", "身份验证器", "认证器", "認証アプリ"];
    const visible = (element: Element | null) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
    };
    const enabled = (element: Element) => {
      const input = element as HTMLButtonElement | HTMLInputElement;
      return input.disabled !== true && element.getAttribute("aria-disabled") !== "true";
    };
    const candidates = Array.from(document.querySelectorAll<HTMLElement>('button, [role="button"], label, li, div'))
      .filter((element) => visible(element) && enabled(element))
      .map((element) => ({
        element,
        text: [element.textContent || "", element.getAttribute("aria-label") || "", element.getAttribute("title") || ""]
          .join(" ").replace(/\s+/g, " ").trim(),
      }))
      .filter((item) => terms.some((term) => item.text.toLowerCase().includes(term.toLowerCase())))
      .sort((a, b) => a.text.length - b.text.length);
    const target = candidates[0]?.element;
    if (!target) return false;
    target.scrollIntoView({ block: "center", inline: "center" });
    target.click();
    return true;
  }).catch(() => false);
}

export async function logBrowserProxyStatus(page: Page, label: string, hasProxy: boolean, log: (message: string) => void): Promise<void> {
  if (!hasProxy) {
    log(`${label}: 直连`);
    return;
  }
  try {
    await page.goto("https://api.ipify.org?format=json", { waitUntil: "domcontentloaded", timeout: 30_000 });
    const text = await page.locator("body").innerText({ timeout: 5000 });
    log(`${label}检测成功，出口信息: ${text.trim().slice(0, 120)}`);
  } catch (error) {
    log(`${label}检测失败: ${(error as Error).message}`);
  }
}

export async function checkBrowserProxyStatus(page: Page, label: string, hasProxy: boolean, log: (message: string) => void): Promise<boolean> {
  if (!hasProxy) {
    log(`${label}: direct connection`);
    return true;
  }
  try {
    await page.goto("https://api.ipify.org?format=json", { waitUntil: "domcontentloaded", timeout: 15_000 });
    const text = await page.locator("body").innerText({ timeout: 5000 });
    log(`${label}: proxy check ok, exit: ${text.trim().slice(0, 160)}`);
    return true;
  } catch (error) {
    const message = `${label}: proxy check failed before opening target page: ${(error as Error).message}`;
    log(message);
    throw new Error(message);
  }
}

export async function clickContinue(page: Page): Promise<boolean> {
  return clickAction(page, /continue|next|sign up|create account|finish creating account|log in|sign in|verify|done|submit|继续|下一步|注册|创建账户|完成|登录|验证|提交|続行|次へ|ログイン|確認/i);
}

export async function fillEmailIfVisible(page: Page, email: string): Promise<boolean> {
  const input = await visibleFirst(page, [
    'input[type="email"]', 'input[name="email"]', 'input[name="username"]', 'input[autocomplete="email"]',
  ]);
  if (!input) return false;
  await input.fill(email);
  if (!await clickContinue(page)) await input.press("Enter");
  return true;
}

export interface EmailOtpStartResult {
  started: boolean;
  detail: string;
}

/** Start the email one-time-code branch from an active auth.openai.com login session. */
export async function startEmailOtpLogin(page: Page): Promise<EmailOtpStartResult> {
  const directAction = /continue with (?:an? )?(?:email |one[- ]?time )?code|use (?:an? )?(?:email |one[- ]?time )?code|send (?:me )?(?:an? )?(?:email )?code|email verification code|log in with (?:an? )?code|sign in with (?:an? )?code|使用(?:邮箱)?验证码|通过(?:邮箱)?验证码|发送(?:邮箱)?验证码|一次性验证码|メール.*コード|コード.*メール|ワンタイム.*コード/i;
  if (await clickAction(page, directAction)) {
    await sleep(700);
    return { started: true, detail: "已选择邮箱一次性验证码登录" };
  }

  const otherMethod = /try another method|use another method|other methods?|choose another option|其他方式|其它方式|换一种方式|別の方法|ほかの方法|他の方法/i;
  if (await clickAction(page, otherMethod)) {
    await sleep(500);
    if (await clickAction(page, directAction)) {
      await sleep(700);
      return { started: true, detail: "已从其他验证方式切换到邮箱一次性验证码" };
    }
  }

  try {
    const endpoint = new URL("/api/accounts/email-otp/send", page.url()).toString();
    const response = await page.context().request.get(endpoint, {
      headers: { accept: "application/json", referer: page.url() },
      timeout: 30_000,
    });
    const body = await response.text();
    if (!response.ok()) return { started: false, detail: `EmailOtpSend HTTP ${response.status()}: ${body.slice(0, 500)}` };
    const payload = JSON.parse(body) as { continue_url?: string };
    const continueUrl = String(payload.continue_url || "").trim();
    if (!continueUrl) return { started: false, detail: "EmailOtpSend 响应缺少 continue_url" };
    await page.goto(new URL(continueUrl, page.url()).toString(), { waitUntil: "domcontentloaded", timeout: 90_000 });
    return { started: true, detail: "已请求邮箱一次性验证码" };
  } catch (error) {
    return { started: false, detail: `邮箱一次性验证码启动失败: ${(error as Error).message}` };
  }
}

export async function passwordInputs(page: Page): Promise<Locator[]> {
  return visibleAll(page, ['input[type="password"]', 'input[name*="password" i]', 'input[autocomplete*="password" i]']);
}

export async function fillPasswordAndSubmit(page: Page, password: string): Promise<boolean> {
  const inputs = await passwordInputs(page);
  if (!inputs.length) return false;
  const beforeUrl = page.url();
  for (const input of inputs) {
    await input.fill(password);
    await input.evaluate((element, value) => {
      const input = element as HTMLInputElement;
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      setter?.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }, password).catch(() => undefined);
  }
  await sleep(150);
  if (!await clickAction(page, /continue|next|log in|sign in|save|confirm|approve|add|done|submit|继续|下一步|登录|保存|确认|批准|添加|完成|提交|続行|次へ|ログイン|確認|承認/i)
    && !await clickPrimaryFormAction(page, /continue|next|log in|sign in|save|confirm|approve|add|done|submit|继续|下一步|登录|保存|确认|批准|添加|完成|提交|続行|次へ|ログイン|確認|承認/i)) {
    await inputs.at(-1)!.press("Enter");
  }
  await sleep(900);
  const stillOnPasswordPage = page.url() === beforeUrl && (await passwordInputs(page)).length > 0;
  if (stillOnPasswordPage) {
    if (!await forceSubmitActiveAuthForm(page)) {
      await inputs.at(-1)!.press("Enter").catch(() => undefined);
    }
  }
  return true;
}

export async function forceSubmitActiveAuthForm(page: Page): Promise<boolean> {
  return page.evaluate(() => {
    const visible = (element: Element | null) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
    };
    const enabled = (element: Element) => {
      const input = element as HTMLButtonElement | HTMLInputElement;
      return input.disabled !== true && element.getAttribute("aria-disabled") !== "true";
    };
    const fields = Array.from(document.querySelectorAll<HTMLInputElement>('input[type="password"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[name*="code" i], input[name*="otp" i], input[name*="totp" i]'))
      .filter(visible);
    const form = fields.at(-1)?.closest("form") as HTMLFormElement | null;
    const scope: ParentNode = form || document;
    const actionPattern = /continue|next|log in|sign in|save|confirm|approve|verify|enable|turn on|done|submit|继续|下一步|登录|保存|确认|批准|验证|启用|开启|完成|提交|続行|次へ|ログイン|確認|承認/i;
    const controls = Array.from(scope.querySelectorAll<HTMLElement>('button, [role="button"], input[type="submit"], input[type="button"]'))
      .filter((element) => visible(element) && enabled(element));
    const target = controls.find((element) => {
      const text = [
        element.textContent || "",
        element.getAttribute("aria-label") || "",
        element.getAttribute("title") || "",
        (element as HTMLInputElement).value || "",
      ].join(" ").replace(/\s+/g, " ").trim();
      return actionPattern.test(text);
    }) || controls.find((element) => (element as HTMLInputElement).type === "submit" || (element as HTMLButtonElement).type === "submit");
    if (target) {
      target.scrollIntoView({ block: "center", inline: "center" });
      target.click();
      return true;
    }
    if (form) {
      if (typeof form.requestSubmit === "function") form.requestSubmit();
      else form.submit();
      return true;
    }
    return false;
  }).catch(() => false);
}

export async function otpInputs(page: Page): Promise<Locator[]> {
  const preferred = await visibleAll(page, [
    'input[autocomplete="one-time-code"]', 'input[inputmode="numeric"]', 'input[name*="code" i]',
    'input[name*="otp" i]', 'input[name*="totp" i]', 'input[id*="otp" i]', 'input[id*="totp" i]',
    'input[aria-label*="code" i]', 'input[placeholder*="code" i]',
    'input[aria-label*="コード" i]', 'input[placeholder*="コード" i]',
    'input[aria-label*="ワンタイム" i]', 'input[placeholder*="ワンタイム" i]',
  ]);
  if (preferred.length) return preferred;
  const text = await pageText(page, 1200);
  if (/mfa-challenge/i.test(page.url()) || /one-?time password|one-?time code|otp|totp|ワンタイム|コード|認証コード|確認コード|验证码/i.test(text)) {
    const fallback = await visibleAll(page, [
      'input:not([type="hidden"]):not([type="password"]):not([type="email"])',
    ]);
    const filtered: Locator[] = [];
    for (const input of fallback) {
      const editable = await input.evaluate((element) => {
        const field = element as HTMLInputElement;
        return !field.disabled && !field.readOnly && ["", "text", "tel", "number", "search"].includes((field.getAttribute("type") || "text").toLowerCase());
      }).catch(() => false);
      if (editable) filtered.push(input);
    }
    if (filtered.length) return filtered;
  }
  return [];
}

export async function fillCodeAndSubmit(page: Page, code: string): Promise<boolean> {
  const inputs = await otpInputs(page);
  if (!inputs.length) return false;
  if (inputs.length >= 6) {
    for (let index = 0; index < 6; index += 1) await inputs[index].fill(code[index]);
  } else {
    await inputs[0].fill(code);
  }
  await sleep(100);
  if (!await clickAction(page, /verify|continue|next|confirm|enable|turn on|done|save|submit|验证|继续|下一步|确认|启用|开启|完成|保存|提交|続行|次へ|確認|検証|認証/i)
    && !await clickPrimaryFormAction(page, /verify|continue|next|confirm|enable|turn on|done|save|submit|验证|继续|下一步|确认|启用|开启|完成|保存|提交|続行|次へ|確認|検証|認証/i)) {
    await inputs.at(-1)!.press("Enter");
  }
  return true;
}

export function detectLoginCodeMode(url: string, pageText: string, hasSecret: boolean, passwordSubmitted: boolean): "email" | "totp" {
  const combined = `${url}\n${pageText}`.toLowerCase();
  if (/mfa-challenge/i.test(url) && hasSecret) return "totp";
  const emailMode = /email-verification|check your email|verification email|email code|inbox|mailbox|邮箱|邮件|验证邮件|メール|認証メール/i.test(combined);
  const totpMode = /authenticator|two-factor|two factor|2fa|mfa|verification app|auth app|one-?time password app|身份验证器|认证器|多因素|認証アプリ|二段階|二要素|ワンタイム\s*パスワード\s*アプリ|ワンタイム\s*コード/i.test(combined);
  if (totpMode && hasSecret) return "totp";
  if (emailMode && !totpMode) return "email";
  if (url.toLowerCase().includes("email-verification")) return "email";
  return hasSecret && passwordSubmitted && !url.toLowerCase().includes("email-verification") ? "totp" : "email";
}

export function normalizeTotpSecret(secret: string): string { return secret.toUpperCase().replace(/[^A-Z2-7]/g, ""); }

export function totpCode(secret: string, timestamp = Date.now()): { code: string; remaining: number } {
  const normalized = normalizeTotpSecret(secret);
  if (normalized.length < 16) throw new Error("2FA 密钥无效");
  const key = decodeBase32(normalized);
  const period = Math.floor(timestamp / 1000 / 30);
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(period));
  const digest = createHmac("sha1", key).update(counter).digest();
  const offset = digest[digest.length - 1] & 0x0f;
  const binary = ((digest[offset] & 0x7f) << 24) | (digest[offset + 1] << 16) | (digest[offset + 2] << 8) | digest[offset + 3];
  return { code: String(binary % 1_000_000).padStart(6, "0"), remaining: 30 - (Math.floor(timestamp / 1000) % 30) };
}

function decodeBase32(value: string): Buffer {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const char of value) {
    const index = alphabet.indexOf(char);
    if (index < 0) throw new Error("2FA 密钥包含无效字符");
    bits += index.toString(2).padStart(5, "0");
  }
  const bytes: number[] = [];
  for (let index = 0; index + 8 <= bits.length; index += 8) bytes.push(Number.parseInt(bits.slice(index, index + 8), 2));
  return Buffer.from(bytes);
}

export async function pageText(page: Page, maxLength = 2000): Promise<string> {
  return (await page.locator("body").innerText({ timeout: 1000 }).catch(() => "")).replace(/\s+/g, " ").trim().slice(0, maxLength);
}

export async function detectRouteError(page: Page): Promise<string> {
  const text = await pageText(page, 500);
  return /糟糕，出错了|operation timed out|route error/i.test(text) ? text : "";
}

export async function retryRoute(page: Page): Promise<void> {
  if (!await clickAction(page, /try again|retry|重试|再试一次/i)) await page.reload({ waitUntil: "domcontentloaded", timeout: 30_000 });
}

export async function hasChallenge(page: Page): Promise<boolean> {
  const combined = `${page.url()}\n${await page.title().catch(() => "")}\n${await pageText(page, 3000)}`;
  return /cloudflare|verify you are human|checking your browser|challenge-platform|验证您是真人|人机验证/i.test(combined);
}

export async function dismissWelcome(page: Page): Promise<boolean> {
  const text = await pageText(page, 1500);
  if (!/you're all set|ready to go|welcome to chatgpt|准备已完成|一切准备就绪|准备好了|準備が完了しました|準備ができました/i.test(text)) return false;
  return clickAction(page, /continue|继续|継続|続行/i);
}

export function generatePassword(): string {
  const upper = "ABCDEFGHJKLMNPQRSTUVWXYZ";
  const lower = "abcdefghijkmnopqrstuvwxyz";
  const digits = "23456789";
  const symbols = "!@#$%";
  const all = upper + lower + digits + symbols;
  const values = [pick(upper), pick(lower), pick(digits), pick(symbols)];
  while (values.length < 12) values.push(pick(all));
  for (let index = values.length - 1; index > 0; index -= 1) {
    const target = randomBytes(1)[0] % (index + 1);
    [values[index], values[target]] = [values[target], values[index]];
  }
  return values.join("");
}

function pick(value: string): string { return value[randomBytes(1)[0] % value.length]; }
export function sleep(milliseconds: number): Promise<void> { return new Promise((resolve) => setTimeout(resolve, milliseconds)); }
