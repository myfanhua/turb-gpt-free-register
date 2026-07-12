import path from "node:path";
import fs from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { app, BrowserWindow, clipboard, ipcMain, Menu } from "electron";
import type { RegistrationEvent, Settings } from "../shared/types.js";
import { detectDeactivationMail, fetchLatestEmailCode, listRecentEmails } from "./mail-service.js";
import { sessionPatch, totpCode } from "./auth-helpers.js";
import { probeAccountHealthByLogin } from "./health-login-probe.js";
import { LoginService } from "./login-service.js";
import { splitProxyPool, takeDynamicProxies } from "./proxy.js";
import { RegistrationService } from "./registration-service.js";
import { checkAccountSession } from "./session-service.js";
import { StateStore } from "./store.js";
import { checkTrial } from "./trial-service.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
let window: BrowserWindow | null = null;
let store: StateStore;
let logWriteQueue: Promise<void> = Promise.resolve();
const registrations = new RegistrationService();
const logins = new LoginService();


if (process.env.REGISTRATION_DESK_USER_DATA) {
  app.setPath("userData", path.resolve(process.env.REGISTRATION_DESK_USER_DATA));
}

function send(event: RegistrationEvent): void {
  if (event.type === "log" && event.message) {
    const line = `[${new Date().toISOString()}] ${event.message}\n`;
    logWriteQueue = logWriteQueue.then(async () => {
      const directory = path.join(app.getPath("userData"), "logs");
      await fs.mkdir(directory, { recursive: true });
      await fs.appendFile(path.join(directory, "registration.log"), line, "utf8");
    }).catch(() => undefined);
  }
  window?.webContents.send("registration:event", event);
}

async function createWindow(): Promise<void> {
  window = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 1100,
    minHeight: 640,
    backgroundColor: "#f4f6f8",
    autoHideMenuBar: true,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "../preload/preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  window.once("ready-to-show", () => window?.show());
  window.setMenuBarVisibility(false);
  await window.loadFile(path.join(__dirname, "../renderer/index.html"));
  if (!window.isVisible()) window.show();
}

app.whenReady().then(async () => {
  Menu.setApplicationMenu(null);
  store = new StateStore(path.join(app.getPath("userData"), "registration-state.json"));
  await store.load();
  registrations.on("event", send);
  logins.on("event", send);
  registerIpc();
  await createWindow();
});

app.on("window-all-closed", () => {
  registrations.stop();
  logins.stop();
  if (process.platform !== "darwin") app.quit();
});

function registerIpc(): void {
  ipcMain.handle("state:get", () => store.snapshot());
  ipcMain.handle("clipboard:write", (_event, value: string) => clipboard.writeText(String(value || "")));
  ipcMain.handle("accounts:import", (_event, text: string) => store.importAccounts(text));
  ipcMain.handle("sessions:import", (_event, text: string, emailHint: string) => store.importSessions(text, emailHint));
  ipcMain.handle("legacy:import", async () => {
    const legacyFile = path.resolve(app.getAppPath(), "..", "state.json");
    const payload = JSON.parse(await fs.readFile(legacyFile, "utf8"));
    return store.importLegacyState(payload);
  });
  ipcMain.handle("accounts:remove", (_event, ids: string[]) => store.removeAccounts(ids));
  ipcMain.handle("settings:save", (_event, settings: Partial<Settings>) => store.saveSettings(settings));
  ipcMain.handle("registrations:start", async (_event, ids: string[]) => {
    if (logins.running) throw new Error("登录任务正在运行");
    const state = store.snapshot();
    const selected = state.accounts.filter((account) => ids.includes(account.id));
    if (!selected.length) throw new Error("没有选中账号");
    const taskSettings = await consumeRegistrationProxies(state.settings, selected.length);
    void registrations.start(selected, taskSettings, (id, patch) => store.updateAccount(id, patch))
      .catch((error) => send({ type: "log", message: `注册队列失败: ${(error as Error).message}` }));
    return { started: selected.length };
  });
  ipcMain.handle("sessions:login", async (_event, ids: string[]) => {
    if (registrations.running) throw new Error("注册任务正在运行");
    const state = store.snapshot();
    const selected = state.accounts.filter((account) => ids.includes(account.id));
    if (!selected.length) throw new Error("没有选中账号");
    const taskSettings = await consumeRegistrationProxies(state.settings, selected.length);
    void logins.start(selected, taskSettings, (id, patch) => store.updateAccount(id, patch))
      .catch((error) => send({ type: "log", message: `登录队列失败: ${(error as Error).message}` }));
    return { started: selected.length };
  });
  ipcMain.handle("registrations:stop", () => { registrations.stop(); logins.stop(); });
  ipcMain.handle("sessions:check", async (_event, ids: string[]) => {
    const state = store.snapshot();
    const selected = state.accounts.filter((account) => ids.includes(account.id));
    if (!selected.length) throw new Error("请先选择要检测的账号");
    const dynamicProxy = splitProxyPool(state.settings.proxyPool)[0] || "";
    for (const account of selected) {
      const result = await checkAccountSession(account, state.settings.localProxy, dynamicProxy);
      await store.updateAccount(account.id, {
        sessionValid: result.valid,
        health: result.deactivated ? "deactivated" : result.valid === true ? "active" : account.health,
        healthDetail: result.deactivated ? result.detail : account.healthDetail,
        statusText: result.valid === true ? "Session 有效" : result.valid === false ? "Session 已失效" : "Session 状态未知",
        lastError: result.valid === true ? "" : result.detail,
      });
    }
    return store.snapshot();
  });
  ipcMain.handle("accounts:trial", async (_event, ids: string[]) => {
    const state = store.snapshot();
    const selected = state.accounts.filter((account) => ids.includes(account.id));
    if (!selected.length) throw new Error("请先选择要检测的账号");
    const results = await checkTrial(selected, state.settings.trialApiUrl);
    for (const account of selected) {
      const result = results.get(account.id);
      if (!result) continue;
      await store.updateAccount(account.id, {
        trialEligible: result.eligible,
        trialMessage: result.message,
        sessionValid: result.valid === false ? false : account.sessionValid,
      });
    }
    return store.snapshot();
  });
  ipcMain.handle("accounts:health", async (_event, ids: string[]) => {
    const state = store.snapshot();
    const selected = state.accounts.filter((account) => ids.includes(account.id));
    if (!selected.length) throw new Error("请先选择要检测的账号");
    const dynamicProxy = splitProxyPool(state.settings.proxyPool)[0] || "";
    send({ type: "log", message: `检测封号开始：${selected.length} 个账号；将依次执行 Session 接口、邮件扫描、登录兜底检测` });
    for (const account of selected) {
      let sessionResult: Awaited<ReturnType<typeof checkAccountSession>> | null = null;
      let sessionError = "";
      let mail = "";
      let mailError = "";
      let loginProbe: Awaited<ReturnType<typeof probeAccountHealthByLogin>> | null = null;
      try {
        send({ type: "log", accountId: account.id, message: `[${account.email}] 封号检测：检查 Session 接口` });
        sessionResult = account.accessToken ? await checkAccountSession(account, state.settings.localProxy, dynamicProxy) : null;
      } catch (error) {
        sessionError = (error as Error).message;
      }
      try {
        send({ type: "log", accountId: account.id, message: `[${account.email}] 封号检测：扫描 OpenAI 邮件通知` });
        mail = await detectDeactivationMail(account);
      } catch (error) {
        mailError = (error as Error).message;
      }
      if (!mail && !sessionResult?.deactivated) {
        send({ type: "log", accountId: account.id, message: `[${account.email}] 封号检测：未发现邮件/Session 封号证据，开始登录兜底检测` });
        loginProbe = await probeAccountHealthByLogin(
          account,
          state.settings,
          state.settings.localProxy,
          dynamicProxy,
          (message) => send({ type: "log", accountId: account.id, message: `[${account.email}] 封号检测：${message}` }),
        );
      }
      const deactivated = Boolean(mail || sessionResult?.deactivated || loginProbe?.deactivated);
      const active = sessionResult?.valid === true || loginProbe?.active === true;
      const detail = mail
        || (sessionResult?.deactivated ? sessionResult.detail : "")
        || loginProbe?.detail
        || sessionResult?.detail
        || [sessionError, mailError].filter(Boolean).join(" | ")
        || "未发现停用证据";
      const sessionPatchFromProbe = loginProbe?.snapshot ? sessionPatch(loginProbe.snapshot) : undefined;
      await store.updateAccount(account.id, {
        ...(sessionPatchFromProbe || {}),
        health: deactivated ? "deactivated" : active ? "active" : "unknown",
        healthDetail: detail,
        sessionValid: deactivated ? false : sessionPatchFromProbe?.sessionValid ?? sessionResult?.valid ?? account.sessionValid,
        status: deactivated ? "failed" : account.status,
        statusText: deactivated ? "账号已封禁/停用" : active ? "封号检测正常" : "封号检测未知",
        lastError: deactivated ? detail : active ? "" : detail,
      });
      send({ type: "log", accountId: account.id, message: `[${account.email}] 封号检测完成：${deactivated ? "已封禁/停用" : active ? "正常" : "未知"}；${detail}` });
    }
    return store.snapshot();
  });
  ipcMain.handle("accounts:mail", async (_event, id: string) => {
    const account = store.snapshot().accounts.find((item) => item.id === id);
    if (!account) throw new Error("账号不存在");
    return listRecentEmails(account, 20);
  });
  ipcMain.handle("accounts:totp", (_event, id: string) => {
    const account = store.snapshot().accounts.find((item) => item.id === id);
    if (!account) throw new Error("账号不存在");
    if (!account.twofaSecret) throw new Error("该账号没有 Authenticator 2FA 密钥");
    return totpCode(account.twofaSecret);
  });
  ipcMain.handle("accounts:email-code", async (_event, id: string) => {
    const account = store.snapshot().accounts.find((item) => item.id === id);
    if (!account) throw new Error("账号不存在");
    return fetchLatestEmailCode(account);
  });
}

async function consumeRegistrationProxies(settings: Settings, accountCount: number): Promise<Settings> {
  const { taken, remaining } = takeDynamicProxies(settings.proxyPool, accountCount);
  if (!taken.length) return { ...settings };
  await store.saveSettings({ proxyPool: remaining.join("\n") });
  return { ...settings, proxyPool: taken.join("\n") };
}
