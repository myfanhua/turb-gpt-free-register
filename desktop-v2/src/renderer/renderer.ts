import type { Account, AppState, RecentEmail, RegistrationEvent, Settings } from "../shared/types.js";
import { calculateTrialMetrics } from "../shared/metrics.js";

let state: AppState;
let lastSelectedIndex: number | null = null;
let showTrialEligibleAccounts = false;
const byId = <T extends Element = HTMLElement>(id: string) => document.getElementById(id) as unknown as T;

async function boot(): Promise<void> {
  state = await window.registrationDesk.getState();
  loadSettings();
  render();
  bind();
  window.registrationDesk.onEvent(onEvent);
}

function bind(): void {
  byId<HTMLButtonElement>("import").onclick = () => void runAction(async () => {
    const result = await window.registrationDesk.importAccounts(byId<HTMLTextAreaElement>("importText").value);
    toast(`新增 ${result.imported}，更新 ${result.updated}${result.errors.length ? `，失败 ${result.errors.length}` : ""}`);
    appendLog(`导入账号完成：新增 ${result.imported}，更新 ${result.updated}，失败 ${result.errors.length}`);
    await reload();
  });
  byId<HTMLButtonElement>("importSession").onclick = () => byId<HTMLDialogElement>("sessionImportDialog").showModal();
  byId<HTMLButtonElement>("closeSessionImport").onclick = () => byId<HTMLDialogElement>("sessionImportDialog").close();
  byId<HTMLButtonElement>("cancelSessionImport").onclick = () => byId<HTMLDialogElement>("sessionImportDialog").close();
  byId<HTMLFormElement>("sessionImportForm").onsubmit = (event) => {
    event.preventDefault();
    void runAction(async () => {
      const result = await window.registrationDesk.importSessions(
        byId<HTMLTextAreaElement>("sessionImportText").value,
        byId<HTMLInputElement>("sessionEmailHint").value,
      );
      toast(`Session：新增 ${result.imported}，更新 ${result.updated}`);
      appendLog(`导入 Session 完成：新增 ${result.imported}，覆盖/更新 ${result.updated}`);
      byId<HTMLDialogElement>("sessionImportDialog").close();
      byId<HTMLTextAreaElement>("sessionImportText").value = "";
      byId<HTMLInputElement>("sessionEmailHint").value = "";
      await reload();
    });
  };
  byId<HTMLButtonElement>("saveSettings").onclick = () => void runAction(async () => {
    state.settings = await window.registrationDesk.saveSettings(readSettings());
    appendLog(`保存设置：并发 ${state.settings.concurrency}，本地代理 ${state.settings.localProxy || "未设置"}，动态代理 ${state.settings.proxyPool ? `${state.settings.proxyPool.split(/\r?\n/).filter(Boolean).length} 条` : "未设置"}，注册后设置 GPT 密码/MFA=${state.settings.setupGptSecurity ? "开启" : "关闭"}`);
    toast("设置已保存");
  });
  byId<HTMLButtonElement>("start").onclick = () => void runAction(async () => {
    const ids = selectedIds();
    logSelection("开始注册", ids);
    await window.registrationDesk.saveSettings(readSettings());
    await window.registrationDesk.start(ids);
    appendLog(`开始注册任务已提交：${ids.length} 个账号`);
    state = await window.registrationDesk.getState();
    loadSettings();
  });
  byId<HTMLButtonElement>("login").onclick = () => void runAction(async () => {
    const ids = selectedIds();
    logSelection("登录获取 Session", ids);
    await window.registrationDesk.saveSettings(readSettings());
    await window.registrationDesk.login(ids);
    appendLog(`登录获取 Session 任务已提交：${ids.length} 个账号`);
    state = await window.registrationDesk.getState();
    loadSettings();
  });
  byId<HTMLButtonElement>("stop").onclick = () => void runAction(async () => {
    appendLog("停止：已发送停止当前注册/登录任务指令");
    await window.registrationDesk.stop();
  });
  byId<HTMLButtonElement>("remove").onclick = () => void runAction(async () => {
    const ids = selectedIds();
    logSelection("删除选中", ids);
    await window.registrationDesk.removeAccounts(ids);
    appendLog(`删除完成：${ids.length} 个账号`);
    await reload();
  });
  byId<HTMLButtonElement>("trial").onclick = () => void runAction(async () => {
    const ids = selectedIds();
    logSelection("检测免费试用", ids);
    state = await window.registrationDesk.checkTrial(ids);
    render();
    appendLog(`检测免费试用完成：${ids.length} 个账号`);
  });
  byId<HTMLButtonElement>("health").onclick = () => void runAction(async () => {
    const ids = selectedIds();
    logSelection("检测封号", ids);
    state = await window.registrationDesk.checkHealth(ids);
    render();
    appendLog(`检测封号完成：${ids.length} 个账号`);
  });
  byId<HTMLButtonElement>("checkSession").onclick = () => void runAction(async () => {
    const ids = selectedIds();
    logSelection("检测 Session", ids);
    state = await window.registrationDesk.checkSessions(ids);
    render();
    appendLog(`检测 Session 完成：${ids.length} 个账号`);
  });
  byId<HTMLButtonElement>("mail").onclick = () => void openMail();
  byId<HTMLButtonElement>("copyTwofaCode").onclick = () => void copyTwofaCode();
  byId<HTMLButtonElement>("copyEmailCode").onclick = () => void copyEmailCode();
  byId<HTMLButtonElement>("exportGptCreds").onclick = () => void exportGptCredentials();
  byId<HTMLButtonElement>("exportMailCreds").onclick = () => void exportMailCredentials();
  byId<HTMLButtonElement>("details").onclick = () => openResult();
  byId<HTMLButtonElement>("closeMail").onclick = () => byId<HTMLDialogElement>("mailDialog").close();
  byId<HTMLButtonElement>("closeResult").onclick = () => byId<HTMLDialogElement>("resultDialog").close();
  byId<HTMLInputElement>("accountGroupSwitch").onchange = (event) => {
    showTrialEligibleAccounts = (event.target as HTMLInputElement).checked;
    lastSelectedIndex = null;
    render();
    appendLog(`切换账号分组：${showTrialEligibleAccounts ? "可免费试用" : "其他账号"}，当前显示 ${visibleAccounts().length} 个账号`);
  };
  document.querySelectorAll<HTMLButtonElement>("[data-copy]").forEach((button) => button.onclick = () => void runAction(async () => {
    const target = byId<HTMLInputElement | HTMLTextAreaElement>(button.dataset.copy || "");
    await window.registrationDesk.copyText(target.value);
    appendLog(`复制结果字段：${button.dataset.copy || "未知字段"}，长度 ${target.value.length}`);
    toast("已复制");
  }));
  byId<HTMLInputElement>("selectAll").onchange = (event) => {
    const checked = (event.target as HTMLInputElement).checked;
    document.querySelectorAll<HTMLInputElement>(".row-check").forEach((input) => input.checked = checked);
    lastSelectedIndex = checked ? Math.max(0, visibleAccounts().length - 1) : null;
    appendLog(`${checked ? "全选" : "取消全选"}：当前选中 ${selectedIds().length} 个账号`);
  };
  byId<HTMLTableSectionElement>("accounts").addEventListener("click", (event) => {
    const input = (event.target as HTMLElement).closest<HTMLInputElement>(".row-check");
    if (!input) return;
    handleRowSelection(input, event as MouseEvent);
  });
  byId<HTMLButtonElement>("clearLogs").onclick = () => byId("log").textContent = "";
}

function render(): void {
  const body = byId<HTMLTableSectionElement>("accounts");
  const checked = new Set(selectedIds());
  const visible = visibleAccounts();
  body.replaceChildren(...visible.map((account, index) => row(account, index)));
  body.classList.remove("account-list-enter");
  void body.offsetWidth;
  body.classList.add("account-list-enter");
  document.querySelectorAll<HTMLInputElement>(".row-check").forEach((input) => { input.checked = checked.has(input.value); });
  const empty = byId("empty");
  empty.textContent = state.accounts.length ? "该分组暂无账号" : "导入账号后即可开始";
  empty.style.display = visible.length ? "none" : "block";
  const completed = state.accounts.filter((account) => account.status === "completed").length;
  byId("summary").textContent = `${state.accounts.length} 个账号，${completed} 个已完成`;
  updateSelectAll();
  renderTrialGauge();
  renderAccountGroupSwitch();
}

function visibleAccounts(): Account[] {
  return state.accounts.filter((account) => showTrialEligibleAccounts
    ? account.trialEligible === true
    : account.trialEligible !== true);
}

function renderAccountGroupSwitch(): void {
  const eligible = state.accounts.filter((account) => account.trialEligible === true).length;
  const other = state.accounts.length - eligible;
  byId("eligibleAccountCount").textContent = String(eligible);
  byId("otherAccountCount").textContent = String(other);
  const panel = byId("accountGroupPanel");
  panel.dataset.group = showTrialEligibleAccounts ? "eligible" : "other";
  byId<HTMLInputElement>("accountGroupSwitch").checked = showTrialEligibleAccounts;
  byId("accountGroupHint").textContent = `正在显示${showTrialEligibleAccounts ? "可免费试用" : "其他"}账号`;
}

function renderTrialGauge(): void {
  const metrics = calculateTrialMetrics(state.accounts);
  const ring = byId<SVGCircleElement>("trialGaugeRing");
  ring.style.strokeDasharray = `${metrics.eligibilityPercentage} ${100 - metrics.eligibilityPercentage}`;
  byId("trialPercent").textContent = `${metrics.eligibilityPercentage}%`;
  byId("trialChecked").textContent = `${metrics.checked} / ${metrics.total}`;
  byId("trialEligible").textContent = String(metrics.eligible);
  byId("trialCoverage").textContent = `${metrics.coveragePercentage}%`;
}

function row(account: Account, index: number): HTMLTableRowElement {
  const tr = document.createElement("tr");
  const trial = account.trialEligible === true ? "√ 有资格" : account.trialEligible === false ? "× 无资格" : "? 未知";
  const health = account.health === "active" ? "正常" : account.health === "deactivated" ? "已封禁/停用" : "未知";
  const session = account.sessionValid === true ? "有效" : account.sessionValid === false ? "失效" : account.accessToken ? "未检测" : "无";
  tr.innerHTML = `<td><input class="row-check" type="checkbox" value="${escapeHtml(account.id)}" data-index="${index}"></td>
    <td title="${escapeHtml(account.email)}">${escapeHtml(account.email)}</td>
    <td class="status-${account.status}" title="${escapeHtml(account.lastError)}">${escapeHtml(account.statusText)}</td>
    <td class="${account.gptPassword ? "credential-set" : "credential-missing"}" title="${account.gptPassword ? "已保存 GPT 登录密码" : "未保存 GPT 登录密码"}">${account.gptPassword ? "已保存" : "未保存"}</td>
    <td class="${account.twofaSecret ? "credential-set" : "credential-missing"}" title="${account.twofaSecret ? "已保存 Authenticator MFA 密钥" : "未保存 Authenticator MFA 密钥"}">${account.twofaSecret ? "已保存" : "未保存"}</td>
    <td class="${account.sessionValid === true ? "status-completed" : account.sessionValid === false ? "status-failed" : ""}" title="${escapeHtml(account.sessionUpdatedAt)}">${session}</td>
    <td class="${account.trialEligible === true ? "trial-yes" : account.trialEligible === false ? "trial-no" : ""}" title="${escapeHtml(account.trialMessage)}">${trial}</td>
    <td class="health-${account.health}" title="${escapeHtml(account.lastError)}">${health}</td>
    <td>${new Date(account.updatedAt).toLocaleString()}</td>`;
  return tr;
}

function onEvent(event: RegistrationEvent): void {
  if (event.type === "log" && event.message) appendLog(event.message);
  if (event.type === "account" && event.account) {
    const index = state.accounts.findIndex((account) => account.id === event.account!.id);
    if (index >= 0) state.accounts[index] = event.account;
    render();
  }
  if (event.type === "queue") {
    byId<HTMLButtonElement>("start").disabled = Boolean(event.running);
    byId<HTMLButtonElement>("login").disabled = Boolean(event.running);
    byId<HTMLButtonElement>("stop").disabled = !event.running;
  }
}

function loadSettings(): void {
  const settings = state.settings;
  byId<HTMLInputElement>("concurrency").value = String(settings.concurrency);
  byId<HTMLInputElement>("localProxy").value = settings.localProxy;
  byId<HTMLTextAreaElement>("proxyPool").value = settings.proxyPool;
  byId<HTMLInputElement>("registrationUrl").value = settings.registrationUrl;
  byId<HTMLInputElement>("trialApiUrl").value = settings.trialApiUrl;
  byId<HTMLInputElement>("headless").checked = settings.headless;
  byId<HTMLInputElement>("setupGptSecurity").checked = settings.setupGptSecurity;
}

function readSettings(): Settings {
  return {
    concurrency: Math.max(1, Math.min(20, Number(byId<HTMLInputElement>("concurrency").value) || 1)),
    localProxy: byId<HTMLInputElement>("localProxy").value.trim(),
    proxyPool: byId<HTMLTextAreaElement>("proxyPool").value.trim(),
    registrationUrl: byId<HTMLInputElement>("registrationUrl").value.trim(),
    trialApiUrl: byId<HTMLInputElement>("trialApiUrl").value.trim(),
    headless: byId<HTMLInputElement>("headless").checked,
    setupGptSecurity: byId<HTMLInputElement>("setupGptSecurity").checked,
  };
}

function selectedIds(): string[] { return Array.from(document.querySelectorAll<HTMLInputElement>(".row-check:checked")).map((input) => input.value); }
function selectedAccountsFromIds(ids: string[]): Account[] {
  const selected = new Set(ids);
  return state.accounts.filter((account) => selected.has(account.id));
}
function describeSelection(ids: string[]): string {
  const accounts = selectedAccountsFromIds(ids);
  if (!accounts.length) return "未选择账号";
  const preview = accounts.slice(0, 8).map((account) => account.email).join("，");
  return `${accounts.length} 个账号：${preview}${accounts.length > 8 ? ` 等 ${accounts.length} 个` : ""}`;
}
function logSelection(action: string, ids: string[]): void { appendLog(`${action}：${describeSelection(ids)}`); }
function updateSelectAll(): void {
  const checks = Array.from(document.querySelectorAll<HTMLInputElement>(".row-check"));
  const selectAll = byId<HTMLInputElement>("selectAll");
  const checked = checks.filter((input) => input.checked).length;
  selectAll.checked = checks.length > 0 && checked === checks.length;
  selectAll.indeterminate = checked > 0 && checked < checks.length;
}
function handleRowSelection(input: HTMLInputElement, event: MouseEvent): void {
  const currentIndex = Number(input.dataset.index);
  if (event.shiftKey && lastSelectedIndex !== null && Number.isFinite(currentIndex)) {
    const [start, end] = [lastSelectedIndex, currentIndex].sort((a, b) => a - b);
    const checks = Array.from(document.querySelectorAll<HTMLInputElement>(".row-check"));
    for (let index = start; index <= end; index += 1) {
      if (checks[index]) checks[index].checked = input.checked;
    }
    appendLog(`Shift 批量${input.checked ? "选中" : "取消"}：第 ${start + 1} 到 ${end + 1} 行，当前选中 ${selectedIds().length} 个账号`);
  } else {
    const account = state.accounts.find((item) => item.id === input.value);
    appendLog(`${input.checked ? "选中" : "取消选中"}账号：${account?.email || input.value}，当前选中 ${selectedIds().length} 个账号`);
  }
  lastSelectedIndex = Number.isFinite(currentIndex) ? currentIndex : lastSelectedIndex;
  updateSelectAll();
}
function selectedAccount(action: string): Account | null {
  const ids = selectedIds();
  if (ids.length !== 1) {
    appendLog(`${action}：${describeSelection(ids)}，需要只选择 1 个账号`);
    toast(`请选择一个账号${action}`);
    return null;
  }
  return state.accounts.find((item) => item.id === ids[0]) || null;
}
function selectedAccountsForBatch(action: string): Account[] {
  const ids = selectedIds();
  if (!ids.length) {
    appendLog(`${action}：未选择账号`);
    toast("请先选择账号");
    return [];
  }
  const accounts = selectedAccountsFromIds(ids);
  appendLog(`${action}：${describeSelection(ids)}`);
  return accounts;
}
async function copyTwofaCode(): Promise<void> {
  const account = selectedAccount("复制 2FA 验证码");
  if (!account) return;
  appendLog(`复制 2FA 验证码：${account.email}`);
  await runAction(async () => {
    const result = await window.registrationDesk.getTotp(account.id);
    await window.registrationDesk.copyText(result.code);
    toast(`2FA 验证码已复制，剩余 ${result.remaining} 秒`);
  });
}
async function copyEmailCode(): Promise<void> {
  const account = selectedAccount("复制邮箱验证码");
  if (!account) return;
  appendLog(`复制邮箱验证码：${account.email}`);
  await runAction(async () => {
    toast("正在获取邮箱验证码...");
    const code = await window.registrationDesk.getEmailCode(account.id);
    await window.registrationDesk.copyText(code);
    toast("邮箱验证码已复制");
  });
}
async function exportGptCredentials(): Promise<void> {
  const accounts = selectedAccountsForBatch("导出 GPT 密码/2FA");
  if (!accounts.length) return;
  const lines = accounts.map((account) => `${account.email}----${account.gptPassword}----${account.twofaSecret}`);
  const missing = accounts.filter((account) => !account.gptPassword || !account.twofaSecret).length;
  await runAction(async () => {
    await window.registrationDesk.copyText(lines.join("\n"));
    appendLog(`导出 GPT 密码/2FA：${accounts.length} 个账号，${missing ? `${missing} 个缺少密码或 2FA` : "全部字段完整"}，已复制到剪贴板`);
    toast(`已复制 ${accounts.length} 行 GPT 密码/2FA`);
  });
}
async function exportMailCredentials(): Promise<void> {
  const accounts = selectedAccountsForBatch("导出邮箱令牌");
  if (!accounts.length) return;
  const lines = accounts.map((account) => `${account.email}----${account.emailPassword}----${account.clientId}----${account.refreshToken}`);
  const missing = accounts.filter((account) => !account.emailPassword || !account.clientId || !account.refreshToken).length;
  await runAction(async () => {
    await window.registrationDesk.copyText(lines.join("\n"));
    appendLog(`导出邮箱令牌：${accounts.length} 个账号，${missing ? `${missing} 个缺少邮箱密码/client_id/refresh_token` : "全部字段完整"}，已复制到剪贴板`);
    toast(`已复制 ${accounts.length} 行邮箱令牌`);
  });
}
async function openMail(): Promise<void> {
  const ids = selectedIds();
  if (ids.length !== 1) { appendLog(`最近邮件：${describeSelection(ids)}，需要只选择 1 个账号`); toast("请选择一个账号查看邮件"); return; }
  const account = state.accounts.find((item) => item.id === ids[0]);
  if (!account) return;
  appendLog(`最近邮件：正在读取 ${account.email}`);
  byId("mailAccount").textContent = account.email;
  byId("mailList").textContent = "正在读取 Outlook 收件箱...";
  byId<HTMLDialogElement>("mailDialog").showModal();
  try { renderEmails(await window.registrationDesk.recentEmails(account.id)); }
  catch (error) { byId("mailList").textContent = `读取失败: ${(error as Error).message}`; }
}
function openResult(): void {
  const ids = selectedIds();
  if (ids.length !== 1) { appendLog(`查看结果：${describeSelection(ids)}，需要只选择 1 个账号`); toast("请选择一个账号查看结果"); return; }
  const account = state.accounts.find((item) => item.id === ids[0]);
  if (!account) return;
  appendLog(`查看结果：${account.email}`);
  byId("resultAccount").textContent = account.email;
  byId<HTMLInputElement>("resultPassword").value = account.gptPassword;
  byId<HTMLInputElement>("resultTwofa").value = account.twofaSecret;
  byId<HTMLInputElement>("resultSessionStatus").value = account.sessionValid === true ? "有效" : account.sessionValid === false ? "失效" : "未检测";
  byId<HTMLInputElement>("resultSessionTime").value = account.sessionUpdatedAt ? new Date(account.sessionUpdatedAt).toLocaleString() : "";
  byId<HTMLTextAreaElement>("resultAccessToken").value = account.accessToken;
  byId<HTMLTextAreaElement>("resultSessionJson").value = account.sessionJson;
  byId<HTMLTextAreaElement>("resultStorageState").value = account.storageStateJson;
  byId<HTMLDialogElement>("resultDialog").showModal();
}
function renderEmails(emails: RecentEmail[]): void {
  const list = byId("mailList");
  if (!emails.length) { list.textContent = "收件箱没有邮件"; return; }
  list.replaceChildren(...emails.map((email) => {
    const button = document.createElement("button");
    button.className = "mail-item";
    button.innerHTML = `<strong>${escapeHtml(email.subject)}</strong><span>${escapeHtml(email.from)}</span><span>${new Date(email.date).toLocaleString()}</span>`;
    button.onclick = () => showEmail(email);
    return button;
  }));
  showEmail(emails[0]);
}
function showEmail(email: RecentEmail): void {
  byId("mailSubject").textContent = email.subject;
  byId("mailMeta").textContent = `${email.from} · ${new Date(email.date).toLocaleString()}`;
  byId("mailBody").textContent = email.text || "(无纯文本正文)";
}
async function reload(): Promise<void> { state = await window.registrationDesk.getState(); render(); }
function appendLog(message: string): void { const log = byId("log"); log.textContent += `[${new Date().toLocaleTimeString()}] ${message}\n`; log.scrollTop = log.scrollHeight; }
function toast(message: string): void { const element = byId("toast"); element.textContent = message; element.classList.add("show"); setTimeout(() => element.classList.remove("show"), 2200); }
async function runAction(action: () => Promise<unknown>): Promise<void> {
  try { await action(); }
  catch (error) {
    const message = (error instanceof Error ? error.message : String(error))
      .replace(/^Error invoking remote method '[^']+': Error:\s*/, "");
    appendLog(`操作失败: ${message}`);
    toast(message);
  }
}
function escapeHtml(value: string): string { return value.replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]!); }

void boot();
