import { EventEmitter } from "node:events";
import type { Browser, BrowserContext, Page } from "playwright-core";
import type { Account, RegistrationEvent, SessionSnapshot, Settings } from "../shared/types.js";
import {
  CHATGPT_BASE_URL,
  checkBrowserProxyStatus,
  createOpenAiAuthUrl,
  detectDeactivationMessage,
  detectRouteError,
  dismissWelcome,
  fillEmailIfVisible,
  fillPasswordAndSubmit,
  generatePassword,
  hasChallenge,
  otpInputs,
  pageText,
  passwordInputs,
  readSession,
  retryRoute,
  sessionPatch,
  sleep,
} from "./auth-helpers.js";
import { launchConfiguredChromium } from "./browser-config.js";
import { captureFlowSnapshot } from "./flow-snapshot.js";
import { setupGptSecurity } from "./gpt-security-service.js";
import { waitForEmailCode } from "./mail-service.js";
import { fillAboutYouAndSubmit, hasAboutYouForm } from "./profile-flow.js";
import { prepareBrowserProxy } from "./proxy-chain-server.js";
import { resolveTaskConcurrency, splitProxyPool } from "./proxy.js";

type UpdateAccount = (id: string, patch: Partial<Account>) => Promise<Account>;

const AUTH_BASE_URL = "https://auth.openai.com";
export class RegistrationService extends EventEmitter {
  private controller: AbortController | null = null;
  private browsers = new Set<Browser>();

  get running(): boolean { return this.controller !== null; }

  stop(): void {
    this.controller?.abort();
    for (const browser of this.browsers) void browser.close();
  }

  async start(accounts: Account[], settings: Settings, update: UpdateAccount): Promise<void> {
    if (this.running) throw new Error("Registration task is already running");
    this.controller = new AbortController();
    this.emitEvent({ type: "queue", running: true });
    const dynamicProxies = splitProxyPool(settings.proxyPool);
    if (dynamicProxies.length > 0 && dynamicProxies.length < accounts.length) {
      throw new Error("Dynamic proxy count is smaller than account count");
    }
    const concurrency = resolveTaskConcurrency(settings.localProxy, dynamicProxies, settings.concurrency);
    try {
      for (let start = 0; start < accounts.length && !this.controller.signal.aborted; start += concurrency) {
        const batch = accounts.slice(start, start + concurrency);
        await Promise.all(batch.map(async (account, batchIndex) => {
          const accountIndex = start + batchIndex;
          const dynamicProxy = dynamicProxies[accountIndex] || "";
          const delay = !dynamicProxies.length && settings.localProxy.trim() && batchIndex > 0 ? Math.min(6000, batchIndex * 1500) : 0;
          if (delay) await abortableSleep(delay, this.controller!.signal);
          await this.registerOne(account, settings, settings.localProxy.trim(), dynamicProxy, update).catch(() => undefined);
        }));
      }
    } finally {
      this.controller = null;
      this.emitEvent({ type: "queue", running: false });
    }
  }

  private async registerOne(account: Account, settings: Settings, localProxy: string, dynamicProxy: string, update: UpdateAccount): Promise<void> {
    const signal = this.controller!.signal;
    let deactivationDetail = "";
    await this.patch(account.id, initialRegistrationPatch(settings), update);
    const proxyRoute = await prepareBrowserProxy(localProxy, dynamicProxy);
    let browser: Browser | null = null;
    let context: BrowserContext | null = null;
    try {
      const launched = await launchConfiguredChromium(settings, proxyRoute.proxy);
      browser = launched.browser;
      this.browsers.add(browser);
      context = launched.context;
      await context.clearCookies();
      this.log(
        account,
        `Browser fingerprint: Chrome/${launched.fingerprint.chromeMajor} `
          + `${launched.fingerprint.viewportWidth}x${launched.fingerprint.viewportHeight} `
          + `${launched.fingerprint.locale} ${launched.fingerprint.timezone} `
          + `cpu=${launched.fingerprint.hardwareConcurrency} mem=${launched.fingerprint.deviceMemory}`,
      );

      const page = await context.newPage();
      page.on("response", async (response) => {
        if (response.status() < 400) return;
        const text = await response.text().catch(() => "");
        deactivationDetail ||= detectDeactivationMessage(text);
      });

      this.log(account, `Start registration, proxy route: ${proxyRoute.label}`);
      const proxyOk = await checkBrowserProxyStatus(page, "registration browser proxy", Boolean(proxyRoute.proxy), (message) => this.log(account, message));
      if (!proxyOk) throw new Error(`Proxy route check failed before opening ChatGPT: ${proxyRoute.label}`);
      await gotoWithTransientRetry(page, CHATGPT_BASE_URL, 60_000, "ChatGPT home", (message) => this.log(account, message), signal);
      await captureFlowSnapshot(page, account.email, "01-chatgpt-home", (message) => this.log(account, message));

      let signupUrl: string;
      try {
        signupUrl = await createOpenAiAuthUrl(context, "signup", account.email, launched.fingerprint.locale);
      } catch (error) {
        if (!settings.registrationUrl) throw error;
        this.log(account, `Standard auth entry failed; using configured registration URL: ${(error as Error).message}`);
        signupUrl = settings.registrationUrl;
      }

      let otpAfter = new Date(Date.now() - 10_000);
      await gotoWithTransientRetry(page, signupUrl, 90_000, "OpenAI signup authorize page", (message) => this.log(account, message), signal);
      this.log(account, "OpenAI registration page opened; complete manual verification in the browser if it appears");

      const deadline = Date.now() + 10 * 60_000;
      let emailCodeSubmitted = false;
      let routeRetries = 0;
      let challengeSince = 0;
      let challengeLastNotice = 0;
      let challengeSnapshotTaken = false;

      while (Date.now() < deadline) {
        if (signal.aborted) throw new Error("Task cancelled");
        if (deactivationDetail) {
          await this.patch(account.id, {
            health: "deactivated",
            healthDetail: deactivationDetail,
            status: "failed",
            statusText: "Account deactivated",
            lastError: deactivationDetail,
          }, update);
          return;
        }

        const snapshot = await readSession(page, context);
        if (snapshot) {
          await captureFlowSnapshot(page, account.email, "09-session-detected-before-finalize", (message) => this.log(account, message));
          this.log(account, "Registration login detected; ChatGPT session is available");
          await this.finalizeRegistration(account, page, context, settings, update);
          return;
        }

        const routeError = await detectRouteError(page);
        if (routeError) {
          if (routeRetries >= 3) throw new Error(`Registration page error, usually caused by proxy or risk controls: ${routeError}`);
          routeRetries += 1;
          this.log(account, `Registration page error; retrying route ${routeRetries}/3`);
          await retryRoute(page);
          await sleep(5000);
          continue;
        }

        if (await hasChallenge(page)) {
          if (!challengeSnapshotTaken) {
            challengeSnapshotTaken = true;
            await captureFlowSnapshot(page, account.email, "03-human-verification-or-cf", (message) => this.log(account, message));
          }
          if (settings.headless) throw new Error("Registration triggered human verification; disable headless mode and complete it manually");
          challengeSince ||= Date.now();
          const elapsed = Date.now() - challengeSince;
          await this.patch(account.id, { status: "waiting-verification", statusText: "Waiting for manual verification" }, update);
          if (!challengeLastNotice || Date.now() - challengeLastNotice >= 10_000) {
            this.log(account, `Still waiting for Cloudflare/human verification, elapsed ${Math.floor(elapsed / 1000)}s`);
            challengeLastNotice = Date.now();
          }
          if (elapsed > 75_000) {
            throw new Error("OpenAI registration is stuck in Cloudflare/human verification; current proxy/IP risk is high, change proxy and retry");
          }
          await sleep(2000);
          continue;
        }

        challengeSince = 0;
        challengeLastNotice = 0;
        if (await dismissWelcome(page)) { await sleep(100); continue; }
        if (/add-phone|phone-verification/i.test(page.url())) {
          throw new Error("This account triggered phone verification; phone branch is intentionally not implemented");
        }

        const passwords = await passwordInputs(page);
        if (passwords.length) {
          await captureFlowSnapshot(page, account.email, "04-registration-password-page-before-fill", (message) => this.log(account, message));
          const password = account.gptPassword || generatePassword();
          if (!account.gptPassword) {
            account.gptPassword = password;
            await this.patch(account.id, { gptPassword: password }, update);
          }
          if (!await fillPasswordAndSubmit(page, password)) throw new Error("Registration password input or submit button was not found");
          await captureFlowSnapshot(page, account.email, "05-registration-password-submitted", (message) => this.log(account, message));
          emailCodeSubmitted = false;
          this.log(account, "Registration password submitted");
          await sleep(500);
          continue;
        }

        if (page.url().includes("about-you") || await hasAboutYouForm(page)) {
          await captureFlowSnapshot(page, account.email, "06-about-you-before-submit", (message) => this.log(account, message));
          emailCodeSubmitted = false;
          await this.patch(account.id, { status: "running", statusText: "Filling profile name and birth date" }, update);
          await fillAboutYouAndSubmit(page);
          await captureFlowSnapshot(page, account.email, "07-about-you-after-submit", (message) => this.log(account, message));
          this.log(account, "Basic profile submitted");
          await sleep(500);
          continue;
        }

        const codeInputs = await otpInputs(page);
        if (codeInputs.length) {
          await captureFlowSnapshot(page, account.email, "08-email-otp-before-submit", (message) => this.log(account, message));
          const text = await pageText(page, 700);
          if (/incorrect|invalid|expired|\u9519\u8bef|\u65e0\u6548|\u5df2\u8fc7\u671f/i.test(text)) emailCodeSubmitted = false;
          if (!emailCodeSubmitted) {
            await this.patch(account.id, { status: "running", statusText: "Waiting for email verification code" }, update);
            await this.submitEmailCodeLegacy(account, page, otpAfter, signal, settings.headless);
            emailCodeSubmitted = true;
            await captureFlowSnapshot(page, account.email, "08-email-otp-after-submit", (message) => this.log(account, message));
            this.log(account, "Email verification code submitted");
          }
          await sleep(500);
          continue;
        }

        if (await fillEmailIfVisible(page, account.email)) {
          await captureFlowSnapshot(page, account.email, "02-signup-email-submitted", (message) => this.log(account, message));
          otpAfter = new Date();
          emailCodeSubmitted = false;
          this.log(account, "Registration email submitted");
          await sleep(500);
          continue;
        }

        await sleep(500);
      }

      throw new Error("Registration flow timed out; browser may be stuck on verification or an unexpected page");
    } catch (error) {
      const stopped = signal.aborted;
      const message = (error as Error).message;
      if (context) {
        const pages = context.pages();
        const lastPage = pages.at(-1);
        if (lastPage) await captureFlowSnapshot(lastPage, account.email, "99-registration-error", (snapshotMessage) => this.log(account, snapshotMessage));
      }
      await this.patch(account.id, {
        status: stopped ? "stopped" : "failed",
        statusText: stopped ? "Stopped" : "Registration failed",
        lastError: message,
      }, update);
      this.log(account, message);
      throw error;
    } finally {
      if (browser) {
        this.browsers.delete(browser);
        await browser.close().catch(() => undefined);
      }
      await proxyRoute.close().catch(() => undefined);
    }
  }

  private async finalizeRegistration(
    account: Account,
    page: Page,
    context: BrowserContext,
    settings: Settings,
    update: UpdateAccount,
  ): Promise<void> {
    if (!settings.setupGptSecurity) {
      const snapshot = await extractSessionInfo(page, context);
      if (!snapshot) throw new Error("Registration logged in, but Session could not be captured");
      await this.complete(account, snapshot, update);
      return;
    }

    const pendingPatch = securitySetupPendingPatch();
    await this.patch(account.id, pendingPatch, update);
    Object.assign(account, pendingPatch);
    this.log(account, "Logged in, but Session is not saved yet; GPT password/MFA must finish first");
    this.log(account, "Post-registration security setup enabled: set GPT password/MFA, then recapture Session");
    await captureFlowSnapshot(page, account.email, "10-before-gpt-security-setup", (message) => this.log(account, message));

    await setupGptSecurity(page, account, {
      signal: this.controller!.signal,
      log: (message) => this.log(account, message),
      patch: async (patch) => {
        const updated = await this.patch(account.id, patch, update);
        Object.assign(account, updated);
        return updated;
      },
    });

    await captureFlowSnapshot(page, account.email, "11-after-gpt-security-setup", (message) => this.log(account, message));
    await this.patch(account.id, { status: "running", statusText: "Security setup completed; recapturing Session" }, update);
    const fresh = await extractSessionInfo(page, context);
    if (!fresh) throw new Error("GPT password/MFA was set, but fresh Session could not be captured");
    await this.complete(account, fresh, update);
  }

  private async submitEmailCodeLegacy(account: Account, page: Page, after: Date, signal: AbortSignal, headless: boolean): Promise<void> {
    this.log(account, "Waiting for OpenAI email verification code");
    const code = await waitForEmailCode(account, after, signal);
    const waitDeadline = Date.now() + 12_000;
    while (Date.now() < waitDeadline && !(await otpInputs(page)).length) {
      if (signal.aborted) throw new Error("Task cancelled");
      await sleep(250);
    }
    const inputs = await otpInputs(page);
    if (!inputs.length) {
      throw new Error(`Verification code input was not found: ${await pageText(page, 300) || page.url()}`);
    }
    if (inputs.length >= 6) {
      for (let index = 0; index < 6; index += 1) await inputs[index].fill(code[index] || "");
    } else {
      await inputs[0].fill(code);
    }
    const continueUrl = await validateEmailCodeApi(page, code, (message) => this.log(account, message), signal, headless);
    this.log(account, "Submitted email verification code through API");
    if (continueUrl) await page.goto(continueUrl, { waitUntil: "domcontentloaded", timeout: 90_000 });
    await waitAfterOtpSubmit(page, signal);
  }

  private async complete(account: Account, snapshot: NonNullable<Awaited<ReturnType<typeof readSession>>>, update: UpdateAccount): Promise<void> {
    await this.patch(account.id, {
      ...sessionPatch(snapshot),
      status: "completed",
      statusText: "Registration completed / Session saved",
      health: "active",
      healthDetail: "Registration login succeeded",
      lastError: "",
    }, update);
    this.log(account, "Registration completed; Session JSON, Access Token and Storage State saved");
  }

  private async patch(id: string, patch: Partial<Account>, update: UpdateAccount): Promise<Account> {
    const account = await update(id, patch);
    this.emitEvent({ type: "account", accountId: id, account });
    return account;
  }

  private log(account: Account, message: string): void {
    this.emitEvent({ type: "log", accountId: account.id, message: `[${account.email}] ${message}` });
  }

  private emitEvent(event: RegistrationEvent): void { this.emit("event", event); }
}

function abortableSleep(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, milliseconds);
    signal.addEventListener("abort", () => { clearTimeout(timer); reject(new Error("Task cancelled")); }, { once: true });
  });
}

async function gotoWithTransientRetry(
  page: Page,
  url: string,
  timeout: number,
  label: string,
  log: (message: string) => void,
  signal: AbortSignal,
  attempts = 3,
): Promise<void> {
  const transientMarkers = [
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_RESET",
    "ERR_NETWORK_CHANGED",
    "ERR_TIMED_OUT",
    "ERR_ABORTED",
    "frame was detached",
  ];
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    if (signal.aborted) throw new Error("Task cancelled");
    try {
      await page.goto(url, { waitUntil: "domcontentloaded", timeout });
      return;
    } catch (error) {
      const detail = (error as Error).message;
      const transient = transientMarkers.some((marker) => detail.toLowerCase().includes(marker.toLowerCase()));
      if (!transient || attempt >= attempts) throw error;
      log(`${label} transient connection interruption; retrying (${attempt + 1}/${attempts}): ${detail.split(/\r?\n/)[0]?.slice(0, 120) || detail}`);
      await abortableSleep(Math.min(2 * attempt, 5) * 1000, signal);
    }
  }
}

export function initialRegistrationPatch(settings: Pick<Settings, "setupGptSecurity">): Partial<Account> {
  return {
    status: "running",
    statusText: "Starting registration browser",
    lastError: "",
    ...(settings.setupGptSecurity ? clearSessionFields() : {}),
  };
}

function securitySetupPendingPatch(): Partial<Account> {
  return {
    status: "running",
    statusText: "Setting GPT password/MFA; Session not saved yet",
    ...clearSessionFields(),
  };
}

function clearSessionFields(): Partial<Account> {
  return {
    accessToken: "",
    sessionJson: "",
    storageStateJson: "",
    sessionExpires: "",
    sessionUpdatedAt: "",
    sessionValid: null,
  };
}

async function extractSessionInfo(_page: Page, context: BrowserContext): Promise<SessionSnapshot | null> {
  const response = await context.request.get(`${CHATGPT_BASE_URL}/api/auth/session`, {
    headers: { accept: "application/json", referer: `${CHATGPT_BASE_URL}/` },
    timeout: 60_000,
  });
  const body = (await response.text()).trim();
  let session: Record<string, unknown>;
  try {
    session = JSON.parse(body) as Record<string, unknown>;
  } catch {
    throw new Error(`Session API did not return valid JSON: ${body.slice(0, 300)}`);
  }
  const accessToken = String(session.accessToken || "");
  if (!accessToken) return null;
  return {
    accessToken,
    sessionJson: JSON.stringify(session, null, 2),
    storageStateJson: JSON.stringify(await context.storageState()),
    expires: String(session.expires || ""),
    capturedAt: new Date().toISOString(),
  };
}

async function validateEmailCodeApi(
  page: Page,
  code: string,
  log: (message: string) => void,
  signal: AbortSignal,
  headless: boolean,
): Promise<string> {
  let lastDetail = "";
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    const result = await page.evaluate(async (code) => {
      const response = await fetch("/api/accounts/email-otp/validate", {
        method: "POST",
        credentials: "include",
        headers: {
          accept: "application/json",
          "content-type": "application/json",
          origin: "https://auth.openai.com",
          referer: "https://auth.openai.com/email-verification",
        },
        body: JSON.stringify({ code }),
      });
      const text = await response.text();
      let data: any = null;
      try { data = JSON.parse(text); } catch { /* noop */ }
      return { ok: response.ok, status: response.status, text, data };
    }, code).catch((error) => ({ ok: false, status: 0, text: (error as Error).message, data: null as any }));
    if (result.ok) {
      const payload = result.data || {};
      return String(payload.continue_url || payload.page?.payload?.url || "");
    }
    lastDetail = String(result.text || result.status || "");
    if (isCloudflareChallengeText(lastDetail) && attempt < 3) {
      log("EmailOtpValidate triggered Cloudflare challenge; opening challenge page and waiting for clearance");
      await handleCloudflareChallenge(page, lastDetail, log, signal, headless);
      continue;
    }
    break;
  }
  if (isCloudflareChallengeText(lastDetail)) {
    throw new Error("EmailOtpValidate was blocked by Cloudflare; use a cleaner proxy or complete the browser challenge and retry");
  }
  throw new Error(`EmailOtpValidate API failed: ${lastDetail.slice(0, 800)}`);
}

function extractCloudflareChallengeUrl(text: string): string {
  const value = String(text || "").replace(/\\\//g, "/");
  const patterns = [
    /cUPMDTk:\s*"([^"]+)"/,
    /history\.replaceState\([^,]+,[^,]+,"([^"]+)"/,
  ];
  for (const pattern of patterns) {
    const match = pattern.exec(value);
    if (!match?.[1]) continue;
    const raw = match[1].replace(/\\\//g, "/");
    return raw.startsWith("http") ? raw : `${AUTH_BASE_URL}${raw}`;
  }
  return "";
}

async function handleCloudflareChallenge(
  page: Page,
  challengeHtml: string,
  log: (message: string) => void,
  signal: AbortSignal,
  headless: boolean,
): Promise<void> {
  if (headless) throw new Error("Cloudflare challenge was triggered, but headless mode cannot complete manual verification");
  const challengeUrl = extractCloudflareChallengeUrl(challengeHtml);
  if (!challengeUrl) throw new Error("Cloudflare challenge was triggered, but challenge URL could not be parsed");

  const challengePage = await page.context().newPage();
  try {
    await challengePage.bringToFront().catch(() => undefined);
    await challengePage.goto(challengeUrl, { waitUntil: "domcontentloaded", timeout: 90_000 });
    log("Cloudflare page opened in a new tab; complete verification in Chromium");
    const started = Date.now();
    let lastNotice = 0;
    while (Date.now() - started < 120_000) {
      if (signal.aborted) throw new Error("Task cancelled");
      await challengePage.bringToFront().catch(() => undefined);
      if (await hasCloudflareClearance(page)) {
        log("Cloudflare passed; retrying email verification code submission");
        break;
      }
      if (!lastNotice || Date.now() - lastNotice >= 10_000) {
        const remain = Math.max(0, Math.ceil((120_000 - (Date.now() - started)) / 1000));
        log(`Still waiting for Cloudflare clearance, about ${remain}s remaining`);
        lastNotice = Date.now();
      }
      await sleep(2000);
    }
    if (!await hasCloudflareClearance(page)) {
      throw new Error("Cloudflare did not clear within 120s; current proxy/IP risk is high, change proxy and retry");
    }
  } finally {
    await challengePage.close().catch(() => undefined);
    await page.bringToFront().catch(() => undefined);
  }
  await page.goto(`${AUTH_BASE_URL}/email-verification`, { waitUntil: "domcontentloaded", timeout: 90_000 });
}

async function hasCloudflareClearance(page: Page): Promise<boolean> {
  const cookies = await page.context().cookies([AUTH_BASE_URL]).catch(() => []);
  return cookies.some((cookie) => cookie.name === "cf_clearance");
}

async function waitAfterOtpSubmit(page: Page, signal: AbortSignal, timeoutMs = 20_000): Promise<void> {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (signal.aborted) throw new Error("Task cancelled");
    if (await readSession(page, page.context())) return;
    if (page.url().includes("about-you") || await hasAboutYouForm(page)) return;
    if (!(page.url().includes("email-verification") || (await otpInputs(page)).length)) return;
    await sleep(1000);
  }
  throw new Error(`After submitting email code, the page is still on email verification. The code may be expired/used or validation failed. Page content: ${await pageText(page, 300) || page.url()}`);
}

function isCloudflareChallengeText(text: string): boolean {
  return /challenges\.cloudflare\.com|__cf_chl|cf_chl|cf-turnstile|challenge-platform|just a moment|checking your browser|verify you are human|verifying you are human|review the security of your connection|cloudflare|please wait|security check/i.test(text || "");
}
