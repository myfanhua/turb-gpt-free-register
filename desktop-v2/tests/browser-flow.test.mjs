import test from "node:test";
import assert from "node:assert/strict";
import { chromium } from "playwright-core";
import { clickLoginConfirmationIfVisible, detectLoginCodeMode, fillCodeAndSubmit, fillPasswordAndSubmit, otpInputs, readSession, selectAuthenticatorIfVisible, startEmailOtpLogin } from "../dist/main/auth-helpers.js";
import { fillAboutYouAndSubmit } from "../dist/main/profile-flow.js";
import { securitySetupInternals } from "../dist/main/gpt-security-service.js";

test("browser helpers submit password and segmented OTP forms", async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.setContent(`
      <form id="password-form"><input type="password" name="password"><button type="submit">Continue</button></form>
      <script>document.querySelector('#password-form').onsubmit = event => { event.preventDefault(); window.passwordSubmitted = true; };</script>
    `);
    assert.equal(await fillPasswordAndSubmit(page, "StrongPass123!"), true);
    assert.equal(await page.locator('input[type=password]').inputValue(), "StrongPass123!");
    assert.equal(await page.evaluate(() => window.passwordSubmitted), true);

    await page.setContent(`
      <form id="otp-form">
        ${Array.from({ length: 6 }, () => '<input inputmode="numeric" maxlength="1">').join("")}
        <button type="submit">Verify</button>
      </form>
      <script>document.querySelector('#otp-form').onsubmit = event => { event.preventDefault(); window.otpSubmitted = true; };</script>
    `);
    assert.equal(await fillCodeAndSubmit(page, "287082"), true);
    assert.deepEqual(await page.locator('input').evaluateAll((items) => items.map((item) => item.value)), [..."287082"]);
    assert.equal(await page.evaluate(() => window.otpSubmitted), true);
  } finally {
    await browser.close();
  }
});

test("browser helpers handle Japanese password and MFA challenge pages", async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <form id="password-form">
        <h1>パスワードの入力</h1>
        <input type="password" name="password">
        <button type="submit">続行</button>
      </form>
      <script>document.querySelector('#password-form').onsubmit = event => { event.preventDefault(); window.passwordSubmitted = true; };</script>
    `);
    assert.equal(await fillPasswordAndSubmit(page, "StrongPass123!"), true);
    assert.equal(await page.evaluate(() => window.passwordSubmitted), true);

    await page.setContent(`
      <form id="mfa-form">
        <h1>ID を検証する</h1>
        <p>ワンタイム パスワード アプリでコードを確認してください</p>
        <label>ワンタイム コード <input type="text"></label>
        <button type="submit">続行</button>
      </form>
      <script>document.querySelector('#mfa-form').onsubmit = event => { event.preventDefault(); window.mfaSubmitted = true; };</script>
    `);
    assert.equal(detectLoginCodeMode("https://auth.openai.com/mfa-challenge/test", "ID を検証する ワンタイム パスワード アプリ ワンタイム コード", true, true), "totp");
    assert.equal((await otpInputs(page)).length, 1);
    assert.equal(await fillCodeAndSubmit(page, "123456"), true);
    assert.equal(await page.locator('input').inputValue(), "123456");
    assert.equal(await page.evaluate(() => window.mfaSubmitted), true);

    await page.setContent(`
      <form id="mfa-form">
        <h1>ID を検証する</h1>
        <p>ワンタイム パスワード アプリでコードを確認してください</p>
        <label>ワンタイム コード <input type="text"></label>
        <button type="submit">検証</button>
      </form>
      <script>document.querySelector('#mfa-form').onsubmit = event => { event.preventDefault(); window.verifySubmitted = true; };</script>
    `);
    assert.equal(await fillCodeAndSubmit(page, "654321"), true);
    assert.equal(await page.evaluate(() => window.verifySubmitted), true);
  } finally {
    await browser.close();
  }
});

test("browser helpers click login confirmation and authenticator method screens", async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <main>
        <h1>Confirm login</h1>
        <p>New sign-in from IP address 127.0.0.1</p>
        <button onclick="window.confirmed = true">Confirm</button>
      </main>
    `);
    assert.equal(await clickLoginConfirmationIfVisible(page), true);
    assert.equal(await page.evaluate(() => window.confirmed), true);

    await page.setContent(`
      <main>
        <h1>Choose a two-factor method</h1>
        <button onclick="window.authenticator = true">Authenticator app</button>
      </main>
    `);
    assert.equal(await selectAuthenticatorIfVisible(page), true);
    assert.equal(await page.evaluate(() => window.authenticator), true);
  } finally {
    await browser.close();
  }
});

test("browser helper switches a rejected password login to an email one-time code", async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <main>
        <p>Incorrect password</p>
        <input type="password" name="password">
        <button onclick="window.emailOtpSelected = true">Continue with email code</button>
      </main>
    `);
    const result = await startEmailOtpLogin(page);
    assert.equal(result.started, true);
    assert.equal(await page.evaluate(() => window.emailOtpSelected), true);
  } finally {
    await browser.close();
  }
});

test("security settings detection ignores unrelated dialogs", async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent('<div role="dialog"><h2>Product update</h2><button>Continue</button></div>');
    assert.equal(await securitySetupInternals.settingsDialogVisible(page), false);
  } finally {
    await browser.close();
  }
});

test("security settings detection recognizes Japanese settings dialog text", async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <div role="dialog">
        <nav>
          <button>一般</button>
          <button>通知</button>
          <button>パーソナライズ</button>
          <button>データ コントロール</button>
          <button>セキュリティとログイン</button>
          <button>アカウント</button>
        </nav>
      </div>
    `);
    assert.equal(await securitySetupInternals.settingsDialogVisible(page), true);
  } finally {
    await browser.close();
  }
});

test("security settings text click prefers the exact interactive control over a parent div", async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <div onclick="window.parentClicked = true">
        <button onclick="event.stopPropagation(); window.settingsClicked = true">Settings</button>
      </div>
    `);
    assert.equal(await securitySetupInternals.clickVisibleText(page, ["Settings"], 'button, div'), true);
    assert.equal(await page.evaluate(() => window.settingsClicked === true), true);
    assert.equal(await page.evaluate(() => window.parentClicked === true), false);
  } finally {
    await browser.close();
  }
});

test("settings menu click only succeeds after the settings dialog is visible", async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <button onclick="
        setTimeout(() => {
          document.querySelector('#dialog').hidden = false;
        }, 200)
      ">Settings</button>
      <div id="dialog" role="dialog" hidden>
        <button>General</button>
        <button>Notifications</button>
        <button>Personalization</button>
        <button>Data controls</button>
        <button>Security</button>
        <button>Account</button>
      </div>
    `);
    assert.equal(await securitySetupInternals.clickSettingsMenuEntry(page), true);
    assert.equal(await securitySetupInternals.settingsDialogVisible(page), true);
  } finally {
    await browser.close();
  }
});

test("security settings entry prefers account security over Japanese safety", async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <div role="dialog">
        <nav>
          <button onclick="document.body.dataset.clicked='safety'">\u30bb\u30fc\u30d5\u30c6\u30a3</button>
          <button onclick="document.body.dataset.clicked='account-security'">\u30bb\u30ad\u30e5\u30ea\u30c6\u30a3\u3068\u30ed\u30b0\u30a4\u30f3</button>
        </nav>
      </div>
    `);
    assert.equal(await securitySetupInternals.clickSecuritySettingsEntry(page), true);
    assert.equal(await page.locator("body").getAttribute("data-clicked"), "account-security");
  } finally {
    await browser.close();
  }
});

test("passkey settings subpage is distinguished from the main security settings page", async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <main>
        <h1>セキュリティキーとパスキー</h1>
        <p>有効なセキュリティキーとパスキーを確認できます。</p>
      </main>
    `);
    assert.equal(await securitySetupInternals.passkeySecuritySubpageVisible(page), true);

    await page.setContent(`
      <main>
        <h1>セキュリティとログイン</h1>
        <section>Authenticator app</section>
        <section>Password</section>
      </main>
    `);
    assert.equal(await securitySetupInternals.passkeySecuritySubpageVisible(page), false);
  } finally {
    await browser.close();
  }
});

test("security password detection recognizes Japanese masked password rows", async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <main>
        <section>
          <h2>セキュリティキーとパスキー</h2>
          <button>セキュリティキーまたはパスキーを追加</button>
        </section>
        <section>
          <div>パスワード ******</div>
        </section>
      </main>
    `);
    assert.equal(await securitySetupInternals.passwordConfigured(page), true);
  } finally {
    await browser.close();
  }
});

test("security password detection recognizes the real Japanese password row with stars", async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <div role="dialog">
        <section>
          <div>\u30d1\u30b9\u30ef\u30fc\u30c9</div>
          <button>****** <span>\u203a</span></button>
        </section>
        <section>
          <div>\u30bb\u30ad\u30e5\u30ea\u30c6\u30a3\u30ad\u30fc\u3068\u30d1\u30b9\u30ad\u30fc</div>
          <p>\u30d1\u30b9\u30ef\u30fc\u30c9\u3088\u308a\u9ad8\u3044\u4fdd\u8b77</p>
          <button>\u8ffd\u52a0\u3059\u308b</button>
        </section>
        <section>
          <h2>\u591a\u8981\u7d20\u8a8d\u8a3c (MFA)</h2>
          <div>Authenticator app</div>
        </section>
      </div>
    `);
    assert.equal(await securitySetupInternals.passwordConfigured(page), true);
  } finally {
    await browser.close();
  }
});

test("security password action clicks Japanese password row instead of passkey row", async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <main>
        <section id="password-row">
          <span>パスワード</span>
          <button onclick="document.body.dataset.clicked='password'">追加する</button>
        </section>
        <section id="passkey-row">
          <span>セキュリティキーとパスキー</span>
          <p>パスワードより高い保護を提供します。</p>
          <button onclick="document.body.dataset.clicked='passkey'">追加する</button>
        </section>
      </main>
    `);
    assert.equal(await securitySetupInternals.clickSectionAction(
      page,
      ["Add password", "Update password", "Password", "添加密码", "更新密码", "登录密码", "密码", "パスワード"],
      ["add password", "update password", "add", "set", "change", "添加", "设置", "更改", "追加", "追加する", "変更"],
      ["security key", "passkey", "安全密钥", "通行密钥", "YubiKey", "セキュリティキー", "パスキー"],
    ), true);
    assert.equal(await page.locator("body").getAttribute("data-clicked"), "password");
  } finally {
    await browser.close();
  }
});

test("security profile menu click targets the inner avatar inside the account container", async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <style>
        [data-testid="accounts-profile-button"] { display: flex; align-items: center; gap: 12px; width: 280px; height: 56px; padding: 8px; }
        [data-testid="avatar"] { display: inline-flex; width: 36px; height: 36px; border-radius: 18px; align-items: center; justify-content: center; }
      </style>
      <div data-testid="accounts-profile-button" aria-label="Account">
        <span data-testid="avatar" onclick="document.querySelector('#menu').hidden = false">AB</span>
        <span>profile-user@example.com</span>
      </div>
      <div id="menu" hidden><button>Settings</button></div>
    `);
    assert.equal(await securitySetupInternals.clickProfileOrSettingsMenu(page, "profile-user@example.com"), true);
    assert.equal(await page.locator("#menu").isVisible(), true);
  } finally {
    await browser.close();
  }
});

test("security profile menu click handles collapsed lower-left avatar", async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
    await page.setContent(`
      <style>
        #avatar { position: fixed; left: 16px; bottom: 16px; width: 32px; height: 32px; border-radius: 50%; background: #20c997; display: flex; align-items: center; justify-content: center; }
      </style>
      <div id="avatar" onclick="document.querySelector('#menu').hidden = false">EB</div>
      <div id="menu" hidden><button>Settings</button></div>
    `);
    assert.equal(await securitySetupInternals.clickProfileOrSettingsMenu(page, "user@example.com"), true);
    assert.equal(await page.locator("#menu").isVisible(), true);
  } finally {
    await browser.close();
  }
});

test("security password setup fills confirmation fields and submits", async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <form id="password-form">
        <input type="password" name="newPassword" autocomplete="new-password">
        <input type="password" name="confirmPassword" autocomplete="new-password">
        <button type="submit">Continue</button>
      </form>
      <div id="done" hidden>Password set</div>
      <script>
        document.querySelector('#password-form').onsubmit = event => {
          event.preventDefault();
          window.passwordValues = Array.from(document.querySelectorAll('input')).map(input => input.value);
          event.target.remove();
          document.querySelector('#done').hidden = false;
        };
      </script>
    `);
    const account = { id: "acct", email: "acct@example.com", gptPassword: "", twofaSecret: "" };
    const result = await securitySetupInternals.fillPasswordSetupFlow(page, account, "StrongPass123!", {
      signal: new AbortController().signal,
      log: () => {},
      patch: async (patch) => Object.assign(account, patch),
    });
    assert.equal(result.success, true);
    assert.equal(result.submitted, true);
    assert.deepEqual(await page.evaluate(() => window.passwordValues), ["StrongPass123!", "StrongPass123!"]);
  } finally {
    await browser.close();
  }
});

test("security password setup does not treat disappearing form as confirmed without saved state", { timeout: 15_000 }, async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <form id="password-form">
        <input type="password" name="newPassword" autocomplete="new-password">
        <input type="password" name="confirmPassword" autocomplete="new-password">
        <button type="submit">Continue</button>
      </form>
      <script>
        document.querySelector('#password-form').onsubmit = event => {
          event.preventDefault();
          window.passwordValues = Array.from(document.querySelectorAll('input')).map(input => input.value);
          event.target.remove();
        };
      </script>
    `);
    const account = { id: "acct", email: "acct@example.com", gptPassword: "", twofaSecret: "" };
    const result = await securitySetupInternals.fillPasswordSetupFlow(page, account, "StrongPass123!", {
      signal: new AbortController().signal,
      log: () => {},
      patch: async (patch) => Object.assign(account, patch),
    });
    assert.equal(result.success, null);
    assert.equal(result.submitted, true);
    assert.equal(result.retry, true);
  } finally {
    await browser.close();
  }
});

test("security MFA helpers recognize authenticator dialogs and otpauth secrets", async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <form>
        <h1>ID を検証する</h1>
        <p>ワンタイム パスワード アプリでコードを確認してください</p>
        <input aria-label="ワンタイム コード">
        <button>続行</button>
      </form>
    `);
    assert.equal(await securitySetupInternals.mfaVerificationDialogVisible(page), true);
    assert.equal(
      securitySetupInternals.extractTotpSecretFromText("otpauth://totp/ChatGPT:test@example.com?secret=JBSWY3DPEHPK3PXP&issuer=OpenAI"),
      "JBSWY3DPEHPK3PXP",
    );
  } finally {
    await browser.close();
  }
});

test("security MFA QR payload extracts only the otpauth secret", async () => {
  const payload = "otpauth://totp/ChatGPT:test@example.com?secret=JBSWY3DPEHPK3PXP&issuer=OpenAI";
  assert.equal(
    securitySetupInternals.extractTotpSecretFromQrPayload(payload),
    "JBSWY3DPEHPK3PXP",
  );
  assert.notEqual(
    securitySetupInternals.extractTotpSecretFromQrPayload(payload),
    "OTPAUTHTOTPCHATGPTTESTEXAMPLECOMSECRETJBSWY3DPEHPK3PXPISSUEROPENAI",
  );
});

test("security MFA switch targets authenticator app instead of passkey add", async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <div role="dialog" aria-modal="true">
        <section>
          <h2>\u30d1\u30b9\u30ef\u30fc\u30c9</h2>
          <button>******</button>
        </section>
        <section>
          <h2>\u30bb\u30ad\u30e5\u30ea\u30c6\u30a3\u30ad\u30fc\u3068\u30d1\u30b9\u30ad\u30fc</h2>
          <p>\u30d1\u30b9\u30ef\u30fc\u30c9\u3088\u308a\u9ad8\u3044\u4fdd\u8b77</p>
          <button onclick="window.passkeyClicked = true">\u8ffd\u52a0\u3059\u308b</button>
        </section>
        <section>
          <h2>\u591a\u8981\u7d20\u8a8d\u8a3c (MFA)</h2>
          <div class="row">
            <div>
              <strong>Authenticator app</strong>
              <p>\u8a8d\u8a3c\u30a2\u30d7\u30ea\u306e\u30ef\u30f3\u30bf\u30a4\u30e0\u30b3\u30fc\u30c9\u3092\u4f7f\u7528\u3057\u307e\u3059\u3002</p>
            </div>
            <button role="switch" aria-checked="false" onclick="window.authenticatorClicked = true"></button>
          </div>
          <div class="row">
            <div>Text message</div>
            <button role="switch" aria-checked="false" onclick="window.smsClicked = true"></button>
          </div>
        </section>
      </div>
    `);
    const info = await securitySetupInternals.mfaSwitchInfo(page);
    assert.ok(info);
    await page.mouse.click(info.x, info.y);
    assert.equal(await page.evaluate(() => window.authenticatorClicked === true), true);
    assert.equal(await page.evaluate(() => window.passkeyClicked === true), false);
    assert.equal(await page.evaluate(() => window.smsClicked === true), false);
  } finally {
    await browser.close();
  }
});

test("security MFA code helper fills code and clicks verify", { timeout: 35_000 }, async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <div role="dialog" aria-modal="true">
        <h2>Connect authenticator app</h2>
        <p>Enter the 6 digit code from your authenticator app.</p>
        <form id="mfa-form">
          <input id="code" inputmode="numeric" autocomplete="one-time-code">
          <button type="submit">Verify</button>
        </form>
      </div>
      <script>
        document.querySelector('#mfa-form').onsubmit = event => {
          event.preventDefault();
          window.submittedCode = document.querySelector('#code').value;
          window.verifyClicked = true;
        };
      </script>
    `);
    const ok = await securitySetupInternals.fillTotpCodeForMfa(page, "JBSWY3DPEHPK3PXP", {
      signal: new AbortController().signal,
      log: () => {},
      patch: async () => ({}),
    });
    assert.equal(ok, true);
    assert.match(await page.evaluate(() => window.submittedCode), /^\d{6}$/);
    assert.equal(await page.evaluate(() => window.verifyClicked), true);
  } finally {
    await browser.close();
  }
});

test("security MFA code helper clicks Japanese verify in the modal over background content", { timeout: 35_000 }, async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <style>
        [role="dialog"] { position: fixed; left: 300px; top: 60px; width: 440px; min-height: 400px; background: white; }
        main { position: fixed; inset: 0; background: #eee; }
      </style>
      <div role="dialog" aria-modal="true">
        <h2>\u8a8d\u8a3c\u30a2\u30d7\u30ea\u3092\u63a5\u7d9a\u3059\u308b</h2>
        <p>\u30b9\u30c6\u30c3\u30d72: 6 \u6841\u306e\u30b3\u30fc\u30c9\u3092\u5165\u529b\u3057\u3066\u304f\u3060\u3055\u3044\u3002</p>
        <input id="code" name="totp_otp" inputmode="numeric" autocomplete="one-time-code">
        <button id="cancel" onclick="window.cancelClicked = true">\u30ad\u30e3\u30f3\u30bb\u30eb\u3059\u308b</button>
        <button id="verify" onclick="window.modalVerifyClicked = true">\u691c\u8a3c</button>
      </div>
      <main>
        <button onclick="window.backgroundVerifyClicked = true">Verify</button>
      </main>
    `);
    const ok = await securitySetupInternals.fillTotpCodeForMfa(page, "JBSWY3DPEHPK3PXP", {
      signal: new AbortController().signal,
      log: () => {},
      patch: async () => ({}),
    });
    assert.equal(ok, true);
    assert.match(await page.locator("#code").inputValue(), /^\d{6}$/);
    assert.equal(await page.evaluate(() => window.modalVerifyClicked === true), true);
    assert.equal(await page.evaluate(() => window.cancelClicked === true), false);
    assert.equal(await page.evaluate(() => window.backgroundVerifyClicked === true), false);
  } finally {
    await browser.close();
  }
});

test("security MFA code helper enables disabled verify button through real input events", { timeout: 35_000 }, async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <div role="dialog" aria-modal="true">
        <h2>\u8a8d\u8a3c\u30a2\u30d7\u30ea\u3092\u63a5\u7d9a\u3059\u308b</h2>
        <input id="code" name="totp_otp" inputmode="numeric" autocomplete="one-time-code">
        <button id="verify" disabled onclick="window.modalVerifyClicked = true">\u691c\u8a3c</button>
      </div>
      <script>
        const code = document.querySelector('#code');
        const verify = document.querySelector('#verify');
        code.addEventListener('input', () => {
          verify.disabled = !/^\\d{6}$/.test(code.value);
        });
      </script>
    `);
    const ok = await securitySetupInternals.fillTotpCodeForMfa(page, "JBSWY3DPEHPK3PXP", {
      signal: new AbortController().signal,
      log: () => {},
      patch: async () => ({}),
    });
    assert.equal(ok, true);
    assert.equal(await page.locator("#verify").isEnabled(), true);
    assert.equal(await page.evaluate(() => window.modalVerifyClicked === true), true);
  } finally {
    await browser.close();
  }
});

test("security TOTP extraction reads Japanese manual secret fields without using the code input", async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <div role="dialog" aria-modal="true">
        <h2>認証アプリを接続する</h2>
        <p>ステップ 1: 認証アプリに以下のセットアップキーを入力してください。</p>
        <input id="secret" readonly value="UEJH7HZNXELD3IKB5QCW6TB4JS3Y2PDC">
        <p>ステップ 2: 6 桁のコードを入力してください。</p>
        <input id="code" inputmode="numeric" autocomplete="one-time-code" placeholder="6 桁のコード">
      </div>
    `);
    assert.equal(
      await securitySetupInternals.extractTotpSecretFromPage(page),
      "UEJH7HZNXELD3IKB5QCW6TB4JS3Y2PDC",
    );

    await page.setContent(`
      <div role="dialog" aria-modal="true">
        <h2>認証アプリを接続する</h2>
        <p>Authenticator app</p>
        <input id="code" inputmode="numeric" autocomplete="one-time-code" placeholder="6 桁のコード">
      </div>
    `);
    assert.equal(await securitySetupInternals.extractTotpSecretFromPage(page), "");
  } finally {
    await browser.close();
  }
});
test("about-you flow waits, fills adult profile, and submits", { timeout: 20_000 }, async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <form id="profile">
        <input name="name" aria-label="Full name">
        <input name="age" type="number" aria-label="Age">
        <button type="submit">Finish creating account</button>
      </form>
      <div id="done" hidden>Done</div>
      <script>
        document.querySelector('#profile').onsubmit = event => {
          event.preventDefault();
          window.profileResult = { name: event.target.name.value, age: event.target.age.value };
          event.target.remove();
          document.querySelector('#done').hidden = false;
        };
      </script>
    `);
    await fillAboutYouAndSubmit(page);
    const result = await page.evaluate(() => window.profileResult);
    assert.match(result.name, /^[A-Z][a-z]+ [A-Z][a-z]+$/);
    assert.ok(Number(result.age) >= 18);
    assert.equal(await page.locator('#done').isVisible(), true);
  } finally {
    await browser.close();
  }
});

test("about-you flow enables a disabled Japanese submit button through input events", { timeout: 20_000 }, async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <form id="profile">
        <label>\u6c0f\u540d<input id="name"></label>
        <label>\u5e74\u9f62<input id="age" type="text"></label>
        <button id="submit" type="submit" disabled>\u30a2\u30ab\u30a6\u30f3\u30c8\u306e\u4f5c\u6210\u3092\u5b8c\u4e86\u3059\u308b</button>
      </form>
      <script>
        const form = document.querySelector('#profile');
        const submit = document.querySelector('#submit');
        function sync() {
          submit.disabled = !(document.querySelector('#name').value.trim() && Number(document.querySelector('#age').value) >= 18);
        }
        form.addEventListener('input', sync);
        form.onsubmit = event => {
          event.preventDefault();
          window.profileResult = { name: document.querySelector('#name').value, age: document.querySelector('#age').value };
          document.body.innerHTML = '<main id="done">done</main>';
        };
      </script>
    `);
    await fillAboutYouAndSubmit(page);
    const result = await page.evaluate(() => window.profileResult);
    assert.ok(result.name.length > 3);
    assert.ok(Number(result.age) >= 18);
    assert.equal(await page.locator('#done').isVisible(), true);
  } finally {
    await browser.close();
  }
});

test("about-you flow clicks Japanese role button submit control", { timeout: 20_000 }, async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <div id="profile">
        <label>\u6c0f\u540d<input id="name"></label>
        <label>\u5e74\u9f62<input id="age" type="text"></label>
        <div id="submit" role="button" tabindex="0">\u30a2\u30ab\u30a6\u30f3\u30c8\u306e\u4f5c\u6210\u3092\u5b8c\u4e86\u3059\u308b</div>
      </div>
      <script>
        document.querySelector('#submit').addEventListener('click', () => {
          window.profileResult = { name: document.querySelector('#name').value, age: document.querySelector('#age').value };
          document.body.innerHTML = '<main id="done">done</main>';
        });
      </script>
    `);
    await fillAboutYouAndSubmit(page);
    const result = await page.evaluate(() => window.profileResult);
    assert.ok(result.name.length > 3);
    assert.ok(Number(result.age) >= 18);
    assert.equal(await page.locator('#done').isVisible(), true);
  } finally {
    await browser.close();
  }
});

test("session capture stores API payload and browser storage state", async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.route("https://chatgpt.com/", (route) => route.fulfill({ body: "<main>ChatGPT</main>", contentType: "text/html" }));
    await page.route("https://chatgpt.com/api/auth/session", (route) => route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ accessToken: "access-token", expires: "2030-01-01T00:00:00Z", user: { email: "u@example.com" } }),
    }));
    await page.goto("https://chatgpt.com/");
    await page.evaluate(() => {
      window.fetch = async () => new Response(JSON.stringify({ accessToken: "access-token", expires: "2030-01-01T00:00:00Z", user: { email: "u@example.com" } }), { status: 200, headers: { "content-type": "application/json" } });
    });
    const session = await readSession(page, context);
    assert.equal(session.accessToken, "access-token");
    assert.match(session.sessionJson, /u@example\.com/);
    assert.match(session.storageStateJson, /cookies/);
  } finally {
    await browser.close();
  }
});

test("session capture does not use visible-page fetch outside ChatGPT", async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.setContent("<main>Auth page</main>");
    await page.evaluate(() => {
      window.fetch = async () => new Response(JSON.stringify({ accessToken: "should-not-be-used" }), { status: 200, headers: { "content-type": "application/json" } });
    });
    const session = await readSession(page, context);
    assert.equal(session, null);
  } finally {
    await browser.close();
  }
});
