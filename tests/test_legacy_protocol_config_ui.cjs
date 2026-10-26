const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {execFileSync} = require('node:child_process');
const html = fs.readFileSync('webui/templates/index_legacy.html', 'utf8');
// 使用后端真实配置声明，确保旧 UI 能直接消费 API 返回的 choices。
const python = process.env.PYTHON || (fs.existsSync('.venv/bin/python') ? '.venv/bin/python' : 'python3');
const fields = JSON.parse(execFileSync(python, ['-c',
  'import json; from webui.config_editor import get_config; print(json.dumps([f for f in get_config() if f["key"] in ["PROTOCOL_HTTP_CLIENT", "CURL_CFFI_IMPERSONATE", "WREQ_EMULATION", "PROTOCOL_REGISTRATION_PHONE_ENABLED", "SEND_HIGH_ENTROPY_CLIENT_HINTS", "SENTINEL_SDK_AUTO_UPDATE", "SENTINEL_SDK_REFRESH_SECONDS", "SENTINEL_SO_COLLECTION_MS", "PROTOCOL_REUSE_FINGERPRINT_BY_EMAIL", "PROTOCOL_PHONE_RESEND_INTERVAL", "PROTOCOL_PHONE_MAX_RESENDS", "PROTOCOL_PHONE_CODE_ATTEMPTS"]]))'
], {encoding: 'utf8'}));
const phoneField = fields.find(f => f.key === 'PROTOCOL_REGISTRATION_PHONE_ENABLED');
assert.equal(phoneField.type, 'bool');
const field = fields.find(f => f.key === 'PROTOCOL_HTTP_CLIENT');
field.value = 'curl_cffi';
function extract(name, end) {
  const start = html.indexOf(`function ${name}(`);
  assert.notEqual(start, -1);
  return html.slice(start, html.indexOf(end, start));
}
const elements = new Map();
function element(selector) {
  if (!elements.has(selector)) elements.set(selector, {
    innerHTML: '', disabled: false, listeners: {},
    addEventListener(event, callback) { this.listeners[event] = callback; },
  });
  return elements.get(selector);
}
const requests = [];
const select = {dataset: {key: field.key}, value: 'wreq'};
select.closest = () => select;
const phoneToggle = {dataset: {key: phoneField.key}, value: 'true'};
phoneToggle.closest = () => phoneToggle;
const hintsToggle = {dataset: {key: 'SEND_HIGH_ENTROPY_CLIENT_HINTS'}, value: 'true'};
hintsToggle.closest = () => hintsToggle;
const profileSelects = ['CURL_CFFI_IMPERSONATE', 'WREQ_EMULATION'].map(key => {
  const el = {dataset: {key}, value: 'chrome136'};
  el.closest = () => el;
  return el;
});
const context = vm.createContext({
  CONFIG: fields, CONFIG_ACTIVE_GROUP: '协议配置', CONFIG_PENDING_UPDATES: {},
  $: element, $$: () => [select, ...profileSelects, phoneToggle, hintsToggle],
  esc: value => String(value ?? ''), attrEsc: value => String(value ?? ''),
  isPlaceholderEmpty: value => !value || value === '-',
  renderConfigRecommendedLinks: () => '', renderRoxyWorkspaceTools: () => '',
  bindRoxyWorkspaceTools() {}, showToast() {},
  async api(url, options) {
    requests.push({url, ...options});
    return {reloaded: true, updated: [field.key]};
  },
});
const run = code => vm.runInContext(code, context);
run(extract('renderConfigField', '\nasync function loadMailCodeCatalog'));
run(extract('renderConfigFields', '\nfunction renderConfigTabs'));
run(extract('renderConfigTabs', '\nfunction renderRoxyWorkspaceTools'));
run(extract('configGroups', '\nfunction renderConfigField'));
run(extract('renderConfigPanel', '\nasync function loadConfig'));
run(extract('readConfigElementValue', "\n$('#configForm')"));
run(html.slice(html.indexOf("$('#configForm').addEventListener('input'"),
               html.indexOf('// ---------- 全局复制委托')));
(async () => {
  run('renderConfigTabs()');
  assert.match(element('#configTabs').innerHTML, /data-config-group="协议配置"/);
  run('renderConfigPanel()');
  let markup = element('#configForm').innerHTML;
  assert.match(markup, /select data-key="PROTOCOL_HTTP_CLIENT"/);
  assert.match(markup, /value="curl_cffi" selected/);
  assert.match(markup, /value="wreq"/);
  assert.match(markup, /select data-key="CURL_CFFI_IMPERSONATE"/);
  assert.match(markup, /select data-key="WREQ_EMULATION"/);
  assert.doesNotMatch(markup, /纯协议 HTTP 客户端可选/);
  assert.match(markup, /PROTOCOL_REGISTRATION_PHONE_ENABLED/);
  assert.match(markup, /SEND_HIGH_ENTROPY_CLIENT_HINTS/);
  for (const key of ['SENTINEL_SDK_AUTO_UPDATE', 'SENTINEL_SDK_REFRESH_SECONDS', 'SENTINEL_SO_COLLECTION_MS'])
    assert.match(markup, new RegExp(key));
  element('#configForm').listeners.change({target: select});
  assert.equal(run('CONFIG_PENDING_UPDATES.PROTOCOL_HTTP_CLIENT'), 'wreq');
  run("CONFIG_ACTIVE_GROUP = '功能开关'; renderConfigPanel()");
  assert.doesNotMatch(element('#configForm').innerHTML, /data-key="PROTOCOL_/);
  run("CONFIG_ACTIVE_GROUP = '协议配置'; renderConfigPanel()");
  assert.equal(run('CONFIG_PENDING_UPDATES.PROTOCOL_HTTP_CLIENT'), 'wreq');
  assert.match(element('#configForm').innerHTML, /value="wreq" selected/);
  for (const el of profileSelects) element('#configForm').listeners.change({target: el});
  element('#configForm').listeners.change({target: phoneToggle});
  element('#configForm').listeners.change({target: hintsToggle});
  await element('#btnSaveConfig').listeners.click();
  assert.equal(requests[0].url, '/api/config');
  assert.equal(requests[0].method, 'POST');
  const saved = JSON.parse(requests[0].body).updates;
  assert.equal(saved.PROTOCOL_HTTP_CLIENT, 'wreq');
  assert.equal(saved.CURL_CFFI_IMPERSONATE, 'chrome136');
  assert.equal(saved.WREQ_EMULATION, 'chrome136');
  assert.equal(saved.PROTOCOL_REGISTRATION_PHONE_ENABLED, true);
  assert.equal(saved.SEND_HIGH_ENTROPY_CLIENT_HINTS, true);
  assert.equal(run("CONFIG.find(f => f.key === 'PROTOCOL_HTTP_CLIENT').value"), 'wreq');
  assert.equal(run('Object.keys(CONFIG_PENDING_UPDATES).length'), 0);
  assert.equal(element('#btnSaveConfig').disabled, false);
  const modern = fs.readFileSync('webui/templates/index.html', 'utf8');
  const start = modern.indexOf('function renderConfigSectionV2(');
  for (const f of fields) assert.equal(f.group, '协议配置');
  context.configGroupSlug = () => 'features';
  context.renderConfigSectionHead = () => '';
  const mixedStart = modern.indexOf('function renderMixedConfigSectionV2(');
  run(modern.slice(mixedStart, modern.indexOf('function codexOauthDriverChoices(', mixedStart)));
  const introStart = modern.indexOf('function configSectionIntro(');
  run(modern.slice(introStart, modern.indexOf('function configFeatureIcon(', introStart)));
  context.renderConfigPlainFieldV2 = context.renderConfigField;
  context.renderRegistrationDriverField = () => '';
  context.renderFeatureSwitchField = f => context.renderConfigField(f);
  run(modern.slice(start, modern.indexOf('function renderConfigLayoutV2(', start)));
  assert.equal(run("configSectionIntro('协议配置')"), "");
  const modernMarkup = run("renderConfigSectionV2('协议配置', CONFIG)");
  assert.match(modernMarkup, /select data-key="CURL_CFFI_IMPERSONATE"/);
  assert.match(modernMarkup, /select data-key="WREQ_EMULATION"/);
  assert.match(modernMarkup, /PROTOCOL_REGISTRATION_PHONE_ENABLED/);
  assert.match(modernMarkup, /SEND_HIGH_ENTROPY_CLIENT_HINTS/);
  for (const key of ['SENTINEL_SDK_AUTO_UPDATE', 'SENTINEL_SDK_REFRESH_SECONDS', 'SENTINEL_SO_COLLECTION_MS'])
    assert.match(modernMarkup, new RegExp(key));
  assert.match(modernMarkup, /data-config-section="协议配置"/);
  for (const f of fields) {
    assert.equal((modernMarkup.match(new RegExp('data-key="' + f.key + '"', 'g')) || []).length, 1);
    assert.match(markup, new RegExp(f.key));
  }
  const allFields = JSON.parse(execFileSync(python, ['-c',
    'import json; from webui.config_editor import get_config; print(json.dumps(get_config()))'
  ], {encoding: 'utf8'}));
  assert.equal(allFields.filter(f => f.group === '协议配置').length, fields.length);
  for (const key of ['ENABLE_2FA', 'ENABLE_CODEX_AUTO', 'ENABLE_FLOW_TRIGGER'])
    assert.equal(allFields.find(f => f.key === key).group, '功能开关');
  assert.equal(allFields.find(f => f.key === 'REGISTRATION_DRIVER').group, '注册方式');
  assert.equal(allFields.find(f => f.key === 'SMS_API_KEY').group, '接码平台');
  // 保存后切换Tab仍使用已保存值。
  run("CONFIG_ACTIVE_GROUP = '功能开关'; renderConfigPanel()");
  assert.doesNotMatch(element('#configForm').innerHTML, /data-key="PROTOCOL_/);
  run("CONFIG_ACTIVE_GROUP = '协议配置'; renderConfigPanel()");
  assert.match(element('#configForm').innerHTML, /value="wreq" selected/);
  // 新UI使用API分组自动生成独立导航及面板，不在其它分组重复渲染。
  context.document = {getElementById: id => ['configNavV2', 'configSectionsV2'].includes(id) ? element('#' + id) : null};
  context.configNavIcon = () => '';
  context.configGroupDisplayName = name => name;
  context.renderRegistrationDriverCard = () => {};
  context.bindRoxyWorkspaceToolsV2 = () => {};
  context.bindCloudMailToolsV2 = () => {};
  const layoutStart = modern.indexOf('function renderConfigLayoutV2(');
  run(modern.slice(layoutStart, modern.indexOf('function scrollToConfigSection(', layoutStart)));
  run('renderConfigLayoutV2()');
  assert.match(element('#configNavV2').innerHTML, /data-config-nav="协议配置"/);
  assert.match(element('#configSectionsV2').innerHTML, /data-config-section="协议配置"/);
  console.log('新旧 UI 协议后端及浏览器指纹：渲染、切换、保存测试通过');
})().catch(error => {console.error(error); process.exitCode = 1;});
