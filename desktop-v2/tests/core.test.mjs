import test from "node:test";
import assert from "node:assert/strict";
import { parseAccountLine } from "../dist/main/account-parser.js";
import { parseProxy, resolveTaskConcurrency, takeDynamicProxies } from "../dist/main/proxy.js";
import { createOpenAiAuthUrl, detectDeactivationMessage, detectLoginCodeMode, sessionPatch, totpCode } from "../dist/main/auth-helpers.js";
import { StateStore } from "../dist/main/store.js";
import { checkTrial } from "../dist/main/trial-service.js";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { calculateTrialMetrics } from "../dist/shared/metrics.js";
import { parseSessionImports } from "../dist/main/session-import.js";
import { mailInternals } from "../dist/main/mail-service.js";
import { initialRegistrationPatch } from "../dist/main/registration-service.js";

test("parses account credentials and optional security fields", () => {
  const account = parseAccountLine("user@example.com----mail-pass----client-id----refresh-token----gpt_password=GptPass123----2fa=ABCDEF");
  assert.equal(account.email, "user@example.com");
  assert.equal(account.gptPassword, "GptPass123");
  assert.equal(account.twofaSecret, "ABCDEF");
  assert.equal(account.status, "pending");
  assert.equal(account.sessionJson, "");
  assert.equal(account.sessionValid, null);
});

test("generates RFC 6238 compatible TOTP codes", () => {
  const result = totpCode("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", 59_000);
  assert.equal(result.code, "287082");
  assert.equal(result.remaining, 1);
});

test("distinguishes email OTP from authenticator OTP", () => {
  assert.equal(detectLoginCodeMode("https://auth.openai.com/email-verification", "Check your email", true, true), "email");
  assert.equal(detectLoginCodeMode("https://auth.openai.com/mfa", "Enter code from your Authenticator app", true, true), "totp");
  assert.equal(detectLoginCodeMode("https://auth.openai.com/challenge", "Enter verification code", true, true), "totp");
});

test("maps a captured session into persistent account fields", () => {
  const patch = sessionPatch({
    accessToken: "token",
    sessionJson: "{\"accessToken\":\"token\"}",
    storageStateJson: "{\"cookies\":[]}",
    expires: "2030-01-01T00:00:00Z",
    capturedAt: "2026-07-01T00:00:00Z",
  });
  assert.equal(patch.sessionValid, true);
  assert.equal(patch.accessToken, "token");
  assert.equal(patch.storageStateJson, "{\"cookies\":[]}");
});

test("security-enabled registration clears stale session fields before setup", () => {
  const patch = initialRegistrationPatch({ setupGptSecurity: true });
  assert.equal(patch.accessToken, "");
  assert.equal(patch.sessionJson, "");
  assert.equal(patch.storageStateJson, "");
  assert.equal(patch.sessionValid, null);
  assert.equal(patch.statusText, "Starting registration browser");
});

test("recognizes explicit account deactivation without treating generic 401 as a ban", () => {
  assert.match(detectDeactivationMessage({ error: { code: "account_deactivated", message: "deleted or deactivated" } }), /account_deactivated/);
  assert.equal(detectDeactivationMessage("HTTP 401 Unauthorized"), "");
});

test("builds the ChatGPT CSRF login authorization request", async () => {
  const calls = {};
  const context = {
    request: {
      get: async (url, options) => {
        calls.get = { url, options };
        return { ok: () => true, status: () => 200, json: async () => ({ csrfToken: "csrf-token" }) };
      },
      post: async (url, options) => {
        calls.post = { url, options };
        return { ok: () => true, status: () => 200, text: async () => JSON.stringify({ url: "https://auth.openai.com/authorize" }) };
      },
    },
    cookies: async () => [{ name: "oai-did", value: "device-id" }],
  };
  const url = await createOpenAiAuthUrl(context, "login", "user@example.com", "zh-CN");
  assert.equal(url, "https://auth.openai.com/authorize");
  assert.match(calls.post.url, /screen_hint=login/);
  assert.match(calls.post.url, /login_hint=user%40example\.com/);
  assert.equal(calls.post.options.form.csrfToken, "csrf-token");
});

test("persists imported accounts and complete session fields", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "registration-desk-"));
  const file = path.join(directory, "state.json");
  try {
    const store = new StateStore(file);
    await store.load();
    await store.importAccounts("user@example.com----mail-pass----client-id----refresh-token----gpt_password=GptPass123----2fa=JBSWY3DPEHPK3PXP");
    const account = store.snapshot().accounts[0];
    await store.updateAccount(account.id, {
      accessToken: "access",
      sessionJson: "{\"accessToken\":\"access\"}",
      storageStateJson: "{\"cookies\":[]}",
      sessionValid: true,
      sessionUpdatedAt: "2026-07-01T00:00:00Z",
    });
    const persisted = JSON.parse(await readFile(file, "utf8"));
    assert.equal(persisted.accounts[0].gptPassword, "GptPass123");
    assert.equal(persisted.accounts[0].twofaSecret, "JBSWY3DPEHPK3PXP");
    assert.equal(persisted.accounts[0].sessionValid, true);
    assert.equal(persisted.accounts[0].storageStateJson, "{\"cookies\":[]}");
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("serializes concurrent account writes without losing results", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "registration-desk-concurrent-"));
  const file = path.join(directory, "state.json");
  try {
    const store = new StateStore(file);
    await store.load();
    await store.importAccounts([
      "one@example.com----mail-pass----client-id----refresh-one",
      "two@example.com----mail-pass----client-id----refresh-two",
    ].join("\n"));
    const [one, two] = store.snapshot().accounts;
    await Promise.all([
      store.updateAccount(one.id, { accessToken: "token-one", sessionJson: "session-one" }),
      store.updateAccount(two.id, { accessToken: "token-two", sessionJson: "session-two" }),
    ]);
    const persisted = JSON.parse(await readFile(file, "utf8"));
    assert.equal(persisted.accounts.find((account) => account.email === "one@example.com").accessToken, "token-one");
    assert.equal(persisted.accounts.find((account) => account.email === "two@example.com").accessToken, "token-two");
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("migrates legacy credentials, session, trial state, and settings", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "registration-desk-legacy-"));
  const file = path.join(directory, "state.json");
  try {
    const store = new StateStore(file);
    await store.load();
    const result = await store.importLegacyState({
      accounts: [{
        email: "legacy@example.com", password: "mail", client_id: "client", refresh_token: "refresh",
        gpt_password: "GptPass123", twofa_secret: "JBSWY3DPEHPK3PXP", status: "Session已获取",
      }],
      session_results: {
        "legacy@example.com": {
          access_token: "legacy-access",
          session_json: JSON.stringify({ accessToken: "legacy-access", expires: "2030-01-01T00:00:00Z" }),
          storage_state_json: JSON.stringify({ cookies: [] }),
          trial_eligible: true,
        },
      },
      settings: { registration_concurrency: 4, local_proxy: "http://127.0.0.1:7890", dynamic_proxies: "proxy:1000:user:pass" },
    });
    assert.deepEqual(result, { imported: 1, updated: 0, errors: [] });
    const state = store.snapshot();
    assert.equal(state.accounts[0].accessToken, "legacy-access");
    assert.equal(state.accounts[0].sessionExpires, "2030-01-01T00:00:00Z");
    assert.equal(state.accounts[0].trialEligible, true);
    assert.equal(state.settings.concurrency, 4);
    assert.equal(state.settings.localProxy, "http://127.0.0.1:7890");
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("maps free-trial API results without confusing session invalidity with a ban", async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Response(JSON.stringify({
      results: [{ valid: true, trial_available: true, message: "eligible" }],
    }), { status: 200, headers: { "content-type": "application/json" } });
    const account = parseAccountLine("trial@example.com----mail----client----refresh");
    account.accessToken = "access";
    const result = (await checkTrial([account], "https://example.test/check")).get(account.id);
    assert.deepEqual(result, { eligible: true, valid: true, message: "eligible" });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("calculates free-trial eligibility and detection coverage percentages", () => {
  const accounts = ["one", "two", "three", "four"].map((name) => parseAccountLine(`${name}@example.com----mail----client----refresh`));
  accounts[0].trialEligible = true;
  accounts[1].trialEligible = false;
  accounts[2].trialEligible = true;
  assert.deepEqual(calculateTrialMetrics(accounts), {
    total: 4,
    checked: 3,
    eligible: 2,
    ineligible: 1,
    eligibilityPercentage: 67,
    coveragePercentage: 75,
  });
});

test("imports Session JSON and derives account identity from its access token", () => {
  const encode = (value) => Buffer.from(JSON.stringify(value)).toString("base64url");
  const token = `${encode({ alg: "none" })}.${encode({ email: "session@example.com", exp: 1893456000 })}.signature`;
  const [parsed] = parseSessionImports(JSON.stringify({ accessToken: token, user: { email: "session@example.com" }, expires: "2030-01-01T00:00:00.000Z" }));
  assert.equal(parsed.email, "session@example.com");
  assert.equal(parsed.accessToken, token);
  assert.equal(parsed.expires, "2030-01-01T00:00:00.000Z");
  assert.match(parsed.sessionJson, /session@example\.com/);
});

test("imports wrapped Session data and persists it as an account awaiting validation", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "registration-desk-session-"));
  const file = path.join(directory, "state.json");
  const token = `eyJhbGciOiJub25lIn0.${Buffer.from(JSON.stringify({ email: "wrapped@example.com" })).toString("base64url")}.${"x".repeat(70)}`;
  try {
    const store = new StateStore(file);
    await store.load();
    const result = await store.importSessions(JSON.stringify({
      session_json: JSON.stringify({ accessToken: token, user: { email: "wrapped@example.com" } }),
      storage_state_json: JSON.stringify({ cookies: [{ name: "session" }] }),
    }));
    assert.deepEqual(result, { imported: 1, updated: 0, errors: [] });
    const account = store.snapshot().accounts[0];
    assert.equal(account.email, "wrapped@example.com");
    assert.equal(account.accessToken, token);
    assert.equal(account.sessionValid, null);
    assert.equal(account.statusText, "Session 已导入，待检测");
    assert.match(account.storageStateJson, /cookies/);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("overwrites Session for a duplicate email while preserving account credentials", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "registration-desk-session-overwrite-"));
  const file = path.join(directory, "state.json");
  const token = `eyJhbGciOiJub25lIn0.${Buffer.from(JSON.stringify({ email: "same@example.com" })).toString("base64url")}.${"y".repeat(70)}`;
  try {
    const store = new StateStore(file);
    await store.load();
    await store.importAccounts("Same@Example.com----mail-pass----client-id----refresh-token----gpt_password=GptPass123----2fa=JBSWY3DPEHPK3PXP");
    const result = await store.importSessions(JSON.stringify({
      accessToken: token,
      user: { email: "same@example.com" },
      expires: "2031-01-01T00:00:00Z",
    }));
    assert.deepEqual(result, { imported: 0, updated: 1, errors: [] });
    const account = store.snapshot().accounts[0];
    assert.equal(account.emailPassword, "mail-pass");
    assert.equal(account.gptPassword, "GptPass123");
    assert.equal(account.twofaSecret, "JBSWY3DPEHPK3PXP");
    assert.equal(account.accessToken, token);
    assert.equal(account.sessionExpires, "2031-01-01T00:00:00Z");
    assert.equal(account.sessionValid, null);
    assert.equal(account.statusText, "Session 已覆盖，待检测");
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("normalizes Outlook REST HTML mail into searchable text", () => {
  const text = mailInternals.restMessageText({
    Subject: "ChatGPT verification",
    BodyPreview: "Your code",
    Body: { Content: "<style>x{}</style><p>Code: <strong>123456</strong>&nbsp;now</p>" },
  });
  assert.match(text, /ChatGPT verification/);
  assert.match(text, /123456 now/);
  assert.equal(mailInternals.isOpenAiMail("noreply@openai.com", text), true);
});

test("parses authenticated HTTP and SOCKS5 proxies", () => {
  assert.deepEqual(parseProxy("host.test:1000:user:pass"), {
    server: "http://host.test:1000",
    username: "user",
    password: "pass",
  });
  assert.deepEqual(parseProxy("socks5://user:pass@host.test:1080"), {
    server: "socks5://host.test:1080",
    username: "user",
    password: "pass",
  });
});

test("matches legacy proxy consumption and concurrency rules", () => {
  assert.throws(() => takeDynamicProxies("one\ntwo", 3), /动态代理只有 2 个/);
  assert.deepEqual(takeDynamicProxies("one\ntwo\nthree", 2), { taken: ["one", "two"], remaining: ["three"] });
  assert.equal(resolveTaskConcurrency("", [], 10), 1);
  assert.equal(resolveTaskConcurrency("http://127.0.0.1:7890", [], 10), 10);
  assert.equal(resolveTaskConcurrency("", ["one", "two"], 10), 2);
  assert.equal(resolveTaskConcurrency("", Array.from({ length: 30 }, (_, index) => String(index)), 30), 20);
});
