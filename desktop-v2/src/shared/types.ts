export type AccountStatus =
  | "pending"
  | "running"
  | "waiting-verification"
  | "completed"
  | "failed"
  | "stopped";

export type AccountType = "unknown" | "free" | "plus" | "team";

export interface Account {
  id: string;
  email: string;
  emailPassword: string;
  clientId: string;
  refreshToken: string;
  gptPassword: string;
  twofaSecret: string;
  accessToken: string;
  sessionJson: string;
  storageStateJson: string;
  sessionExpires: string;
  sessionUpdatedAt: string;
  sessionValid: boolean | null;
  trialEligible: boolean | null;
  trialMessage: string;
  health: "unknown" | "active" | "deactivated";
  healthDetail: string;
  accountType: AccountType;
  accountTypeDetail: string;
  status: AccountStatus;
  statusText: string;
  lastError: string;
  createdAt: string;
  updatedAt: string;
}

export interface Settings {
  concurrency: number;
  headless: boolean;
  localProxy: string;
  proxyPool: string;
  registrationUrl: string;
  trialApiUrl: string;
  setupGptSecurity: boolean;
}

export interface AppState {
  version: 2;
  accounts: Account[];
  settings: Settings;
}

export interface ImportResult {
  imported: number;
  updated: number;
  errors: string[];
}

export interface RegistrationEvent {
  type: "log" | "account" | "queue";
  accountId?: string;
  message?: string;
  account?: Account;
  running?: boolean;
}

export interface SessionSnapshot {
  accessToken: string;
  sessionJson: string;
  storageStateJson: string;
  expires: string;
  capturedAt: string;
}

export interface RecentEmail {
  id: string;
  subject: string;
  from: string;
  date: string;
  text: string;
}
