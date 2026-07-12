import { _electron as electron } from "playwright-core";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

const root = await mkdtemp(path.join(tmpdir(), "registration-desk-ui-"));
const errors = [];
let electronApp;

try {
  electronApp = await electron.launch({ args: ["."], env: { ...process.env, REGISTRATION_DESK_USER_DATA: root } });
  const page = await electronApp.firstWindow({ timeout: 15_000 });
  page.on("pageerror", (error) => errors.push(`page: ${error.message}`));
  page.on("console", (message) => { if (message.type() === "error") errors.push(`console: ${message.text()}`); });
  await page.waitForSelector("#import");

  const actualUserData = await electronApp.evaluate(({ app }) => app.getPath("userData"));
  if (path.resolve(actualUserData) !== path.resolve(root)) throw new Error(`测试数据目录未隔离: ${actualUserData}`);

  await page.locator("#importText").fill([
    "ui@example.com----mail----client----refresh----2fa=JBSWY3DPEHPK3PXP",
    "ui2@example.com----mail----client----refresh",
    "ui3@example.com----mail----client----refresh",
  ].join("\n"));
  await page.locator("#import").click();
  await page.waitForFunction(() => Array.from(document.querySelectorAll("#accounts td:nth-child(2)")).some((cell) => cell.textContent === "ui@example.com"));
  await page.locator(".row-check").first().check();
  await page.locator(".row-check").nth(2).click({ modifiers: ["Shift"] });
  const selectedAfterShift = await page.locator(".row-check:checked").count();
  if (selectedAfterShift !== 3) throw new Error(`Shift 批量选择失败: ${selectedAfterShift}`);
  await page.locator("#selectAll").uncheck();
  await page.locator(".row-check").first().check();
  await page.locator("#copyTwofaCode").click();
  await page.waitForFunction(() => document.querySelector("#toast")?.textContent?.includes("2FA 验证码已复制"));

  await page.locator("#importSession").click();
  await page.locator("#sessionEmailHint").fill("session-ui@example.com");
  const token = `eyJhbGciOiJub25lIn0.${Buffer.from(JSON.stringify({ sub: "session-ui" })).toString("base64url")}.${"x".repeat(90)}`;
  await page.locator("#sessionImportText").fill(JSON.stringify({ accessToken: token, expires: "2030-01-01T00:00:00Z" }));
  await page.locator('#sessionImportForm button[type="submit"]').click();
  await page.waitForFunction(() => Array.from(document.querySelectorAll("#accounts td:nth-child(2)")).some((cell) => cell.textContent === "session-ui@example.com"));

  const otherCount = await page.locator("#otherAccountCount").innerText();
  if (otherCount !== "4") throw new Error(`其他账号分组数量错误: ${otherCount}`);
  await page.locator("#accountGroupSwitch").check();
  await page.waitForFunction(() => document.querySelectorAll("#accounts .row-check").length === 0 && document.querySelector("#empty")?.textContent === "该分组暂无账号");
  await page.locator("#accountGroupSwitch").uncheck();
  await page.waitForFunction(() => document.querySelectorAll("#accounts .row-check").length === 4);

  const state = await page.evaluate(() => window.registrationDesk.getState());
  const layout = await page.evaluate(() => ({
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    toolbarOverflow: document.querySelector(".table-tools").scrollWidth > document.querySelector(".table-tools").clientWidth,
    buttons: Array.from(document.querySelectorAll(".table-tools button")).map((button) => button.textContent),
  }));
  await page.locator("#importSession").click();
  await page.screenshot({ path: path.join(root, "ui-session-import-check.png") });
  console.log(JSON.stringify({
    errors,
    accountCount: state.accounts.length,
    selectedAfterShift,
    sessionStatus: state.accounts.find((account) => account.email === "session-ui@example.com")?.statusText,
    layout,
  }, null, 2));
} finally {
  await electronApp?.close().catch(() => undefined);
  await rm(root, { recursive: true, force: true });
}
