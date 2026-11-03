#!/usr/bin/env node
// 运行页面真实测试函数；网络边界与确认框可控，不启浏览器、不调用平台。
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const html = fs.readFileSync(path.join(__dirname, '..', 'assets', 'config.html'), 'utf8');
const denyNetwork = () => { throw Error('网络/服务访问已禁止'); };
require('net').Socket.prototype.connect = denyNetwork;
require('tls').connect = denyNetwork;
require('http').request = denyNetwork;
require('https').request = denyNetwork;
global.fetch = denyNetwork;
function extract(name) {
  let start = html.indexOf('async function ' + name + '(');
  if (start < 0) start = html.indexOf('function ' + name + '(');
  assert(start >= 0, '找到实际函数 ' + name);
  let depth = 0;
  for (let i = html.indexOf('{', start); i < html.length; i++) {
    if (html[i] === '{') depth++;
    if (html[i] === '}' && --depth === 0) return html.slice(start, i + 1);
  }
  throw Error('函数括号不完整：' + name);
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((a, b) => { resolve = a; reject = b; });
  return {promise, resolve, reject};
}
function setup(confirm, request, failFast = true) {
  const calls = [], buttons = [{dataset:{mid:'tts',act:'test-model'}, disabled:false, closest(){ return null; }},
    {dataset:{mid:'tts',act:'real-test-model'}, disabled:false, closest(){ return null; }}];
  const box = {innerHTML: '旧结果'}, control = {checked:failFast};
  const entered = deferred();
  const context = {
    console, Set, Object, String, JSON, encodeURIComponent,
    modelTestsPending: new Set(), modelTestResults: new Map(),
    document: {querySelectorAll() { return buttons; }, createElement() { return {innerHTML:''}; }}, fetch:denyNetwork,
    el(id) { return id === 'test-fail-fast' ? control : null; },
    uiConfirm: confirm,
    api: async (p, opts) => { calls.push({path:p, options:opts}); entered.resolve(); return request(); },
    renderChecks(target, result) { target.innerHTML = result.summary; },
  };
  vm.createContext(context);
  const funcs = ['esc', 'testModel'];
  for (const name of ['setModelTestBusy', 'modelTestItem', 'mountModelTestResult', 'setModelTestResult']) {
    if (html.includes('function ' + name + '(')) funcs.push(name);
  }
  vm.runInContext(funcs.map(extract).join('\n') + '\nthis.run = testModel;', context);
  return {context, calls, buttons, box, control, entered: entered.promise};
}
let passed = 0;
async function test(name, fn) { await fn(); passed++; console.log('[PASS] ' + name); }
const watchdog = setTimeout(() => { console.error('测试未完成（未决Promise）'); process.exitCode = 1; }, 5000);
(async () => {
  await test('确认取消不请求、不创建结果卡、不覆盖旧结果', async () => {
    const confirm = deferred();
    const s = setup(() => confirm.promise, () => ({ok:true}));
    let cards = 0;
    const run = s.context.run(() => { cards++; return s.box; }, {id:'tts'}, true);
    assert.strictEqual(s.calls.length, 0);
    assert.strictEqual(cards, 0);
    confirm.resolve(false);
    await run;
    assert.strictEqual(s.calls.length, 0);
    assert.strictEqual(cards, 0);
    assert.strictEqual(s.box.innerHTML, '旧结果');
    assert(s.buttons.every(b => !b.disabled));
  });
  await test('确认和请求中同模型不能重复提交，结束恢复按钮', async () => {
    const confirm = deferred(), request = deferred();
    let confirmations = 0;
    const s = setup(() => { confirmations++; return confirm.promise; }, () => request.promise);
    let cards = 0;
    const first = s.context.run(() => {cards++; return s.box;}, {id:'tts'}, true);
    const duplicateConfirm = s.context.run(() => {cards++; return s.box;}, {id:'tts'}, true);
    assert.strictEqual(confirmations, 1);
    await duplicateConfirm;
    assert(s.buttons.every(b => b.disabled));
    confirm.resolve(true);
    await s.entered;
    const duplicateRequest = s.context.run(s.box, {id:'tts'}, false);
    assert.strictEqual(s.calls.length, 1);
    await duplicateRequest;
    assert.strictEqual(cards, 1);
    const payload = JSON.parse(s.calls[0].options.body);
    assert.strictEqual(s.calls[0].path, '/api/model/test');
    assert.strictEqual(payload.deep, true);
    assert.strictEqual(payload.fail_fast, true);
    request.resolve({ok:true, real_call:{ok:true, files:[], elapsed_ms:20, notes:[]}});
    await first;
    assert(s.buttons.every(b => !b.disabled));
  });
  await test('免费与真实测试都读取当前开关值，不保存配置或额外提交', async () => {
    for (const deep of [false, true]) {
      const s = setup(async () => true, () => ({ok:true, summary:'预检通过', real_call:{ok:true, files:[]}}), false);
      await s.context.run(s.box, {id:'tts'}, deep);
      s.control.checked = true;
      await s.context.run(s.box, {id:'tts'}, deep);
      assert.strictEqual(s.calls.length, 2);
      assert.deepStrictEqual(s.calls.map(call => JSON.parse(call.options.body).fail_fast), [false, true]);
      assert(s.calls.every(call => call.path === '/api/model/test'));
      assert(s.calls.every(call => JSON.parse(call.options.body).deep === deep));
      assert(s.buttons.every(button => !button.disabled));
    }
  });
  await test('污染表单payload不能把身份、凭据或资源带入模型测试请求', async () => {
    for (const deep of [false, true]) {
      const s = setup(async () => true, () => ({ok:true, summary:'预检通过', real_call:{ok:true, files:[]}}));
      await s.context.run(s.box, {id:'tts', model:'not-approved', vendor:'other-account',
        api_key:'private-key', params:{model:'not-approved'}, options:{endpoint:'https://other.invalid'},
        images:['private.png'], extra:{resource_id:'other-resource'}, count:7}, deep);
      assert.strictEqual(s.calls.length, 1);
      assert.deepStrictEqual(JSON.parse(s.calls[0].options.body), {id:'tts', deep, fail_fast:true});
      assert(s.calls.every(call => call.path === '/api/model/test'));
    }
  });
  await test('真实测试确认说明保存启用边界，不把未保存表单自动保存', async () => {
    let message = '';
    const s = setup(async (title, text) => { message = title + text; return false; }, () => ({ok:true}));
    await s.context.run(s.box, {id:'tts'}, true);
    assert(message.includes('已保存且启用'));
    assert(message.includes('未保存改动不参与'));
    assert(message.includes('请先保存再测'));
    assert.strictEqual(s.calls.length, 0);
  });
  await test('确认弹窗期间切换只影响下次测试，不修改已发起策略', async () => {
    const confirm = deferred();
    const s = setup(() => confirm.promise, () => ({ok:true, real_call:{ok:true, files:[]}}), false);
    const running = s.context.run(s.box, {id:'tts'}, true);
    s.control.checked = true;
    confirm.resolve(true);
    await running;
    assert.strictEqual(s.calls.length, 1);
    assert.strictEqual(JSON.parse(s.calls[0].options.body).fail_fast, false);
    assert.strictEqual(s.control.checked, true);
  });
  await test('本次音频成功显示播放器、档案、耗时与验收边界', async () => {
    const file = 'C:\\测试 目录\\语音.wav';
    const s = setup(async () => true, () => ({ok:true, real_call:{ok:true, files:[file],
      profiles:[{path:file, format:'wav', bytes:32044, size_human:'31.3KB', duration_seconds:1}],
      elapsed_ms:123, assessment:{status:'unverified'}, notes:['尚未验证听感']}}));
    await s.context.run(s.box, {id:'tts'}, true);
    assert(s.box.innerHTML.includes('<audio'));
    assert(s.box.innerHTML.includes('controls'));
    assert(s.box.innerHTML.includes(encodeURIComponent(file)));
    assert(s.box.innerHTML.includes('本次生成成功'));
    assert(!s.box.innerHTML.includes('这个模型确实可用'));
    assert(s.box.innerHTML.includes('123'));
    assert(s.box.innerHTML.includes('31.3KB'));
    assert(s.box.innerHTML.includes('尚未验证听感'));
  });
  await test('真实错误脱敏摘要原文建议转义且不显示通过', async () => {
    const s = setup(async () => true, () => ({ok:false, real_call:{ok:false,
      error:'HTTP400 <script>bad</script>', hint:'根因待确认', elapsed_ms:12,
      diagnostic:{summary:'厂商引擎错误',detail:'[411] <script>bad</script>',hint:'根因待确认'}}}));
    await s.context.run(s.box, {id:'tts'}, true);
    assert(s.box.innerHTML.includes('411'));
    assert(s.box.innerHTML.includes('根因待确认'));
    assert(!s.box.innerHTML.includes('<script>'));
    assert(!s.box.innerHTML.includes('本次生成成功'));
    assert(s.buttons.every(b => !b.disabled));
  });
  await test('请求抛错后可再次确认且按钮解锁', async () => {
    let confirmations = 0;
    const s = setup(async () => { confirmations++; return true; }, () => { throw Error('offline'); });
    await s.context.run(s.box, {id:'tts'}, true);
    await s.context.run(s.box, {id:'tts'}, true);
    assert.strictEqual(confirmations, 2);
    assert.strictEqual(s.calls.length, 2);
    assert(s.buttons.every(b => !b.disabled));
    assert(s.box.innerHTML.includes('offline'));
  });
  console.log('通过 ' + passed + ' 项异步交互测试');
})().catch(e => { console.error(e); process.exitCode = 1; }).finally(() => clearTimeout(watchdog));
