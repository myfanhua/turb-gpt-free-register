'use strict';

const fs = require('node:fs');
const vm = require('node:vm');
const cryptoMod = require('node:crypto');

// 移植自目标 vendor/gpt-register-tool/协议/openai_sentinel_quickjs.js。
// 本地差异：Cookie/sid 注入、timer/rAF 清理、stderr 日志、0ms 禁用行为采集。
// SDK hook、浏览器环境、时区实现及默认行为序列沿用目标。
const sdkTimeouts = new Set();
const sdkIntervals = new Set();
function sdkSetTimeout(cb, ms, ...args) {
  const timer = setTimeout(() => { sdkTimeouts.delete(timer); cb(...args); }, ms);
  sdkTimeouts.add(timer);
  return timer;
}
function sdkClearTimeout(timer) { clearTimeout(timer); sdkTimeouts.delete(timer); }
function sdkSetInterval(cb, ms, ...args) {
  const timer = setInterval(cb, ms, ...args);
  sdkIntervals.add(timer);
  return timer;
}
function sdkClearInterval(timer) { clearInterval(timer); sdkIntervals.delete(timer); }
async function withTimeout(promise, ms, label) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(label)), ms);
    })]);
  } finally { clearTimeout(timer); }
}

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const sdkRaw = fs.readFileSync(process.env.OPENAI_SENTINEL_SDK_FILE, 'utf8');

// ─── SDK hook strings ──────────────────────────────────────────
// 这三处替换是整条 Sentinel 链路的命门：外面（node）能拿到 SDK 内部函数，
// 全靠往下载到的 sdk.js 里插三个全局出口。
//
// ⚠️ OpenAI 会在**同一个版本 URL 下原地重建 sdk.js**（2026-09 实测：
//    /sentinel/20260810913b/sdk.js 字节数与 SHA 都没变，但内部标识符整体重命名）。
//    老锚点 `var P=new _;` / `},t.token=ye,t}({});` 就是被这次重建打死的：
//    三处只中一处 → __debugP/__debug_n/__debug_bindProof 全 undefined →
//    只在回退分支炸 TypeError，日志长得像"PoW 算不出来"，白排查一轮。
//    所以下面一律走 mustReplace()，失配立刻 exit 3，不再静默。
//
// 名字映射（20260810913b，实测得出，不是猜的）：
//   SDK 全局   var SentinelSDK=   → 未变
//   SDK 实例   var P=new _;       → var E=new O;
//   token 入口 t.token=ye         → t.token=je
//   t 计算函数 _n                 → Rn（在 je 里以 (challenge, dx) 调用，语义一致）
//   bindProof  D                  → 未改名
// 锚点用尾部短串 `,t.token=je,t}({});`：它同时越过新插进来的
// `},t.timing=function(){...return Ae}`，比带前缀的长串稳。
const EXPOSE_PATCH = ",t.token=je,t}({});";
const EXPOSE_REPLACEMENT =
  ",t.token=je,t.__debug_n=Rn,t.__debug_bindProof=D,t}({});";
const INSTANCE_PATCH = "var E=new O;";
const INSTANCE_REPLACEMENT = "var E=new O;globalThis.__debugP=E;";
const SDK_GLOBAL_PATCH = "var SentinelSDK=";
const SDK_GLOBAL_REPLACEMENT = "globalThis.SentinelSDK=";

const HOOK_EXIT_CODE = 3;

// 命中即替换，未命中即响亮失败。以前这里是裸的 String.replace()：
// 失配无声无息，只在回退分支以"PoW 失败"的假象暴露 —— 这是本文件最坑的地方。
function mustReplace(src, pattern, replacement, label) {
  const hits = src.split(pattern).length - 1;
  if (hits === 0) {
    process.stderr.write(
      '[sentinel-hook] MISS ' + label + ': 锚点未命中 -> ' + JSON.stringify(pattern) + '\n' +
      '[sentinel-hook] sdk.js 疑似被原地重建，三处 hook 已失效；拒绝继续（exit ' +
      HOOK_EXIT_CODE + '），避免产出假 token 掩盖真因。\n'
    );
    process.exit(HOOK_EXIT_CODE);
  }
  if (hits > 1) {
    // 多命中不致命（String.replace 只换第一处），加载后的 POST-LOAD CHECK 会兜底。
    process.stderr.write('[sentinel-hook] WARN ' + label + ': 锚点命中 ' + hits + ' 次，只替换第一处\n');
  }
  return src.replace(pattern, replacement);
}

let sdk = sdkRaw;
sdk = mustReplace(sdk, SDK_GLOBAL_PATCH, SDK_GLOBAL_REPLACEMENT, 'SDK_GLOBAL');
sdk = mustReplace(sdk, INSTANCE_PATCH, INSTANCE_REPLACEMENT, 'INSTANCE');
sdk = mustReplace(sdk, EXPOSE_PATCH, EXPOSE_REPLACEMENT, 'EXPOSE');

// ─── Helpers ───────────────────────────────────────────────────

function createStorage() {
  const map = new Map();
  return {
    get length() { return map.size; },
    clear() { map.clear(); },
    getItem(key) { return map.has(String(key)) ? map.get(String(key)) : null; },
    setItem(key, value) { map.set(String(key), String(value)); },
    removeItem(key) { map.delete(String(key)); },
    key(index) { return [...map.keys()][index] || null; },
  };
}

function genericElement(tagName) {
  const tag = String(tagName || 'div').toLowerCase();
  return {
    nodeType: 1,
    tagName: tag.toUpperCase(),
    nodeName: tag.toUpperCase(),
    style: {},
    children: [],
    childNodes: [],
    src: '',
    id: '',
    className: '',
    innerHTML: '',
    textContent: '',
    parentNode: null,
    appendChild(child) { this.children.push(child); child.parentNode = this; return child; },
    removeChild(child) { this.children = this.children.filter(x => x !== child); return child; },
    insertBefore(n) { this.children.push(n); return n; },
    setAttribute() {},
    getAttribute() { return null; },
    hasAttribute() { return false; },
    removeAttribute() {},
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() { return true; },
    cloneNode() { return genericElement(tagName); },
    contains() { return false; },
    getBoundingClientRect() {
      return { x: 0, y: 0, width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0 };
    },
    focus() {},
    blur() {},
    click() {},
  };
}

function canvasElement() {
  const el = genericElement('canvas');
  el.width = 300;
  el.height = 150;
  el.toDataURL = () => 'data:image/png;base64,';
  el.toBlob = (cb) => { if (cb) cb(new Uint8Array(0)); };
  el.getContext = (kind) => {
    if (kind === '2d') {
      return {
        fillRect() {}, clearRect() {}, strokeRect() {},
        getImageData() { return { data: new Uint8Array(0) }; },
        putImageData() {}, createImageData() { return { data: new Uint8Array(0) }; },
        setTransform() {}, resetTransform() {}, drawImage() {},
        save() {}, restore() {}, beginPath() {}, closePath() {},
        moveTo() {}, lineTo() {}, clip() {}, quadraticCurveTo() {},
        bezierCurveTo() {}, arc() {}, arcTo() {}, rect() {},
        fill() {}, stroke() {}, measureText() { return { width: 0 }; },
        fillText() {}, strokeText() {},
        scale() {}, rotate() {}, translate() {},
        createLinearGradient() { return { addColorStop() {} }; },
        createRadialGradient() { return { addColorStop() {} }; },
        canvas: el,
        fillStyle: '', strokeStyle: '', lineWidth: 1, font: '10px sans-serif',
        textAlign: 'start', textBaseline: 'alphabetic',
        globalAlpha: 1, globalCompositeOperation: 'source-over',
      };
    }
    if (!['webgl', 'experimental-webgl', 'webgl2'].includes(kind)) return null;
    const dbg = { UNMASKED_VENDOR_WEBGL: 0x9245, UNMASKED_RENDERER_WEBGL: 0x9246 };
    return {
      VENDOR: 0x1F00, RENDERER: 0x1F01,
      getExtension(name) { return name === 'WEBGL_debug_renderer_info' ? dbg : null; },
      getParameter(p) {
        if (p === dbg.UNMASKED_VENDOR_WEBGL || p === 0x1F00) return 'Google Inc. (Intel)';
        if (p === dbg.UNMASKED_RENDERER_WEBGL || p === 0x1F01)
          return 'ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)';
        return 0;
      },
      getSupportedExtensions() { return ['WEBGL_debug_renderer_info']; },
      createBuffer() { return {}; }, createTexture() { return {}; },
      createShader() { return {}; }, createProgram() { return {}; },
      bindBuffer() {}, bufferData() {}, bindTexture() {},
      viewport() {}, clear() {}, enable() {}, disable() {},
      drawArrays() {}, drawElements() {},
      canvas: el,
    };
  };
  return el;
}

// ─── Event listener infrastructure (shared between main & VM) ─

const _listeners = new Map();

function addListener(type, callback) {
  if (typeof callback !== 'function') return;
  const bucket = _listeners.get(type) || [];
  bucket.push(callback);
  _listeners.set(type, bucket);
}

function removeListener(type, callback) {
  const bucket = _listeners.get(type) || [];
  _listeners.set(type, bucket.filter(fn => fn !== callback));
}

async function dispatch(type, init) {
  const event = {
    type,
    bubbles: true,
    cancelable: true,
    defaultPrevented: false,
    timeStamp: performance.now(),
    target: null,
    currentTarget: null,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() {},
    stopImmediatePropagation() {},
    ...(init || {}),
  };
  for (const cb of [...(_listeners.get(type) || [])]) {
    try { await cb(event); } catch (_) {}
  }
}

// ─── iframe mock ───────────────────────────────────────────────

let iframeObject = null;
let capturedProof = null;

// ─── Build the VM context ──────────────────────────────────────

const screenW = Number(input.screen_width || 1920);
const screenH = Number(input.screen_height || 1080);
const scripts = [];

const documentElement = genericElement('html');
documentElement.clientWidth = screenW;
documentElement.clientHeight = screenH;

const bodyEl = genericElement('body');
bodyEl.appendChild = function (child) {
  this.children.push(child);
  child.parentNode = this;
  if (child === iframeObject) {
    setTimeout(() => {
      for (const cb of (iframeObject._load || [])) {
        try { cb(); } catch (_) {}
      }
    }, 1);
  }
  return child;
};

const navPlatform = input.platform != null ? String(input.platform) : 'Win32';
const navVendor = input.vendor != null ? String(input.vendor) : 'Google Inc.';

const targetTz = String(input.timezone || 'UTC');
// 真 V8 的 Date 长时区名 / Intl 默认值都跟着**页面默认 locale** 走，
// 所以统一取画像的 language，不要写死 en-US。
const pageLocale = String(input.language || 'en-US');
const OrigDTF = Intl.DateTimeFormat;

// ── 时区 ID：**原样透传**，不要做任何"规范化"映射 ────────────────────────
// 曾经想按「CLDR 46+ 已把规范名改成 Asia/Ho_Chi_Minh / Asia/Kolkata」去映射
// —— **那是错的，已回退**。真 Chrome 基线实测（2026-09-22）：
//
//   Playwright Chromium 148.0.7778.96 + 系统 Chrome 153.0.8010.53
//   都用 CDP timezone override 复测，结论一致：
//       timezone_id=Asia/Ho_Chi_Minh -> resolvedOptions().timeZone = "Asia/Saigon"
//       timezone_id=Asia/Kolkata     -> resolvedOptions().timeZone = "Asia/Calcutta"
//   反着传（Asia/Saigon / Asia/Calcutta）也照样返回同一个名字。
//
// 即：浏览器报的就是这两个"旧"名字，node v24.16.0 / ICU 78.3 的答案与之一致。
// 所以正确做法就是**别动它**。基线脚本：exports/revive/probes/_chrome_tz_baseline.py
//
// ⚠️ 教训：这类"规范化"推断必须先用真 Chrome 验，不能凭 CLDR 版本号想当然 ——
//    我凭理论改了一版，方向正好是反的，会把一个正确的行为改成指纹异常。
const tzCanonical = (() => {
  try {
    // 用**真 ICU**做规范化（OrigDTF 是打补丁前抓下来的原生 Intl.DateTimeFormat）。
    // 真 Chrome 会规范化：传 Asia/Ho_Chi_Minh 进去，resolvedOptions().timeZone 报的是
    // Asia/Saigon；传 Asia/Kolkata 报的是 Asia/Calcutta（Chrome 148 / 153 实测一致）。
    // node v24.16.0 / ICU 78.3 对这几个时区的答案与 Chrome **一致**，所以直接借它。
    // 这个"一致"是实测结论、不是保证 —— _tz_fidelity.py 就是守这条的回归测试，
    // 哪天 node 换 ICU 版本导致两者分叉，那个探针会立刻报出来。
    return new OrigDTF('en-US', { timeZone: targetTz }).resolvedOptions().timeZone;
  } catch (_) {
    return targetTz;
  }
})();

const PatchedDTF = function (locales, options) {
  const inst = new OrigDTF(locales, options);
  const orig = inst.resolvedOptions.bind(inst);
  inst.resolvedOptions = function () { const r = orig(); r.timeZone = tzCanonical; return r; };
  return inst;
};
Object.setPrototypeOf(PatchedDTF, OrigDTF);
PatchedDTF.prototype = OrigDTF.prototype;
PatchedDTF.supportedLocalesOf = OrigDTF.supportedLocalesOf;

// ── Date 也必须跟着目标时区走（2026-09-22 修）─────────────────────────────
// 之前只改了 Intl.DateTimeFormat，Date.prototype.toString() 用的还是**宿主机**
// 时区。sentinel 的 p 载荷第 2 项就是 Date 的字符串形式，实测泄漏成
//   "Mon Sep 21 2026 19:29:47 GMT+0800 (GMT+08:00)"     <- 中国
// 而我们头部声称越南（+0700）。服务端把 IP / Accept-Language / 时区一对比，
// 立刻能看出这是台中国机器 —— 注册出来的号拿不到 Plus 试用。
//
// 这里按 IANA 名算出**目标时区在“当前这一刻”的偏移**（含夏令时），
// 再伪造 getTimezoneOffset / toString / toTimeString / toDateString / toLocaleString。
const _tzOffsetMinutes = (() => {
  try {
    const dtf = new OrigDTF('en-US', {
      timeZone: targetTz, hour12: false,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
    const parts = {};
    for (const p of dtf.formatToParts(new Date())) parts[p.type] = p.value;
    // 把目标时区的“墙上时间”当成 UTC 解析，减去真实 UTC，差就是偏移
    const asUTC = Date.UTC(
      Number(parts.year), Number(parts.month) - 1, Number(parts.day),
      Number(parts.hour) % 24, Number(parts.minute), Number(parts.second),
    );
    return Math.round((asUTC - Math.floor(Date.now() / 1000) * 1000) / 60000);
  } catch (_) {
    return 0;
  }
})();

// Date.toString() 括号里那一段是 **ICU 长时区名**，不是 "GMT+07:00"。
// 真 V8 实测（本机 node，同 locale 口径）：
//     en-US -> (Indochina Time)      vi-VN -> (Giờ Đông Dương)
//     th-TH -> (เวลาอินโดจีน)         id-ID -> (Waktu Indochina)
//     pt-BR -> (Horário da Indochina)
// 旧代码拼的是 "(GMT+07:00 Standard Time)" —— 任何真实浏览器都不会产生这个串，
// 等于把 §9 修掉的时区不一致换个形式又漏一遍。
const _tzLongName = (() => {
  try {
    const s = new OrigDTF(pageLocale, { timeZone: targetTz, timeZoneName: 'long' })
      .formatToParts(new Date()).find((p) => p.type === 'timeZoneName');
    return s ? s.value : null;
  } catch (_) { return null; }
})();
// 兜底：ICU 拿不到长名时退化成 "GMT+07:00"（小 ICU 精简版的真实输出就是这个）
const _tzParen = _tzLongName || _tzLong;

// ⚠️ 注意符号：_tzOffsetMinutes 是「墙上时间 - UTC」（越南=+420），
// 而 getTimezoneOffset() 返回的是「UTC - 墙上时间」（越南=-420）—— 两者相反。
// 显示标号用前者：+420 -> GMT+0700。
const _tzSign = _tzOffsetMinutes >= 0 ? '+' : '-';
const _tzAbs = Math.abs(_tzOffsetMinutes);
const _tzLabel = 'GMT' + _tzSign
  + String(Math.floor(_tzAbs / 60)).padStart(2, '0') + String(_tzAbs % 60).padStart(2, '0');
const _tzLong = 'GMT' + _tzSign
  + String(Math.floor(_tzAbs / 60)).padStart(2, '0') + ':' + String(_tzAbs % 60).padStart(2, '0');

const _DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const _MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const _pad = (n) => String(n).padStart(2, '0');

// 把真实 Date 的 UTC 毫秒平移到“目标时区的墙上时间”
const _shifted = (self) => new Date(self.getTime() + _tzOffsetMinutes * 60000);

Date.prototype.getTimezoneOffset = function () { return -_tzOffsetMinutes; };
Date.prototype.toString = function () {
  const d = _shifted(this);
  return _DAYS[d.getUTCDay()] + ' ' + _MONTHS[d.getUTCMonth()] + ' ' + _pad(d.getUTCDate())
    + ' ' + d.getUTCFullYear() + ' ' + _pad(d.getUTCHours()) + ':' + _pad(d.getUTCMinutes())
    + ':' + _pad(d.getUTCSeconds()) + ' ' + _tzLabel + ' (' + _tzParen + ')';
};
Date.prototype.toTimeString = function () {
  const d = _shifted(this);
  return _pad(d.getUTCHours()) + ':' + _pad(d.getUTCMinutes()) + ':' + _pad(d.getUTCSeconds())
    + ' ' + _tzLabel + ' (' + _tzParen + ')';
};
Date.prototype.toDateString = function () {
  const d = _shifted(this);
  return _DAYS[d.getUTCDay()] + ' ' + _MONTHS[d.getUTCMonth()] + ' ' + _pad(d.getUTCDate())
    + ' ' + d.getUTCFullYear();
};
Date.prototype.toLocaleString = function () { return this.toString(); };
Date.prototype.toLocaleDateString = function () { return this.toDateString(); };
Date.prototype.toLocaleTimeString = function () { return this.toTimeString(); };

globalThis.__tzProbe = {
  target: targetTz, canonical: tzCanonical, locale: pageLocale,
  offsetMin: _tzOffsetMinutes, label: _tzLabel, paren: _tzParen,
  sample: new Date().toString(),
  timeSample: new Date().toTimeString(),
  resolvedViaPatched: new PatchedDTF('en-US', {}).resolvedOptions().timeZone,
  resolvedViaRawIcu: new OrigDTF('en-US', { timeZone: targetTz }).resolvedOptions().timeZone,
};

const navigatorObj = {
  userAgent: String(input.user_agent || 'Mozilla/5.0'),
  language: String(input.language || 'en-US'),
  languages: Array.isArray(input.languages) ? input.languages : ['en-US', 'en'],
  hardwareConcurrency: Number(input.hardware_concurrency || 8),
  platform: navPlatform,
  vendor: navVendor,
  maxTouchPoints: Number(input.max_touch_points || 0),
  webdriver: false,
  onLine: true,
  cookieEnabled: true,
  doNotTrack: null,
  appCodeName: 'Mozilla',
  appName: 'Netscape',
  appVersion: '5.0',
  product: 'Gecko',
  productSub: '20030107',
  vendorSub: '',
  connection: { effectiveType: '4g', rtt: 50, downlink: 10, saveData: false },
  plugins: { length: 5 },
  mimeTypes: { length: 2 },
  mediaDevices: { enumerateDevices: async () => [] },
  getBattery: async () => ({ charging: true, chargingTime: 0, dischargingTime: Infinity, level: 1 }),
  sendBeacon: () => true,
  permissions: { query: async () => ({ state: 'prompt' }) },
};
if (input.device_memory != null && !Number.isNaN(Number(input.device_memory))) {
  navigatorObj.deviceMemory = Number(input.device_memory);
}

const cryptoObj = {
  getRandomValues: (arr) => { cryptoMod.randomFillSync(arr); return arr; },
};
if (typeof cryptoMod.randomUUID === 'function') {
  let usedSid = false;
  cryptoObj.randomUUID = () => {
    if (!usedSid && input.sentinel_sid) { usedSid = true; return String(input.sentinel_sid); }
    return cryptoMod.randomUUID();
  };
}
if (cryptoMod.webcrypto && cryptoMod.webcrypto.subtle) {
  cryptoObj.subtle = cryptoMod.webcrypto.subtle;
}

const context = {
  // stdout 专用于协议 JSON；SDK 日志不得污染输出。
  console: new console.Console({ stdout: process.stderr, stderr: process.stderr }),
  setTimeout: sdkSetTimeout,
  clearTimeout: sdkClearTimeout,
  setInterval: sdkSetInterval,
  clearInterval: sdkClearInterval,
  queueMicrotask,
  Promise,
  URL,
  URLSearchParams,
  Math,
  // ⚠️ 载重行，别删：把 **node 那个已被 patch 过的 Date** 显式塞进 VM。
  // vm.createContext() 会给新上下文一整套独立 intrinsic；不传这一行的话，
  // 上面 Date.prototype.* 的时区伪造对 SDK **完全无效** —— 实测只传 Intl 不传 Date 时，
  // VM 里 new Date().toString() 仍然是宿主机的 GMT+0800，时区裸奔原样复现。
  // （2026-09-22 排查确认：这条和 Intl 那条是时区伪造的两个入口，缺一不可。）
  Date,
  JSON,
  Array,
  Object,
  String,
  Number,
  Boolean,
  RegExp,
  Function,
  Symbol,
  Reflect,
  Proxy,
  Error,
  TypeError,
  RangeError,
  ReferenceError,
  SyntaxError,
  Map,
  Set,
  WeakMap,
  WeakSet,
  Int8Array,
  Uint8Array,
  Uint8ClampedArray,
  Int16Array,
  Uint16Array,
  Int32Array,
  Uint32Array,
  Float32Array,
  Float64Array,
  ArrayBuffer,
  DataView,
  TextEncoder,
  TextDecoder,

  btoa: (s) => Buffer.from(String(s || ''), 'binary').toString('base64'),
  atob: (s) => Buffer.from(String(s || ''), 'base64').toString('binary'),
  unescape,
  encodeURIComponent,
  decodeURIComponent,
  encodeURI,
  decodeURI,
  parseInt,
  parseFloat,
  isFinite,
  isNaN,
  NaN,
  Infinity,
  undefined,
  Intl: { ...Intl, DateTimeFormat: PatchedDTF },

  crypto: cryptoObj,

  performance: {
    now: () => performance.now(),
    timeOrigin: performance.timeOrigin,
    memory: { jsHeapSizeLimit: 4294967296 },
    getEntriesByType: () => [],
    getEntriesByName: () => [],
    mark: () => {},
    measure: () => {},
  },

  screen: {
    width: screenW,
    height: screenH,
    availWidth: screenW,
    availHeight: screenH,
    colorDepth: 24,
    pixelDepth: 24,
    orientation: { type: 'landscape-primary', angle: 0 },
  },

  navigator: navigatorObj,

  history: {
    length: 1, state: null,
    back() {}, forward() {}, go() {},
    pushState() {}, replaceState() {},
  },

  localStorage: createStorage(),
  sessionStorage: createStorage(),

  innerWidth: screenW,
  innerHeight: screenH,
  outerWidth: screenW,
  outerHeight: screenH + 80,
  devicePixelRatio: Number(input.device_pixel_ratio || 1),
  scrollX: 0,
  scrollY: 0,
  pageXOffset: 0,
  pageYOffset: 0,

  requestAnimationFrame: (cb) => sdkSetTimeout(() => cb(performance.now()), 16),
  cancelAnimationFrame: sdkClearTimeout,
  requestIdleCallback: (cb) => {
    if (typeof cb === 'function') cb({ didTimeout: false, timeRemaining: () => 50 });
    return 1;
  },
  cancelIdleCallback: () => {},

  getComputedStyle: () => ({ getPropertyValue() { return ''; } }),
  matchMedia: (query) => ({
    media: String(query || ''),
    matches: false,
    onchange: null,
    addListener() {}, removeListener() {},
    addEventListener() {}, removeEventListener() {},
    dispatchEvent() { return false; },
  }),

  Event: class Event {
    constructor(type, init) {
      this.type = type;
      this.bubbles = (init && init.bubbles) || false;
      this.cancelable = (init && init.cancelable) || false;
    }
  },
  CustomEvent: class CustomEvent {
    constructor(type, init) {
      this.type = type;
      this.detail = init && Object.prototype.hasOwnProperty.call(init, 'detail') ? init.detail : null;
    }
  },
  MessageChannel: class MessageChannel {
    constructor() {
      this.port1 = { postMessage() {}, addEventListener() {}, removeEventListener() {}, start() {}, close() {} };
      this.port2 = { postMessage() {}, addEventListener() {}, removeEventListener() {}, start() {}, close() {} };
    }
  },

  chrome: { runtime: {}, app: {} },
  CSS: { supports() { return true; } },
  indexedDB: {
    open() { return { onerror: null, onsuccess: null, onupgradeneeded: null, result: {}, error: null }; },
    deleteDatabase() { return {}; },
  },

  fetch: async () => { throw new Error('fetch should not be called'); },
  postMessage: () => {},

  addEventListener: addListener,
  removeEventListener: removeListener,
  dispatchEvent: (event) => { dispatch(event.type, event); return true; },

  origin: 'https://auth.openai.com',

  location: {
    href: 'https://auth.openai.com/',
    origin: 'https://auth.openai.com',
    protocol: 'https:',
    host: 'auth.openai.com',
    hostname: 'auth.openai.com',
    pathname: '/',
    search: '',
    hash: '',
    assign() {},
    replace() {},
    reload() {},
  },

  document: {
    readyState: 'complete',
    hidden: false,
    visibilityState: 'visible',
    referrer: 'https://auth.openai.com/',
    URL: 'https://auth.openai.com/',
    documentURI: 'https://auth.openai.com/',
    location: {
      href: 'https://auth.openai.com/',
      origin: 'https://auth.openai.com',
      pathname: '/',
      search: '',
    },
    cookie: input.cookie != null ? String(input.cookie) : 'oai-did=' + encodeURIComponent(input.device_id || ''),
    title: '',
    characterSet: 'UTF-8',
    contentType: 'text/html',
    scripts,
    currentScript: {
      src: 'https://sentinel.openai.com/sentinel/sdk.js',
      getAttribute() { return null; },
    },
    documentElement,
    body: bodyEl,
    head: genericElement('head'),
    createElement(tag) {
      const t = String(tag || '').toLowerCase();
      if (t === 'canvas') return canvasElement();
      if (t === 'iframe') {
        iframeObject = genericElement('iframe');
        iframeObject._load = [];
        iframeObject.addEventListener = (type, cb) => {
          if (type === 'load') iframeObject._load.push(cb);
        };
        iframeObject.removeEventListener = () => {};
        iframeObject.contentWindow = {
          postMessage(message, origin) {
            capturedProof = message.p;
            const result = input.action === 'solve'
              ? { cachedChatReq: input.challenge, cachedProof: input.request_p || message.p }
              : null;
            const ev = {
              source: iframeObject.contentWindow,
              data: { type: 'response', requestId: message.requestId, result },
              origin,
            };
            setTimeout(() => {
              for (const cb of [...(_listeners.get('message') || [])]) {
                try { cb(ev); } catch (_) {}
              }
            }, 0);
          },
        };
        return iframeObject;
      }
      const el = genericElement(tag);
      if (t === 'script') scripts.push(el);
      return el;
    },
    createElementNS(_ns, tag) { return this.createElement(tag); },
    createDocumentFragment() { return genericElement('fragment'); },
    createTextNode(text) { return { nodeType: 3, textContent: text }; },
    createComment(text) { return { nodeType: 8, textContent: text }; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    getElementById() { return null; },
    getElementsByTagName(tag) { return tag === 'script' ? scripts : []; },
    getElementsByClassName() { return []; },
    addEventListener: addListener,
    removeEventListener: removeListener,
    dispatchEvent(event) { dispatch(event.type, event); return true; },
  },
};

context.window = context;
context.globalThis = context;
context.self = context;
context.top = context;
context.parent = context;

// ─── Create VM sandbox & load SDK ──────────────────────────────

vm.createContext(context);
vm.runInContext(sdk, context, { timeout: 10000 });

// ─── Hook 生效校验（替换"命中"≠"生效"） ───────────────────────
// 锚点命中不代表出口可用：SDK 可能在别处遮蔽/重赋值，或锚点命中了但不是我们要的那处。
// 加载后按类型硬校验三个出口；缺任何一个就 exit 3，绝不带着半个 hook 往下跑。
// 这样"hook 断了"和"PoW 算不出来"在日志里再也不会长得一样。
const hookTypes = {
  __debugP: typeof context.__debugP,
  __debug_n: typeof (context.SentinelSDK && context.SentinelSDK.__debug_n),
  __debug_bindProof: typeof (context.SentinelSDK && context.SentinelSDK.__debug_bindProof),
  SentinelSDK: typeof context.SentinelSDK,
};
const hookMissing = [];
if (hookTypes.__debugP !== 'object')
  hookMissing.push('globalThis.__debugP (want object, got ' + hookTypes.__debugP + ')');
if (hookTypes.__debug_n !== 'function')
  hookMissing.push('SentinelSDK.__debug_n (want function, got ' + hookTypes.__debug_n + ')');
if (hookTypes.__debug_bindProof !== 'function')
  hookMissing.push('SentinelSDK.__debug_bindProof (want function, got ' + hookTypes.__debug_bindProof + ')');
if (hookTypes.SentinelSDK === 'undefined')
  hookMissing.push('globalThis.SentinelSDK (undefined)');
if (hookMissing.length) {
  process.stderr.write(
    '[sentinel-hook] POST-LOAD CHECK FAILED: ' + hookMissing.join('; ') + '\n' +
    '[sentinel-hook] 三处替换字符串需按当前 sdk.js 重新定位（见 exports/revive/F1-sentinel-fix.md）\n'
  );
  process.exit(HOOK_EXIT_CODE);
}
if (process.env.SENTINEL_HOOK_DEBUG) {
  process.stderr.write('[sentinel-hook] OK ' + JSON.stringify(hookTypes) + '\n');
}

// ─── Behavior simulation ──────────────────────────────────────

function _rng(min, max) {
  return min + Math.floor(Math.random() * Math.max(1, max - min + 1));
}

async function dispatchBehavior(durationMs) {
  if (durationMs <= 0) return;
  const started = Date.now();
  const moves = _rng(12, 16);
  let x = _rng(260, 420);
  let y = _rng(180, 300);
  for (let i = 0; i < moves; i++) {
    const dx = _rng(5, 18);
    const dy = _rng(-4, 12);
    x += dx;
    y += dy;
    await new Promise(r => setTimeout(r, _rng(70, 145)));
    await dispatch('pointermove', {
      clientX: x, clientY: y, screenX: x, screenY: y,
      movementX: dx, movementY: dy, buttons: 0,
    });
  }
  await new Promise(r => setTimeout(r, _rng(90, 220)));
  await dispatch('click', {
    clientX: x, clientY: y, screenX: x, screenY: y, button: 0, buttons: 0,
  });
  for (let i = 0; i < _rng(3, 4); i++) {
    await new Promise(r => setTimeout(r, _rng(80, 180)));
    context.scrollY = (context.scrollY || 0) + _rng(35, 120);
    context.pageYOffset = context.scrollY;
    await dispatch('scroll', { scrollX: 0, scrollY: context.scrollY });
  }
  await new Promise(r => setTimeout(r, _rng(80, 160)));
  await dispatch('wheel', {
    deltaX: 0, deltaY: _rng(70, 140), clientX: x, clientY: y,
  });
  const keys = ['L', 'u', 'Tab'];
  for (const key of keys) {
    await new Promise(r => setTimeout(r, _rng(90, 210)));
    await dispatch('keydown', {
      key,
      code: key === 'Tab' ? 'Tab' : 'Key' + key.toUpperCase(),
      repeat: false, altKey: false, ctrlKey: false, metaKey: false,
    });
  }
  const remaining = Math.max(0, Number(durationMs || 0) - (Date.now() - started));
  if (remaining > 0) await new Promise(r => setTimeout(r, remaining));
}

// ─── Main ──────────────────────────────────────────────────────

(async () => {
  const action = input.action;
  const flow = String(input.flow || 'authorize_continue');

  // 只读探针：把伪造出来的时区画像原样吐出来，供"真 node vs 伪造版"并排对比。
  // 不碰 SDK、不发请求 —— 改时区逻辑后先用这个复验，别动辄跑整条 Sentinel。
  if (action === 'tzprobe') {
    // 从 **node 的** globalThis 读，不是 context：__tzProbe 故意不放进 context，
    // 免得 SDK 在 VM 里看见这个全局名（多一个可探测的指纹面）。
    process.stdout.write(JSON.stringify(globalThis.__tzProbe));
    return;
  }

  if (action === 'requirements') {
    try {
      await withTimeout(context.SentinelSDK.init(flow), 8000, 'init timeout');
      if (capturedProof) {
        process.stdout.write(JSON.stringify({ request_p: capturedProof }));
        return;
      }
    } catch (_) {}
    const requestP = await context.__debugP.getRequirementsToken();
    process.stdout.write(JSON.stringify({ request_p: requestP }));
    return;
  }

  if (action === 'solve') {
    const behaviorMs = Number(input.behavior_duration_ms ?? 4200);

    try {
      const mainToken = await withTimeout(context.SentinelSDK.token(flow), 8000, 'SDK token timeout');
      if (mainToken) {
        await dispatchBehavior(behaviorMs);
        let soToken = '';
        try {
          soToken = await withTimeout(context.SentinelSDK.sessionObserverToken(flow), 5000, 'SO timeout');
        } catch (_) {
          soToken = '';
        }
        process.stdout.write(JSON.stringify({ token: mainToken, so_token: soToken || '' }));
        return;
      }
    } catch (_) {}

    const challenge = input.challenge || {};
    const requestP = String(input.request_p || '').trim();
    if (!requestP) throw new Error('missing request_p');
    const finalP = await context.__debugP.getEnforcementToken(challenge);
    context.SentinelSDK.__debug_bindProof(challenge, requestP);
    const dx = challenge && challenge.turnstile ? challenge.turnstile.dx : null;
    const tValue = dx ? await context.SentinelSDK.__debug_n(challenge, dx) : null;
    // 坑 B：这里以前只吐 {final_p, t, so_token}，而 sentinel_quickjs.py:307 读的是
    // solved["token"] —— 键名对不上，回退分支**必然**判失败（哪怕 hook 全修好）。
    // 上层拿到的是 openai-sentinel-token 头的原值，必须是 {p,t,c,id,flow} 的 JSON 串，
    // 所以这里直接拼出完整 token，而不是只塞一个裸的 p（裸 p 会让请求照样被拒，
    // 等于把"回退失败"从"响亮"变回"静默"）。
    // c 就是 challenge.token，id 就是 device_id（已用真实 token 对照验证）。
    const fallbackToken = JSON.stringify({
      p: String(finalP || ''),
      t: tValue == null ? '' : String(tValue),
      c: String((challenge && challenge.token) || ''),
      id: String(input.device_id || ''),
      flow,
    });
    process.stdout.write(JSON.stringify({
      token: fallbackToken,
      final_p: finalP,
      t: tValue,
      so_token: '',
      fallback: true,
    }));
    return;
  }

  throw new Error('unsupported action: ' + action);
})().catch(err => {
  process.stderr.write(String((err && err.stack) || err));
  process.exitCode = 1;
}).finally(() => {
  for (const timer of sdkTimeouts) clearTimeout(timer);
  for (const timer of sdkIntervals) clearInterval(timer);
});
