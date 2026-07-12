import { chromium, type Browser, type BrowserContext } from "playwright-core";
import { randomInt } from "node:crypto";
import { existsSync, readdirSync } from "node:fs";
import path from "node:path";
import type { Settings } from "../shared/types.js";
import type { ProxySettings } from "./proxy.js";

export interface BrowserBundle {
  browser: Browser;
  context: BrowserContext;
  fingerprint: DeviceFingerprint;
}

interface DeviceFingerprint {
  userAgent: string;
  locale: string;
  languages: string[];
  timezone: string;
  viewportWidth: number;
  viewportHeight: number;
  screenWidth: number;
  screenHeight: number;
  outerWidth: number;
  outerHeight: number;
  deviceScaleFactor: number;
  hardwareConcurrency: number;
  deviceMemory: number;
  platform: string;
  vendor: string;
  maxTouchPoints: number;
  chromeMajor: string;
  chromeFull: string;
  acceptLanguage: string;
}

export async function launchConfiguredChromium(settings: Settings, proxy: ProxySettings | undefined): Promise<BrowserBundle> {
  const fingerprint = generateRegisterFingerprint();
  const browser = await chromium.launch({
    executablePath: resolveChromiumExecutablePath(),
    headless: settings.headless,
    args: [
      "--disable-blink-features=AutomationControlled",
      `--lang=${fingerprint.locale}`,
      `--window-size=${fingerprint.outerWidth},${fingerprint.outerHeight}`,
      "--disable-features=IsolateOrigins,site-per-process",
    ],
    proxy,
  });
  const context = await browser.newContext({
    userAgent: fingerprint.userAgent,
    locale: fingerprint.locale,
    timezoneId: fingerprint.timezone,
    viewport: { width: fingerprint.viewportWidth, height: fingerprint.viewportHeight },
    screen: { width: fingerprint.screenWidth, height: fingerprint.screenHeight },
    deviceScaleFactor: fingerprint.deviceScaleFactor,
    isMobile: false,
    hasTouch: false,
  });
  await installFingerprint(context, fingerprint);
  await installWelcomeAutoDismiss(context);
  return { browser, context, fingerprint };
}

function resolveChromiumExecutablePath(): string | undefined {
  const explicit = process.env.REGISTRATION_DESK_CHROMIUM_PATH;
  if (explicit && existsSync(explicit)) return explicit;
  const playwrightPath = chromium.executablePath();
  if (existsSync(playwrightPath)) return playwrightPath;
  const localAppData = process.env.LOCALAPPDATA || "";
  const msPlaywright = path.join(localAppData, "ms-playwright");
  if (existsSync(msPlaywright)) {
    const candidates = readdirSync(msPlaywright, { withFileTypes: true })
      .filter((entry) => entry.isDirectory() && /^chromium-\d+$/i.test(entry.name))
      .map((entry) => ({
        revision: Number(entry.name.replace(/\D+/g, "")),
        executable: path.join(msPlaywright, entry.name, "chrome-win64", "chrome.exe"),
      }))
      .filter((item) => existsSync(item.executable))
      .sort((a, b) => b.revision - a.revision);
    if (candidates[0]) return candidates[0].executable;
  }
  const chromeCandidates = [
    path.join(localAppData, "Google", "Chrome", "Application", "chrome.exe"),
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  ];
  return chromeCandidates.find((candidate) => existsSync(candidate));
}

function generateRegisterFingerprint(): DeviceFingerprint {
  const profile = { locale: "ja-JP", languages: ["ja-JP", "ja"], timezone: "Asia/Tokyo" };
  const viewports = [
    [1280, 720, 1280, 720, 1],
    [1365, 768, 1366, 768, 1],
    [1440, 900, 1440, 900, 1],
    [1536, 864, 1536, 864, 1.25],
    [1600, 900, 1600, 900, 1],
    [1920, 1080, 1920, 1080, 1],
  ] as const;
  const viewport = choice(viewports);
  const major = randomInt(134, 147);
  const build = randomInt(6000, 10_000);
  const patch = randomInt(50, 221);
  const chromeFull = `${major}.0.${build}.${patch}`;
  const languages = [...profile.languages];
  return {
    userAgent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${chromeFull} Safari/537.36`,
    locale: profile.locale,
    languages,
    timezone: profile.timezone,
    viewportWidth: viewport[0],
    viewportHeight: viewport[1],
    screenWidth: viewport[2],
    screenHeight: viewport[3],
    outerWidth: viewport[0] + randomInt(8, 17),
    outerHeight: viewport[1] + randomInt(72, 97),
    deviceScaleFactor: viewport[4],
    hardwareConcurrency: choice([4, 6, 8, 8, 12, 16]),
    deviceMemory: choice([4, 8, 8, 16]),
    platform: "Win32",
    vendor: "Google Inc.",
    maxTouchPoints: 0,
    chromeMajor: String(major),
    chromeFull,
    acceptLanguage: acceptLanguage(languages),
  };
}

function choice<T>(values: readonly T[]): T {
  return values[randomInt(0, values.length)];
}

function acceptLanguage(languages: string[]): string {
  if (!languages.length) return "ja-JP";
  return [languages[0], ...languages.slice(1).map((language, index) => `${language};q=${Math.max(0.5, 0.9 - index * 0.1).toFixed(1)}`)].join(",");
}

async function installFingerprint(context: BrowserContext, fingerprint: DeviceFingerprint): Promise<void> {
  await context.setExtraHTTPHeaders({
    "Accept-Language": fingerprint.acceptLanguage,
    "sec-ch-ua": `"Google Chrome";v="${fingerprint.chromeMajor}", "Chromium";v="${fingerprint.chromeMajor}", "Not.A/Brand";v="24"`,
    "sec-ch-ua-full-version-list": `"Google Chrome";v="${fingerprint.chromeFull}", "Chromium";v="${fingerprint.chromeFull}", "Not.A/Brand";v="24.0.0.0"`,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"15.0.0"',
  });
  await context.addInitScript((fp) => {
    const defineGetter = (obj: object, prop: string, value: unknown) => {
      try { Object.defineProperty(obj, prop, { get: () => value, configurable: true }); } catch {}
    };
    defineGetter(Navigator.prototype, "webdriver", undefined);
    defineGetter(Navigator.prototype, "platform", fp.platform);
    defineGetter(Navigator.prototype, "vendor", fp.vendor);
    defineGetter(Navigator.prototype, "language", fp.languages[0]);
    defineGetter(Navigator.prototype, "languages", fp.languages);
    defineGetter(Navigator.prototype, "hardwareConcurrency", fp.hardwareConcurrency);
    defineGetter(Navigator.prototype, "deviceMemory", fp.deviceMemory);
    defineGetter(Navigator.prototype, "maxTouchPoints", fp.maxTouchPoints);
    defineGetter(Screen.prototype, "width", fp.screenWidth);
    defineGetter(Screen.prototype, "height", fp.screenHeight);
    defineGetter(Screen.prototype, "availWidth", fp.screenWidth);
    defineGetter(Screen.prototype, "availHeight", fp.screenHeight - 40);
    defineGetter(window, "outerWidth", fp.outerWidth);
    defineGetter(window, "outerHeight", fp.outerHeight);
    defineGetter(window, "devicePixelRatio", fp.deviceScaleFactor);
    const nav = navigator as Navigator & { userAgentData?: unknown };
    if (!nav.userAgentData) {
      defineGetter(Navigator.prototype, "userAgentData", {
        mobile: false,
        platform: "Windows",
        brands: [
          { brand: "Google Chrome", version: fp.chromeMajor },
          { brand: "Chromium", version: fp.chromeMajor },
          { brand: "Not.A/Brand", version: "24" },
        ],
        getHighEntropyValues: async (hints: string[]) => {
          const values: Record<string, unknown> = {
            architecture: "x86",
            bitness: "64",
            mobile: false,
            model: "",
            platform: "Windows",
            platformVersion: "15.0.0",
            uaFullVersion: fp.chromeFull,
            fullVersionList: [
              { brand: "Google Chrome", version: fp.chromeFull },
              { brand: "Chromium", version: fp.chromeFull },
              { brand: "Not.A/Brand", version: "24.0.0.0" },
            ],
            wow64: false,
          };
          return Object.fromEntries(hints.filter((hint) => hint in values).map((hint) => [hint, values[hint]]));
        },
      });
    }
    try {
      const originalQuery = navigator.permissions?.query;
      if (originalQuery) {
        navigator.permissions.query = (params) => params?.name === "notifications"
          ? Promise.resolve({ state: Notification.permission } as PermissionStatus)
          : originalQuery.call(navigator.permissions, params);
      }
    } catch {}
    try {
      const getParameter = WebGLRenderingContext.prototype.getParameter;
      WebGLRenderingContext.prototype.getParameter = function(parameter: number) {
        if (parameter === 37445) return "Intel Inc.";
        if (parameter === 37446) return "Intel Iris OpenGL Engine";
        return getParameter.call(this, parameter);
      };
    } catch {}
  }, fingerprint);
}

async function installWelcomeAutoDismiss(context: BrowserContext): Promise<void> {
  await context.addInitScript(() => {
    if ((window as Window & { __oaiWelcomeAutoDismissInstalled?: boolean }).__oaiWelcomeAutoDismissInstalled) return;
    (window as Window & { __oaiWelcomeAutoDismissInstalled?: boolean }).__oaiWelcomeAutoDismissInstalled = true;

    const visible = (element: Element | null) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
    };
    const welcomePattern = /you're all set|ready to go|welcome to chatgpt|准备已完成|一切准备就绪|准备好了|準備が完了しました|準備ができました/i;
    const actionPattern = /^(continue|继续|継続|続行)$/i;
    const dismiss = () => {
      const welcomeVisible = Array.from(document.querySelectorAll("h1, h2, [role='heading']"))
        .filter(visible)
        .some((element) => welcomePattern.test((element.textContent || "").replace(/\s+/g, " ").trim()));
      if (!welcomeVisible) return false;
      const target = Array.from(document.querySelectorAll<HTMLElement>("button, a, [role='button']"))
        .filter((element) => visible(element) && !element.hasAttribute("disabled") && element.getAttribute("aria-disabled") !== "true")
        .find((element) => actionPattern.test((element.textContent || element.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim()));
      if (!target) return false;
      target.click();
      const state = window as Window & { __oaiWelcomeAutoDismissCount?: number };
      state.__oaiWelcomeAutoDismissCount = (state.__oaiWelcomeAutoDismissCount || 0) + 1;
      return true;
    };
    const install = () => {
      if (!document.documentElement) {
        setTimeout(install, 50);
        return;
      }
      let scheduled = false;
      const schedule = () => {
        if (scheduled) return;
        scheduled = true;
        setTimeout(() => {
          scheduled = false;
          dismiss();
        }, 0);
      };
      new MutationObserver(schedule).observe(document.documentElement, {
        childList: true,
        subtree: true,
        characterData: true,
      });
      setInterval(dismiss, 200);
      schedule();
    };
    install();
  });
}
