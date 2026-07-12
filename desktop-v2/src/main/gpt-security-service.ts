// @ts-nocheck
import fs from "node:fs/promises";
import path from "node:path";
import jsQR from "jsqr";
import { PNG } from "pngjs";
import type { Locator, Page } from "playwright-core";
import type { Account } from "../shared/types.js";
import {
  CHATGPT_BASE_URL,
  fillCodeAndSubmit,
  generatePassword,
  normalizeTotpSecret,
  otpInputs,
  pageText,
  sleep,
  totpCode,
} from "./auth-helpers.js";
import { captureFlowSnapshot } from "./flow-snapshot.js";
import { waitForEmailCode } from "./mail-service.js";

export interface GptSecuritySetupResult {
  passwordOk: boolean | null;
  mfaOk: boolean;
  password: string;
  twofaSecret: string;
}

interface SetupOptions {
  signal: AbortSignal;
  log(message: string): void;
  patch(patch: Partial<Account>): Promise<Account>;
}

type PasswordDialogResult = { success: boolean | null; submitted: boolean; retry: boolean };

const SETTINGS_TERMS = ["Settings", "\u8bbe\u7f6e", "\u8a2d\u5b9a"];
const SECURITY_TERMS = [
  "Account security & login",
  "Account security",
  "\u8d26\u6237\u5b89\u5168\u4e0e\u767b\u5f55",
  "\u8d26\u53f7\u5b89\u5168\u4e0e\u767b\u5f55",
  "\u30bb\u30ad\u30e5\u30ea\u30c6\u30a3\u3068\u30ed\u30b0\u30a4\u30f3",
  "\u30bb\u30ad\u30e5\u30ea\u30c6\u30a3\u3068\u30ed\u30b0",
];
const SECURITY_EXCLUDE_TERMS = [
  "Safety",
  "Content safety",
  "\u5185\u5bb9\u5b89\u5168",
  "\u5b89\u5168\u4e0e\u9690\u79c1",
  "\u30bb\u30fc\u30d5\u30c6\u30a3",
  "\u30b3\u30f3\u30c6\u30f3\u30c4\u5b89\u5168",
];
const PASSWORD_SECTION_TERMS = [
  "Add password",
  "Update password",
  "Password",
  "\u767b\u5f55\u5bc6\u7801",
  "\u5bc6\u7801",
  "\u30d1\u30b9\u30ef\u30fc\u30c9",
];
const PASSWORD_ACTION_TERMS = [
  "add password",
  "update password",
  "add",
  "set",
  "change",
  "\u6dfb\u52a0",
  "\u8bbe\u7f6e",
  "\u66f4\u6539",
  "\u8ffd\u52a0",
  "\u5909\u66f4",
];
const PASSKEY_EXCLUDE_TERMS = [
  "security key",
  "passkey",
  "YubiKey",
  "\u5b89\u5168\u5bc6\u94a5",
  "\u901a\u884c\u5bc6\u94a5",
  "\u30bb\u30ad\u30e5\u30ea\u30c6\u30a3\u30ad\u30fc",
  "\u30d1\u30b9\u30ad\u30fc",
];
const FORM_ACTION_TERMS = [
  "Save",
  "Continue",
  "Confirm",
  "Add",
  "Done",
  "Submit",
  "Verify",
  "Enable",
  "\u4fdd\u5b58",
  "\u7ee7\u7eed",
  "\u786e\u8ba4",
  "\u6dfb\u52a0",
  "\u5b8c\u6210",
  "\u9a8c\u8bc1",
  "\u542f\u7528",
  "\u7d9a\u884c",
  "\u78ba\u8a8d",
  "\u8ffd\u52a0",
  "\u5b8c\u4e86",
  "\u691c\u8a3c",
  "\u6709\u52b9",
];
const FORM_CANCEL_TERMS = [
  "Cancel",
  "Close",
  "Back",
  "\u53d6\u6d88",
  "\u5173\u95ed",
  "\u8fd4\u56de",
  "\u30ad\u30e3\u30f3\u30bb\u30eb",
  "\u9589\u3058\u308b",
  "\u623b\u308b",
];
const PASSWORD_ERROR_PATTERN = /password is invalid|passwords do not match|failed to set password|invalid password|do not match|\u5bc6\u7801\u65e0\u6548|\u5bc6\u7801\u4e0d\u4e00\u81f4|\u30d1\u30b9\u30ef\u30fc\u30c9\u304c\u4e00\u81f4/i;

export async function setupGptSecurity(page: Page, account: Account, options: SetupOptions): Promise<GptSecuritySetupResult> {
  options.log("Wait 3s after ChatGPT login before opening security settings");
  await abortableSleep(3000, options.signal);
  await captureFlowSnapshot(page, account.email, "security-00-after-login-wait", options.log);

  options.log("Step 1/4: open ChatGPT account security settings");
  if (!await openSecuritySettings(page, options.log, account.email)) {
    await captureFlowSnapshot(page, account.email, "security-01-open-settings-failed", options.log);
    throw new Error("Unable to open ChatGPT account security settings");
  }
  await captureFlowSnapshot(page, account.email, "security-02-settings-opened", options.log);
  options.log("Step 1/4: account security settings opened");

  const passwordOk = await setupGptPassword(page, account, options);
  if (!await securitySettingsPageReady(page) && !await openSecuritySettings(page, options.log, account.email)) {
    throw new Error("Unable to reopen account security settings after password setup");
  }

  await captureFlowSnapshot(page, account.email, "security-20-before-mfa", options.log);
  const mfaOk = await setupAuthenticatorMfa(page, account, options);
  await captureFlowSnapshot(page, account.email, "security-30-after-mfa", options.log);

  let finalPasswordOk = passwordOk;
  if (finalPasswordOk === false && mfaOk) {
    options.log("MFA completed; rechecking GPT login password state");
    if (await openSecuritySettings(page, options.log, account.email) && await waitUntil(() => passwordConfigured(page), 12000, 500)) {
      finalPasswordOk = true;
      options.log("Step 2/4: delayed password recheck passed");
    }
  }

  const passwordStatus = finalPasswordOk === true ? "ok" : finalPasswordOk === null ? "unavailable" : "fail";
  options.log(`GPT security setup result: password=${passwordStatus}, authenticator=${mfaOk ? "ok" : "fail"}`);
  const setupOk = mfaOk && finalPasswordOk !== false;
  options.log(`Step 4/4: final recheck ${setupOk ? "passed" : "failed"}`);
  if (!setupOk) throw new Error("GPT password/MFA was not fully completed; Session will not be saved");

  return { passwordOk: finalPasswordOk, mfaOk, password: account.gptPassword, twofaSecret: account.twofaSecret };
}

async function setupGptPassword(page: Page, account: Account, options: SetupOptions): Promise<boolean | null> {
  if (await passwordConfigured(page)) {
    options.log("Step 2/4: GPT login password already exists");
    return true;
  }
  await captureFlowSnapshot(page, account.email, "security-10-password-start", options.log);

  const previousPassword = account.gptPassword;
  const password = account.gptPassword || generatePassword();
  if (!account.gptPassword) {
    account.gptPassword = password;
    await options.patch({ gptPassword: password });
    options.log("Generated GPT login password");
  }
  options.log("Start setting GPT login password");

  let submitted = false;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    throwIfAborted(options.signal);
    assertPageOpen(page, "set GPT password");

    if (await passwordConfigured(page)) {
      if (account.gptPassword !== password) {
        account.gptPassword = password;
        await options.patch({ gptPassword: password });
      }
      options.log("Step 2/4: GPT login password setup confirmed");
      return true;
    }

    if (attempt > 1 && !await securitySettingsPageReady(page) && !await openSecuritySettings(page, options.log, account.email)) {
      options.log(`Password setup retry ${attempt}/3: security settings could not be reopened`);
      continue;
    }

    const clicked = await clickSectionAction(page, PASSWORD_SECTION_TERMS, PASSWORD_ACTION_TERMS, PASSKEY_EXCLUDE_TERMS);
    if (!clicked) {
      if (await passwordConfigured(page)) {
        if (account.gptPassword !== password) {
          account.gptPassword = password;
          await options.patch({ gptPassword: password });
        }
        options.log("Step 2/4: GPT login password setup confirmed");
        return true;
      }
      if (attempt === 3) {
        await captureSecurityDebug(page, "password-entry-not-found", options.log);
        account.gptPassword = submitted ? password : previousPassword;
        await options.patch({ gptPassword: account.gptPassword || "" });
        options.log("Password add/update entry was not found");
        return null;
      }
      await sleep(500);
      continue;
    }

    await captureFlowSnapshot(page, account.email, `security-11-password-action-clicked-attempt-${attempt}`, options.log);
    const result = await fillPasswordSetupFlow(page, account, password, options);
    submitted ||= result.submitted;

    if (result.success === true) {
      if ((await securitySettingsPageReady(page) || await openSecuritySettings(page, options.log, account.email)) && await waitUntil(() => passwordConfigured(page), 8000, 500)) {
        options.log("Step 2/4: GPT login password setup confirmed");
        return true;
      }
      account.gptPassword = submitted ? password : previousPassword;
      await options.patch({ gptPassword: account.gptPassword || "" });
      options.log("Password form was submitted, but saved state was not confirmed");
      return false;
    }

    if (result.success === null || result.retry) {
      await sleep(600);
      continue;
    }

    break;
  }

  account.gptPassword = submitted ? password : previousPassword;
  await options.patch({ gptPassword: account.gptPassword || "" });
  return false;
}

async function fillPasswordSetupFlow(page: Page, account: Account, password: string, options: SetupOptions): Promise<PasswordDialogResult> {
  const otpAfter = new Date(Date.now() - 120000);
  const deadline = Date.now() + 60000;
  let submitted = false;
  let lastSubmitAt = 0;
  let missingSince = 0;
  let passwordInputsSnapshotTaken = false;
  let passwordSubmittedSnapshotTaken = false;
  let emailOtpSnapshotTaken = false;

  while (Date.now() < deadline) {
    throwIfAborted(options.signal);
    assertPageOpen(page, "fill GPT password");

    if (await dismissPostLoginWelcomeIfVisible(page)) {
      options.log("Password setup was interrupted by the welcome page; dismissed and retrying");
      return { success: null, submitted, retry: true };
    }
    await dismissOfferModal(page);

    const passwordInputsVisible = (await visiblePasswordInputsInActivePasswordSurface(page)).length > 0;
    const codeInputs = await otpInputs(page);
    if (codeInputs.length && !passwordInputsVisible) {
      if (!emailOtpSnapshotTaken) {
        emailOtpSnapshotTaken = true;
        await captureFlowSnapshot(page, account.email, "security-14-password-email-otp-visible", options.log);
      }
      const code = await waitForEmailCode(account, otpAfter, options.signal);
      if (!await fillCodeAndSubmit(page, code)) return { success: false, submitted, retry: false };
      submitted = true;
      options.log("Submitted email code during password setup");
      await sleep(1000);
      continue;
    }

    const passInputs = await visiblePasswordInputsInActivePasswordSurface(page);
    if (passInputs.length) {
      missingSince = 0;
      if (!passwordInputsSnapshotTaken) {
        passwordInputsSnapshotTaken = true;
        await captureFlowSnapshot(page, account.email, "security-12-password-inputs-visible", options.log);
      }

      const domFilled = await forceFillPasswordPageByDom(page, password);
      for (const input of passInputs) await forceFillLocator(input, password);
      const confirmed = Math.max(domFilled, await passwordPageFilledCount(page, password));
      options.log(`GPT password inputs filled: ${confirmed}`);

      if (confirmed >= Math.min(2, passInputs.length) && Date.now() - lastSubmitAt > 1200) {
        submitted = await clickPrimaryFormActionByDom(page)
          || await clickDialogButtonByText(page, FORM_ACTION_TERMS)
          || await clickButtonByText(page, FORM_ACTION_TERMS);
        if (!submitted) {
          await passInputs.at(-1)?.press("Enter").then(() => { submitted = true; }).catch(() => undefined);
        }
        lastSubmitAt = Date.now();
        if (submitted) {
          options.log("GPT password form submitted");
          if (!passwordSubmittedSnapshotTaken) {
            passwordSubmittedSnapshotTaken = true;
            await captureFlowSnapshot(page, account.email, "security-13-password-submitted", options.log);
          }
        }
      }

      if (submitted && PASSWORD_ERROR_PATTERN.test(await pageText(page, 2000))) {
        await captureSecurityDebug(page, "password-error", options.log);
        return { success: false, submitted, retry: false };
      }
      await sleep(350);
      continue;
    }

    if (submitted) {
      if (await passwordConfigured(page)) return { success: true, submitted, retry: false };
      if (!missingSince) missingSince = Date.now();
      if (Date.now() - missingSince >= 2500) return { success: null, submitted, retry: true };
    } else if (await securitySettingsPageReady(page) && !await passwordConfigured(page)) {
      if (!missingSince) missingSince = Date.now();
      if (Date.now() - missingSince >= 2500) return { success: null, submitted, retry: true };
    } else {
      missingSince = 0;
    }

    await sleep(300);
  }

  await captureSecurityDebug(page, "password-timeout", options.log);
  return { success: false, submitted, retry: false };
}

async function setupAuthenticatorMfa(page: Page, account: Account, options: SetupOptions): Promise<boolean> {
  options.log("Start enabling Authenticator app MFA");
  if (!await waitUntil(() => mfaSectionPresent(page), 12000, 500)) {
    await captureFlowSnapshot(page, account.email, "security-21-mfa-section-missing", options.log);
    await captureSecurityDebug(page, "mfa-section-not-loaded", options.log);
    options.log("Authenticator app MFA section was not loaded");
    return false;
  }

  if (await mfaSwitchState(page) === true) {
    if (!trustedSecret(account.twofaSecret)) {
      if (account.twofaSecret) {
        account.twofaSecret = "";
        await options.patch({ twofaSecret: "" });
      }
      options.log("Authenticator app MFA is enabled, but no trusted local 2FA secret is saved");
      return false;
    }
    options.log("Step 3/4: Authenticator app MFA is already enabled");
    return true;
  }

  await captureFlowSnapshot(page, account.email, "security-22-mfa-section-ready", options.log);
  if (!await clickMfaSwitch(page) && !await clickSectionAction(page, ["Authenticator app", "authenticator", "MFA", "\u8eab\u4efd\u9a8c\u8bc1\u5668", "\u8ba4\u8bc1\u5668"], ["enable", "turn on", "add", "\u5f00\u542f", "\u542f\u7528", "\u6dfb\u52a0", "\u8ffd\u52a0"], [])) {
    await captureFlowSnapshot(page, account.email, "security-23-mfa-enable-click-failed", options.log);
    await captureSecurityDebug(page, "mfa-entry-not-found", options.log);
    options.log("Authenticator app MFA switch or entry was not found");
    return false;
  }

  if (account.twofaSecret) {
    account.twofaSecret = "";
    await options.patch({ twofaSecret: "" });
    options.log("Cleared old unverified 2FA secret");
  }
  await sleep(250);
  await waitUntil(() => mfaVerificationDialogVisible(page), 4000, 250);
  await captureFlowSnapshot(page, account.email, "security-24-mfa-enable-clicked", options.log);
  await captureSecurityDebug(page, "mfa-after-entry-click", options.log);

  const deadline = Date.now() + 120000;
  let secret = "";
  let lastSubmittedPeriod = -1;
  let waitingForSecretLogged = false;

  while (Date.now() < deadline) {
    throwIfAborted(options.signal);
    assertPageOpen(page, "set Authenticator app MFA");

    if (secret && await mfaSetupSucceeded(page)) {
      account.twofaSecret = secret;
      await options.patch({ twofaSecret: secret });
      options.log("Authenticator app MFA enabled; 2FA secret saved");
      return true;
    }

    const passwordInputs = await visiblePasswordPageInputs(page);
    if (passwordInputs.length && account.gptPassword) {
      for (const input of passwordInputs) await forceFillLocator(input, account.gptPassword);
      if (await clickButtonByText(page, ["Continue", "Confirm", "Verify", "\u7ee7\u7eed", "\u786e\u8ba4", "\u9a8c\u8bc1", "\u7d9a\u884c", "\u78ba\u8a8d", "\u691c\u8a3c"])) {
        options.log("Submitted MFA password confirmation");
        await sleep(2000);
        continue;
      }
    }

    if (!secret) {
      secret = await extractTotpSecretFromPage(page, options.log);
      if (secret) {
        options.log("Read Authenticator app 2FA secret; waiting for verification");
        await captureFlowSnapshot(page, account.email, "security-25-mfa-secret-read", options.log);
      }
    }

    const currentPeriod = Math.floor(Date.now() / 30000);
    if (secret && currentPeriod !== lastSubmittedPeriod && await fillTotpCodeForMfa(page, secret, options, async () => {
      await captureFlowSnapshot(page, account.email, "security-26-mfa-code-filled-before-submit", options.log);
    })) {
      lastSubmittedPeriod = currentPeriod;
      await captureFlowSnapshot(page, account.email, "security-26-mfa-code-submitted", options.log);
      if (await waitUntil(() => mfaSetupSucceeded(page), 5000, 500)) {
        account.twofaSecret = secret;
        await options.patch({ twofaSecret: secret });
        options.log("Authenticator app MFA enabled; 2FA secret saved");
        return true;
      }
      await captureSecurityDebug(page, "mfa-submit-still-visible", options.log);
    }

    if (await hasOtpInput(page) && !secret) {
      if (await mfaVerificationDialogVisible(page)) {
        if (!waitingForSecretLogged) {
          options.log("No explicit Authenticator secret has been read yet; code submission is paused");
          waitingForSecretLogged = true;
        }
        await sleep(500);
        continue;
      }
      const code = await waitForEmailCode(account, new Date(Date.now() - 120000), options.signal);
      if (await fillCodeAndSubmit(page, code)) {
        options.log("Submitted email code during MFA setup");
        await sleep(250);
        await captureSecurityDebug(page, "mfa-after-email-code", options.log);
        continue;
      }
    }

    await sleep(250);
  }

  await captureFlowSnapshot(page, account.email, "security-29-mfa-timeout", options.log);
  await captureSecurityDebug(page, "mfa-timeout", options.log);
  options.log("Authenticator app MFA was not confirmed; this 2FA secret will not be saved");
  return false;
}

async function openSecuritySettings(page: Page, log: (message: string) => void, email = ""): Promise<boolean> {
  if (await securitySettingsPageReady(page)) return true;

  for (let attempt = 1; attempt <= 3; attempt += 1) {
    if (!await openChatGptSettings(page, log, email)) {
      await sleep(500);
      continue;
    }
    if (await clickSecuritySettingsEntry(page)) {
      log("Clicked account security and login settings");
    } else {
      log("Account security and login entry was not found in settings panel");
    }
    if (await waitForSecuritySettingsPage(page, log, 12000)) return true;
    await captureSecurityDebug(page, `security-page-not-ready-attempt-${attempt}`, log);
    await page.keyboard.press("Escape").catch(() => undefined);
    await sleep(500);
  }
  return false;
}

async function openChatGptSettings(page: Page, log: (message: string) => void, email = ""): Promise<boolean> {
  assertPageOpen(page, "open ChatGPT settings");
  log("Opening ChatGPT settings panel");
  if (!page.url().startsWith(CHATGPT_BASE_URL)) {
    await page.goto(CHATGPT_BASE_URL, { waitUntil: "domcontentloaded", timeout: 60000 });
  }
  if (!await waitUntil(() => hasChatGptSession(page), 5000, 500)) {
    log("ChatGPT session was not visible on current page; refreshing ChatGPT home");
    await page.goto(CHATGPT_BASE_URL, { waitUntil: "domcontentloaded", timeout: 60000 });
    await waitUntil(() => hasChatGptSession(page), 20000, 500);
  }

  for (let i = 0; i < 3; i += 1) {
    const dismissed = await dismissPostLoginWelcomeIfVisible(page) || await dismissOfferModal(page);
    if (!dismissed) break;
    await sleep(500);
  }

  if (await settingsDialogVisible(page)) return true;

  for (let attempt = 1; attempt <= 4; attempt += 1) {
    await dismissPostLoginWelcomeIfVisible(page);
    await dismissOfferModal(page);
    if (!await clickProfileOrSettingsMenu(page, email)) {
      log(`Profile/account menu not found; retry ${attempt}/4`);
      await sleep(500);
      continue;
    }
    log(`Clicked profile/account menu (${attempt}/4)`);
    if (await clickSettingsMenuEntry(page)) {
      log("Clicked Settings and confirmed settings panel");
      return true;
    }
    await page.keyboard.press("Escape").catch(() => undefined);
    await sleep(500);
  }

  await captureSecurityDebug(page, "open-settings-failed", log);
  return false;
}

async function waitForSecuritySettingsPage(page: Page, log: (message: string) => void, timeoutMs: number): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await securitySettingsPageReady(page)) return true;
    if (await passkeySecuritySubpageVisible(page)) {
      log("Passkey/security-key subpage detected; returning to main security settings");
      await recoverFromPasskeySecuritySubpage(page);
    }
    await sleep(500);
  }
  return false;
}

async function hasChatGptSession(page: Page): Promise<boolean> {
  if (!page.url().startsWith(CHATGPT_BASE_URL)) return false;
  return page.evaluate(async () => {
    const response = await fetch("/api/auth/session", { credentials: "include" }).catch(() => null);
    if (!response?.ok) return false;
    const payload = await response.json().catch(() => null);
    return Boolean(payload?.accessToken);
  }).catch(() => false);
}

async function dismissPostLoginWelcomeIfVisible(page: Page): Promise<boolean> {
  const box = await page.evaluate(() => {
    const visible = (element: Element | null) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const body = (document.body?.innerText || "").replace(/\s+/g, " ").toLowerCase();
    const welcome = /you're all set|you are all set|ready to go|welcome to chatgpt|\u51c6\u5907\u5df2\u5b8c\u6210|\u51c6\u5907\u597d\u4e86|\u4e00\u5207\u51c6\u5907\u5c31\u7eea|\u6e96\u5099\u304c\u5b8c\u4e86|\u3088\u3046\u3053\u305d/.test(body);
    if (!welcome) return null;
    const buttons = Array.from(document.querySelectorAll<HTMLElement>('button, [role="button"], a')).filter(visible);
    const candidates = buttons.map((element) => {
      const label = `${element.textContent || ""} ${element.getAttribute("aria-label") || ""}`.replace(/\s+/g, " ").trim().toLowerCase();
      const rect = element.getBoundingClientRect();
      let score = 0;
      if (/continue|next|start|done|\u7ee7\u7eed|\u4e0b\u4e00\u6b65|\u5f00\u59cb|\u5b8c\u6210|\u7d9a\u884c|\u6b21\u3078|\u59cb\u3081\u308b|\u5b8c\u4e86/.test(label)) score += 500;
      if (/terms|privacy|\u8a73\u3057\u304f|\u4e86\u89e3\u66f4\u591a/.test(label)) score -= 300;
      return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, score };
    }).filter((item) => item.score > 0).sort((a, b) => b.score - a.score);
    return candidates[0] || null;
  }).catch(() => null);
  if (!box) return false;
  await page.mouse.click(box.x, box.y).catch(() => undefined);
  await sleep(500);
  return true;
}

async function dismissOfferModal(page: Page): Promise<boolean> {
  const box = await page.evaluate(() => {
    const visible = (element: Element | null) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const roots = Array.from(document.querySelectorAll<HTMLElement>('[role="dialog"], [aria-modal="true"], main, body')).filter(visible);
    for (const root of roots) {
      const text = (root.textContent || "").replace(/\s+/g, " ").toLowerCase();
      if (!/plus|offer|trial|free|memory|memories|\u30aa\u30d5\u30a1\u30fc|\u7121\u6599|\u8a66\u7528|\u30c8\u30e9\u30a4\u30a2\u30eb|\u30e1\u30e2\u30ea|\u4f18\u60e0|\u8bb0\u5fc6/.test(text)) continue;
      const close = Array.from(root.querySelectorAll<HTMLElement>('button, [role="button"], a'))
        .filter(visible)
        .map((element) => {
          const rect = element.getBoundingClientRect();
          const label = `${element.textContent || ""} ${element.getAttribute("aria-label") || ""}`.replace(/\s+/g, " ").trim().toLowerCase();
          let score = 0;
          if (/close|dismiss|not now|maybe later|\u4eca\u306f\u3057\u306a\u3044|\u30ad\u30e3\u30f3\u30bb\u30eb|\u9589\u3058\u308b|\u5173\u95ed|\u53d6\u6d88|\u6682\u4e0d|x|\u00d7/.test(label)) score += 500;
          if (rect.top < window.innerHeight * 0.25 && rect.left > window.innerWidth * 0.7) score += 150;
          if (/\u8868\u793a\u3059\u308b|\u7121\u6599|offer|trial|plus|\u9886\u53d6|\u53d7\u3051\u53d6\u308b/.test(label)) score -= 1000;
          return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, score };
        })
        .filter((item) => item.score > 0)
        .sort((a, b) => b.score - a.score)[0];
      if (close) return close;
    }
    return null;
  }).catch(() => null);
  if (!box) return false;
  await page.mouse.click(box.x, box.y).catch(() => undefined);
  await sleep(300);
  return true;
}

async function settingsDialogVisible(page: Page): Promise<boolean> {
  if (page.isClosed()) return false;
  return page.evaluate(() => {
    const visible = (element: Element | null) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const groups = [
      ["general", "\u5e38\u89c4", "\u4e00\u822c"],
      ["notifications", "\u901a\u77e5", "\u901a\u77e5\u8a2d\u5b9a"],
      ["personalization", "\u4e2a\u6027\u5316", "\u30d1\u30fc\u30bd\u30ca\u30e9\u30a4\u30ba"],
      ["data controls", "\u6570\u636e\u7ba1\u7406", "\u30c7\u30fc\u30bf"],
      ["security", "\u8d26\u6237\u5b89\u5168\u4e0e\u767b\u5f55", "\u5b89\u5168", "\u30bb\u30ad\u30e5\u30ea\u30c6\u30a3"],
      ["account", "\u8d26\u6237", "\u30a2\u30ab\u30a6\u30f3\u30c8"],
      ["settings", "\u8bbe\u7f6e", "\u8a2d\u5b9a"],
    ];
    const dialogs = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"]')).filter(visible);
    return dialogs.some((dialog) => {
      const text = (dialog.textContent || "").replace(/\s+/g, " ").toLowerCase();
      const hits = groups.filter((group) => group.some((term) => text.includes(term.toLowerCase()))).length;
      return hits >= 3;
    });
  }).catch(() => false);
}

async function securitySettingsPageReady(page: Page): Promise<boolean> {
  return await settingsDialogVisible(page) && await hasPasswordSection(page) && await mfaSectionPresent(page);
}

async function hasPasswordSection(page: Page): Promise<boolean> {
  const text = (await pageText(page, 3000)).toLowerCase();
  return /password|\u5bc6\u7801|\u30d1\u30b9\u30ef\u30fc\u30c9/.test(text);
}

async function mfaSectionPresent(page: Page): Promise<boolean> {
  const text = (await pageText(page, 3000)).toLowerCase();
  return /authenticator app|multi-factor|mfa|\u591a\u56e0\u7d20|\u8eab\u4efd\u9a8c\u8bc1\u5668|\u8ba4\u8bc1\u5668|\u8a8d\u8a3c/.test(text);
}

async function passkeySecuritySubpageVisible(page: Page): Promise<boolean> {
  const text = (await pageText(page, 3000)).toLowerCase();
  if (/authenticator app|mfa|password|multi-factor|\u30d1\u30b9\u30ef\u30fc\u30c9/i.test(text)) return false;
  return /security key|passkey|yubikey|\u30bb\u30ad\u30e5\u30ea\u30c6\u30a3\u30ad\u30fc|\u30d1\u30b9\u30ad\u30fc|\u5b89\u5168\u5bc6\u94a5|\u901a\u884c\u5bc6\u94a5/.test(text);
}

async function recoverFromPasskeySecuritySubpage(page: Page): Promise<boolean> {
  const clickedBack = await clickVisibleText(page, ["Back", "\u8fd4\u56de", "\u623b\u308b"], 'button, a, [role="button"], div');
  if (clickedBack) return true;
  await page.keyboard.press("Alt+Left").catch(() => undefined);
  await sleep(500);
  return true;
}

async function passwordConfigured(page: Page): Promise<boolean> {
  const rowState = await page.evaluate(() => {
    const visible = (element: Element | null) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const passwordPattern = /password|\u5bc6\u7801|\u30d1\u30b9\u30ef\u30fc\u30c9/i;
    const passkeyPattern = /security key|passkey|yubikey|\u5b89\u5168\u5bc6\u94a5|\u901a\u884c\u5bc6\u94a5|\u30bb\u30ad\u30e5\u30ea\u30c6\u30a3\u30ad\u30fc|\u30d1\u30b9\u30ad\u30fc/i;
    const configuredPattern = /\*{4,}|\u2022{4,}|set|configured|enabled|saved|password set|\u5df2\u8bbe\u7f6e|\u5df2\u4fdd\u5b58|\u5f00\u542f|\u8a2d\u5b9a\u6e08\u307f|\u4fdd\u5b58\u6e08\u307f/i;
    const rows = Array.from(document.querySelectorAll<HTMLElement>('section, li, [role="row"], button, [role="button"], div')).filter(visible);
    return rows.some((row) => {
      const text = (row.textContent || "").replace(/\s+/g, " ");
      if (!passwordPattern.test(text)) return false;
      if (passkeyPattern.test(text)) return false;
      return configuredPattern.test(text);
    });
  }).catch(() => false);
  if (rowState) return true;

  const text = await pageText(page, 6000);
  const lower = text.toLowerCase();
  const hasPassword = /password|\u5bc6\u7801|\u30d1\u30b9\u30ef\u30fc\u30c9/.test(lower);
  const configured = /\*{4,}|\u2022{4,}|set|configured|enabled|saved|password set|\u5df2\u8bbe\u7f6e|\u5df2\u4fdd\u5b58|\u5f00\u542f|\u8a2d\u5b9a\u6e08\u307f|\u4fdd\u5b58\u6e08\u307f/i.test(text);
  return hasPassword && configured;
}

async function visiblePasswordInputsInActivePasswordSurface(page: Page): Promise<Locator[]> {
  const dialogInputs = await visiblePasswordDialogInputs(page);
  if (dialogInputs.length) return dialogInputs;
  return visiblePasswordPageInputs(page);
}

async function visiblePasswordDialogInputs(page: Page): Promise<Locator[]> {
  const dialogs = page.locator('[role="dialog"], [aria-modal="true"]');
  const count = await dialogs.count().catch(() => 0);
  for (let index = count - 1; index >= 0; index -= 1) {
    const dialog = dialogs.nth(index);
    if (!await dialog.isVisible().catch(() => false)) continue;
    const inputs = await visiblePasswordInputs(dialog.locator('input[type="password"], input[name*="password" i], input[autocomplete*="password" i]'));
    if (inputs.length) return inputs;
  }
  return [];
}

async function visiblePasswordPageInputs(page: Page): Promise<Locator[]> {
  return visiblePasswordInputs(page.locator('input[type="password"], input[name*="password" i], input[autocomplete*="password" i]'));
}

async function visiblePasswordInputs(locator: Locator): Promise<Locator[]> {
  const output: Locator[] = [];
  const count = await locator.count().catch(() => 0);
  for (let index = 0; index < count; index += 1) {
    const input = locator.nth(index);
    if (await input.isVisible().catch(() => false) && await input.isEnabled().catch(() => false)) output.push(input);
  }
  return output;
}

async function forceFillLocator(locator: Locator, value: string): Promise<boolean> {
  await locator.scrollIntoViewIfNeeded().catch(() => undefined);
  await locator.click({ timeout: 1000 }).catch(() => undefined);
  await locator.fill(value, { timeout: 2000 }).catch(async () => {
    await locator.press("Control+A").catch(() => undefined);
    await locator.pressSequentially(value, { delay: 15 }).catch(() => undefined);
  });
  return locator.evaluate((element, value) => {
    const input = element as HTMLInputElement;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setter?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    input.dispatchEvent(new Event("blur", { bubbles: true }));
    return input.value === value;
  }, value).catch(() => false);
}

async function forceTypeLocator(locator: Locator, value: string): Promise<boolean> {
  await locator.scrollIntoViewIfNeeded().catch(() => undefined);
  await locator.click({ timeout: 1000 }).catch(() => undefined);
  await locator.press(process.platform === "darwin" ? "Meta+A" : "Control+A").catch(() => undefined);
  await locator.press("Backspace").catch(() => undefined);
  await locator.pressSequentially(value, { delay: 35 }).catch(() => undefined);
  const typed = await locator.inputValue({ timeout: 1000 }).catch(() => "");
  if (typed === value) return true;
  return locator.evaluate((element, value) => {
    const input = element as HTMLInputElement | HTMLTextAreaElement;
    input.focus();
    const setter = Object.getOwnPropertyDescriptor(input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype, "value")?.set;
    setter?.call(input, value);
    try { input.dispatchEvent(new InputEvent("beforeinput", { bubbles: true, cancelable: true, inputType: "insertText", data: value })); } catch {}
    try { input.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value })); } catch { input.dispatchEvent(new Event("input", { bubbles: true })); }
    input.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true, key: value.at(-1) || "" }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    return input.value === value;
  }, value).catch(() => false);
}

async function forceFillPasswordPageByDom(page: Page, password: string): Promise<number> {
  return page.evaluate((password) => {
    const visible = (element: Element | null) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    let filled = 0;
    for (const input of Array.from(document.querySelectorAll<HTMLInputElement>('input[type="password"], input[name*="password" i], input[autocomplete*="password" i]'))) {
      if (!visible(input) || input.disabled || input.readOnly) continue;
      input.focus();
      setter?.call(input, password);
      input.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: password }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      input.dispatchEvent(new Event("blur", { bubbles: true }));
      if (input.value === password) filled += 1;
    }
    return filled;
  }, password).catch(() => 0);
}

async function passwordPageFilledCount(page: Page, password: string): Promise<number> {
  const inputs = await visiblePasswordInputsInActivePasswordSurface(page);
  let count = 0;
  for (const input of inputs) if (await input.inputValue().catch(() => "") === password) count += 1;
  return count;
}

async function clickPrimaryFormActionByDom(page: Page): Promise<boolean> {
  const box = await page.evaluate(({ actionTerms, cancelTerms }) => {
    const visible = (element: Element | null) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const enabled = (element: Element) => !(element as HTMLButtonElement).disabled && element.getAttribute("aria-disabled") !== "true";
    const modalRoots = Array.from(document.querySelectorAll<HTMLElement>('[role="dialog"], [aria-modal="true"]')).filter(visible);
    const formRoots = Array.from(document.querySelectorAll<HTMLElement>('form')).filter(visible);
    const roots = (modalRoots.length ? modalRoots : formRoots.length ? formRoots : [document.body]);
    const candidates = roots.flatMap((root, rootIndex) => Array.from(root.querySelectorAll<HTMLElement>('button, [role="button"], input[type="submit"], input[type="button"]'))
      .filter((element) => visible(element) && enabled(element))
      .map((element) => {
        const rect = element.getBoundingClientRect();
        const text = `${element.textContent || ""} ${element.getAttribute("aria-label") || ""} ${(element as HTMLInputElement).value || ""}`.replace(/\s+/g, " ").trim().toLowerCase();
        const type = String((element as HTMLButtonElement | HTMLInputElement).type || "").toLowerCase();
        let score = 0;
        if (actionTerms.some((term) => text.includes(term.toLowerCase()))) score += 500;
        if (type === "submit") score += 200;
        if (cancelTerms.some((term) => text.includes(term.toLowerCase()))) score -= 1000;
        score += Math.max(0, rootIndex === 0 ? 80 : 0);
        return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, score, area: rect.width * rect.height, top: rect.top };
      })
      .filter((item) => item.score > 0)
    ).sort((a, b) => b.score - a.score || b.area - a.area || b.top - a.top);
    return candidates[0] || null;
  }, { actionTerms: FORM_ACTION_TERMS, cancelTerms: FORM_CANCEL_TERMS }).catch(() => null);
  if (!box) return false;
  await page.mouse.click(box.x, box.y);
  return true;
}

async function clickButtonByText(page: Page, texts: string[]): Promise<boolean> {
  return clickVisibleText(page, texts, 'button, [role="button"], input[type="submit"], input[type="button"], a');
}

async function clickDialogButtonByText(page: Page, texts: string[]): Promise<boolean> {
  const box = await page.evaluate((texts) => {
    const visible = (element: Element | null) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const enabled = (element: Element) => !(element as HTMLButtonElement).disabled && element.getAttribute("aria-disabled") !== "true";
    const wanted = texts.map((text) => text.toLowerCase());
    const modalRoots = Array.from(document.querySelectorAll<HTMLElement>('[role="dialog"], [aria-modal="true"]')).filter(visible);
    const formRoots = Array.from(document.querySelectorAll<HTMLElement>('form')).filter(visible);
    const roots = (modalRoots.length ? modalRoots : formRoots.length ? formRoots : [document.body]);
    const matches = roots.flatMap((root, rootIndex) => Array.from(root.querySelectorAll<HTMLElement>('button, [role="button"], input[type="submit"], input[type="button"], a')).filter((element) => visible(element) && enabled(element)).flatMap((element) => {
      const text = `${element.textContent || ""} ${element.getAttribute("aria-label") || ""} ${(element as HTMLInputElement).value || ""}`.replace(/\s+/g, " ").trim().toLowerCase();
      const hit = wanted.find((term) => text.includes(term));
      if (!hit) return [];
      const rect = element.getBoundingClientRect();
      return [{ x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, exact: text === hit ? 1 : 0, len: text.length, root: rootIndex === 0 ? 1 : 0, right: rect.right }];
    })).sort((a, b) => b.exact - a.exact || b.root - a.root || a.len - b.len || b.right - a.right);
    return matches[0] || null;
  }, texts).catch(() => null);
  if (!box) return false;
  await page.mouse.click(box.x, box.y);
  return true;
}

async function clickVisibleText(page: Page, terms: string[], selectors = 'button, [role="button"], a, [role="menuitem"], [role="tab"], [role="switch"], div'): Promise<boolean> {
  const box = await page.evaluate(({ terms, selectors }) => {
    const visible = (element: Element | null) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const wanted = terms.map((term) => String(term || "").toLowerCase()).filter(Boolean);
    const matches = Array.from(document.querySelectorAll<HTMLElement>(selectors)).filter(visible).flatMap((element) => {
      const text = `${element.textContent || ""} ${element.getAttribute("aria-label") || ""}`.replace(/\s+/g, " ").trim().toLowerCase();
      const hit = wanted.find((term) => text.includes(term));
      if (!hit) return [];
      const rect = element.getBoundingClientRect();
      const interactive = /^(BUTTON|A|INPUT)$/.test(element.tagName) || ["button", "menuitem", "tab", "switch"].includes(element.getAttribute("role") || "");
      return [{ x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, exact: text === hit ? 1 : 0, interactive: interactive ? 1 : 0, len: text.length }];
    }).sort((a, b) => b.exact - a.exact || b.interactive - a.interactive || a.len - b.len);
    return matches[0] || null;
  }, { terms, selectors }).catch(() => null);
  if (!box) return false;
  await page.mouse.click(box.x, box.y);
  return true;
}

async function clickSettingsMenuEntry(page: Page): Promise<boolean> {
  if (!await clickVisibleText(page, SETTINGS_TERMS, 'button, a, [role="button"], [role="menuitem"], [role="menuitemradio"], div')) return false;
  return waitUntil(() => settingsDialogVisible(page), 5000, 250);
}

async function clickSecuritySettingsEntry(page: Page): Promise<boolean> {
  const box = await page.evaluate(({ terms, excludes }) => {
    const visible = (element: Element | null) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const wanted = terms.map((term: string) => term.toLowerCase());
    const blocked = excludes.map((term: string) => term.toLowerCase());
    const dialogs = Array.from(document.querySelectorAll<HTMLElement>('[role="dialog"], [aria-modal="true"]')).filter(visible);
    const roots = dialogs.length ? dialogs : [document.body];
    const candidates = roots.flatMap((root) => {
      const controls = Array.from(root.querySelectorAll<HTMLElement>('button, a, [role="button"], [role="menuitem"], [role="tab"], div')).filter(visible);
      return controls.flatMap((element) => {
        const text = `${element.textContent || ""} ${element.getAttribute("aria-label") || ""}`.replace(/\s+/g, " ").trim().toLowerCase();
        if (!text) return [];
        if (blocked.some((term) => text.includes(term))) return [];
        const hit = wanted.find((term) => text.includes(term));
        if (!hit) return [];
        const rect = element.getBoundingClientRect();
        const interactive = /^(BUTTON|A)$/.test(element.tagName) || ["button", "menuitem", "tab"].includes(element.getAttribute("role") || "");
        let score = 0;
        score += hit.length * 20;
        if (text === hit) score += 1000;
        if (interactive) score += 300;
        if (/login|\u767b\u5f55|\u767b\u9304|\u30ed\u30b0/.test(text)) score += 500;
        if (/account|\u8d26\u6237|\u8d26\u53f7|\u30a2\u30ab\u30a6\u30f3\u30c8/.test(text)) score += 300;
        return [{ x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, score, len: text.length }];
      });
    }).sort((a, b) => b.score - a.score || a.len - b.len);
    return candidates[0] || null;
  }, { terms: SECURITY_TERMS, excludes: SECURITY_EXCLUDE_TERMS }).catch(() => null);
  if (!box) return false;
  await page.mouse.click(box.x, box.y);
  await sleep(350);
  return true;
}

async function clickProfileOrSettingsMenu(page: Page, email = ""): Promise<boolean> {
  const box = await page.evaluate((email) => {
    const visible = (element: Element | null) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const markTarget = (target: HTMLElement, container?: HTMLElement) => {
      document.querySelectorAll("[data-codex-profile-target]").forEach((element) => element.removeAttribute("data-codex-profile-target"));
      document.querySelectorAll("[data-codex-profile-container]").forEach((element) => element.removeAttribute("data-codex-profile-container"));
      target.setAttribute("data-codex-profile-target", "true");
      (container || target).setAttribute("data-codex-profile-container", "true");
      return target;
    };
    const offerRelated = (element: Element | null, stopAt?: Element | null) => {
      let current = element as HTMLElement | null;
      for (let depth = 0; current && current !== stopAt && depth < 6; depth += 1, current = current.parentElement) {
        const text = `${current.textContent || ""} ${current.getAttribute("aria-label") || ""}`.replace(/\s+/g, " ").toLowerCase();
        if (/offer|trial|free|gift|plus|\u30aa\u30d5\u30a1\u30fc|\u7121\u6599|\u8a66\u7528|\u30c8\u30e9\u30a4\u30a2\u30eb|\u4f18\u60e0|\u793c\u5305/.test(text)) return true;
      }
      return false;
    };
    const avatarInside = (container: HTMLElement) => {
      const crect = container.getBoundingClientRect();
      const preferred = Array.from(container.querySelectorAll<HTMLElement>('[data-testid*="avatar" i], [class*="avatar" i], img'))
        .filter(visible)
        .filter((element) => !offerRelated(element, container));
      const geometric = Array.from(container.querySelectorAll<HTMLElement>("div, span, button"))
        .filter(visible)
        .filter((element) => !offerRelated(element, container))
        .filter((element) => {
          const rect = element.getBoundingClientRect();
          const square = Math.abs(rect.width - rect.height) <= 8;
          const sized = rect.width >= 20 && rect.width <= 64 && rect.height >= 20 && rect.height <= 64;
          const onLeft = rect.left <= crect.left + Math.min(80, crect.width * 0.35);
          const text = (element.textContent || "").trim();
          const initials = /^[A-Z]{1,3}$/i.test(text);
          const radius = parseFloat(getComputedStyle(element).borderRadius || "0");
          const circular = radius >= Math.min(rect.width, rect.height) * 0.35;
          return square && sized && onLeft && (initials || circular);
        });
      return [...preferred, ...geometric]
        .filter((element, index, all) => all.indexOf(element) === index)
        .map((element) => {
          const rect = element.getBoundingClientRect();
          const text = (element.textContent || "").trim();
          const squarePenalty = Math.abs(rect.width - rect.height) * 10;
          const leftPenalty = Math.abs(rect.left - crect.left);
          const topPenalty = Math.max(0, rect.top - crect.top) * 2;
          const initialsBonus = /^[A-Z]{1,3}$/i.test(text) ? -200 : 0;
          const avatarBonus = /avatar/i.test(`${element.className || ""} ${element.getAttribute("data-testid") || ""}`) ? -300 : 0;
          return { element, score: squarePenalty + leftPenalty + topPenalty + rect.width + initialsBonus + avatarBonus };
        })
        .sort((a, b) => a.score - b.score)[0]?.element || null;
    };
    const selectors = [
      '[data-testid="accounts-profile-button"]',
      '[data-testid*="accounts-profile" i]',
      '[data-testid="profile-button"]',
      'button[aria-label*="profile" i]',
      'button[aria-label*="account" i]',
      'button[aria-label*="user menu" i]',
      'button[aria-label*="\\u30d7\\u30ed\\u30d5\\u30a3\\u30fc\\u30eb" i]',
      'button[aria-label*="\\u30a2\\u30ab\\u30a6\\u30f3\\u30c8" i]',
      'button[aria-label*="\\u4e2a\\u4eba\\u8d44\\u6599" i]',
      'button[aria-label*="\\u8d26\\u6237" i]',
    ];
    for (const selector of selectors) {
      const container = Array.from(document.querySelectorAll<HTMLElement>(selector)).find((element) => visible(element) && !offerRelated(element));
      if (container) {
        const target = /accounts-profile/i.test(container.getAttribute("data-testid") || "") ? (avatarInside(container) || container) : container;
        markTarget(target, container);
        target.scrollIntoView({ block: "center", inline: "center" });
        const rect = target.getBoundingClientRect();
        const crect = container.getBoundingClientRect();
        return {
          x: rect.left + rect.width / 2,
          y: rect.top + rect.height / 2,
          containerX: crect.left + Math.min(28, Math.max(12, crect.width * 0.16)),
          containerY: crect.top + crect.height / 2,
          containerCenterX: crect.left + crect.width / 2,
          containerCenterY: crect.top + crect.height / 2,
        };
      }
    }
    const preferred = Array.from(document.querySelectorAll<HTMLElement>('button, [role="button"], a'))
      .filter(visible)
      .filter((element) => !offerRelated(element))
      .filter((element) => {
        const text = `${element.textContent || ""} ${element.getAttribute("aria-label") || ""} ${element.getAttribute("data-testid") || ""}`.toLowerCase();
        return (email && text.includes(email.toLowerCase())) || /profile|account|user menu|avatar|\u8d26\u6237|\u8d26\u53f7|\u4e2a\u4eba\u8d44\u6599|\u30d7\u30ed\u30d5\u30a3\u30fc\u30eb|\u30a2\u30ab\u30a6\u30f3\u30c8/.test(text);
      })
      .map((element) => ({ element, text: (element.textContent || "").trim(), rect: element.getBoundingClientRect() }))
      .sort((a, b) => a.text.length - b.text.length || b.rect.top - a.rect.top);
    const container = preferred[0]?.element;
    if (container) {
      const target = /accounts-profile/i.test(container.getAttribute("data-testid") || "") ? (avatarInside(container) || container) : container;
      markTarget(target, container);
      const rect = target.getBoundingClientRect();
      const crect = container.getBoundingClientRect();
      return {
        x: rect.left + rect.width / 2,
        y: rect.top + rect.height / 2,
        containerX: crect.left + Math.min(28, Math.max(12, crect.width * 0.16)),
        containerY: crect.top + crect.height / 2,
        containerCenterX: crect.left + crect.width / 2,
        containerCenterY: crect.top + crect.height / 2,
      };
    }
    const collapsed = Array.from(document.querySelectorAll<HTMLElement>("button, [role='button'], div, span"))
      .filter(visible)
      .filter((element) => !offerRelated(element))
      .map((element) => {
        const rect = element.getBoundingClientRect();
        const text = (element.textContent || "").trim();
        const radius = parseFloat(getComputedStyle(element).borderRadius || "0");
        const square = Math.abs(rect.width - rect.height) <= 8;
        const sized = rect.width >= 20 && rect.width <= 56 && rect.height >= 20 && rect.height <= 56;
        const lowerLeft = rect.left <= 90 && rect.top >= window.innerHeight * 0.72;
        const initials = /^[A-Z]{1,3}$/i.test(text);
        const circular = radius >= Math.min(rect.width, rect.height) * 0.35;
        const score = (lowerLeft ? 500 : 0) + (initials ? 200 : 0) + (circular ? 100 : 0) - rect.left;
        return { element, x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, containerX: rect.left + rect.width / 2, containerY: rect.top + rect.height / 2, containerCenterX: rect.left + rect.width / 2, containerCenterY: rect.top + rect.height / 2, score, ok: square && sized && lowerLeft && (initials || circular) };
      })
      .filter((item) => item.ok)
      .sort((a, b) => b.score - a.score)[0];
    if (collapsed) {
      markTarget(collapsed.element);
      return collapsed;
    }
    return null;
  }, email).catch(() => null);

  if (!box) return false;
  const activationScript = (selector: string) => {
    const element = document.querySelector(selector) as HTMLElement | null;
    if (!element) return false;
    element.scrollIntoView({ block: "center", inline: "center" });
    try { element.focus({ preventScroll: true }); } catch {}
    const eventInit = { bubbles: true, cancelable: true, view: window };
    for (const name of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
      try {
        const event = name.startsWith("pointer") ? new PointerEvent(name, eventInit) : new MouseEvent(name, eventInit);
        element.dispatchEvent(event);
      } catch {}
    }
    try { element.click(); } catch {}
    return true;
  };
  let clicked = false;
  for (const selector of ['[data-codex-profile-target="true"]', '[data-codex-profile-container="true"]']) {
    clicked = await page.evaluate(activationScript, selector).catch(() => false) || clicked;
    if (clicked && await waitUntil(() => chatgptSettingsMenuVisible(page), 500, 100)) return true;
  }
  for (const [xKey, yKey] of [["containerX", "containerY"], ["x", "y"], ["containerCenterX", "containerCenterY"]]) {
    if (box[xKey] == null || box[yKey] == null) continue;
    await page.mouse.click(Number(box[xKey]), Number(box[yKey])).catch(() => undefined);
    clicked = true;
    if (await waitUntil(() => chatgptSettingsMenuVisible(page), 500, 100)) return true;
  }
  return clicked;
}

async function chatgptSettingsMenuVisible(page: Page): Promise<boolean> {
  if (await settingsDialogVisible(page)) return true;
  const text = (await pageText(page, 700)).toLowerCase();
  return /settings|\u8bbe\u7f6e|\u8a2d\u5b9a/.test(text);
}

async function clickSectionAction(page: Page, sectionTerms: string[], actionTerms: string[], excludeTerms: string[] = []): Promise<boolean> {
  const box = await page.evaluate(({ sectionTerms, actionTerms, excludeTerms }) => {
    const visible = (element: Element | null) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const lower = (values: string[]) => values.map((value) => String(value || "").toLowerCase()).filter(Boolean);
    const sections = lower(sectionTerms);
    const actions = lower(actionTerms);
    const excludes = lower(excludeTerms);
    const roots = Array.from(document.querySelectorAll<HTMLElement>('section, li, [role="row"], div')).filter(visible);
    const candidates = roots.flatMap((root) => {
      const rootText = (root.textContent || "").replace(/\s+/g, " ").toLowerCase();
      if (!sections.some((term) => rootText.includes(term))) return [];
      if (excludes.some((term) => rootText.includes(term))) return [];
      const controls = Array.from(root.querySelectorAll<HTMLElement>('button, [role="button"], a')).filter(visible);
      return controls.flatMap((control) => {
        const text = `${control.textContent || ""} ${control.getAttribute("aria-label") || ""}`.replace(/\s+/g, " ").trim().toLowerCase();
        const actionHit = actions.length === 0 || actions.some((term) => text.includes(term));
        if (!actionHit) return [];
        const rect = control.getBoundingClientRect();
        const rootRect = root.getBoundingClientRect();
        const sectionSize = rootRect.width * rootRect.height;
        return [{ x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, area: rect.width * rect.height, sectionSize, textLength: text.length }];
      });
    }).sort((a, b) => a.sectionSize - b.sectionSize || b.area - a.area || a.textLength - b.textLength);
    return candidates[0] || null;
  }, { sectionTerms, actionTerms, excludeTerms }).catch(() => null);
  if (!box) return false;
  await page.mouse.click(box.x, box.y);
  return true;
}

async function mfaSwitchState(page: Page): Promise<boolean | null> {
  const info = await mfaSwitchInfo(page);
  return info ? info.enabled : null;
}

async function mfaSwitchInfo(page: Page): Promise<{ enabled: boolean; x: number; y: number } | null> {
  return page.evaluate(() => {
    const visible = (element: Element | null) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const terms = [
      "authenticator app",
      "authenticator",
      "\u8eab\u4efd\u9a8c\u8bc1\u5668",
      "\u8ba4\u8bc1\u5668",
      "\u8a8d\u8a3c\u30a2\u30d7\u30ea",
      "\u8a8d\u8a3c\u5668",
      "\u8a8d\u8a3c",
    ];
    const labels = Array.from(document.querySelectorAll<HTMLElement>("strong, span, p, h1, h2, h3, div"))
      .filter(visible)
      .map((element) => ({ element, text: (element.textContent || "").replace(/\s+/g, " ").trim() }))
      .filter((item) => terms.some((term) => item.text.toLowerCase().includes(term.toLowerCase())))
      .sort((a, b) => a.text.length - b.text.length);
    const isNativeSwitch = (element: Element | null) => Boolean(element && element.matches('[role="switch"], input[type="checkbox"]') && visible(element));
    const isSwitchLike = (element: Element | null) => {
      if (!element || !visible(element)) return false;
      if (isNativeSwitch(element)) return true;
      if (!element.matches('button, [role="button"], span, div')) return false;
      const text = (element.textContent || "").replace(/\s+/g, " ").trim();
      const aria = `${element.getAttribute("aria-label") || ""} ${element.getAttribute("title") || ""}`.replace(/\s+/g, " ").trim();
      const rect = element.getBoundingClientRect();
      if (element.matches("span, div") && (text + aria).length > 6) return false;
      if (!element.matches("span, div") && (text + aria).length > 24) return false;
      return rect.width >= 20 && rect.width <= 96 && rect.height >= 12 && rect.height <= 56;
    };
    let target: HTMLElement | null = null;
    let labelText = "";
    for (const label of labels) {
      let current: HTMLElement | null = label.element;
      for (let depth = 0; current && depth < 7; depth += 1, current = current.parentElement) {
        const switches = Array.from(current.querySelectorAll<HTMLElement>('[role="switch"], input[type="checkbox"], button, [role="button"], span, div')).filter(isSwitchLike);
        if (!switches.length) continue;
        const labelRect = label.element.getBoundingClientRect();
        target = switches
          .map((element) => {
            const rect = element.getBoundingClientRect();
            const dy = Math.abs((rect.top + rect.height / 2) - (labelRect.top + labelRect.height / 2));
            const rightPenalty = rect.left + rect.width / 2 >= labelRect.left ? 0 : 1000;
            const nativeBonus = isNativeSwitch(element) ? -100 : 0;
            return { element, score: nativeBonus + rightPenalty + dy * 20 + Math.abs((rect.left + rect.width / 2) - (labelRect.left + labelRect.width / 2)) };
          })
          .sort((a, b) => a.score - b.score)[0].element;
        labelText = label.text;
        break;
      }
      if (target) break;
    }
    if (!target && labels.length) {
      const label = labels[0];
      const labelRect = label.element.getBoundingClientRect();
      const switches = Array.from(document.querySelectorAll<HTMLElement>('[role="switch"], input[type="checkbox"], button, [role="button"], span, div')).filter(isSwitchLike);
      const nearest = switches
        .map((element) => {
          const rect = element.getBoundingClientRect();
          const dy = Math.abs((rect.top + rect.height / 2) - (labelRect.top + labelRect.height / 2));
          const rightPenalty = rect.left + rect.width / 2 >= labelRect.left ? 0 : 1000;
          const farRowPenalty = dy <= 80 ? 0 : 5000;
          const nativeBonus = isNativeSwitch(element) ? -100 : 0;
          return { element, distance: nativeBonus + rightPenalty + farRowPenalty + dy * 20 + Math.abs((rect.left + rect.width / 2) - (labelRect.left + labelRect.width / 2)) };
        })
        .sort((a, b) => a.distance - b.distance)[0];
      target = nearest?.element || null;
      labelText = label.text;
    }
    if (!target) return null;
    const rect = target.getBoundingClientRect();
    const state = String(target.getAttribute("data-state") || "").toLowerCase();
    const pressed = String(target.getAttribute("aria-pressed") || "").toLowerCase();
    const nativeSwitch = isNativeSwitch(target);
    const enabled = (target as HTMLInputElement).checked === true
      || target.getAttribute("aria-checked") === "true"
      || pressed === "true"
      || ["checked", "on", "active", "enabled"].includes(state);
    return {
      enabled,
      disabled: (target as HTMLButtonElement).disabled === true || target.getAttribute("aria-disabled") === "true",
      x: rect.left + rect.width / 2,
      y: rect.top + rect.height / 2,
      label: labelText,
      kind: nativeSwitch ? "native" : "button-like",
    };
  }).catch(() => null);
}

async function clickMfaSwitch(page: Page): Promise<boolean> {
  const info = await mfaSwitchInfo(page);
  if (!info || info.disabled) return false;
  if (info.enabled) return true;
  await page.mouse.click(info.x, info.y);
  return true;
}

async function hasOtpInput(page: Page): Promise<boolean> {
  return (await otpInputs(page)).length > 0;
}

async function mfaVerificationDialogVisible(page: Page): Promise<boolean> {
  return page.evaluate(() => {
    const visible = (element: Element | null) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const roots = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], form, main')).filter(visible);
    return roots.some((root) => {
      const text = (root.textContent || "").replace(/\s+/g, " ").toLowerCase();
      const hasInput = Array.from(root.querySelectorAll("input")).some(visible);
      return hasInput && /authenticator|verification app|one-time|one time|otp|totp|code|mfa|\u30ef\u30f3\u30bf\u30a4\u30e0|\u30b3\u30fc\u30c9|\u8a8d\u8a3c|\u78ba\u8a8d|\u9a8c\u8bc1\u7801/i.test(text);
    });
  }).catch(() => false);
}

async function mfaSetupSucceeded(page: Page): Promise<boolean> {
  if (/recovery code|recovery codes|\u6062\u590d\u4ee3\u7801|\u6062\u590d\u7801/i.test(await pageText(page, 3000))) return true;
  if (await mfaVerificationDialogVisible(page)) return false;
  return await mfaSwitchState(page) === true;
}

async function fillTotpCodeForMfa(page: Page, secret: string, options: SetupOptions, afterFill?: () => Promise<void>): Promise<boolean> {
  let generated = totpCode(secret);
  if (generated.remaining <= 15) {
    options.log(`Current Authenticator code has only ${generated.remaining}s left; waiting for next period`);
    await abortableSleep((generated.remaining + 1) * 1000, options.signal);
    generated = totpCode(secret);
  }
  options.log(`Submitting Authenticator app code; ${generated.remaining}s remaining`);

  const inputs = await visibleTotpCodeInputs(page);
  if (!inputs.length) return false;

  if (inputs.length >= 6) {
    for (let index = 0; index < 6; index += 1) if (!await forceTypeLocator(inputs[index], generated.code[index] || "")) return false;
  } else if (!await forceTypeLocator(inputs[0], generated.code)) {
    return false;
  }
  await afterFill?.();
  await waitUntil(() => totpSubmitButtonEnabled(page), 2500, 100);

  const clicked = await clickTotpSubmitButtonByDom(page)
    || await clickDialogButtonByText(page, ["Verify", "Continue", "Enable", "Turn on", "Done", "Save", "\u9a8c\u8bc1", "\u7ee7\u7eed", "\u542f\u7528", "\u5f00\u542f", "\u5b8c\u6210", "\u4fdd\u5b58", "\u691c\u8a3c", "\u78ba\u8a8d", "\u7d9a\u884c", "\u5b8c\u4e86"])
    || await clickPrimaryFormActionByDom(page);
  if (!clicked) {
    await captureSecurityDebug(page, "mfa-verify-button-not-found", options.log);
    options.log("MFA verification submit button was not found");
  }
  return clicked;
}

async function totpSubmitButtonEnabled(page: Page): Promise<boolean> {
  return Boolean(await totpSubmitTarget(page));
}

async function clickTotpSubmitButtonByDom(page: Page): Promise<boolean> {
  const target = await totpSubmitTarget(page);
  if (!target) return false;
  const submitted = await page.evaluate((selector) => {
    const element = document.querySelector(selector) as HTMLElement | null;
    if (!element) return false;
    element.scrollIntoView({ block: "center", inline: "center" });
    try { element.focus({ preventScroll: true }); } catch {}
    const eventInit = { bubbles: true, cancelable: true, view: window };
    for (const name of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
      try {
        const event = name.startsWith("pointer") ? new PointerEvent(name, eventInit) : new MouseEvent(name, eventInit);
        element.dispatchEvent(event);
      } catch {}
    }
    try { (element as HTMLButtonElement).click(); } catch {}
    return true;
  }, target.selector).catch(() => false);
  if (submitted) return true;
  if (target.x && target.y) {
    await page.mouse.click(target.x, target.y).catch(() => undefined);
    return true;
  }
  return false;
}

async function totpSubmitTarget(page: Page): Promise<{ x: number; y: number; selector: string } | null> {
  return page.evaluate(() => {
    const visible = (element: Element | null) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const enabled = (element: Element) => !(element as HTMLButtonElement).disabled && element.getAttribute("aria-disabled") !== "true";
    const codeInput = Array.from(document.querySelectorAll<HTMLInputElement>('input, textarea')).filter(visible).find((input) => {
      const meta = `${input.name || ""} ${input.id || ""} ${input.autocomplete || ""} ${input.placeholder || ""} ${input.getAttribute("aria-label") || ""} ${input.inputMode || ""}`.toLowerCase();
      return String(input.value || "").replace(/\D/g, "").length >= 4 || /otp|totp|code|one-time|numeric|6|\u30b3\u30fc\u30c9|\u691c\u8a3c|\u8a8d\u8a3c|\u9a8c\u8bc1/.test(meta);
    });
    if (!codeInput) return null;
    const root = codeInput.closest<HTMLElement>('[role="dialog"], [aria-modal="true"], form, main') || document.body;
    const rootRect = root.getBoundingClientRect();
    const actionPattern = /verify|continue|enable|turn on|done|save|submit|\u9a8c\u8bc1|\u7ee7\u7eed|\u542f\u7528|\u5f00\u542f|\u5b8c\u6210|\u4fdd\u5b58|\u691c\u8a3c|\u78ba\u8a8d|\u7d9a\u884c|\u5b8c\u4e86/i;
    const cancelPattern = /cancel|close|back|\u53d6\u6d88|\u5173\u95ed|\u8fd4\u56de|\u30ad\u30e3\u30f3\u30bb\u30eb|\u9589\u3058\u308b|\u623b\u308b/i;
    const buttons = Array.from(root.querySelectorAll<HTMLElement>('button, [role="button"], input[type="submit"], input[type="button"]'))
      .filter((element) => visible(element))
      .map((element) => {
        const rect = element.getBoundingClientRect();
        const text = `${element.textContent || ""} ${element.getAttribute("aria-label") || ""} ${(element as HTMLInputElement).value || ""}`.replace(/\s+/g, " ");
        const type = String((element as HTMLButtonElement | HTMLInputElement).type || "").toLowerCase();
        let score = 0;
        if (actionPattern.test(text)) score += 700;
        if (type === "submit") score += 300;
        if (cancelPattern.test(text)) score -= 2000;
        score += Math.max(0, rect.left - rootRect.left) / 10;
        score += Math.max(0, rect.top - rootRect.top) / 20;
        return { element, score, enabled: enabled(element), x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
      })
      .filter((item) => item.score > 0)
      .sort((a, b) => Number(b.enabled) - Number(a.enabled) || b.score - a.score)[0];
    if (buttons?.enabled) {
      buttons.element.setAttribute("data-codex-totp-submit", "true");
      return { x: buttons.x, y: buttons.y, selector: "[data-codex-totp-submit='true']" };
    }
    return null;
  }).catch(() => null);
}

async function visibleTotpCodeInputs(page: Page): Promise<Locator[]> {
  const selectors = [
    'input[autocomplete="one-time-code"]',
    'input[inputmode="numeric"]',
    'input[name*="code" i]',
    'input[name*="otp" i]',
    'input[name*="totp" i]',
    'input[aria-label*="code" i]',
    'input[placeholder*="code" i]',
    'input[placeholder*="6"]',
  ];
  const inputs = await visibleDialogInputs(page, selectors);
  if (inputs.length) return inputs;
  return visibleInputs(page, selectors);
}

async function visibleDialogInputs(page: Page, selectors: string[]): Promise<Locator[]> {
  const dialogs = page.locator('[role="dialog"], [aria-modal="true"], form, main');
  const count = await dialogs.count().catch(() => 0);
  for (let index = count - 1; index >= 0; index -= 1) {
    const dialog = dialogs.nth(index);
    if (!await dialog.isVisible().catch(() => false)) continue;
    const inputs = await visibleInputsInLocator(dialog, selectors);
    if (inputs.length) return inputs;
  }
  return [];
}

async function visibleInputs(page: Page, selectors: string[]): Promise<Locator[]> {
  return visibleInputsInLocator(page.locator("body"), selectors);
}

async function visibleInputsInLocator(root: Locator, selectors: string[]): Promise<Locator[]> {
  const matches = root.locator(selectors.join(", "));
  const output: Locator[] = [];
  const count = await matches.count().catch(() => 0);
  for (let index = 0; index < count; index += 1) {
    const input = matches.nth(index);
    if (await input.isVisible().catch(() => false) && await input.isEnabled().catch(() => false)) output.push(input);
  }
  return output;
}

function extractTotpSecretFromText(value: string): string {
  const text = String(value || "");
  const otpauthMatches = text.match(/otpauth:\/\/[^\s"'<>]+/gi) || [];
  for (const url of otpauthMatches) {
    const secret = /[?&]secret=([^&\s"'<>]+)/i.exec(url)?.[1] || "";
    const candidate = validSecretCandidate(decodeURIComponent(secret));
    if (candidate) return candidate;
  }

  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const labelPattern = /setup\s*key|secret\s*key|manual\s*(?:entry|key)|secret\s*code|\u5bc6\u94a5|\u79d8\u5bc6|\u30bb\u30c3\u30c8\u30a2\u30c3\u30d7\u30ad\u30fc|\u30b7\u30fc\u30af\u30ec\u30c3\u30c8|\u624b\u52d5\u5165\u529b/i;
  const candidatePattern = /\b[A-Z2-7](?:[A-Z2-7\s-]{14,})[A-Z2-7]\b/gi;
  for (let index = 0; index < lines.length; index += 1) {
    if (!labelPattern.test(lines[index])) continue;
    const chunk = lines.slice(index, index + 4).join("\n");
    for (const match of chunk.matchAll(candidatePattern)) {
      const candidate = validSecretCandidate(match[0]);
      if (candidate) return candidate;
    }
  }

  for (const match of text.matchAll(candidatePattern)) {
    const candidate = validSecretCandidate(match[0]);
    if (candidate) return candidate;
  }
  return "";
}

async function extractTotpSecretFromPage(page: Page, log: (message: string) => void = () => {}): Promise<string> {
  const bodyText = await page.locator("body").innerText({ timeout: 1500 }).catch(() => "");
  let candidate = extractTotpSecretFromText(bodyText);
  if (candidate) return candidate;

  candidate = await secretFromDialogInputs(page);
  if (candidate) return candidate;

  const revealActions = ["Can't scan", "cannot scan", "Problem loading", "trouble loading", "setup key", "secret key", "manual entry", "secret", "\u65e0\u6cd5\u626b\u63cf", "\u624b\u52a8\u8f93\u5165", "\u5bc6\u94a5", "\u30b7\u30fc\u30af\u30ec\u30c3\u30c8", "\u624b\u52d5\u5165\u529b"];
  for (const text of revealActions) {
    if (!await clickVisibleText(page, [text], 'button, [role="button"], a, [role="link"]')) continue;
    await waitUntil(async () => Boolean(await secretFromDialogInputs(page) || extractTotpSecretFromText(await page.locator("body").innerText({ timeout: 1500 }).catch(() => ""))), 5000, 250);
    candidate = extractTotpSecretFromText(await page.locator("body").innerText({ timeout: 1500 }).catch(() => ""));
    if (candidate) return candidate;
    candidate = await secretFromDialogInputs(page);
    if (candidate) return candidate;
  }

  try {
    const buffer = await page.screenshot({ fullPage: true });
    const png = PNG.sync.read(buffer);
    const code = jsQR(new Uint8ClampedArray(png.data), png.width, png.height);
    candidate = extractTotpSecretFromQrPayload(code?.data || "");
    if (candidate) return candidate;
  } catch (error) {
    log(`QR decode failed: ${(error as Error).message.slice(0, 160)}`);
  }
  return "";
}

async function secretFromDialogInputs(page: Page): Promise<string> {
  const fields = await page.evaluate(() => {
    const visible = (element: Element | null) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const dialogs = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"]')).filter(visible);
    const root = dialogs.at(-1) || document.body;
    return Array.from(root.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>("input, textarea")).filter(visible).map((input) => {
      const container = input.closest("label, div, section, form, [role='dialog']") || input.parentElement || root;
      return {
        value: input.value || "",
        text: (container.textContent || "").replace(/\s+/g, " ").trim(),
        placeholder: input.getAttribute("placeholder") || "",
        aria: input.getAttribute("aria-label") || "",
        name: input.getAttribute("name") || "",
        autocomplete: input.getAttribute("autocomplete") || "",
        inputmode: input.getAttribute("inputmode") || "",
        type: input.getAttribute("type") || "",
        readonly: input.readOnly === true,
      };
    });
  }).catch(() => [] as any[]);
  const labelPattern = /setup\s*key|secret\s*key|secret\s*code|manual\s*(?:entry|key)|\u5bc6\u94a5|\u79d8\u5bc6|\u30bb\u30c3\u30c8\u30a2\u30c3\u30d7\u30ad\u30fc|\u30b7\u30fc\u30af\u30ec\u30c3\u30c8|\u624b\u52d5\u5165\u529b/i;
  const codeInputPattern = /one-time-code|6\s*digit|\u9a8c\u8bc1\u7801|\u8a8d\u8a3c\u30b3\u30fc\u30c9|\u30ef\u30f3\u30bf\u30a4\u30e0|code/i;
  for (const field of fields) {
    const candidate = validSecretCandidate(field?.value || "");
    if (!candidate) continue;
    const metadata = [field.text, field.placeholder, field.aria, field.name, field.autocomplete, field.inputmode, field.type].join(" ");
    if (codeInputPattern.test(metadata) && !labelPattern.test(metadata)) continue;
    if (field.readonly || labelPattern.test(metadata)) return candidate;
  }
  return "";
}

function validSecretCandidate(value: string): string {
  const normalized = normalizeTotpSecret(value || "");
  if (normalized.length < 16) return "";
  if (!/^[A-Z2-7]+$/.test(normalized)) return "";
  if (isKnownNonSecretToken(normalized)) return "";
  return normalized;
}

function extractTotpSecretFromQrPayload(value: string): string {
  return extractTotpSecretFromText(value || "") || validSecretCandidate(value || "");
}

function isKnownNonSecretToken(value: string): boolean {
  return [
    "AUTHENTICATORAPP",
    "AUTHENTICATOR",
    "TEXTMESSAGE",
    "SECURITYANDLOGIN",
    "ACTIVESESSIONS",
    "PASSWORD",
    "CHATGPT",
    "OPENAI",
    "MULTIFACTORAUTHENTICATION",
  ].includes(value);
}

async function waitUntil(predicate: () => Promise<boolean> | boolean, timeoutMs: number, intervalMs = 250): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await Promise.resolve(predicate()).catch(() => false)) return true;
    await sleep(intervalMs);
  }
  return false;
}

function abortableSleep(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, milliseconds);
    signal.addEventListener("abort", () => { clearTimeout(timer); reject(new Error("Task cancelled")); }, { once: true });
  });
}

function throwIfAborted(signal: AbortSignal): void {
  if (signal.aborted) throw new Error("Task cancelled");
}

function assertPageOpen(page: Page, action: string): void {
  if (page.isClosed()) throw new Error(`${action} failed: browser page was closed`);
}

function trustedSecret(secret: string): boolean {
  return validSecretCandidate(secret || "").length >= 16;
}

async function captureSecurityDebug(page: Page, label: string, log: (message: string) => void): Promise<void> {
  try {
    const userData = process.env.REGISTRATION_DESK_USER_DATA?.trim() || path.join(process.env.APPDATA || process.cwd(), "registration-desk");
    const directory = path.join(userData, "security-debug");
    await fs.mkdir(directory, { recursive: true });
    const stem = path.join(directory, `${new Date().toISOString().replace(/[:.]/g, "-")}-${label}`);
    const body = page.isClosed() ? "[page closed]" : await page.locator("body").innerText({ timeout: 3000 }).catch(() => "");
    const inputDebug = page.isClosed() ? [] : await page.evaluate(() => {
      const visible = (element: Element | null) => {
        if (!element) return false;
        const rect = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
      };
      return Array.from(document.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>("input, textarea")).map((input, index) => {
        const rect = input.getBoundingClientRect();
        return {
          index,
          visible: visible(input),
          enabled: !input.disabled && !input.readOnly,
          type: input.type || "",
          name: input.name || "",
          id: input.id || "",
          placeholder: input.placeholder || "",
          autocomplete: input.autocomplete || "",
          aria: input.getAttribute("aria-label") || "",
          valueLength: String(input.value || "").length,
          rect: [Math.round(rect.left), Math.round(rect.top), Math.round(rect.width), Math.round(rect.height)],
        };
      });
    }).catch(() => []);
    await fs.writeFile(`${stem}.txt`, `${body}\n\n--- input debug ---\n${JSON.stringify(inputDebug, null, 2)}`, "utf8");
    if (!page.isClosed()) await page.screenshot({ path: `${stem}.png`, fullPage: true }).catch(() => undefined);
    log(`Security debug saved: ${stem}.txt`);
  } catch (error) {
    log(`Security debug save failed: ${(error as Error).message}`);
  }
}

export const securitySetupInternals = {
  settingsDialogVisible,
  clickVisibleText,
  clickSettingsMenuEntry,
  clickSecuritySettingsEntry,
  passkeySecuritySubpageVisible,
  passwordConfigured,
  clickSectionAction,
  clickProfileOrSettingsMenu,
  fillPasswordSetupFlow,
  mfaVerificationDialogVisible,
  mfaSwitchInfo,
  extractTotpSecretFromText,
  extractTotpSecretFromQrPayload,
  extractTotpSecretFromPage,
  fillTotpCodeForMfa,
  openSecuritySettings,
  securitySettingsPageReady,
};
