// 新旧配置页共用的 Tiger SMS 项目 → 国家/报价选择器。
const TIGER_CATALOG = { services: null, countries: [], service: '', sort: 'asc', busy: false, error: '', seq: 0, serviceFilter: '', countryFilter: '' };
function tigerConfigValue(key) {
  if (Object.prototype.hasOwnProperty.call(CONFIG_PENDING_UPDATES, key)) return String(CONFIG_PENDING_UPDATES[key] ?? '');
  return String((CONFIG.find(f => f.key === key) || {}).value ?? '');
}
function tigerServiceCode() {
  const value = tigerConfigValue('SMS_SERVICE').trim();
  return ['openai', 'chatgpt'].includes(value.toLowerCase()) ? 'dr' : value;
}
function renderTigerCatalogTools() {
  return `<style>
    #tigerSmsCatalog { grid-column:1/-1; min-width:0; width:100%; margin-top:8px; container-type:inline-size; }
    #tigerSmsCatalog * { box-sizing:border-box; }
    #tigerSmsCatalog .tiger-toolbar { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:16px; }
    #tigerSmsCatalog .tiger-title { font-size:14px; font-weight:700; color:var(--text,#303133); }
    #tigerSmsCatalog .tiger-refresh { flex:none; border:1px solid #dce3ed; background:#fff; border-radius:8px; padding:8px 12px; font-size:12px; color:#526077; cursor:pointer; }
    #tigerSmsCatalog .tiger-refresh:hover { border-color:#377dff; color:#377dff; }
    #tigerSmsCatalog .tiger-refresh:disabled { opacity:.5; cursor:wait; }
    #tigerSmsCatalog .tiger-selectors { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; }
    #tigerSmsCatalog .tiger-card { min-width:0; padding:16px; border:1px solid #e5eaf2; border-radius:12px; background:#fafbfd; }
    #tigerSmsCatalog .tiger-card-head { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:12px; }
    #tigerSmsCatalog .tiger-card-head label { margin:0; font-size:13px; font-weight:700; }
    #tigerSmsCatalog .tiger-count { font-size:11px; color:#9098a5; font-weight:400; white-space:nowrap; }
    #tigerSmsCatalog input, #tigerSmsCatalog select { display:block; width:100%; min-width:0; max-width:100%; height:40px; margin:0; padding:0 11px; border:1px solid #dce3ed; border-radius:7px; background:#fff; color:#303b4c; font-family:inherit; font-size:13px; font-weight:400; outline:none; text-overflow:ellipsis; }
    #tigerSmsCatalog input:focus, #tigerSmsCatalog select:focus { border-color:#377dff; box-shadow:0 0 0 3px #377dff12; }
    #tigerSmsCatalog input::placeholder { color:#a0a8b4; font-weight:400; }
    #tigerSmsCatalog select:disabled { color:#a0a8b4; background:#f5f7fa; }
    #tigerSmsCatalog .tiger-search { margin-bottom:10px; }
    #tigerSmsCatalog .tiger-prices { display:grid; grid-template-columns:minmax(0,1.5fr) minmax(0,1fr) minmax(0,1fr); gap:16px; margin-top:20px; }
    #tigerSmsCatalog .tiger-field { display:flex; flex-direction:column; min-width:0; gap:8px; margin:0; font-size:12px; font-weight:600; color:#526077; }
    #tigerSmsCatalog .tiger-status { margin-top:12px; font-size:12px; line-height:1.5; color:#9098a5; overflow-wrap:anywhere; }
    #tigerSmsCatalog .tiger-status:empty { display:none; }
    @container (max-width:540px) {
      #tigerSmsCatalog .tiger-selectors, #tigerSmsCatalog .tiger-prices { grid-template-columns:minmax(0,1fr); gap:12px; }
      #tigerSmsCatalog .tiger-prices { margin-top:16px; }
    }
  </style><div id="tigerSmsCatalog" aria-live="polite"></div>`;
}
function tigerPriceBuckets(country) {
  return (country?.buckets || []).filter(bucket => Number.isFinite(Number(bucket.price)) && Number(bucket.price) > 0 && bucket.count > 0);
}
function tigerCountryPrice(country) {
  const buckets = tigerPriceBuckets(country);
  return buckets.length ? Math.min(...buckets.map(bucket => Number(bucket.price))) : Number(country.price);
}
function tigerPriceTierView() {
  const country = TIGER_CATALOG.service === tigerServiceCode()
    ? TIGER_CATALOG.countries.find(item => item.id === tigerConfigValue('SMS_COUNTRY')) : null;
  const buckets = [...tigerPriceBuckets(country)].sort((a, b) => TIGER_CATALOG.sort === 'desc'
    ? Number(b.price) - Number(a.price) : Number(a.price) - Number(b.price));
  const cap = tigerConfigValue('SMS_MAX_PRICE').trim();
  const selected = buckets.find(bucket => cap !== '' && Number(bucket.price) === Number(cap));
  return {
    options: (!selected ? tigerOption('', cap ? `自定义上限：$${cap}` : '请选择价格档位', true) : '')
      + buckets.map(bucket => tigerOption(bucket.price, `$${bucket.price} · 库存 ${bucket.count}`, bucket === selected)).join('')
      + (!buckets.length ? '<option disabled>暂无可用价格档位</option>' : ''),
    disabled: TIGER_CATALOG.busy || !buckets.length,
  };
}
function updateTigerPriceTiers() {
  const select = document.getElementById('tigerPriceTierSelect');
  if (!select) return;
  const view = tigerPriceTierView();
  select.innerHTML = view.options;
  select.disabled = view.disabled;
}
function selectTigerPriceTier(value) {
  if (TIGER_CATALOG.busy || TIGER_CATALOG.service !== tigerServiceCode()) return;
  const country = TIGER_CATALOG.countries.find(item => item.id === tigerConfigValue('SMS_COUNTRY'));
  const bucket = tigerPriceBuckets(country).find(item => String(item.price) === value);
  if (!bucket) return;
  setTigerConfig('SMS_MAX_PRICE', String(bucket.price));
  updateTigerPriceTiers();
}
function setTigerMaxPrice(value) {
  setTigerConfig('SMS_MAX_PRICE', value);
  updateTigerPriceTiers();
}
function sortedTigerCountries(countries, direction) {
  return [...countries].sort((a, b) => (direction === 'desc' ? tigerCountryPrice(b) - tigerCountryPrice(a) : tigerCountryPrice(a) - tigerCountryPrice(b)) || String(a.id).localeCompare(String(b.id)));
}
function filterTigerItems(items, query, fields) {
  const terms = String(query || '').trim().toLowerCase().split(/\s+/).filter(Boolean);
  return items.filter(item => {
    const text = fields.map(key => String(item[key] ?? '')).join(' ').toLowerCase();
    return terms.every(term => text.includes(term));
  });
}
function tigerOption(value, label, selected) {
  return `<option value="${attrEsc(value)}"${selected ? ' selected' : ''}>${esc(label)}</option>`;
}
function tigerFilteredCatalog() {
  const service = tigerServiceCode(), country = tigerConfigValue('SMS_COUNTRY');
  const allServices = TIGER_CATALOG.services || [];
  const allCountries = TIGER_CATALOG.service === service ? TIGER_CATALOG.countries : [];
  const services = filterTigerItems(allServices, TIGER_CATALOG.serviceFilter, ['name', 'code']);
  const countries = sortedTigerCountries(filterTigerItems(allCountries.map(item => ({...item,
    priceTerms: tigerPriceBuckets(item).map(bucket => `${bucket.price} ${bucket.count}`).join(' ')})), TIGER_CATALOG.countryFilter,
    ['name', 'id', 'price', 'count', 'priceTerms']), TIGER_CATALOG.sort);
  const serviceLabel = item => `${item.name} (${item.code})`;
  const countryLabel = item => `${item.name} (${item.id}) · $${tigerCountryPrice(item)}${tigerPriceBuckets(item).length ? ' 起 · ' + tigerPriceBuckets(item).length + '档' : ''} · 总库存 ${item.count}`;
  const selectedService = allServices.find(item => item.code === service);
  const selectedCountry = allCountries.find(item => item.id === country);
  // 筛选只影响可选列表，始终保留当前值，不自动选中第一个匹配结果。
  const serviceOptions = (!services.some(item => item.code === service)
    ? tigerOption(service, selectedService ? `当前：${serviceLabel(selectedService)}（不匹配筛选）` : service || '请选择项目', true) : '')
    + services.map(item => tigerOption(item.code, serviceLabel(item), item.code === service)).join('')
    + (!services.length ? '<option disabled>无匹配项目</option>' : '');
  const countryOptions = (!countries.some(item => item.id === country)
    ? tigerOption(country, selectedCountry ? `当前：${countryLabel(selectedCountry)}（不匹配筛选）`
      : country ? `当前：${country}（不在本项目可用列表）` : '请选择国家', true) : '')
    + countries.map(item => tigerOption(item.id, countryLabel(item), item.id === country)).join('')
    + (!countries.length ? '<option disabled>无匹配国家</option>' : '');
  return { serviceOptions, countryOptions, countryDisabled: TIGER_CATALOG.busy || !countries.length,
    serviceCount: `匹配 ${services.length} / ${allServices.length} 个项目`,
    countryCount: `匹配 ${countries.length} / ${allCountries.length} 个国家` };
}
function filterTigerCatalog(kind, value) {
  if (kind !== 'services' && kind !== 'countries') return;
  TIGER_CATALOG[kind === 'services' ? 'serviceFilter' : 'countryFilter'] = value;
  const view = tigerFilteredCatalog();
  // 仅更新选项与计数，不重建输入框，保持连续输入、光标和中文输入法状态。
  const select = document.getElementById(kind === 'services' ? 'tigerServiceSelect' : 'tigerCountrySelect');
  const count = document.getElementById(kind === 'services' ? 'tigerServiceCount' : 'tigerCountryCount');
  if (select) {
    select.innerHTML = kind === 'services' ? view.serviceOptions : view.countryOptions;
    if (kind === 'countries') select.disabled = view.countryDisabled;
  }
  if (count) count.textContent = kind === 'services' ? view.serviceCount : view.countryCount;
}
function paintTigerCatalog() {
  const root = document.getElementById('tigerSmsCatalog');
  if (!root) return;
  const view = tigerFilteredCatalog();
  const tiers = tigerPriceTierView();
  root.innerHTML = `
    <div class="tiger-toolbar">
      <span class="tiger-title">号码选择</span>
      <button type="button" class="tiger-refresh" onclick="loadTigerCatalog(true)"${TIGER_CATALOG.busy ? ' disabled' : ''}>${TIGER_CATALOG.busy ? '刷新中…' : '刷新列表'}</button>
    </div>
    <div class="tiger-selectors">
      <div class="tiger-card">
        <div class="tiger-card-head"><label for="tigerServiceSelect">项目</label><span class="tiger-count" id="tigerServiceCount">${esc(view.serviceCount)}</span></div>
        <input class="tiger-search" type="search" aria-label="Tiger SMS 项目过滤" placeholder="搜索项目名称 / 代码" value="${attrEsc(TIGER_CATALOG.serviceFilter)}" oninput="filterTigerCatalog('services',this.value)">
        <select id="tigerServiceSelect" aria-label="Tiger SMS 项目" onchange="selectTigerService(this.value)">${view.serviceOptions}</select>
      </div>
      <div class="tiger-card">
        <div class="tiger-card-head"><label for="tigerCountrySelect">国家</label><span class="tiger-count" id="tigerCountryCount">${esc(view.countryCount)}</span></div>
        <input class="tiger-search" type="search" aria-label="Tiger SMS 国家过滤" placeholder="搜索国家 / 代码 / 价格 / 库存" value="${attrEsc(TIGER_CATALOG.countryFilter)}" oninput="filterTigerCatalog('countries',this.value)">
        <select id="tigerCountrySelect" aria-label="Tiger SMS 国家" onchange="selectTigerCountry(this.value)"${view.countryDisabled ? ' disabled' : ''}>${view.countryOptions}</select>
      </div>
    </div>
    <div class="tiger-prices">
      <label class="tiger-field">价格档位 / 库存（USD，上限）
        <select id="tigerPriceTierSelect" aria-label="Tiger SMS 价格档位" onchange="selectTigerPriceTier(this.value)"${tiers.disabled ? ' disabled' : ''}>${tiers.options}</select>
      </label>
      <label class="tiger-field">购买最高单价（USD）
        <input type="number" min="0.0001" step="any" data-key="SMS_MAX_PRICE" aria-label="Tiger SMS 购买最高单价" value="${attrEsc(tigerConfigValue('SMS_MAX_PRICE'))}" placeholder="留空不限" oninput="setTigerMaxPrice(this.value)">
      </label>
      <label class="tiger-field">价格排序
        <select aria-label="Tiger SMS 价格排序" onchange="TIGER_CATALOG.sort=this.value;paintTigerCatalog()">
          ${tigerOption('asc', '低 → 高', TIGER_CATALOG.sort === 'asc')}
          ${tigerOption('desc', '高 → 低', TIGER_CATALOG.sort === 'desc')}
        </select>
      </label>
    </div>
    <div class="tiger-status">${esc(TIGER_CATALOG.busy ? '正在加载…' : TIGER_CATALOG.error || '')}</div>`;
}

function setTigerConfig(key, value) {
  CONFIG_PENDING_UPDATES[key] = value;
  // 若通用字段同时可见，同步其值，避免保存时被旧输入覆盖。
  document.querySelectorAll(`[data-key="${key}"]`).forEach(el => { el.value = value; });
}
function selectTigerCountry(value) {
  if (TIGER_CATALOG.busy || TIGER_CATALOG.service !== tigerServiceCode()) return;
  if (TIGER_CATALOG.countries.some(c => c.id === value)) {
    setTigerConfig('SMS_COUNTRY', value);
    updateTigerPriceTiers();
  }
}
function selectTigerService(value) {
  setTigerConfig('SMS_SERVICE', value);
  loadTigerCatalog(false);
}
async function loadTigerCatalog(refresh = false) {
  const seq = ++TIGER_CATALOG.seq;
  const service = tigerServiceCode();
  TIGER_CATALOG.busy = true;
  TIGER_CATALOG.error = '';
  // 切换项目时立即清掉旧报价，不把旧国家误认为新项目的可用国家。
  TIGER_CATALOG.countries = [];
  TIGER_CATALOG.service = service;
  paintTigerCatalog();
  try {
    if (refresh || TIGER_CATALOG.services === null) {
      const catalog = await api('/api/sms/tiger/catalog');
      if (seq !== TIGER_CATALOG.seq) return;
      if (!catalog.ok) throw new Error(catalog.error || '项目加载失败');
      TIGER_CATALOG.services = catalog.services || [];
    }
    if (service) {
      const result = await api('/api/sms/tiger/catalog?service=' + encodeURIComponent(service));
      if (seq !== TIGER_CATALOG.seq) return;
      if (!result.ok) throw new Error(result.error || '国家价格加载失败');
      TIGER_CATALOG.countries = result.countries || [];
    }
  } catch (e) {
    if (seq !== TIGER_CATALOG.seq) return;
    TIGER_CATALOG.error = '加载失败：' + e.message;
  } finally {
    if (seq === TIGER_CATALOG.seq) {
      TIGER_CATALOG.busy = false;
      paintTigerCatalog();
    }
  }
}
function mountTigerCatalog() {
  if (!document.getElementById('tigerSmsCatalog')) return;
  paintTigerCatalog();
  if (!TIGER_CATALOG.busy && (TIGER_CATALOG.services === null || TIGER_CATALOG.service !== tigerServiceCode())) loadTigerCatalog();
}
