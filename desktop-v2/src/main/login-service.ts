import { EventEmitter } from "node:events";
import type { Browser, Page } from "playwright-core";
import type { Account, RegistrationEvent, Settings } from "../shared/types.js";
import {
  CHATGPT_BASE_URL,
  checkBrowserProxyStatus as logBrowserProxyStatus,
  clickAction,
  clickContinue,
  clickLoginConfirmationIfVisible,
  createOpenAiAuthUrl,
  detectDeactivationMessage,
  detectLoginCodeMode,
  detectRouteError,
  dismissWelcome,
  fillCodeAndSubmit,
  fillEmailIfVisible,
  fillPasswordAndSubmit,
  forceSubmitActiveAuthForm,
  hasChallenge,
  normalizeTotpSecret,
  otpInputs,
  pageText,
  passwordInputs,
  readSession,
  retryRoute,
  selectAuthenticatorIfVisible,
  sessionPatch,
  sleep,
  startEmailOtpLogin,
  totpCode,
} from "./auth-helpers.js";
import { launchConfiguredChromium } from "./browser-config.js";
import { waitForEmailCode } from "./mail-service.js";
import { prepareBrowserProxy } from "./proxy-chain-server.js";
import { resolveTaskConcurrency, splitProxyPool } from "./proxy.js";

type UpdateAccount = (id: string, patch: Partial<Account>) => Promise<Account>;

export class LoginService extends EventEmitter {
  private controller: AbortController | null = null;
  private browsers = new Set<Browser>();

  get running(): boolean { return this.controller !== null; }

  stop(): void {
    this.controller?.abort();
    for (const browser of this.browsers) void browser.close();
  }

  async start(accounts: Account[], settings: Settings, update: UpdateAccount): Promise<void> {
    if (this.running) throw new Error("登录任务已在运行");
    this.controller = new AbortController();
    this.emitEvent({ type: "queue", running: true });
    const dynamicProxies = splitProxyPool(settings.proxyPool);
    if (dynamicProxies.length > 0 && dynamicProxies.length < accounts.length) throw new Error("动态代理数量少于登录账号数量");
    const concurrency = resolveTaskConcurrency(settings.localProxy, dynamicProxies, settings.concurrency);
    try {
      for (let start = 0; start < accounts.length && !this.controller.signal.aborted; start += concurrency) {
        const batch = accounts.slice(start, start + concurrency);
        await Promise.all(batch.map(async (account, batchIndex) => {
          const accountIndex = start + batchIndex;
          const dynamicProxy = dynamicProxies[accountIndex] || "";
          const delay = !dynamicProxies.length && settings.localProxy.trim() && batchIndex > 0 ? Math.min(6000, batchIndex * 1500) : 0;
          if (delay) await abortableSleep(delay, this.controller!.signal);
          await this.loginOne(account, settings, settings.localProxy.trim(), dynamicProxy, update).catch(() => undefined);
        }));
      }
    } finally {
      this.controller = null;
      this.emitEvent({ type: "queue", running: false });
    }
  }

  private async loginOne(account: Account, settings: Settings, localProxy: string, dynamicProxy: string, update: UpdateAccount): Promise<void> {
    const signal = this.controller!.signal;
    let deactivationDetail = "";
    await this.patch(account.id, { status: "running", statusText: "登录并获取 Session", lastError: "" }, update);
    const proxyRoute = await prepareBrowserProxy(localProxy, dynamicProxy);
    let browser: Browser | null = null;
    try {
      const launched = await launchConfiguredChromium(settings, proxyRoute.proxy);
      browser = launched.browser;
      this.browsers.add(browser);
      const context = launched.context;
      await context.clearCookies();
      const page = await context.newPage();
      page.on("response", async (response) => {
        if (response.status() < 400) return;
        const text = await response.text().catch(() => "");
        deactivationDetail ||= detectDeactivationMessage(text);
      });
      this.log(account, `打开登录页，代理链路: ${proxyRoute.label}`);
      await logBrowserProxyStatus(page, "登录浏览器代理", Boolean(proxyRoute.proxy), (message) => this.log(account, message));
      await page.goto(CHATGPT_BASE_URL, { waitUntil: "domcontentloaded", timeout: 60_000 });
      const existing = await readSession(page, context);
      if (existing) { await this.complete(account, existing, update); return; }
      const loginUrl = await createOpenAiAuthUrl(context, "login", account.email);
      let otpAfter = new Date(Date.now() - 10_000);
      await page.goto(loginUrl, { waitUntil: "domcontentloaded", timeout: 90_000 });

      const deadline = Date.now() + 10 * 60_000;
      let emailCodeSubmitted = false;
      let passwordSubmitted = false;
      let passwordSubmitAttempts = 0;
      let emailOtpFallbackAttempted = false;
      let lastTotpPeriod = -1;
      let routeRetries = 0;
      let challengeSince = 0;
      while (Date.now() < deadline) {
        if (signal.aborted) throw new Error("任务已停止");
        if (deactivationDetail) {
          await this.patch(account.id, {
            health: "deactivated",
            healthDetail: deactivationDetail,
            sessionValid: false,
            status: "failed",
            statusText: "账号已封禁/停用",
            lastError: deactivationDetail,
          }, update);
          return;
        }
        const snapshot = await readSession(page, context);
        if (snapshot) { await this.complete(account, snapshot, update); return; }

        const routeError = await detectRouteError(page);
        if (routeError) {
          if (routeRetries >= 3) throw new Error(`登录页面错误: ${routeError}`);
          routeRetries += 1;
          this.log(account, `登录页面错误，重试 ${routeRetries}/3`);
          await retryRoute(page);
          await sleep(5000);
          continue;
        }
        if (await hasChallenge(page)) {
          if (settings.headless) throw new Error("登录触发人机验证，请关闭无头模式后手动完成");
          challengeSince ||= Date.now();
          await this.patch(account.id, { status: "waiting-verification", statusText: "等待手动验证" }, update);
          if (Date.now() - challengeSince > 75_000) throw new Error("人机验证持续超过 75 秒，请更换代理后重试");
          await sleep(1500);
          continue;
        }
        challengeSince = 0;
        if (await dismissWelcome(page)) { await sleep(100); continue; }
        if (await clickLoginConfirmationIfVisible(page)) {
          this.log(account, "已点击登录/IP确认页面");
          await sleep(700);
          continue;
        }
        if (/add-phone|phone-verification/i.test(page.url())) throw new Error("当前账号要求手机号验证，Session 获取已停止");

        const passwords = await passwordInputs(page);
        if (passwords.length) {
          const passwordPageText = await pageText(page, 700);
          const passwordRejected = passwordSubmitted
            && /incorrect password|wrong password|invalid password|password is incorrect|密码错误|密码无效|密码不正确|パスワードが正しくありません|パスワードが間違っています/i.test(passwordPageText);
          const shouldUseEmailOtp = !account.gptPassword || passwordRejected || passwordSubmitAttempts >= 2;
          if (shouldUseEmailOtp && !emailOtpFallbackAttempted) {
            emailOtpFallbackAttempted = true;
            const fallback = await startEmailOtpLogin(page);
            deactivationDetail ||= detectDeactivationMessage(fallback.detail);
            if (fallback.started) {
              otpAfter = new Date(Date.now() - 5_000);
              passwordSubmitted = false;
              emailCodeSubmitted = false;
              this.log(account, `${passwordRejected ? "GPT 密码被拒绝" : !account.gptPassword ? "未保存 GPT 密码" : "密码页连续提交未跳转"}，${fallback.detail}`);
              await sleep(700);
              continue;
            }
            this.log(account, `邮箱一次性验证码降级失败: ${fallback.detail}`);
          }
          if (!account.gptPassword) throw new Error("登录页要求 GPT 密码，账号未保存 GPT 密码，且无法切换邮箱一次性验证码登录");
          if (passwordRejected) {
            throw new Error("GPT 登录密码错误，且无法切换邮箱一次性验证码登录");
          }
          if (passwordSubmitAttempts >= 2) {
            throw new Error("GPT 密码连续提交两次后页面仍未跳转，且无法切换邮箱一次性验证码登录");
          }
          if (passwordSubmitted) {
            if (!await forceSubmitActiveAuthForm(page)) throw new Error("GPT 密码已填写，但无法再次点击登录确认按钮");
            passwordSubmitAttempts += 1;
            this.log(account, "密码页仍未跳转，已再次点击继续/提交");
            await sleep(1500);
            continue;
          }
          if (!await fillPasswordAndSubmit(page, account.gptPassword)) throw new Error("GPT 密码输入框或提交按钮未找到");
          passwordSubmitted = true;
          passwordSubmitAttempts = 1;
          emailCodeSubmitted = false;
          this.log(account, "已提交 GPT 登录密码");
          await sleep(1500);
          continue;
        }

        if (normalizeTotpSecret(account.twofaSecret).length >= 16 && await selectAuthenticatorIfVisible(page)) {
          this.log(account, "已选择 Authenticator app 验证方式");
          await sleep(700);
          continue;
        }

        const codes = await otpInputs(page);
        if (codes.length) {
          const text = await pageText(page, 700);
          if (/incorrect|invalid|expired|错误|无效|已过期/i.test(text)) emailCodeSubmitted = false;
          const mode = detectLoginCodeMode(page.url(), text, normalizeTotpSecret(account.twofaSecret).length >= 16, passwordSubmitted);
          if (mode === "totp") {
            if (normalizeTotpSecret(account.twofaSecret).length < 16) throw new Error("登录要求 Authenticator 2FA，但账号未保存有效 2FA 密钥");
            let generated = totpCode(account.twofaSecret);
            if (generated.remaining <= 8) {
              await abortableSleep((generated.remaining + 1) * 1000, signal);
              generated = totpCode(account.twofaSecret);
            }
            const period = Math.floor(Date.now() / 30_000);
            if (period === lastTotpPeriod) { await sleep(1000); continue; }
            if (!await fillCodeAndSubmit(page, generated.code)) throw new Error("无法填写或提交 Authenticator 2FA 验证码");
            lastTotpPeriod = period;
            emailCodeSubmitted = false;
            this.log(account, `已提交 Authenticator 验证码，剩余 ${generated.remaining} 秒`);
          } else if (!emailCodeSubmitted) {
            const code = await waitForEmailCode(account, otpAfter, signal);
            if (!await fillCodeAndSubmit(page, code)) throw new Error("无法填写或提交邮箱验证码");
            emailCodeSubmitted = true;
            this.log(account, "已提交邮箱验证码");
          }
          await sleep(500);
          continue;
        }

        const factorText = await pageText(page, 1200);
        if (passwordSubmitted && normalizeTotpSecret(account.twofaSecret).length >= 16
          && /authenticator|two-factor|2fa|mfa|身份验证器|认证器|認証アプリ/i.test(factorText)
          && await clickAction(page, /authenticator app|authenticator|身份验证器|认证器|認証アプリ/i)) {
          this.log(account, "已选择 Authenticator app 验证方式");
          await sleep(500);
          continue;
        }

        if (await fillEmailIfVisible(page, account.email)) {
          emailCodeSubmitted = false;
          this.log(account, "已填写登录邮箱");
          await sleep(500);
          continue;
        }
        if (await clickContinue(page)) { await sleep(500); continue; }
        await sleep(500);
      }
      throw new Error("登录并获取 Session 超时");
    } catch (error) {
      const stopped = signal.aborted;
      const message = (error as Error).message;
      await this.patch(account.id, {
        status: stopped ? "stopped" : "failed",
        statusText: stopped ? "已停止" : "Session 获取失败",
        sessionValid: stopped ? account.sessionValid : false,
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

  private async complete(account: Account, snapshot: NonNullable<Awaited<ReturnType<typeof readSession>>>, update: UpdateAccount): Promise<void> {
    await this.patch(account.id, {
      ...sessionPatch(snapshot),
      health: "active",
      healthDetail: "登录成功",
      status: "completed",
      statusText: "Session 已获取",
      lastError: "",
    }, update);
    this.log(account, "登录完成，Session JSON、Access Token 和 Storage State 已保存");
  }

  private async patch(id: string, patch: Partial<Account>, update: UpdateAccount): Promise<void> {
    const account = await update(id, patch);
    this.emitEvent({ type: "account", accountId: id, account });
  }

  private log(account: Account, message: string): void {
    this.emitEvent({ type: "log", accountId: account.id, message: `[${account.email}] ${message}` });
  }

  private emitEvent(event: RegistrationEvent): void { this.emit("event", event); }
}

function abortableSleep(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, milliseconds);
    signal.addEventListener("abort", () => { clearTimeout(timer); reject(new Error("任务已停止")); }, { once: true });
  });
}
