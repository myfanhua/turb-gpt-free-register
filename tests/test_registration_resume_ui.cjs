const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
(async () => {
  for (const template of ['index.html', 'index_legacy.html']) {
    const source = fs.readFileSync(`webui/templates/${template}`, 'utf8');
    const action = source.match(/function _codexAction\(r\) \{[\s\S]*?\n\}/)[0];
    const context = vm.createContext({esc: String});
    vm.runInContext(action, context);
    for (const row of [
      {id: 3624, registration_stage: 'password_registered'},
      {id: 3624, extra_json: JSON.stringify({registration_stage: 'password_registered'})},
      {id: 3624, extra_json: {registration_stage: 'password_registered'}},
    ]) {
      assert.match(context._codexAction(row), /data-registration-resume="3624"/);
      assert.doesNotMatch(context._codexAction(row), /data-codex-retry/);
    }
    assert.equal(context._codexAction({id: 3624, registration_stage:'password_registered', live_check_status:'deactivated'}), '');
    assert.match(context._codexAction({id: 1, email:'complete@example.com', registration_stage:'complete'}), /data-codex-retry/);
    const start = source.indexOf("  const resumeBtn = e.target.closest('[data-registration-resume]');");
    const end = source.indexOf("  const btn = e.target.closest('[data-codex-retry]');", start);
    const block = source.slice(start, end);
    const button = {dataset: {registrationResume:'3624'}, disabled:false};
    const requests = [], logs = [];
    Object.assign(context, {
      e: {target: {closest: () => button}}, confirm: () => true,
      api: async (url, opts) => { requests.push([url,opts.method]); return {job:{id:7}}; },
      showToast() {}, openLog(id) { logs.push(id); }, refreshJobs() {}, loadAccounts() {},
    });
    await vm.runInContext(`(async () => {${block}})()`, context);
    assert.deepEqual(requests, [['/api/accounts/3624/continue-registration', 'POST']]);
    assert.deepEqual(logs, [7]);
    context.api = async () => {throw Error('offline');};
    await vm.runInContext(`(async () => {${block}})()`, context);
    assert.equal(button.disabled, false);
    const suffix = template === 'index.html' ? 'V2' : '';
    assert.match(source, new RegExp('id="btnResumeSelectedRegistration' + suffix + '"'));
    const bulk = source.match(/let registrationResumeBulkSubmitting = false;\nasync function resumeSelectedRegistration\(\) \{[\s\S]*?\n\}/)[0];
    const workersEl = {value:'3'};
    const selected = new Set([3624, 3625]);
    const bulkRequests = [];
    let updates = 0;
    Object.assign(context, {
      ACCOUNT_SELECTED: selected,
      $: selector => selector.includes('Workers') ? workersEl : button,
      api: async (url, opts) => {bulkRequests.push([url, JSON.parse(opts.body)]); return {created:[{job_id:12}], skipped:[], failed:[]};},
      updateAccountSelectionUi() {updates++;}, alert() {},
    });
    vm.runInContext(bulk, context);
    await context.resumeSelectedRegistration();
    assert.equal(bulkRequests.length, 1);
    assert.equal(bulkRequests[0][0], '/api/accounts/continue-registration-bulk');
    assert.deepEqual(bulkRequests[0][1], {account_ids:[3624,3625], workers:3});
    assert.equal(logs.at(-1), 12);
    assert.equal(updates, 1);
    workersEl.value = '0';
    await context.resumeSelectedRegistration();
    assert.equal(bulkRequests.length, 1);
    workersEl.value = '1';
    context.api = async () => {throw Error('offline');};
    await context.resumeSelectedRegistration();
    assert.equal(updates, 2);
    assert.equal(vm.runInContext('registrationResumeBulkSubmitting', context), false);
    selected.clear();
    await context.resumeSelectedRegistration();
    assert.equal(updates, 2);

  }
  console.log('新旧界面继续注册按钮、请求、日志与失败恢复测试通过。');
})().catch(error => {console.error(error);process.exitCode=1;});
