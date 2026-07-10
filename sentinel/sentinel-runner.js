#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const crypto = require("node:crypto");
const { performance } = require("node:perf_hooks");

function readArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i++) {
    const item = argv[i];
    if (!item.startsWith("--")) continue;
    const key = item.slice(2);
    const next = argv[i + 1];
    if (!next || next.startsWith("--")) {
      args[key] = "1";
      continue;
    }
    args[key] = next;
    i++;
  }
  return args;
}

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

function parseJson(text, source) {
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new Error(`${source} 不是合法 JSON：${error.message}`);
  }
}

function pick(...values) {
  for (const value of values) {
    if (value !== undefined && value !== null && value !== "") return value;
  }
  return "";
}

function truthy(value) {
  return value === true || value === "1" || value === "true" || value === "yes";
}

function readConfig(args) {
  const explicitPath = args.config || process.env.SENTINEL_CONFIG;
  const candidates = explicitPath
    ? [path.resolve(explicitPath)]
    : [
        path.resolve(process.cwd(), "sentinel.config.json"),
        path.resolve(process.cwd(), "tools", "sentinel.config.json"),
        path.resolve(__dirname, "sentinel.config.json"),
        path.resolve(__dirname, "..", "sentinel.config.json"),
      ];

  for (const filePath of candidates) {
    if (!fs.existsSync(filePath)) continue;
    return {
      path: filePath,
      data: parseJson(fs.readFileSync(filePath, "utf8"), filePath),
    };
  }

  return { path: null, data: {} };
}

function configGetter(config) {
  return (...keys) => {
    for (const key of keys) {
      if (config[key] !== undefined && config[key] !== null && config[key] !== "") {
        return config[key];
      }
    }
    return "";
  };
}

function normalizeList(value, fallback) {
  const source = Array.isArray(value) ? value.join(",") : pick(value, fallback);
  return String(source)
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function xorDecode(text, key) {
  let output = "";
  const decoded = atobBinary(text);
  for (let i = 0; i < decoded.length; i++) {
    output += String.fromCharCode(decoded.charCodeAt(i) ^ key.charCodeAt(i % key.length));
  }
  return output;
}

function decodeDx(dx, proof) {
  return JSON.parse(xorDecode(dx, proof));
}

function normalizeChallenge(raw) {
  if (typeof raw === "string") {
    const trimmed = raw.trim();
    if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return trimmed;
    raw = parseJson(trimmed, "challenge 字符串");
  }

  const candidates = [
    raw?.cachedChatReq,
    raw?.result?.cachedChatReq,
    raw?.data?.cachedChatReq,
    raw?.data,
    raw,
  ];

  for (const candidate of candidates) {
    if (!candidate || typeof candidate !== "object") continue;
    if (candidate.proofofwork || candidate.token || candidate.turnstile || candidate.so) {
      return candidate;
    }
  }

  throw new Error("challenge 缺少 cachedChatReq/proofofwork/token 字段，无法喂给 SDK");
}

function readChallengeFile(filePath) {
  const absolutePath = path.resolve(filePath);
  const raw = fs.readFileSync(absolutePath, "utf8");
  return normalizeChallenge(parseJson(raw, absolutePath));
}

const OFFICIAL_CHALLENGE_URL = "https://chatgpt.com/backend-api/sentinel/req";

function headerMapFromEnv(options = {}) {
  const headers = {
    accept: "*/*",
    "content-type":
      options.contentType ||
      (options.ignoreEnv ? "" : process.env.SENTINEL_CONTENT_TYPE) ||
      "text/plain;charset=UTF-8",
  };
  const cookie =
    options.cookie ||
    (options.ignoreEnv ? "" : process.env.SENTINEL_COOKIE || process.env.CHATGPT_COOKIE);
  const authorization =
    options.bearer ||
    (options.ignoreEnv ? "" : process.env.SENTINEL_AUTHORIZATION || process.env.CHATGPT_BEARER_TOKEN);
  const userAgent = options.userAgent || (options.ignoreEnv ? "" : process.env.SENTINEL_USER_AGENT);

  if (cookie) headers.cookie = cookie;
  if (authorization) {
    headers.authorization = authorization.toLowerCase().startsWith("bearer ")
      ? authorization
      : `Bearer ${authorization}`;
  }
  if (userAgent) {
    headers["user-agent"] = userAgent;
  }
  if (options.pageUrl) headers.referer = options.pageUrl;
  if (options.origin) headers.origin = options.origin;
  if (options.deviceId) headers["oai-device-id"] = options.deviceId;
  if (process.env.SENTINEL_HEADERS_JSON) {
    Object.assign(headers, parseJson(process.env.SENTINEL_HEADERS_JSON, "SENTINEL_HEADERS_JSON"));
  }
  return headers;
}

function assertAllowedChallengeHost(challengeUrl, officialMode) {
  const host = new URL(challengeUrl).hostname.toLowerCase();
  const allowed = (process.env.SENTINEL_ALLOW_HOST || "")
    .split(",")
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean);

  if ((host === "chatgpt.com" || host.endsWith(".chatgpt.com")) && !officialMode && !allowed.includes(host)) {
    throw new Error(
      "为避免误打真实生产接口，默认不请求 chatgpt.com。若这是比赛授权接口，请使用 --official 或设置 SENTINEL_ALLOW_HOST=chatgpt.com。"
    );
  }
}

async function fetchChallenge(challengeUrl, flow, proof, deviceId, options = {}) {
  assertAllowedChallengeHost(challengeUrl, options.officialMode);
  const hasCookie = Boolean(
    options.cookie || (options.ignoreEnv ? "" : process.env.SENTINEL_COOKIE || process.env.CHATGPT_COOKIE)
  );
  const hasBearer = Boolean(
    options.bearer ||
      (options.ignoreEnv ? "" : process.env.SENTINEL_AUTHORIZATION || process.env.CHATGPT_BEARER_TOKEN)
  );
  if (options.officialMode && !hasCookie && !hasBearer) {
    throw new Error("官方接口模式至少需要 Cookie 或 Bearer；请传 --cookie 或 --bearer。");
  }
  const body = JSON.stringify({ p: proof, id: deviceId, flow });
  const response = await fetch(challengeUrl, {
    method: "POST",
    headers: headerMapFromEnv({
      pageUrl: options.pageUrl,
      origin: new URL(challengeUrl).origin,
      userAgent: options.userAgent,
      deviceId,
      cookie: options.cookie,
      bearer: options.bearer,
      contentType: options.contentType,
      ignoreEnv: options.ignoreEnv,
    }),
    body,
  });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(`challenge API 返回 HTTP ${response.status}：${text.slice(0, 300)}`);
  }
  return normalizeChallenge(text);
}

function createEventTarget() {
  const listeners = new Map();
  return {
    addEventListener(type, listener) {
      const bucket = listeners.get(type) || [];
      bucket.push(listener);
      listeners.set(type, bucket);
    },
    removeEventListener(type, listener) {
      const bucket = listeners.get(type) || [];
      listeners.set(
        type,
        bucket.filter((item) => item !== listener)
      );
    },
    dispatchEvent(event) {
      const bucket = listeners.get(event.type) || [];
      for (const listener of [...bucket]) listener.call(this, event);
    },
  };
}

function btoaBinary(value) {
  return Buffer.from(String(value), "binary").toString("base64");
}

function atobBinary(value) {
  return Buffer.from(String(value), "base64").toString("binary");
}

function createStorage() {
  const values = new Map();
  return {
    get length() {
      return values.size;
    },
    key(index) {
      return [...values.keys()][Number(index)] ?? null;
    },
    getItem(key) {
      const name = String(key);
      return values.has(name) ? values.get(name) : null;
    },
    setItem(key, value) {
      values.set(String(key), String(value));
    },
    removeItem(key) {
      values.delete(String(key));
    },
    clear() {
      values.clear();
    },
  };
}

function createDomRect(width = 0, height = 0) {
  return {
    x: 0,
    y: 0,
    width,
    height,
    top: 0,
    left: 0,
    right: width,
    bottom: height,
    toJSON() {
      return {
        x: this.x,
        y: this.y,
        width: this.width,
        height: this.height,
        top: this.top,
        left: this.left,
        right: this.right,
        bottom: this.bottom,
      };
    },
  };
}


function makeNativeFunction(name, impl = () => undefined) {
  const fn = function (...args) { return impl.apply(this, args); };
  Object.defineProperty(fn, "name", { value: name });
  Object.defineProperty(fn, "toString", { value: () => `function ${name}() { [native code] }` });
  return fn;
}

function createPluginArray(isSafari = false) {
  const makePlugin = (name) => ({
    name,
    filename: "internal-pdf-viewer",
    description: "Portable Document Format",
    length: 2,
    item(index) { return this[index] || null; },
    namedItem(type) { return this[type] || null; },
  });
  const pdf = { type: "application/pdf", suffixes: "pdf", description: "Portable Document Format", enabledPlugin: null };
  const textPdf = { type: "text/pdf", suffixes: "pdf", description: "Portable Document Format", enabledPlugin: null };
  const plugins = isSafari ? [
    makePlugin("WebKit built-in PDF"),
    makePlugin("PDF Viewer"),
  ] : [
    makePlugin("PDF Viewer"),
    makePlugin("Chrome PDF Viewer"),
    makePlugin("Chromium PDF Viewer"),
    makePlugin("Microsoft Edge PDF Viewer"),
    makePlugin("WebKit built-in PDF"),
  ];
  for (const plugin of plugins) {
    plugin[0] = pdf;
    plugin[1] = textPdf;
    plugin["application/pdf"] = pdf;
    plugin["text/pdf"] = textPdf;
  }
  pdf.enabledPlugin = plugins[0];
  textPdf.enabledPlugin = plugins[0];
  plugins.item = (index) => plugins[index] || null;
  plugins.namedItem = (name) => plugins.find((p) => p.name === name) || null;
  plugins.refresh = () => undefined;
  Object.defineProperty(plugins, Symbol.toStringTag, { value: "PluginArray" });
  return plugins;
}

function createMimeTypeArray() {
  const plugin = { name: "PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format" };
  const mimes = [
    { type: "application/pdf", suffixes: "pdf", description: "Portable Document Format", enabledPlugin: plugin },
    { type: "text/pdf", suffixes: "pdf", description: "Portable Document Format", enabledPlugin: plugin },
  ];
  mimes.item = (index) => mimes[index] || null;
  mimes.namedItem = (type) => mimes.find((m) => m.type === type) || null;
  Object.defineProperty(mimes, Symbol.toStringTag, { value: "MimeTypeArray" });
  return mimes;
}

function createCanvas(width = 300, height = 150, isSafari = false) {
  const canvas = {
    tagName: "CANVAS",
    style: {},
    width,
    height,
    parentNode: null,
    getBoundingClientRect() { return createDomRect(this.width, this.height); },
    toDataURL() { return "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lxv8dQAAAABJRU5ErkJggg=="; },
    getContext(type) {
      const name = String(type || "").toLowerCase();
      if (name === "2d") {
        return {
          canvas,
          fillStyle: "#000000",
          strokeStyle: "#000000",
          font: "10px sans-serif",
          fillRect() {}, clearRect() {}, strokeRect() {}, beginPath() {}, closePath() {}, moveTo() {}, lineTo() {}, stroke() {}, fill() {},
          fillText() {}, strokeText() {}, measureText(text) { return { width: String(text || "").length * 6.5 }; },
          getImageData() { return { data: new Uint8ClampedArray(canvas.width * canvas.height * 4), width: canvas.width, height: canvas.height }; },
          putImageData() {}, createImageData(w, h) { return { data: new Uint8ClampedArray(w * h * 4), width: w, height: h }; },
        };
      }
      if (name === "webgl" || name === "experimental-webgl" || name === "webgl2") {
        return {
          canvas,
          getParameter(param) {
            const values = new Map([
              [0x1f00, "WebKit"],                    // VENDOR
              [0x1f01, "WebKit WebGL"],              // RENDERER
              [0x1f02, isSafari ? "WebGL 2.0" : "WebGL 2.0 (OpenGL ES 3.0 Chromium)"],
              [0x8b8c, isSafari ? "WebGL GLSL ES 1.0" : "WebGL GLSL ES 3.00 (OpenGL ES GLSL ES 3.0 Chromium)"],
              [0x0d33, 16384],                       // MAX_TEXTURE_SIZE
              [0x8869, 16],                          // MAX_VERTEX_ATTRIBS
            ]);
            return values.has(param) ? values.get(param) : 0;
          },
          getExtension(name) {
            if (name === "WEBGL_debug_renderer_info") {
              return { UNMASKED_VENDOR_WEBGL: 0x9245, UNMASKED_RENDERER_WEBGL: 0x9246 };
            }
            return {};
          },
          getSupportedExtensions() { return ["ANGLE_instanced_arrays", "EXT_blend_minmax", "WEBGL_debug_renderer_info", "WEBGL_lose_context"]; },
          clearColor() {}, clear() {}, viewport() {}, createBuffer() { return {}; }, bindBuffer() {}, bufferData() {},
        };
      }
      return null;
    },
    addEventListener() {}, removeEventListener() {},
  };
  return canvas;
}

function createAudioContext() {
  return class AudioContextMock {
    constructor() { this.sampleRate = 48000; this.state = "running"; this.destination = {}; }
    createOscillator() { return { type: "sine", frequency: { value: 440 }, connect() {}, start() {}, stop() {} }; }
    createAnalyser() { return { fftSize: 2048, frequencyBinCount: 1024, getFloatFrequencyData() {}, getByteFrequencyData() {} }; }
    createGain() { return { gain: { value: 1 }, connect() {} }; }
    close() { this.state = "closed"; return Promise.resolve(); }
    resume() { this.state = "running"; return Promise.resolve(); }
    suspend() { this.state = "suspended"; return Promise.resolve(); }
  };
}

function createBrowserContext(options) {
  const windowTarget = createEventTarget();
  const managedTimers = new Set();
  const managedSetTimeout = (callback, delay, ...args) => {
    const id = setTimeout(() => {
      managedTimers.delete(id);
      callback(...args);
    }, delay);
    managedTimers.add(id);
    return id;
  };
  const managedClearTimeout = (id) => {
    managedTimers.delete(id);
    clearTimeout(id);
  };
  const forcedRandomUUID = options.sentinelSid || "";
  let randomUUIDCalls = 0;
  const browserCrypto = Object.create(crypto.webcrypto);
  browserCrypto.randomUUID = () => {
    randomUUIDCalls += 1;
    // 当前 sdk.js 的第一次 UUID 调用用于 Sentinel 内部 sid。
    // 只固定这一次，避免后续 UUID 全部重复。
    if (forcedRandomUUID && randomUUIDCalls === 1) return forcedRandomUUID;
    return crypto.randomUUID();
  };
  browserCrypto.getRandomValues = crypto.webcrypto.getRandomValues.bind(crypto.webcrypto);

  const browserPerformance = {
    now: () => performance.now(),
    timeOrigin: performance.timeOrigin || Date.now() - performance.now(),
    memory: {
      jsHeapSizeLimit: options.jsHeapSizeLimit,
    },
  };
  const mathObject = Object.create(Math);
  if (Number.isFinite(options.fixedRandom)) {
    mathObject.random = () => options.fixedRandom;
  }
  const currentScript = { src: options.scriptSrc, length: options.scriptSrc.length };
  const appScriptSrc = `https://chatgpt.com/${options.buildId}ssg.js`;
  const scripts = [
    currentScript,
    { src: appScriptSrc, length: appScriptSrc.length },
    { src: "https://chatgpt.com/_next/static/chunks/webpack.js", length: 48 },
    { src: "https://js.stripe.com/v3/", length: 24 },
  ];
  const attrs = new Map([["data-build", options.buildId]]);
  const reactListeningKey = options.reactListeningKey || "_reactListening" + crypto.randomBytes(6).toString("hex");

  let iframeNode = null;
  const bodyChildren = [];
  const document = {
    currentScript,
    scripts,
    cookie: options.cookie,
    URL: options.pageUrl,
    documentURI: options.pageUrl,
    referrer: "https://auth.openai.com/",
    title: "",
    characterSet: "UTF-8",
    charset: "UTF-8",
    compatMode: "CSS1Compat",
    contentType: "text/html",
    readyState: "complete",
    visibilityState: "visible",
    hidden: false,
    hasFocus() { return true; },
    [reactListeningKey]: true,
    documentElement: {
      style: {},
      clientWidth: options.screen.width,
      clientHeight: options.screen.height,
      scrollWidth: options.screen.width,
      scrollHeight: options.screen.height,
      getAttribute(name) {
        return attrs.get(name) ?? null;
      },
      setAttribute(name, value) {
        attrs.set(name, String(value));
      },
      getBoundingClientRect() {
        return createDomRect(options.screen.width, options.screen.height);
      },
    },
    body: {
      style: {},
      clientWidth: options.screen.width,
      clientHeight: options.screen.height,
      getBoundingClientRect() {
        return createDomRect(options.screen.width, options.screen.height);
      },
      appendChild(node) {
        bodyChildren.push(node);
        node.parentNode = document.body;
        if (node?.tagName === "IFRAME") iframeNode = node;
        managedSetTimeout(() => node?._emitLoad?.(), 0);
        return node;
      },
      removeChild(node) {
        const index = bodyChildren.indexOf(node);
        if (index >= 0) bodyChildren.splice(index, 1);
        if (iframeNode === node) iframeNode = null;
        if (node) node.parentNode = null;
        return node;
      },
    },
    addEventListener() {},
    removeEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    getElementById() { return null; },
    getElementsByTagName(name) { return String(name).toLowerCase() === "script" ? scripts : []; },
    createElement(tagName) {
      const lowerTag = String(tagName).toLowerCase();
      if (lowerTag === "canvas") return createCanvas(300, 150, isSafari);
      if (lowerTag !== "iframe") {
        const children = [];
        const element = {
          tagName: String(tagName).toUpperCase(),
          style: {},
          parentNode: null,
          children,
          appendChild(node) {
            children.push(node);
            node.parentNode = element;
            return node;
          },
          removeChild(node) {
            const index = children.indexOf(node);
            if (index >= 0) children.splice(index, 1);
            if (node) node.parentNode = null;
            return node;
          },
          addEventListener() {},
          removeEventListener() {},
          getBoundingClientRect() {
            return createDomRect();
          },
        };
        return element;
      }

      const target = createEventTarget();
      const iframe = {
        tagName: "IFRAME",
        style: {},
        src: "",
        getBoundingClientRect() {
          return createDomRect();
        },
        contentWindow: {
          postMessage(message, origin) {
            Promise.resolve()
              .then(async () => {
                const result = await options.handleIframeMessage(message);
                windowTarget.dispatchEvent({
                  type: "message",
                  source: iframe.contentWindow,
                  origin,
                  data: {
                    type: "response",
                    requestId: message.requestId,
                    result,
                  },
                });
              })
              .catch((error) => {
                windowTarget.dispatchEvent({
                  type: "message",
                  source: iframe.contentWindow,
                  origin,
                  data: {
                    type: "response",
                    requestId: message.requestId,
                    error: error?.message || String(error),
                  },
                });
              });
          },
        },
        addEventListener: target.addEventListener,
        removeEventListener: target.removeEventListener,
        _emitLoad() {
          target.dispatchEvent.call(iframe, { type: "load", target: iframe });
        },
      };
      return iframe;
    },
  };

  const location = new URL(options.pageUrl);
  const browserFamily = String(options.browserFamily || "chrome").toLowerCase();
  const isSafari = browserFamily === "safari" || /Version\/[^ ]+ Safari\//.test(String(options.userAgent || ""));
  const exposeRequestIdleCallback = !isSafari && options.requestIdleCallback !== false;
  const navigatorProto = isSafari ? {
    javaEnabled: makeNativeFunction("javaEnabled", () => false),
    sendBeacon: makeNativeFunction("sendBeacon", () => true),
    getGamepads: makeNativeFunction("getGamepads", () => []),
    webkitGetUserMedia: makeNativeFunction("webkitGetUserMedia"),
  } : {
    clearOriginJoinedAdInterestGroups: makeNativeFunction("clearOriginJoinedAdInterestGroups"),
    canLoadAdAuctionFencedFrame: makeNativeFunction("canLoadAdAuctionFencedFrame", () => false),
    getBattery: makeNativeFunction("getBattery", () => Promise.resolve({ charging: true, chargingTime: 0, dischargingTime: Infinity, level: 1 })),
    getGamepads: makeNativeFunction("getGamepads", () => []),
    javaEnabled: makeNativeFunction("javaEnabled", () => false),
    sendBeacon: makeNativeFunction("sendBeacon", () => true),
    vibrate: makeNativeFunction("vibrate", () => false),
  };
  const navigator = Object.create(navigatorProto);
  Object.assign(navigator, {
    userAgent: options.userAgent,
    language: options.language,
    languages: options.languages,
    cookieEnabled: true,
    onLine: true,
    pdfViewerEnabled: true,
    doNotTrack: null,
    plugins: createPluginArray(isSafari),
    mimeTypes: createMimeTypeArray(),
    hardwareConcurrency: options.hardwareConcurrency,
    ...(isSafari ? {} : { deviceMemory: options.deviceMemory }),
    maxTouchPoints: 0,
    platform: "MacIntel",
    vendor: isSafari ? "Apple Computer, Inc." : "Google Inc.",
    webdriver: false,
    bluetooth: { toString: () => "[object Bluetooth]" },
    connection: { downlink: 10, effectiveType: "4g", rtt: 50, saveData: false },
    permissions: { query: async () => ({ state: "prompt", onchange: null }) },
    geolocation: {
      getCurrentPosition(success, error) { if (typeof error === "function") error({ code: 1, message: "User denied Geolocation" }); },
      watchPosition() { return 1; },
      clearWatch() {},
    },
    mediaDevices: {
      enumerateDevices: async () => [],
      getUserMedia: async () => { throw new Error("Permission denied"); },
    },
    storage: { estimate: async () => ({ quota: 10737418240, usage: 0 }) },
    ...(isSafari ? {} : {
      userAgentData: {
        mobile: false,
        platform: "macOS",
        brands: [
          { brand: "Not)A;Brand", version: "8" },
          { brand: "Chromium", version: String(options.chromeMajor || "") },
          { brand: "Google Chrome", version: String(options.chromeMajor || "") },
        ],
        getHighEntropyValues: async () => ({
          architecture: "arm",
          bitness: "64",
          mobile: false,
          model: "",
          platform: "macOS",
          platformVersion: "15.5.0",
          uaFullVersion: options.chromeFullVersion || "",
          fullVersionList: [
            { brand: "Not)A;Brand", version: "8.0.0.0" },
            { brand: "Chromium", version: options.chromeFullVersion || "" },
            { brand: "Google Chrome", version: options.chromeFullVersion || "" },
          ],
        }),
      },
    }),
  });
  const localStorage = createStorage();
  const sessionStorage = createStorage();
  const history = {
    length: 1,
    state: null,
    back() {},
    forward() {},
    go() {},
    pushState(state) {
      this.state = state ?? null;
    },
    replaceState(state) {
      this.state = state ?? null;
    },
  };

  const window = Object.assign(windowTarget, {
    window: null,
    self: null,
    top: null,
    parent: null,
    name: "",
    closed: false,
    length: 0,
    opener: null,
    frames: null,
    focus() {},
    blur() {},
    scrollTo() {},
    scrollBy() {},
    matchMedia(query) { return { matches: false, media: String(query), onchange: null, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent() { return false; } }; },
    document,
    navigator,
    screen: options.screen,
    location,
    localStorage,
    sessionStorage,
    history,
    innerWidth: options.screen.width,
    innerHeight: options.screen.height,
    outerWidth: options.screen.width,
    outerHeight: options.screen.height + 88,
    devicePixelRatio: options.devicePixelRatio,
    ...(isSafari ? { safari: { pushNotification: {} } } : { chrome: { runtime: {}, app: {} } }),
    performance: browserPerformance,
    crypto: browserCrypto,
    TextEncoder,
    TextDecoder,
    URL,
    URLSearchParams,
    AbortController,
    setTimeout: managedSetTimeout,
    clearTimeout: managedClearTimeout,
    btoa: btoaBinary,
    atob: atobBinary,
    fetch,
    console,
    Math: mathObject,
    Date,
    Intl,
    AudioContext: createAudioContext(),
    webkitAudioContext: createAudioContext(),
    JSON,
    Array,
    Object,
    Reflect,
    Number,
    String,
    Promise,
    RegExp,
    Error,
    Map,
    Set,
    WeakMap,
    Uint8Array,
    encodeURIComponent,
    decodeURIComponent,
    unescape,
    ...(exposeRequestIdleCallback ? {
      requestIdleCallback(callback) {
        return managedSetTimeout(() => callback({ timeRemaining: () => 5, didTimeout: false }), 0);
      },
      cancelIdleCallback(id) {
        managedClearTimeout(id);
      },
    } : {}),
    requestAnimationFrame(callback) {
      return managedSetTimeout(() => callback(performance.now()), 16);
    },
    cancelAnimationFrame(id) {
      managedClearTimeout(id);
    },
    webkitRequestAnimationFrame(callback) {
      return this.requestAnimationFrame(callback);
    },
    __privateStripeFrame8094: {},
    onpageswap: null,
    onpagehide: null,
    onpageshow: null,
    onvisibilitychange: null,
    onfocus: null,
    onblur: null,
  });

  window.window = window;
  window.self = window;
  window.top = window;
  window.parent = window;
  window.frames = window;

  return {
    iframeNode: () => iframeNode,
    context: vm.createContext({
      window,
      self: window,
      globalThis: window,
      document,
      navigator,
      screen: options.screen,
      location,
      localStorage,
      sessionStorage,
      history,
      performance: browserPerformance,
      crypto: browserCrypto,
      TextEncoder,
      TextDecoder,
      URL,
      URLSearchParams,
      AbortController,
      setTimeout: managedSetTimeout,
      clearTimeout: managedClearTimeout,
      btoa: btoaBinary,
      atob: atobBinary,
      fetch,
      console,
      Math: mathObject,
      Date,
      Intl,
      AudioContext: window.AudioContext,
      webkitAudioContext: window.webkitAudioContext,
      JSON,
      Array,
      Object,
      Reflect,
      Number,
      String,
      Promise,
      RegExp,
      Error,
      Map,
      Set,
      WeakMap,
      Uint8Array,
      encodeURIComponent,
      decodeURIComponent,
      unescape,
      ...(exposeRequestIdleCallback ? {
        requestIdleCallback: window.requestIdleCallback,
        cancelIdleCallback: window.cancelIdleCallback,
      } : {}),
      requestAnimationFrame: window.requestAnimationFrame,
      cancelAnimationFrame: window.cancelAnimationFrame,
      webkitRequestAnimationFrame: window.webkitRequestAnimationFrame,
      __privateStripeFrame8094: window.__privateStripeFrame8094,
      onpageswap: window.onpageswap,
    }),
    clearTimers() {
      for (const id of [...managedTimers]) managedClearTimeout(id);
    },
  };
}

async function main(argv = process.argv.slice(2), writeOutput = true) {
  const args = readArgs(argv);
  if (args.help === "1" || args.h === "1") {
    const helpText = [
      "用法：",
      "  node sentinel-runner.js --cookie \"你的 Cookie\"",
      "  node sentinel-runner.js --bearer \"Bearer 你的 token\"",
      "  node sentinel-runner.js --cookie \"你的 Cookie\" --bearer \"Bearer 你的 token\"",
      "  node sentinel-runner.js --config sentinel.config.json",
      "",
      "默认会读取当前目录、tools 目录或项目根目录的 sentinel.config.json。",
      "",
      "常用参数：",
      "  --flow checkout_session_approval",
      "  --page-url https://chatgpt.com/checkout/openai_llc/cs_xxx",
      "  --device-id 你的_oai-did",
      "  --challenge-url 自定义题目 challenge API",
      "  --sdk 指定 sdk.js 路径",
      "  --no-cookie 生成 token 时不向 challenge API 发送 Cookie",
    ].join("\n");
    if (writeOutput) process.stdout.write(`${helpText}\n`);
    return helpText;
  }

  const { path: configPath, data: config } = readConfig(args);
  const ignoreEnvForCredentials = Boolean(configPath);
  const cfg = configGetter(config);
  const defaultSdkPath = fs.existsSync(path.resolve(__dirname, "sdk.js"))
    ? path.resolve(__dirname, "sdk.js")
    : path.resolve(__dirname, "..", "sdk.js");
  const sdkPath = path.resolve(pick(args["sdk"], cfg("sdk", "sdkPath"), process.env.SENTINEL_SDK_PATH, defaultSdkPath));
  const flow = pick(args.flow, cfg("flow"), process.env.SENTINEL_FLOW, "checkout_session_approval");
  const challengeFile = pick(args["challenge-file"], cfg("challengeFile", "challenge_file"), process.env.SENTINEL_CHALLENGE_FILE);
  const officialMode =
    args.official === "1" ||
    truthy(cfg("official")) ||
    process.env.SENTINEL_OFFICIAL === "1" ||
    (!challengeFile && !args["challenge-url"] && !cfg("challengeUrl", "challenge_url") && !process.env.SENTINEL_CHALLENGE_URL);
  const challengeUrl =
    pick(args["challenge-url"], cfg("challengeUrl", "challenge_url"), process.env.SENTINEL_CHALLENGE_URL) ||
    (officialMode ? OFFICIAL_CHALLENGE_URL : "");
  const noCookie = args["no-cookie"] === "1" || truthy(cfg("noCookie", "no_cookie"));
  const cookieArg = noCookie ? "" : pick(args.cookie, args.cookies, cfg("cookie", "cookies"));
  const bearerArg = pick(args.bearer, args.authorization, cfg("bearer", "bearerToken", "authorization", "accessToken"));
  const contentType = pick(args["content-type"], cfg("contentType", "content_type"));
  const debugDx = args["debug-dx"] === "1" || truthy(cfg("debugDx", "debug_dx"));
  const debugDxLimit = Number(pick(args["debug-dx-limit"], cfg("debugDxLimit", "debug_dx_limit"), 80));
  const deviceId =
    pick(args["device-id"], cfg("deviceId", "device_id", "oaiDid", "oai_did"), process.env.SENTINEL_OAI_DID) ||
    "8a5ad769-e9e7-4461-ae3a-6755d7f46b0b";

  if (!fs.existsSync(sdkPath)) throw new Error(`找不到 SDK 文件：${sdkPath}`);
  if (!challengeFile && !challengeUrl) {
    throw new Error("请提供 --challenge-file、--challenge-url 或 --official，用于把题目服务器 challenge 喂回 SDK。");
  }

  let cachedChallenge = null;
  const options = {
    flow,
    sentinelSid: pick(args["sentinel-sid"], cfg("sentinelSid", "sentinel_sid"), process.env.SENTINEL_SID, ""),
    pageUrl: pick(args["page-url"], cfg("pageUrl", "page_url"), process.env.SENTINEL_PAGE_URL, "https://chatgpt.com/checkout/openai_llc/cs_ctf"),
    scriptSrc:
      pick(
        args["script-src"],
        cfg("scriptSrc", "script_src"),
        process.env.SENTINEL_SCRIPT_SRC,
      "https://chatgpt.com/sentinel/20260423af3c/sdk.js",
      ),
    buildId: pick(args["build-id"], cfg("buildId", "build_id"), process.env.SENTINEL_BUILD_ID, "prod-4987068829830ddc3ae6683bd4e633f61b79dec9"),
    reactListeningKey: pick(args["react-listening-key"], cfg("reactListeningKey", "react_listening_key"), process.env.SENTINEL_REACT_LISTENING_KEY, ""),
    cookie: noCookie
      ? `oai-did=${deviceId}`
      : cookieArg ||
        (ignoreEnvForCredentials ? "" : process.env.SENTINEL_COOKIE || process.env.CHATGPT_COOKIE) ||
        `oai-did=${deviceId}`,
    userAgent:
      pick(
        args["user-agent"],
        cfg("userAgent", "user_agent"),
        process.env.SENTINEL_USER_AGENT,
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Safari/605.1.15",
      ),
    contentType,
    browserFamily: pick(args["browser-family"], cfg("browserFamily", "browser_family"), process.env.SENTINEL_BROWSER_FAMILY, "safari"),
    requestIdleCallback: truthy(pick(args["request-idle-callback"], cfg("requestIdleCallback", "request_idle_callback"), process.env.SENTINEL_REQUEST_IDLE_CALLBACK, "0")),
    language: pick(args.language, cfg("language"), process.env.SENTINEL_LANGUAGE, "zh-CN"),
    languages: normalizeList(pick(args.languages, cfg("languages")), process.env.SENTINEL_LANGUAGES || "zh-CN,zh,en-US,en"),
    hardwareConcurrency: Number(pick(args.cores, cfg("cores", "hardwareConcurrency"), process.env.SENTINEL_CORES, 10)),
    jsHeapSizeLimit: Number(pick(args["js-heap-size-limit"], cfg("jsHeapSizeLimit", "js_heap_size_limit"), process.env.SENTINEL_JS_HEAP_SIZE_LIMIT, 4294967296)),
    fixedRandom:
      pick(args.random, cfg("random", "fixedRandom"), process.env.SENTINEL_FIXED_RANDOM)
        ? Number(pick(args.random, cfg("random", "fixedRandom"), process.env.SENTINEL_FIXED_RANDOM))
        : Number.NaN,
    deviceMemory: Number(pick(args["device-memory"], cfg("deviceMemory", "device_memory"), process.env.SENTINEL_DEVICE_MEMORY, 8)),
    devicePixelRatio: Number(pick(args["device-pixel-ratio"], cfg("devicePixelRatio", "device_pixel_ratio"), process.env.SENTINEL_DEVICE_PIXEL_RATIO, 2)),
    chromeMajor: pick(args["chrome-major"], cfg("chromeMajor", "chrome_major"), process.env.SENTINEL_CHROME_MAJOR, ""),
    chromeFullVersion: pick(args["chrome-full-version"], cfg("chromeFullVersion", "chrome_full_version"), process.env.SENTINEL_CHROME_FULL_VERSION, ""),
    screen: (() => {
      const width = Number(pick(args.width, cfg("width", "screenWidth"), process.env.SENTINEL_SCREEN_WIDTH, 1728));
      const height = Number(pick(args.height, cfg("height", "screenHeight"), process.env.SENTINEL_SCREEN_HEIGHT, 1117));
      return {
        width,
        height,
        availWidth: width,
        availHeight: Math.max(0, height - 38),
        colorDepth: 30,
        pixelDepth: 30,
        orientation: { type: "landscape-primary", angle: 0 },
      };
    })(),
    async handleIframeMessage(message) {
      if (message.type !== "token" && message.type !== "init") {
        throw new Error(`未知 iframe 消息类型：${message.type}`);
      }
      const proof = message.p;
      if (challengeFile) {
        cachedChallenge ||= readChallengeFile(challengeFile);
      } else {
        cachedChallenge = await fetchChallenge(challengeUrl, flow, proof, deviceId, {
          officialMode,
          pageUrl: options.pageUrl,
          userAgent: options.userAgent,
          cookie: noCookie ? "" : cookieArg,
          bearer: bearerArg,
          contentType: options.contentType,
          ignoreEnv: ignoreEnvForCredentials,
        });
      }
      if (debugDx && cachedChallenge?.turnstile?.dx) {
        try {
          const decoded = decodeDx(cachedChallenge.turnstile.dx, proof);
          const limit = Number.isFinite(debugDxLimit) && debugDxLimit > 0 ? debugDxLimit : 80;
          process.stderr.write(`dx 前 ${limit} 条指令：${JSON.stringify(decoded.slice(0, limit))}\n`);
        } catch (error) {
          process.stderr.write(`dx 解码失败：${error.message}\n`);
        }
      }
      return {
        cachedProof: proof,
        cachedChatReq: cachedChallenge,
      };
    },
  };

  const { context, clearTimers } = createBrowserContext(options);
  let sdkCode = fs.readFileSync(sdkPath, "utf8");
  if (debugDx) {
    sdkCode = sdkCode.replace(
      "Cn.set(n,Cn.get(e)[Cn.get(r)].bind(Cn[t(24)](e)))",
      "(()=>{const __o=Cn.get(e),__p=Cn.get(r);if(!__o||!__o[__p])console.error('[dx bind missing]',typeof __o,__p,Object.prototype.toString.call(__o));return Cn.set(n,__o[__p].bind(__o))})()"
    );
  }
  vm.runInContext(sdkCode, context, { filename: sdkPath });
  if (!context.SentinelSDK?.token) {
    throw new Error("SDK 加载后没有暴露 SentinelSDK.token");
  }

  const tokenText = await context.SentinelSDK.token(flow);
  clearTimers();
  if (!writeOutput) return tokenText;
  if (args.pretty || process.env.SENTINEL_PRETTY === "1") {
    process.stdout.write(`${JSON.stringify(JSON.parse(tokenText), null, 2)}\n`);
  } else {
    process.stdout.write(`${tokenText}\n`);
  }
  return tokenText;
}

if (require.main === module) {
  main().catch((error) => fail(error?.stack || error?.message || String(error)));
}

module.exports = {
  main,
  normalizeChallenge,
};
