import fs from "node:fs/promises";
import path from "node:path";
import { randomUUID } from "node:crypto";
import type { Account, AppState, ImportResult, Settings } from "../shared/types.js";
import { parseAccountLine } from "./account-parser.js";
import { parseSessionImports } from "./session-import.js";

const defaults: Settings = {
  concurrency: 1,
  headless: false,
  localProxy: "",
  proxyPool: "",
  registrationUrl: "https://auth.openai.com/create-account",
  trialApiUrl: "https://upi.iceaix.com/api/upi/check",
  setupGptSecurity: false,
};

export class StateStore {
  private state: AppState = { version: 2, accounts: [], settings: defaults };
  private writeQueue: Promise<void> = Promise.resolve();
  constructor(private readonly filePath: string) {}

  async load(): Promise<void> {
    try {
      const parsed = JSON.parse(await fs.readFile(this.filePath, "utf8")) as Partial<AppState>;
      this.state = {
        version: 2,
        accounts: Array.isArray(parsed.accounts) ? parsed.accounts.map((account) => ({
          ...account,
          accessToken: account.accessToken ?? "",
          sessionJson: account.sessionJson ?? "",
          storageStateJson: account.storageStateJson ?? "",
          sessionExpires: account.sessionExpires ?? "",
          sessionUpdatedAt: account.sessionUpdatedAt ?? "",
          sessionValid: account.sessionValid ?? null,
          trialEligible: account.trialEligible ?? null,
          trialMessage: account.trialMessage ?? "",
          health: account.health ?? "unknown",
          healthDetail: account.healthDetail ?? "",
          accountType: account.accountType ?? "unknown",
          accountTypeDetail: account.accountTypeDetail ?? "",
        })) as Account[] : [],
        settings: { ...defaults, ...(parsed.settings ?? {}) },
      };
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      await this.save();
    }
  }

  snapshot(): AppState {
    return structuredClone(this.state);
  }

  async importAccounts(text: string): Promise<ImportResult> {
    const result: ImportResult = { imported: 0, updated: 0, errors: [] };
    const byEmail = new Map(this.state.accounts.map((account) => [account.email.toLowerCase(), account]));
    text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).forEach((line, index) => {
      try {
        const account = parseAccountLine(line);
        const existing = byEmail.get(account.email.toLowerCase());
        if (existing) {
          Object.assign(existing, {
            emailPassword: account.emailPassword,
            clientId: account.clientId,
            refreshToken: account.refreshToken,
            gptPassword: account.gptPassword || existing.gptPassword,
            twofaSecret: account.twofaSecret || existing.twofaSecret,
            updatedAt: new Date().toISOString(),
          });
          result.updated += 1;
        } else {
          this.state.accounts.push(account);
          byEmail.set(account.email.toLowerCase(), account);
          result.imported += 1;
        }
      } catch (error) {
        result.errors.push(`第 ${index + 1} 行: ${(error as Error).message}`);
      }
    });
    await this.save();
    return result;
  }

  async importSessions(text: string, emailHint = ""): Promise<ImportResult> {
    const parsed = parseSessionImports(text, emailHint);
    const result: ImportResult = { imported: 0, updated: 0, errors: [] };
    const byEmail = new Map(this.state.accounts.map((account) => [account.email.toLowerCase(), account]));
    for (const item of parsed) {
      const now = new Date().toISOString();
      const existing = byEmail.get(item.email.toLowerCase());
      if (existing) {
        Object.assign(existing, {
          accessToken: item.accessToken,
          sessionJson: item.sessionJson,
          storageStateJson: item.storageStateJson || existing.storageStateJson,
          sessionExpires: item.expires,
          sessionUpdatedAt: now,
          sessionValid: null,
          status: "completed",
          statusText: "Session 已覆盖，待检测",
          lastError: "",
          updatedAt: now,
        });
        result.updated += 1;
        continue;
      }
      const account: Account = {
        id: randomUUID(),
        email: item.email,
        emailPassword: "",
        clientId: "",
        refreshToken: "",
        gptPassword: "",
        twofaSecret: "",
        accessToken: item.accessToken,
        sessionJson: item.sessionJson,
        storageStateJson: item.storageStateJson,
        sessionExpires: item.expires,
        sessionUpdatedAt: now,
        sessionValid: null,
        trialEligible: null,
        trialMessage: "",
        health: "unknown",
        healthDetail: "",
        accountType: "unknown",
        accountTypeDetail: "",
        status: "completed",
        statusText: "Session 已导入，待检测",
        lastError: "",
        createdAt: now,
        updatedAt: now,
      };
      this.state.accounts.push(account);
      byEmail.set(account.email.toLowerCase(), account);
      result.imported += 1;
    }
    await this.save();
    return result;
  }

  async importLegacyState(value: unknown): Promise<ImportResult> {
    const root = asRecord(value);
    const legacyAccounts = Array.isArray(root.accounts) ? root.accounts.map(asRecord) : [];
    const sessions = asRecord(root.session_results);
    const byEmail = new Map(this.state.accounts.map((account) => [account.email.toLowerCase(), account]));
    const result: ImportResult = { imported: 0, updated: 0, errors: [] };
    for (const [index, source] of legacyAccounts.entries()) {
      const email = String(source.email || "").trim();
      if (!email.includes("@")) { result.errors.push(`旧版第 ${index + 1} 个账号缺少有效邮箱`); continue; }
      const session = asRecord(sessions[email]);
      const now = new Date().toISOString();
      const accessToken = String(session.access_token || "");
      const legacyStatus = String(source.status || "");
      const deactivated = /封禁|停用|deactivat|deleted/i.test(`${legacyStatus} ${session.session_json || ""}`);
      const imported: Account = {
        id: randomUUID(),
        email,
        emailPassword: String(source.password || ""),
        clientId: String(source.client_id || ""),
        refreshToken: String(source.refresh_token || ""),
        gptPassword: String(source.gpt_password || ""),
        twofaSecret: String(source.twofa_secret || ""),
        accessToken,
        sessionJson: String(session.session_json || ""),
        storageStateJson: String(session.storage_state_json || ""),
        sessionExpires: extractSessionExpires(session.session_json),
        sessionUpdatedAt: String(session.session_checked_at || session.updated_at || ""),
        sessionValid: typeof session.session_valid === "boolean" ? session.session_valid : null,
        trialEligible: booleanOrNull(session.trial_eligible ?? session.trial_available),
        trialMessage: String(session.trial_message || session.trial_reason || ""),
        health: deactivated ? "deactivated" : "unknown",
        healthDetail: deactivated ? legacyStatus || "旧版记录显示账号已停用" : "",
        accountType: "unknown",
        accountTypeDetail: "",
        status: accessToken ? "completed" : "pending",
        statusText: accessToken ? "旧版 Session 已导入" : legacyStatus || "待处理",
        lastError: "",
        createdAt: now,
        updatedAt: now,
      };
      const existing = byEmail.get(email.toLowerCase());
      if (existing) {
        Object.assign(existing, {
          emailPassword: imported.emailPassword || existing.emailPassword,
          clientId: imported.clientId || existing.clientId,
          refreshToken: imported.refreshToken || existing.refreshToken,
          gptPassword: imported.gptPassword || existing.gptPassword,
          twofaSecret: imported.twofaSecret || existing.twofaSecret,
          accessToken: imported.accessToken || existing.accessToken,
          sessionJson: imported.sessionJson || existing.sessionJson,
          storageStateJson: imported.storageStateJson || existing.storageStateJson,
          sessionExpires: imported.sessionExpires || existing.sessionExpires,
          sessionUpdatedAt: imported.sessionUpdatedAt || existing.sessionUpdatedAt,
          sessionValid: imported.sessionValid ?? existing.sessionValid,
          trialEligible: imported.trialEligible ?? existing.trialEligible,
          trialMessage: imported.trialMessage || existing.trialMessage,
          health: imported.health === "deactivated" ? "deactivated" : existing.health,
          healthDetail: imported.healthDetail || existing.healthDetail,
          status: imported.accessToken ? "completed" : existing.status,
          statusText: imported.accessToken ? imported.statusText : existing.statusText,
          updatedAt: now,
        });
        result.updated += 1;
      } else {
        this.state.accounts.push(imported);
        byEmail.set(email.toLowerCase(), imported);
        result.imported += 1;
      }
    }
    const legacySettings = asRecord(root.settings);
    this.state.settings = {
      ...this.state.settings,
      concurrency: clampNumber(legacySettings.registration_concurrency, 1, 20, this.state.settings.concurrency),
      headless: typeof legacySettings.headless === "boolean" ? legacySettings.headless : this.state.settings.headless,
      localProxy: String(legacySettings.local_proxy || this.state.settings.localProxy),
      proxyPool: String(legacySettings.dynamic_proxies || this.state.settings.proxyPool),
      setupGptSecurity: typeof legacySettings.setup_gpt_security === "boolean" ? legacySettings.setup_gpt_security : this.state.settings.setupGptSecurity,
    };
    await this.save();
    return result;
  }

  async updateAccount(id: string, patch: Partial<Account>): Promise<Account> {
    const account = this.state.accounts.find((item) => item.id === id);
    if (!account) throw new Error("账号不存在");
    Object.assign(account, patch, { updatedAt: new Date().toISOString() });
    await this.save();
    return structuredClone(account);
  }

  async removeAccounts(ids: string[]): Promise<void> {
    const selected = new Set(ids);
    this.state.accounts = this.state.accounts.filter((account) => !selected.has(account.id));
    await this.save();
  }

  async saveSettings(settings: Partial<Settings>): Promise<Settings> {
    this.state.settings = { ...this.state.settings, ...settings };
    await this.save();
    return structuredClone(this.state.settings);
  }

  private save(): Promise<void> {
    const content = `${JSON.stringify(this.state, null, 2)}\n`;
    const operation = this.writeQueue.then(async () => {
      await fs.mkdir(path.dirname(this.filePath), { recursive: true });
      const temporary = `${this.filePath}.tmp`;
      await fs.writeFile(temporary, content, "utf8");
      await fs.rename(temporary, this.filePath);
    });
    this.writeQueue = operation.catch(() => undefined);
    return operation;
  }
}

function asRecord(value: unknown): Record<string, any> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, any> : {};
}

function booleanOrNull(value: unknown): boolean | null { return typeof value === "boolean" ? value : null; }

function extractSessionExpires(value: unknown): string {
  try { return String(JSON.parse(String(value || "{}"))?.expires || ""); }
  catch { return ""; }
}

function clampNumber(value: unknown, minimum: number, maximum: number, fallback: number): number {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(minimum, Math.min(maximum, Math.trunc(number))) : fallback;
}
