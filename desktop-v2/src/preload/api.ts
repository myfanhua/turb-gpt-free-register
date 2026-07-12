import type { AppState, ImportResult, RecentEmail, RegistrationEvent, Settings } from "../shared/types.js";

export interface RegistrationDeskApi {
  getState(): Promise<AppState>;
  copyText(value: string): Promise<void>;
  importAccounts(text: string): Promise<ImportResult>;
  importSessions(text: string, emailHint?: string): Promise<ImportResult>;
  importLegacy(): Promise<ImportResult>;
  removeAccounts(ids: string[]): Promise<void>;
  saveSettings(settings: Partial<Settings>): Promise<Settings>;
  start(ids: string[]): Promise<{ started: number }>;
  login(ids: string[]): Promise<{ started: number }>;
  stop(): Promise<void>;
  checkSessions(ids: string[]): Promise<AppState>;
  checkTrial(ids: string[]): Promise<AppState>;
  checkHealth(ids: string[]): Promise<AppState>;
  recentEmails(id: string): Promise<RecentEmail[]>;
  getTotp(id: string): Promise<{ code: string; remaining: number }>;
  getEmailCode(id: string): Promise<string>;
  onEvent(listener: (event: RegistrationEvent) => void): () => void;
}
