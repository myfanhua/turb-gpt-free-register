import type { Locator, Page } from "playwright-core";
import { clickAction, clickContinue, dismissWelcome, passwordInputs, readSession, sleep, visibleAll, visibleFirst } from "./auth-helpers.js";

interface Profile { name: string; birthdate: string; age: string }

export async function hasAboutYouForm(page: Page): Promise<boolean> {
  return page.evaluate(() => {
    const visible = (element: Element) => {
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
    };
    const controls = Array.from(document.querySelectorAll("input, textarea, [contenteditable=true], [role=spinbutton]")).filter(visible);
    const labelText = (element: Element) => {
      const id = element.getAttribute("id") || "";
      const byFor = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`)?.textContent || "" : "";
      const closest = element.closest("label, div, section, form")?.textContent || "";
      return `${byFor} ${closest}`;
    };
    const meta = controls.map((element) => `${element.getAttribute("name") || ""} ${element.getAttribute("id") || ""} ${element.getAttribute("placeholder") || ""} ${element.getAttribute("aria-label") || ""} ${labelText(element)}`).join(" ");
    return /full.?name|name|age|birth|姓名|全名|年龄|生日|生年月日|氏名/i.test(meta) && controls.length >= 2;
  }).catch(() => false);
}

export async function fillAboutYouAndSubmit(page: Page): Promise<void> {
  const profile = randomProfile();
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline && !await hasAboutYouForm(page)) await sleep(500);
  if (!await hasAboutYouForm(page)) throw new Error("基础资料页未显示姓名/年龄输入框");
  if (!await fillProfileValues(page, profile)) throw new Error("无法填写有效的姓名和出生日期");
  await sleep(600);
  if (!await ensureProfileValues(page, profile)) throw new Error("提交前基础资料值被清空且无法恢复");
  await submitProfile(page, profile);
}

async function fillProfileValues(page: Page, profile: Profile): Promise<boolean> {
  const controls = await visibleAll(page, ["input", "textarea", '[contenteditable="true"]', '[role="spinbutton"]']);
  const metadata = await Promise.all(controls.map(async (control) => ({
    control,
    meta: await control.evaluate((element) => {
      const input = element as HTMLInputElement;
      const label = input.id ? document.querySelector(`label[for="${CSS.escape(input.id)}"]`)?.textContent || "" : "";
      const container = input.closest("label, div, section, form")?.textContent || "";
      return `${input.type || ""} ${input.name || ""} ${input.id || ""} ${input.placeholder || ""} ${input.getAttribute("aria-label") || ""} ${label} ${container}`.toLowerCase();
    }).catch(() => ""),
  })));
  const name = metadata.find((item) => /full.?name|(^|\s)name(\s|$)|姓名|全名|氏名|お名前/i.test(item.meta))?.control ?? controls[0];
  if (name) await forceFill(name, profile.name);

  const dateInput = metadata.find((item) => /type.?date|birth.?date|birthday|出生日期|生日|生年月日/i.test(item.meta))?.control;
  if (dateInput) {
    await forceFill(dateInput, profile.birthdate);
    if ((await dateInput.inputValue().catch(() => "")).includes(profile.birthdate.slice(0, 4))) return true;
  }

  const [year, month, day] = profile.birthdate.split("-");
  const yearInput = metadata.find((item) => /year|yyyy|出生年|年/i.test(item.meta) && item.control !== name)?.control;
  const monthInput = metadata.find((item) => /month|mm|月份|月/i.test(item.meta) && item.control !== name)?.control;
  const dayInput = metadata.find((item) => /day|dd|日期|日/i.test(item.meta) && item.control !== name)?.control;
  if (yearInput && monthInput && dayInput) {
    await forceFill(yearInput, year);
    await forceFill(monthInput, month);
    await forceFill(dayInput, day);
    return true;
  }

  const ageInput = metadata.find((item) => /(^|\s)age(\s|$)|年龄|年齢/i.test(item.meta) && item.control !== name)?.control;
  if (ageInput) { await forceFill(ageInput, profile.age); return true; }

  const remaining = controls.filter((control) => control !== name);
  if (remaining.length >= 3 && remaining.every((control) => control !== name)) {
    await forceFill(remaining[0], month);
    await forceFill(remaining[1], day);
    await forceFill(remaining[2], year);
    return true;
  }
  if (remaining.length) { await forceFill(remaining[0], profile.age); return true; }
  return false;
}

async function forceFill(locator: Locator, value: string): Promise<void> {
  await locator.scrollIntoViewIfNeeded().catch(() => undefined);
  await locator.click({ timeout: 1500 }).catch(() => undefined);
  await locator.fill(value, { timeout: 1500 }).catch(() => undefined);
  if (await locator.inputValue({ timeout: 500 }).catch(() => "") !== value) {
    await locator.press(process.platform === "darwin" ? "Meta+A" : "Control+A").catch(() => undefined);
    await locator.press("Backspace").catch(() => undefined);
    await locator.pressSequentially(value, { delay: 5 }).catch(() => undefined);
  }
  await locator.evaluate((element, value) => {
    const input = element as HTMLInputElement | HTMLTextAreaElement;
    if (input.value !== value) {
      const setter = Object.getOwnPropertyDescriptor(input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype, "value")?.set;
      setter?.call(input, value);
    }
    try { input.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value })); } catch { input.dispatchEvent(new Event("input", { bubbles: true })); }
    input.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true, key: value.at(-1) || "" }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    input.dispatchEvent(new Event("blur", { bubbles: true }));
  }, value).catch(() => undefined);
}

async function ensureProfileValues(page: Page, profile: Profile): Promise<boolean> {
  if (await profileValuesPresent(page, profile)) return true;
  return fillProfileValues(page, profile);
}

async function profileValuesPresent(page: Page, profile: Profile): Promise<boolean> {
  const values = await page.locator("input:visible, textarea:visible").evaluateAll((elements) => elements.map((element) => (element as HTMLInputElement).value)).catch(() => [] as string[]);
  const hasName = values.some((value) => value.trim() === profile.name);
  const hasAdultValue = values.some((value) => value.includes(profile.birthdate.slice(0, 4)) || value === profile.age)
    || values.some((value) => /^(0?[1-9]|1[0-2])$/.test(value)) && values.some((value) => /^(19|20)\d{2}$/.test(value));
  return hasName && hasAdultValue;
}

async function submitProfile(page: Page, profile: Profile): Promise<void> {
  const beforeUrl = page.url();
  let attempts = 0;
  let lastClick = 0;
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    if (await readSession(page, page.context())) return;
    if (await passwordInputs(page).then((items) => items.length > 0)) return;
    if (await dismissWelcome(page)) return;
    if (!await hasAboutYouForm(page) || (page.url() !== beforeUrl && !page.url().includes("about-you"))) return;
    if (!await profileValuesPresent(page, profile)) {
      await ensureProfileValues(page, profile);
      await sleep(100);
      continue;
    }
    const pending = await page.evaluate(() => Array.from(document.querySelectorAll('button, [role="button"]')).some((element) => {
      const button = element as HTMLButtonElement;
      const rect = button.getBoundingClientRect();
      const visible = rect.width > 0 && rect.height > 0;
      const text = (button.textContent || "").trim();
      const action = /finish creating account|create account|continue|创建账户|完成|继续|続行|完了|アカウント/i.test(text) || button.type === "submit";
      return visible && action && (button.getAttribute("aria-busy") === "true" || button.getAttribute("data-loading") === "true");
    })).catch(() => false);
    if (pending) { await sleep(300); continue; }
    if (attempts < 6 && Date.now() - lastClick >= 800) {
      const clicked = await clickAction(page, /finish creating account|create account|continue|创建账户|完成|继续|続行|次へ|完了|作成|アカウント/i)
        || await clickContinue(page)
        || await clickSubmitByDom(page);
      if (clicked) { attempts += 1; lastClick = Date.now(); }
    }
    await sleep(250);
  }
  throw new Error("基础资料提交超时，页面未进入可识别的下一步");
}

async function clickSubmitByDom(page: Page): Promise<boolean> {
  return page.evaluate(() => {
    const visible = (element: Element) => {
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
    };
    const buttons = Array.from(document.querySelectorAll<HTMLElement>('button, [role="button"], input[type="submit"], input[type="button"]')).filter((element) => visible(element) && !(element as HTMLButtonElement).disabled && element.getAttribute("aria-disabled") !== "true");
    const target = buttons.find((button) => (button as HTMLButtonElement).type === "submit") || buttons.find((button) => /finish|create|continue|完成|创建|继续|続行|完了|作成|アカウント/i.test(`${button.textContent || ""} ${(button as HTMLInputElement).value || ""}`));
    if (!target) return false;
    target.scrollIntoView({ block: "center" });
    const form = target.closest("form");
    if (form?.requestSubmit && target instanceof HTMLButtonElement) form.requestSubmit(target);
    else {
      for (const name of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
        target.dispatchEvent(new MouseEvent(name, { bubbles: true, cancelable: true, view: window }));
      }
      target.click();
    }
    return true;
  }).catch(() => false);
}

function randomProfile(): Profile {
  const first = ["Alex", "Daniel", "Emma", "Helen", "James", "Linda", "Michael", "Nora"];
  const last = ["Brown", "Davis", "Johnson", "Miller", "Smith", "Taylor", "Wilson", "Young"];
  const year = 1988 + Math.floor(Math.random() * 12);
  const month = 1 + Math.floor(Math.random() * 12);
  const day = 1 + Math.floor(Math.random() * 27);
  return {
    name: `${first[Math.floor(Math.random() * first.length)]} ${last[Math.floor(Math.random() * last.length)]}`,
    birthdate: `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`,
    age: String(new Date().getUTCFullYear() - year),
  };
}
