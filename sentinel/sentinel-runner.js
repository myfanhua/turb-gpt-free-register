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

function parseSecChBrands(value, major = "") {
  const text = String(value || "").trim();
  if (!text) {
    return [
      { brand: "Not)A;Brand", version: "8" },
      { brand: "Chromium", version: String(major || "") },
      { brand: "Google Chrome", version: String(major || "") },
    ];
  }
  const out = [];
  const re = /"([^"]+)"\s*;\s*v="([^"]+)"/g;
  let m;
  while ((m = re.exec(text))) out.push({ brand: m[1], version: m[2] });
  return out.length ? out : text.split(",").map((part) => ({ brand: part.trim(), version: String(major || "") })).filter((x) => x.brand);
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


function createDomTokenList(initial = []) {
  const tokens = new Set(initial);
  const api = {
    add(...items) { for (const item of items) if (item) tokens.add(String(item)); },
    remove(...items) { for (const item of items) tokens.delete(String(item)); },
    contains(item) { return tokens.has(String(item)); },
    toggle(item, force) {
      const token = String(item);
      const shouldAdd = force === undefined ? !tokens.has(token) : Boolean(force);
      if (shouldAdd) tokens.add(token); else tokens.delete(token);
      return shouldAdd;
    },
    replace(oldToken, newToken) {
      if (!tokens.has(String(oldToken))) return false;
      tokens.delete(String(oldToken));
      tokens.add(String(newToken));
      return true;
    },
    item(index) { return [...tokens][Number(index)] || null; },
    get length() { return tokens.size; },
    toString() { return [...tokens].join(" "); },
    [Symbol.iterator]() { return tokens[Symbol.iterator](); },
  };
  Object.defineProperty(api, Symbol.toStringTag, { value: "DOMTokenList" });
  return api;
}

function createStyleDeclaration() {
  const values = Object.create(null);
  return {
    get cssText() {
      return Object.entries(values).map(([k, v]) => `${k}: ${v};`).join(" ");
    },
    set cssText(text) {
      for (const part of String(text || "").split(";")) {
        const idx = part.indexOf(":");
        if (idx > 0) this.setProperty(part.slice(0, idx).trim(), part.slice(idx + 1).trim());
      }
    },
    get length() { return Object.keys(values).length; },
    item(index) { return Object.keys(values)[Number(index)] || ""; },
    getPropertyValue(name) { return values[String(name)] || ""; },
    setProperty(name, value) { values[String(name)] = String(value); this[String(name)] = String(value); },
    removeProperty(name) { const key = String(name); const old = values[key] || ""; delete values[key]; delete this[key]; return old; },
  };
}

function createElementNode(tagName, ownerDocument, rect = createDomRect()) {
  const target = createEventTarget();
  const children = [];
  const attrs = new Map();
  const dataset = {};
  const element = {
    nodeType: 1,
    nodeName: String(tagName).toUpperCase(),
    tagName: String(tagName).toUpperCase(),
    ownerDocument,
    parentNode: null,
    parentElement: null,
    children,
    childNodes: children,
    firstChild: null,
    lastChild: null,
    style: createStyleDeclaration(),
    dataset,
    classList: createDomTokenList(),
    textContent: "",
    innerHTML: "",
    appendChild(node) {
      children.push(node);
      node.parentNode = element;
      node.parentElement = element;
      element.firstChild = children[0] || null;
      element.lastChild = children[children.length - 1] || null;
      return node;
    },
    removeChild(node) {
      const index = children.indexOf(node);
      if (index >= 0) children.splice(index, 1);
      if (node) { node.parentNode = null; node.parentElement = null; }
      element.firstChild = children[0] || null;
      element.lastChild = children[children.length - 1] || null;
      return node;
    },
    insertBefore(node, before) {
      const index = children.indexOf(before);
      if (index < 0) return this.appendChild(node);
      children.splice(index, 0, node);
      node.parentNode = element;
      node.parentElement = element;
      element.firstChild = children[0] || null;
      element.lastChild = children[children.length - 1] || null;
      return node;
    },
    remove() { if (element.parentNode?.removeChild) element.parentNode.removeChild(element); },
    setAttribute(name, value) {
      const key = String(name);
      const val = String(value);
      attrs.set(key, val);
      if (key === "class") element.classList = createDomTokenList(val.split(/\s+/).filter(Boolean));
      if (key === "id") element.id = val;
      if (key.startsWith("data-")) {
        const prop = key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
        dataset[prop] = val;
      }
    },
    getAttribute(name) { return attrs.get(String(name)) ?? null; },
    hasAttribute(name) { return attrs.has(String(name)); },
    removeAttribute(name) { attrs.delete(String(name)); },
    getAttributeNames() { return [...attrs.keys()]; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    matches() { return false; },
    closest() { return null; },
    getBoundingClientRect() { return rect; },
    addEventListener: target.addEventListener,
    removeEventListener: target.removeEventListener,
    dispatchEvent: target.dispatchEvent,
    click() { target.dispatchEvent.call(element, { type: "click", target: element }); },
  };
  Object.defineProperty(element, "attributes", {
    get() { return [...attrs].map(([name, value]) => ({ name, value, nodeName: name, nodeValue: value, valueOf: () => value })); },
  });
  return element;
}


function createIntlObject(options) {
  const intlObject = Object.create(Intl);
  const NativeDateTimeFormat = Intl.DateTimeFormat;
  function DateTimeFormatMock(locales, formatOptions = {}) {
    const mergedOptions = { ...(formatOptions || {}) };
    if (options.timeZone) mergedOptions.timeZone = options.timeZone;
    const fmt = new NativeDateTimeFormat(locales || options.languages || options.language, mergedOptions);
    const nativeResolvedOptions = fmt.resolvedOptions.bind(fmt);
    Object.defineProperty(fmt, "resolvedOptions", {
      value: () => {
        const resolved = nativeResolvedOptions();
        if (options.timeZone) resolved.timeZone = options.timeZone;
        if (options.language) resolved.locale = options.language;
        return resolved;
      },
    });
    return fmt;
  }
  DateTimeFormatMock.prototype = NativeDateTimeFormat.prototype;
  Object.setPrototypeOf(DateTimeFormatMock, NativeDateTimeFormat);
  intlObject.DateTimeFormat = DateTimeFormatMock;
  return intlObject;
}

function createPerformanceObserver() {
  return class PerformanceObserverMock {
    constructor(callback) { this.callback = callback; this._observed = false; }
    observe() { this._observed = true; }
    disconnect() { this._observed = false; }
    takeRecords() { return []; }
    static get supportedEntryTypes() { return ["navigation", "resource", "paint", "mark", "measure"]; }
  };
}

function createNetworkInformation() {
  const target = createEventTarget();
  const info = {
    downlink: 10,
    effectiveType: "4g",
    rtt: 50,
    saveData: false,
    type: "wifi",
    onchange: null,
    addEventListener: target.addEventListener,
    removeEventListener: target.removeEventListener,
    dispatchEvent: target.dispatchEvent,
  };
  Object.defineProperty(info, Symbol.toStringTag, { value: "NetworkInformation" });
  return info;
}

function createCookieJar(initialCookie = "") {
  const values = new Map();
  for (const part of String(initialCookie || "").split(";")) {
    const trimmed = part.trim();
    if (!trimmed) continue;
    const idx = trimmed.indexOf("=");
    if (idx <= 0) continue;
    values.set(trimmed.slice(0, idx), trimmed.slice(idx + 1));
  }
  return {
    get cookie() {
      return [...values.entries()].map(([k, v]) => `${k}=${v}`).join("; ");
    },
    set cookie(value) {
      const first = String(value || "").split(";")[0];
      const idx = first.indexOf("=");
      if (idx > 0) values.set(first.slice(0, idx).trim(), first.slice(idx + 1).trim());
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

  const perfEntries = [{
    name: options.pageUrl,
    entryType: "navigation",
    startTime: 0,
    duration: Math.max(1, performance.now()),
    initiatorType: "navigation",
    nextHopProtocol: options.nextHopProtocol || "h2",
    transferSize: 0,
    encodedBodySize: 0,
    decodedBodySize: 0,
    toJSON() { return { ...this }; },
  }];
  const browserIntl = createIntlObject(options);

  const browserPerformance = {
    now: () => performance.now(),
    timeOrigin: performance.timeOrigin || Date.now() - performance.now(),
    memory: {
      jsHeapSizeLimit: options.jsHeapSizeLimit,
      totalJSHeapSize: Math.floor(options.jsHeapSizeLimit / 3),
      usedJSHeapSize: Math.floor(options.jsHeapSizeLimit / 8),
    },
    getEntries() { return perfEntries.slice(); },
    getEntriesByType(type) { return perfEntries.filter((entry) => entry.entryType === String(type)); },
    getEntriesByName(name) { return perfEntries.filter((entry) => entry.name === String(name)); },
    mark(name) { perfEntries.push({ name: String(name), entryType: "mark", startTime: this.now(), duration: 0, toJSON() { return { ...this }; } }); },
    measure(name) { perfEntries.push({ name: String(name), entryType: "measure", startTime: this.now(), duration: 0, toJSON() { return { ...this }; } }); },
    clearMarks() {},
    clearMeasures() {},
  };
  const mathObject = Object.create(Math);
  if (Number.isFinite(options.fixedRandom)) {
    mathObject.random = () => options.fixedRandom;
  }
  const currentScript = { src: options.scriptSrc, length: options.scriptSrc.length };
  const appBuildPath = options.buildId && String(options.buildId).startsWith("c/")
    ? String(options.buildId)
    : (options.buildId ? `c/${options.buildId}/_/` : "c/prod-fb4a8a2a751dfec391053cfd7b01c52699ccf78c/_/");
  const appScriptSrc = `https://chatgpt.com/${appBuildPath}ssg.js`;
  const scripts = [
    currentScript,
    { src: "https://accounts.google.com/gsi/client", length: 38 },
    { src: "https://chatgpt.com/cdn-cgi/challenge-platform/scripts/jsd/api.js?onload=jsdOnload", length: 84 },
    { src: appScriptSrc, length: appScriptSrc.length },
    { src: "https://chatgpt.com/_next/static/chunks/webpack.js", length: 48 },
    { src: "https://js.stripe.com/v3/", length: 24 },
  ];
  const attrs = new Map();
  if (options.buildId) attrs.set("data-build", options.buildId);
  const reactListeningKey = options.reactListeningKey || "_reactListening" + crypto.randomBytes(6).toString("hex");
  const reactContainerKey = options.reactContainerKey || "__reactContainer$" + crypto.randomBytes(6).toString("hex");
  const reactResourcesKey = options.reactResourcesKey || reactContainerKey.replace("__reactContainer$", "__reactResources$");

  const cookieJar = createCookieJar(options.cookie);
  const location = new URL(options.pageUrl);
  let iframeNode = null;
  const bodyChildren = [];
  const documentTarget = createEventTarget();
  const document = {
    currentScript,
    scripts,
    get cookie() { return cookieJar.cookie; },
    set cookie(value) { cookieJar.cookie = value; },
    URL: options.pageUrl,
    documentURI: options.pageUrl,
    referrer: options.referrer || "https://auth.openai.com/",
    title: "",
    origin: location.origin,
    location,
    characterSet: "UTF-8",
    charset: "UTF-8",
    compatMode: "CSS1Compat",
    contentType: "text/html",
    readyState: "complete",
    visibilityState: "visible",
    hidden: false,
    hasFocus() { return true; },
    [reactListeningKey]: true,
    [reactContainerKey]: true,
    [reactResourcesKey]: true,
    defaultView: null,
    head: null,
    documentElement: {
      nodeType: 1,
      nodeName: "HTML",
      tagName: "HTML",
      ownerDocument: null,
      style: createStyleDeclaration(),
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
      querySelector() { return null; },
      querySelectorAll() { return []; },
      getBoundingClientRect() {
        return createDomRect(options.screen.width, options.screen.height);
      },
    },
    body: {
      nodeType: 1,
      nodeName: "BODY",
      tagName: "BODY",
      ownerDocument: null,
      parentNode: null,
      parentElement: null,
      children: bodyChildren,
      childNodes: bodyChildren,
      style: createStyleDeclaration(),
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
    addEventListener: documentTarget.addEventListener,
    removeEventListener: documentTarget.removeEventListener,
    dispatchEvent: documentTarget.dispatchEvent,
    querySelector(selector) {
      const q = String(selector || "").toLowerCase();
      if (q === "head") return this.head;
      if (q === "body") return this.body;
      if (q === "html" || q === "documentelement") return this.documentElement;
      return null;
    },
    querySelectorAll(selector) { const item = this.querySelector(selector); return item ? [item] : []; },
    getElementById() { return null; },
    getElementsByTagName(name) {
      const n = String(name).toLowerCase();
      if (n === "script") return scripts;
      if (n === "head") return [this.head];
      if (n === "body") return [this.body];
      if (n === "html") return [this.documentElement];
      return [];
    },
    createTextNode(text) { return { nodeType: 3, nodeName: "#text", textContent: String(text || ""), parentNode: null, ownerDocument: document }; },
    createElement(tagName) {
      const lowerTag = String(tagName).toLowerCase();
      if (lowerTag === "canvas") {
        const canvas = createCanvas(300, 150, isSafari);
        canvas.ownerDocument = document;
        return canvas;
      }
      if (lowerTag !== "iframe") {
        return createElementNode(tagName, document);
      }

      const target = createEventTarget();
      const iframe = createElementNode("iframe", document);
      Object.assign(iframe, {
        src: "",
        width: "",
        height: "",
        sandbox: { value: "", toString() { return this.value; } },
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
        dispatchEvent: target.dispatchEvent,
        _emitLoad() {
          target.dispatchEvent.call(iframe, { type: "load", target: iframe });
        },
      });
      return iframe;
    },
  };

  document.defaultView = null;
  document.documentElement.ownerDocument = document;
  document.body.ownerDocument = document;
  document.head = createElementNode("head", document);

  const browserFamily = String(options.browserFamily || "chrome").toLowerCase();
  const isSafari = browserFamily === "safari" || /Version\/[^ ]+ Safari\//.test(String(options.userAgent || ""));
  const exposeRequestIdleCallback = !isSafari && options.requestIdleCallback !== false;
  const navigatorProto = isSafari ? {
    javaEnabled: makeNativeFunction("javaEnabled", () => false),
    sendBeacon: makeNativeFunction("sendBeacon", () => true),
    getGamepads: makeNativeFunction("getGamepads", () => []),
    webkitGetUserMedia: makeNativeFunction("webkitGetUserMedia"),
  } : {
    createAuctionNonce: makeNativeFunction("createAuctionNonce", () => crypto.randomUUID()),
    clearOriginJoinedAdInterestGroups: makeNativeFunction("clearOriginJoinedAdInterestGroups"),
    updateAdInterestGroups: makeNativeFunction("updateAdInterestGroups"),
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
    ...(isSafari ? {} : { gpu: { toString: () => "[object GPU]" } }),
    connection: createNetworkInformation(),
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
      login: { toString: () => "[object NavigatorLogin]" },
      userAgentData: {
        mobile: false,
        platform: options.secChUaPlatform || "macOS",
        brands: parseSecChBrands(options.secChUa, options.chromeMajor),
        getHighEntropyValues: async (hints = []) => {
          const values = {
            architecture: options.secChUaArch || "arm",
            bitness: options.secChUaBitness || "64",
            mobile: false,
            model: options.secChUaModel || "",
            platform: options.secChUaPlatform || "macOS",
            platformVersion: options.secChUaPlatformVersion || "15.5.0",
            uaFullVersion: options.chromeFullVersion || "",
            fullVersionList: parseSecChBrands(options.secChUaFullVersionList, options.chromeFullVersion || options.chromeMajor),
          };
          if (!Array.isArray(hints) || hints.length === 0) return values;
          const picked = {};
          for (const hint of hints) if (hint in values) picked[hint] = values[hint];
          return picked;
        },
        toJSON() { return { brands: this.brands, mobile: this.mobile, platform: this.platform }; },
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
    getComputedStyle(element) { return element?.style || createStyleDeclaration(); },
    MessageEvent: class MessageEvent { constructor(type, init = {}) { this.type = type; Object.assign(this, init); } },
    Event: class Event { constructor(type, init = {}) { this.type = type; Object.assign(this, init); } },
    CustomEvent: class CustomEvent { constructor(type, init = {}) { this.type = type; this.detail = init.detail; Object.assign(this, init); } },
    DOMRect: class DOMRect { constructor(x = 0, y = 0, width = 0, height = 0) { Object.assign(this, { x, y, width, height, top: y, left: x, right: x + width, bottom: y + height }); } },
    HTMLElement: function HTMLElement() {},
    HTMLIFrameElement: function HTMLIFrameElement() {},
    MutationObserver: class MutationObserver { constructor(callback) { this.callback = callback; } observe() {} disconnect() {} takeRecords() { return []; } },
    PerformanceObserver: createPerformanceObserver(),
    document,
    navigator,
    screen: options.screen,
    location,
    locationbar: { visible: true },
    menubar: { visible: true },
    personalbar: { visible: true },
    scrollbars: { visible: true },
    statusbar: { visible: true },
    toolbar: { visible: true },
    scrollX: 0,
    scrollY: 0,
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
    setInterval: managedSetTimeout,
    clearInterval: managedClearTimeout,
    queueMicrotask: queueMicrotask.bind(globalThis),
    btoa: btoaBinary,
    atob: atobBinary,
    fetch,
    console,
    Math: mathObject,
    Date,
    Intl: browserIntl,
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
    ondevicemotion: null,
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
  document.defaultView = window;

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
      MessageEvent: window.MessageEvent,
      Event: window.Event,
      CustomEvent: window.CustomEvent,
      DOMRect: window.DOMRect,
      HTMLElement: window.HTMLElement,
      HTMLIFrameElement: window.HTMLIFrameElement,
      MutationObserver: window.MutationObserver,
      PerformanceObserver: window.PerformanceObserver,
      setTimeout: managedSetTimeout,
      clearTimeout: managedClearTimeout,
      setInterval: managedSetTimeout,
      clearInterval: managedClearTimeout,
      queueMicrotask: queueMicrotask.bind(globalThis),
      btoa: btoaBinary,
      atob: atobBinary,
      fetch,
      console,
      Math: mathObject,
      Date,
      Intl: browserIntl,
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
      "https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js",
      ),
    buildId: pick(args["build-id"], cfg("buildId", "build_id"), process.env.SENTINEL_BUILD_ID, ""),
    reactListeningKey: pick(args["react-listening-key"], cfg("reactListeningKey", "react_listening_key"), process.env.SENTINEL_REACT_LISTENING_KEY, ""),
    reactContainerKey: pick(args["react-container-key"], cfg("reactContainerKey", "react_container_key"), process.env.SENTINEL_REACT_CONTAINER_KEY, ""),
    reactResourcesKey: pick(args["react-resources-key"], cfg("reactResourcesKey", "react_resources_key"), process.env.SENTINEL_REACT_RESOURCES_KEY, ""),
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
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
      ),
    contentType,
    browserFamily: pick(args["browser-family"], cfg("browserFamily", "browser_family"), process.env.SENTINEL_BROWSER_FAMILY, "chrome"),
    requestIdleCallback: truthy(pick(args["request-idle-callback"], cfg("requestIdleCallback", "request_idle_callback"), process.env.SENTINEL_REQUEST_IDLE_CALLBACK, "0")),
    language: pick(args.language, cfg("language"), process.env.SENTINEL_LANGUAGE, "ja-JP"),
    languages: normalizeList(pick(args.languages, cfg("languages")), process.env.SENTINEL_LANGUAGES || "ja-JP"),
    timeZone: pick(args["time-zone"], args.timezone, cfg("timeZone", "time_zone", "timezone"), process.env.SENTINEL_TIME_ZONE, "Asia/Tokyo"),
    timezoneName: pick(args["timezone-name"], cfg("timezoneName", "timezone_name"), process.env.SENTINEL_TIMEZONE_NAME, "Japan Standard Time"),
    timezoneOffsetMinutes: Number(pick(args["timezone-offset-minutes"], cfg("timezoneOffsetMinutes", "timezone_offset_minutes"), process.env.SENTINEL_TIMEZONE_OFFSET_MINUTES, 540)),
    hardwareConcurrency: Number(pick(args.cores, cfg("cores", "hardwareConcurrency"), process.env.SENTINEL_CORES, 6)),
    jsHeapSizeLimit: Number(pick(args["js-heap-size-limit"], cfg("jsHeapSizeLimit", "js_heap_size_limit"), process.env.SENTINEL_JS_HEAP_SIZE_LIMIT, 4395630592)),
    fixedRandom:
      pick(args.random, cfg("random", "fixedRandom"), process.env.SENTINEL_FIXED_RANDOM)
        ? Number(pick(args.random, cfg("random", "fixedRandom"), process.env.SENTINEL_FIXED_RANDOM))
        : Number.NaN,
    deviceMemory: Number(pick(args["device-memory"], cfg("deviceMemory", "device_memory"), process.env.SENTINEL_DEVICE_MEMORY, 8)),
    devicePixelRatio: Number(pick(args["device-pixel-ratio"], cfg("devicePixelRatio", "device_pixel_ratio"), process.env.SENTINEL_DEVICE_PIXEL_RATIO, 2)),
    chromeMajor: pick(args["chrome-major"], cfg("chromeMajor", "chrome_major"), process.env.SENTINEL_CHROME_MAJOR, "149"),
    chromeFullVersion: pick(args["chrome-full-version"], cfg("chromeFullVersion", "chrome_full_version"), process.env.SENTINEL_CHROME_FULL_VERSION, "149.0.0.0"),
    secChUa: pick(args["sec-ch-ua"], cfg("secChUa", "sec_ch_ua"), process.env.SENTINEL_SEC_CH_UA, '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"'),
    secChUaPlatform: String(pick(args["sec-ch-ua-platform"], cfg("secChUaPlatform", "sec_ch_ua_platform"), process.env.SENTINEL_SEC_CH_UA_PLATFORM, "macOS")).replace(/^"|"$/g, ""),
    secChUaFullVersionList: pick(args["sec-ch-ua-full-version-list"], cfg("secChUaFullVersionList", "sec_ch_ua_full_version_list"), process.env.SENTINEL_SEC_CH_UA_FULL_VERSION_LIST, ""),
    secChUaPlatformVersion: String(pick(args["sec-ch-ua-platform-version"], cfg("secChUaPlatformVersion", "sec_ch_ua_platform_version"), process.env.SENTINEL_SEC_CH_UA_PLATFORM_VERSION, "15.5.0")).replace(/^"|"$/g, ""),
    secChUaArch: String(pick(args["sec-ch-ua-arch"], cfg("secChUaArch", "sec_ch_ua_arch"), process.env.SENTINEL_SEC_CH_UA_ARCH, "arm")).replace(/^"|"$/g, ""),
    secChUaBitness: String(pick(args["sec-ch-ua-bitness"], cfg("secChUaBitness", "sec_ch_ua_bitness"), process.env.SENTINEL_SEC_CH_UA_BITNESS, "64")).replace(/^"|"$/g, ""),
    secChUaModel: String(pick(args["sec-ch-ua-model"], cfg("secChUaModel", "sec_ch_ua_model"), process.env.SENTINEL_SEC_CH_UA_MODEL, "")).replace(/^"|"$/g, ""),
    screen: (() => {
      const width = Number(pick(args.width, cfg("width", "screenWidth"), process.env.SENTINEL_SCREEN_WIDTH, 1680));
      const height = Number(pick(args.height, cfg("height", "screenHeight"), process.env.SENTINEL_SCREEN_HEIGHT, 1050));
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

  if (options.timeZone) {
    process.env.TZ = options.timeZone;
  }

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
