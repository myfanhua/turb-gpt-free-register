const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync('webui/templates/sms_tiger_catalog.js', 'utf8');
let markup = '';
const root = {set innerHTML(value) {markup = value;}};
const elements = new Map();
function getElementById(id) {
  if (id === 'tigerSmsCatalog') return root;
  if (!elements.has(id)) elements.set(id, {innerHTML:'', textContent:'', disabled:false});
  return elements.get(id);
}
const requests = [];
const pending = {};
const sandbox = vm.createContext({
  CONFIG: [{key:'SMS_SERVICE', value:'dr'}, {key:'SMS_COUNTRY', value:'6'}],
  CONFIG_PENDING_UPDATES: pending,
  document: {getElementById, querySelectorAll: () => []},
  esc: v => String(v ?? ''), attrEsc: v => String(v ?? ''),
  api: async url => {
    requests.push(url);
    return url.includes('?') ? {ok:true, countries:[
      {id:'6', name:'美国', price:10, count:1}, {id:'19', name:'巴西', price:2, count:3}
    ]} : {ok:true, services:[{code:'dr',name:'OpenAI'},{code:'tg',name:'Telegram'}]};
  },
});
const run = s => vm.runInContext(s, sandbox);
vm.runInContext(code, sandbox);
(async () => {
  await run('loadTigerCatalog()');
  assert.equal(requests.length, 2);
  assert.match(markup, /购买最高单价（USD）/);
  assert.match(markup, /data-key="SMS_MAX_PRICE"/);
  assert.match(markup, /Tiger SMS 项目过滤/);
  assert.match(markup, /Tiger SMS 国家过滤/);
  assert.match(markup, /class="tiger-selectors"/);
  assert.match(markup, /class="tiger-prices"/);
  assert.doesNotMatch(markup, /class="fld"|style="display:flex/); // 避免旧版字段卡片叠加及随意换行。
  assert.match(run('renderTigerCatalogTools()'), /@container \(max-width:540px\)/);
  const beforeFilterMarkup = markup;
  run("filterTigerCatalog('services', '  TELEgram ')");
  assert.match(elements.get('tigerServiceSelect').innerHTML, /Telegram/);
  assert.match(elements.get('tigerServiceSelect').innerHTML, /当前：OpenAI.*不匹配筛选/);
  assert.equal(elements.get('tigerServiceCount').textContent, '匹配 1 / 2 个项目');
  assert.equal(markup, beforeFilterMarkup); // 输入过滤不重建整个面板，不丢失输入焦点。
  assert.equal(run("tigerConfigValue('SMS_SERVICE')"), 'dr');
  run("filterTigerCatalog('services', 'dr')");
  assert.doesNotMatch(elements.get('tigerServiceSelect').innerHTML, /Telegram/);
  run("filterTigerCatalog('countries', '巴西')");
  assert.match(elements.get('tigerCountrySelect').innerHTML, /巴西/);
  assert.match(elements.get('tigerCountrySelect').innerHTML, /当前：美国.*不匹配筛选/);
  assert.equal(elements.get('tigerCountryCount').textContent, '匹配 1 / 2 个国家');
  assert.equal(run("tigerConfigValue('SMS_COUNTRY')"), '6');
  run("filterTigerCatalog('countries', '2 3')"); // 单价和库存多词匹配。
  assert.equal(elements.get('tigerCountryCount').textContent, '匹配 1 / 2 个国家');
  run("filterTigerCatalog('countries', '19')");
  assert.equal(elements.get('tigerCountryCount').textContent, '匹配 1 / 2 个国家');
  run("filterTigerCatalog('countries', '不存在')");
  assert.match(elements.get('tigerCountrySelect').innerHTML, /无匹配国家/);
  assert.equal(elements.get('tigerCountrySelect').disabled, true);
  run('paintTigerCatalog()');
  assert.match(markup, /value="不存在"/); // 排序/刷新重绘后保留过滤词。
  run("filterTigerCatalog('services', '');filterTigerCatalog('countries', '')");
  assert.equal(elements.get('tigerCountrySelect').disabled, false);
  assert.equal(elements.get('tigerCountryCount').textContent, '匹配 2 / 2 个国家');
  assert.equal(requests.length, 2); // 本地过滤不发网络请求。
  run("setTigerConfig('SMS_MAX_PRICE', '0.15')");
  assert.equal(pending.SMS_MAX_PRICE, '0.15');
  run('paintTigerCatalog()');
  assert.match(markup, /value="0.15"/);
  assert.ok(markup.indexOf('巴西') < markup.indexOf('美国'));
  assert.match(markup, /value="6" selected/);
  run("TIGER_CATALOG.sort='desc';paintTigerCatalog()");
  assert.ok(markup.indexOf('美国') < markup.indexOf('巴西'));
  run("selectTigerCountry('19')");
  assert.equal(pending.SMS_COUNTRY, '19');
  run("selectTigerCountry('unknown')");
  assert.equal(pending.SMS_COUNTRY, '19');
  // 同一国家多档报价，按该国最低有库存价格排序，而不是推荐均价。
  run(`TIGER_CATALOG.countries[0].buckets = [{price:'0.2500',count:15},{price:'0.1500',count:20}];
       TIGER_CATALOG.countries[1].buckets = [{price:'2.0000',count:3},{price:'3.0000',count:2}];
       TIGER_CATALOG.sort='asc';paintTigerCatalog()`);
  assert.match(markup, /Tiger SMS 价格档位/);
  assert.ok(markup.indexOf('美国') < markup.indexOf('巴西'));
  run("selectTigerCountry('6')");
  assert.match(elements.get('tigerPriceTierSelect').innerHTML, /\$0.1500 · 库存 20/);
  assert.match(elements.get('tigerPriceTierSelect').innerHTML, /\$0.2500 · 库存 15/);
  assert.ok(elements.get('tigerPriceTierSelect').innerHTML.indexOf('0.1500') < elements.get('tigerPriceTierSelect').innerHTML.indexOf('0.2500'));
  run("selectTigerPriceTier('0.1500')");
  assert.equal(pending.SMS_MAX_PRICE, '0.1500');
  assert.match(elements.get('tigerPriceTierSelect').innerHTML, /value="0.1500" selected/);
  run("setTigerMaxPrice('0.20')");
  assert.match(elements.get('tigerPriceTierSelect').innerHTML, /自定义上限：\$0.20/);
  run("selectTigerPriceTier('999')");
  assert.equal(pending.SMS_MAX_PRICE, '0.20');
  run("selectTigerCountry('19')");
  assert.match(elements.get('tigerPriceTierSelect').innerHTML, /\$2.0000 · 库存 3/);
  assert.doesNotMatch(elements.get('tigerPriceTierSelect').innerHTML, /0.1500/);
  assert.equal(pending.SMS_MAX_PRICE, '0.20'); // 换国家不自动提高价格上限。
  run("selectTigerPriceTier('0.1500')");
  assert.equal(pending.SMS_MAX_PRICE, '0.20'); // 不能误用其他国家的档位。
  run("selectTigerPriceTier('2.0000')");
  assert.equal(pending.SMS_MAX_PRICE, '2.0000');
  run("TIGER_CATALOG.busy=true;selectTigerPriceTier('3.0000');TIGER_CATALOG.busy=false");
  assert.equal(pending.SMS_MAX_PRICE, '2.0000');
  run("TIGER_CATALOG.sort='desc';updateTigerPriceTiers()");
  assert.ok(elements.get('tigerPriceTierSelect').innerHTML.indexOf('3.0000') < elements.get('tigerPriceTierSelect').innerHTML.indexOf('2.0000'));
  await run("setTigerConfig('SMS_SERVICE', 'tg');loadTigerCatalog()");
  assert.match(requests.at(-1), /service=tg/);
  assert.equal(requests.length, 3); // 项目切换复用项目列表，只刷新该项目国家。
  assert.equal(pending.SMS_COUNTRY, '19'); // 不擅自修改国家。
  let resolveOld;
  sandbox.api = url => url.endsWith('service=old')
    ? new Promise(resolve => { resolveOld = resolve; })
    : Promise.resolve({ok:true,countries:[{id:'20',name:'新国家',price:1,count:1}]});
  run("setTigerConfig('SMS_SERVICE', 'old')");
  const oldRequest = run('loadTigerCatalog()');
  run("setTigerConfig('SMS_SERVICE', 'new')");
  await run('loadTigerCatalog()');
  resolveOld({ok:true,countries:[{id:'10',name:'旧国家',price:9,count:1}]});
  await oldRequest;
  assert.match(markup, /新国家/);
  assert.doesNotMatch(markup, /旧国家/);
  sandbox.api = async () => {throw new Error('网络异常')};
  await run('loadTigerCatalog()');
  assert.match(markup, /加载失败/);
  assert.match(markup, /网络异常/);
  assert.equal(pending.SMS_COUNTRY, '19');
  console.log('Tiger SMS 国家动态加载、数值升降序、输入过滤、保留选择及请求竞态测试通过');
})().catch(e => { console.error(e); process.exitCode=1; });
