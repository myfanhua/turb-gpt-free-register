import fs from "node:fs/promises";
import path from "node:path";
import type { Page } from "playwright-core";

export async function captureFlowSnapshot(
  page: Page,
  email: string,
  stage: string,
  log?: (message: string) => void,
): Promise<void> {
  try {
    const userData = process.env.REGISTRATION_DESK_USER_DATA?.trim() || path.join(process.env.APPDATA || process.cwd(), "registration-desk");
    const directory = path.join(userData, "flow-snapshots", new Date().toISOString().slice(0, 10), sanitizePathPart(email || "unknown"));
    await fs.mkdir(directory, { recursive: true });
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const stem = path.join(directory, `${stamp}-${sanitizePathPart(stage)}`);

    const closed = page.isClosed();
    const url = closed ? "[page closed]" : page.url();
    const title = closed ? "" : await page.title().catch(() => "");
    const body = closed ? "" : await page.locator("body").innerText({ timeout: 2500 }).catch(() => "");
    await fs.writeFile(`${stem}.txt`, `stage=${stage}\nurl=${url}\ntitle=${title}\n\n${body.slice(0, 12000)}`, "utf8");
    if (!closed) await page.screenshot({ path: `${stem}.png`, fullPage: false, timeout: 5000 }).catch(() => undefined);
    log?.(`Flow snapshot saved: ${stem}`);
  } catch (error) {
    log?.(`Flow snapshot failed at ${stage}: ${(error as Error).message}`);
  }
}

function sanitizePathPart(value: string): string {
  return String(value || "unknown")
    .replace(/[^a-zA-Z0-9@._-]+/g, "-")
    .replace(/-+/g, "-")
    .slice(0, 120) || "unknown";
}
