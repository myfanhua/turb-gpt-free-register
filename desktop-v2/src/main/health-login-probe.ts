import type { Browser } from "playwright-core";
import type { Account, SessionSnapshot, Settings } from "../shared/types.js";
import {
  CHATGPT_BASE_URL,
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
  checkBrowserProxyStatus as logBrowserProxyStatus,
  normalizeTotpSecret,
  otpInputs,
  pageText,
  passwordInputs,
  readSession,
  retryRoute,
  selectAuthenticatorIfVisible,
  sleep,
  startEmailOtpLogin,
  totpCode,
} from "./auth-helpers.js";
import { launchConfiguredChromium } from "./browser-config.js";
import { waitForEmailCode } from "./mail-service.js";
import { prepareBrowserProxy } from "./proxy-chain-server.js";

export interface LoginHealthProbeResult {
  deactivated: boolean;
  active: boolean;
  detail: string;
  snapshot?: SessionSnapshot;
}

export async function probeAccountHealthByLogin(
  account: Account,
  settings: Settings,
  localProxy: string,
  dynamicProxy: string,
  log: (message: string) => void = () => undefined,
): Promise<LoginHealthProbeResult> {
  let browser: Browser | null = null;
  let responseDeactivation = "";
  const proxyRoute = await prepareBrowserProxy(localProxy, dynamicProxy);
  try {
    log(`登录兜底检测启动，代理链路: ${proxyRoute.label}`);
    const launched = await launchConfiguredChromium(settings, proxyRoute.proxy);
    browser = launched.browser;
    const context = launched.context;
    await context.clearCookies();
    const page = await context.newPage();
    await logBrowserProxyStatus(page, "登录兜底检测浏览器代理", Boolean(proxyRoute.proxy), log);
    page.on("response", async (response) => {
      if (response.status() < 400) return;
      const text = await response.text().catch(() => "");
      responseDeactivation ||= detectDeactivationMessage(text);
    });

    await page.goto(CHATGPT_BASE_URL, { waitUntil: "domcontentloaded", timeout: 60_000 });
    const existing = await readSession(page, context);
    if (existing) return { deactivated: false, active: true, detail: "登录兜底检测：账号可正常登录", snapshot: existing };

    let loginUrl = "";
    try {
      loginUrl = await createOpenAiAuthUrl(context, "login", account.email);
    } catch (error) {
      const detail = detectDeactivationMessage((error as Error).message);
      if (detail) return { deactivated: true, active: false, detail };
      throw error;
    }

    let otpAfter = new Date(Date.now() - 10_000);
    await page.goto(loginUrl, { waitUntil: "domcontentloaded", timeout: 90_000 });

    const deadline = Date.now() + 4 * 60_000;
    let emailCodeSubmitted = false;
    let passwordSubmitted = false;
    let passwordSubmitAttempts = 0;
    let emailOtpFallbackAttempted = false;
    let lastTotpPeriod = -1;
    let routeRetries = 0;
    let challengeSince = 0;

    while (Date.now() < deadline) {
      if (responseDeactivation) return { deactivated: true, active: false, detail: responseDeactivation };

      const text = await pageText(page, 3000);
      const pageDeactivation = detectDeactivationMessage(`${page.url()}\n${text}`);
      if (pageDeactivation) return { deactivated: true, active: false, detail: pageDeactivation };

      const snapshot = await readSession(page, context);
      if (snapshot) return { deactivated: false, active: true, detail: "登录兜底检测：账号可正常登录", snapshot };

      const routeError = await detectRouteError(page);
      if (routeError) {
        if (routeRetries >= 2) return { deactivated: false, active: false, detail: `登录页面错误: ${routeError}` };
        routeRetries += 1;
        log(`登录页面错误，重试 ${routeRetries}/2`);
        await retryRoute(page);
        await sleep(3000);
        continue;
      }

      if (await hasChallenge(page)) {
        if (settings.headless) return { deactivated: false, active: false, detail: "登录兜底检测触发人机验证，请关闭无头模式后手动完成" };
        challengeSince ||= Date.now();
        if (Date.now() - challengeSince > 75_000) return { deactivated: false, active: false, detail: "人机验证持续超过 75 秒，无法确认是否封号" };
        await sleep(1500);
        continue;
      }
      challengeSince = 0;

      if (await dismissWelcome(page)) { await sleep(300); continue; }
      if (await clickLoginConfirmationIfVisible(page)) {
        log("已点击登录/IP确认页面");
        await sleep(700);
        continue;
      }
      if (/add-phone|phone-verification/i.test(page.url())) {
        return { deactivated: false, active: false, detail: "登录兜底检测遇到手机号验证，无法确认是否封号" };
      }

      const passwords = await passwordInputs(page);
      if (passwords.length) {
        const passwordRejected = passwordSubmitted
          && /incorrect password|wrong password|invalid password|password is incorrect|密码错误|密码无效|密码不正确|パスワードが正しくありません|パスワードが間違っています/i.test(text);
        const shouldUseEmailOtp = !account.gptPassword || passwordRejected || passwordSubmitAttempts >= 2;
        if (shouldUseEmailOtp && !emailOtpFallbackAttempted) {
          emailOtpFallbackAttempted = true;
          const fallback = await startEmailOtpLogin(page);
          const fallbackDeactivation = detectDeactivationMessage(fallback.detail);
          if (fallbackDeactivation) return { deactivated: true, active: false, detail: fallbackDeactivation };
          if (fallback.started) {
            otpAfter = new Date(Date.now() - 5_000);
            passwordSubmitted = false;
            emailCodeSubmitted = false;
            log(`${passwordRejected ? "GPT 密码被拒绝" : !account.gptPassword ? "未保存 GPT 密码" : "密码页连续提交未跳转"}，${fallback.detail}`);
            await sleep(700);
            continue;
          }
          log(`邮箱一次性验证码降级失败: ${fallback.detail}`);
        }
        if (!account.gptPassword) return { deactivated: false, active: false, detail: "登录要求 GPT 密码，账号未保存密码，且无法切换邮箱一次性验证码" };
        if (passwordRejected) return { deactivated: false, active: false, detail: "GPT 登录密码错误，且无法切换邮箱一次性验证码" };
        if (passwordSubmitAttempts >= 2) return { deactivated: false, active: false, detail: "GPT 密码连续提交两次仍未跳转，且无法切换邮箱一次性验证码" };
        if (passwordSubmitted) {
          if (!await forceSubmitActiveAuthForm(page)) return { deactivated: false, active: false, detail: "GPT 密码已填写，但无法再次点击登录确认按钮" };
          passwordSubmitAttempts += 1;
          log("密码页仍未跳转，已再次点击继续/提交");
          await sleep(1500);
          continue;
        }
        if (!await fillPasswordAndSubmit(page, account.gptPassword)) return { deactivated: false, active: false, detail: "GPT 密码输入框或提交按钮未找到" };
        passwordSubmitted = true;
        passwordSubmitAttempts = 1;
        emailCodeSubmitted = false;
        log("已提交 GPT 登录密码，继续等待账号状态");
        await sleep(1500);
        continue;
      }

      if (normalizeTotpSecret(account.twofaSecret).length >= 16 && await selectAuthenticatorIfVisible(page)) {
        log("已选择 Authenticator app 验证方式");
        await sleep(700);
        continue;
      }

      const codes = await otpInputs(page);
      if (codes.length) {
        const mode = detectLoginCodeMode(page.url(), text, normalizeTotpSecret(account.twofaSecret).length >= 16, passwordSubmitted);
        if (mode === "totp") {
          if (normalizeTotpSecret(account.twofaSecret).length < 16) {
            return { deactivated: false, active: false, detail: "登录要求 Authenticator 2FA，但该账号未保存有效 2FA 密钥" };
          }
          let generated = totpCode(account.twofaSecret);
          if (generated.remaining <= 8) {
            await sleep((generated.remaining + 1) * 1000);
            generated = totpCode(account.twofaSecret);
          }
          const period = Math.floor(Date.now() / 30_000);
          if (period !== lastTotpPeriod) {
            if (!await fillCodeAndSubmit(page, generated.code)) return { deactivated: false, active: false, detail: "无法提交 Authenticator 2FA 验证码" };
            lastTotpPeriod = period;
            log(`已提交 Authenticator 验证码，剩余 ${generated.remaining} 秒`);
          }
        } else if (!emailCodeSubmitted) {
          if (!account.clientId || !account.refreshToken) return { deactivated: false, active: false, detail: "登录要求邮箱验证码，但该账号缺少邮箱令牌" };
          const code = await waitForEmailCode(account, otpAfter, new AbortController().signal);
          if (!await fillCodeAndSubmit(page, code)) return { deactivated: false, active: false, detail: "无法提交邮箱验证码" };
          emailCodeSubmitted = true;
          log("已提交邮箱验证码，继续等待账号状态");
        }
        await sleep(700);
        continue;
      }

      if (passwordSubmitted && normalizeTotpSecret(account.twofaSecret).length >= 16
        && /authenticator|two-factor|2fa|mfa|身份验证器|认证器|認証アプリ/i.test(text)
        && await clickAction(page, /authenticator app|authenticator|身份验证器|认证器|認証アプリ/i)) {
        log("已选择 Authenticator app 验证方式");
        await sleep(700);
        continue;
      }

      if (await fillEmailIfVisible(page, account.email)) {
        emailCodeSubmitted = false;
        log("已填写登录邮箱，继续等待账号状态");
        await sleep(700);
        continue;
      }
      if (await clickContinue(page)) { await sleep(700); continue; }
      await sleep(700);
    }

    return { deactivated: false, active: false, detail: "登录兜底检测超时，未发现封号提示" };
  } catch (error) {
    const message = (error as Error).message;
    const detail = detectDeactivationMessage(message);
    if (detail) return { deactivated: true, active: false, detail };
    return { deactivated: false, active: false, detail: `登录兜底检测失败: ${message}` };
  } finally {
    if (browser) await browser.close().catch(() => undefined);
    await proxyRoute.close().catch(() => undefined);
  }
}
