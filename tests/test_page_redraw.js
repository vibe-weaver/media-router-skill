#!/usr/bin/env node
// 执行页面真实函数与可分离 DOM：零依赖、无浏览器、无服务、网络入口一律拒绝。
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const denyNetwork = () => { throw Error('网络/服务访问已禁止'); };
require('net').Socket.prototype.connect = denyNetwork;
require('tls').connect = denyNetwork;
require('http').request = denyNetwork;
require('https').request = denyNetwork;
global.fetch = denyNetwork;
const html = fs.readFileSync(path.join(__dirname, '..', 'assets', 'config.html'), 'utf8');
const script = html.slice(html.indexOf('<script>') + '<script>'.length, html.lastIndexOf('</script>'));
const functions = script.slice(0, script.indexOf('document.querySelectorAll(".tab")'));
assert(functions.includes('function renderModels(') && functions.includes('async function testModel('));

function decode(text) {
  return text.replace(/&(?:quot|#39|lt|gt|amp);/g, s => ({'&quot;':'"', '&#39;':"'", '&lt;':'<', '&gt;':'>', '&amp;':'&'}[s]));
}
class Element {
  constructor(tag, doc) {
    this.tagName = tag.toLowerCase(); this.ownerDocument = doc;
    this.children = []; this.parentElement = null; this.dataset = {}; this.style = {};
    this.attributes = {}; this.disabled = false; this._html = ''; this.textContent = '';
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === 'class') this.className = value;
    if (name === 'id') this.id = value;
    if (name === 'disabled') this.disabled = true;
    if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
  }
  get innerHTML() { return this._html; }
  set innerHTML(value) {
    this._html = String(value);
    this.children.forEach(child => { child.parentElement = null; }); this.children = [];
    const stack = [this];
    for (const hit of this._html.matchAll(/<\/?([a-z][\w-]*)([^>]*)>/gi)) {
      const full = hit[0], tag = hit[1].toLowerCase();
      if (full.startsWith('</')) { if (stack.length > 1) stack.pop(); continue; }
      const child = new Element(tag, this.ownerDocument);
      for (const attr of hit[2].matchAll(/([\w-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) {
        child.setAttribute(attr[1], decode(attr[2] ?? attr[3] ?? attr[4] ?? ''));
      }
      stack[stack.length - 1].appendChild(child);
      if (!/^(input|img|br|hr|meta|link|source)$/.test(tag) && !full.endsWith('/>')) stack.push(child);
    }
  }
  appendChild(child) {
    child.remove(); child.parentElement = this; this.children.push(child); return child;
  }
  insertAdjacentElement(where, child) {
    assert.strictEqual(where, 'afterend');
    if (!this.parentElement) return null;
    child.remove(); const parent = this.parentElement;
    parent.children.splice(parent.children.indexOf(this) + 1, 0, child); child.parentElement = parent;
    return child;
  }
  remove() {
    if (!this.parentElement) return;
    const parent = this.parentElement; parent.children.splice(parent.children.indexOf(this), 1); this.parentElement = null;
  }
  get nextElementSibling() {
    if (!this.parentElement) return null;
    return this.parentElement.children[this.parentElement.children.indexOf(this) + 1] || null;
  }
  get isConnected() {
    let node = this; while (node.parentElement) node = node.parentElement;
    return node === this.ownerDocument.body;
  }
  matches(selector) {
    if (selector.startsWith('.')) return String(this.className || '').split(/\s+/).includes(selector.slice(1));
    const tag = selector.match(/^[\w-]+/);
    if (tag && this.tagName !== tag[0]) return false;
    for (const attr of selector.matchAll(/\[([\w-]+)(?:=["']?([^\]"']*)["']?)?\]/g)) {
      const name = attr[1], key = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      const value = name.startsWith('data-') ? this.dataset[key] : this.attributes[name];
      if (value === undefined || (attr[2] !== undefined && value !== attr[2])) return false;
    }
    return !!tag || selector.startsWith('[');
  }
  closest(selector) { for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node; return null; }
  querySelectorAll(selector) {
    const results = [];
    const visit = node => node.children.forEach(child => { if (child.matches(selector)) results.push(child); visit(child); });
    visit(this); return results;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
function deferred() {
  let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; });
  return {promise, resolve, reject};
}
function setup(confirm, request, ids = ['tts']) {
  const doc = {createElement(tag) { return new Element(tag, this); }};
  doc.body = doc.createElement('body');
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  doc.getElementById = id => {
    const walk = node => node.id === id ? node : node.children.map(walk).find(Boolean);
    return walk(doc.body) || null;
  };
  for (const id of ['model-list-image', 'model-list-video', 'model-list-audio', 'model-count', 'test-fail-fast']) {
    const element = doc.createElement('div'); element.id = id; doc.body.appendChild(element);
  }
  doc.getElementById('test-fail-fast').checked = true;
  const calls = [], entered = deferred();
  const denyNetwork = () => { throw Error('网络/服务访问已禁止'); };
  const context = vm.createContext({console, document:doc, fetch:denyNetwork, XMLHttpRequest:denyNetwork});
  vm.runInContext(functions + '\nthis.page = {state, pick, renderModels, testModel, resultBoxAfter};', context);
  context.confirmStub = confirm;
  context.requestStub = async (url, opts) => { calls.push({path:url, options:opts}); entered.resolve(); return request(url, opts); };
  vm.runInContext('uiConfirm = (...args) => confirmStub(...args); api = (...args) => requestStub(...args);', context);
  const page = context.page;
  page.state.kinds = ['image', 'video', 'audio']; page.pick.activeKind = 'audio';
  page.state.models = {image:[], video:[], audio:ids.map(id => ({id, kind:'audio', model:'offline-tts', label:'离线测试',
    provider:'openai', vendor:'fixture', priority:1, weight:50, enabled:true, supports:['text2audio']}))};
  page.renderModels();
  function buttons(mid = ids[0]) { return doc.querySelectorAll('button[data-mid]').filter(b => b.dataset.mid === mid && /^(test-model|real-test-model)$/.test(b.dataset.act)); }
  function card(mid = ids[0]) { const btn = buttons(mid)[0]; assert(btn && btn.isConnected, '找到当前真实模型卡'); return btn.closest('.mitem'); }
  function result(mid = ids[0]) { return doc.querySelectorAll('[data-mr-result]').find(b => b.dataset.mrResult === 'model:' + mid) || null; }
  function start(deep = true, mid = ids[0]) {
    const item = card(mid);
    return page.testModel(() => page.resultBoxAfter(item, 'model:' + mid), {id:mid}, deep);
  }
  return {page, doc, calls, entered:entered.promise, buttons, card, result, start};
}
let passed = 0, failed = 0;
async function test(name, fn) {
  try { await fn(); passed++; console.log('[PASS] ' + name); }
  catch (error) { failed++; console.error('[FAIL] ' + name + '\n' + error.stack); }
}
const watchdog = setTimeout(() => { console.error('未决 Promise：重绘测试未完成'); process.exitCode = 1; }, 5000);
(async () => {
  await test('请求中实际重绘恢复 pending 卡与新按钮锁，最终结果写回当前卡且只提交一次', async () => {
    const request = deferred(); const file = 'C:\\离线 <目录>\\语音 "样本"&.wav';
    const s = setup(async () => true, () => request.promise);
    const running = s.start(); await s.entered;
    const old = s.result(); assert(old && old.isConnected);
    s.page.renderModels(); assert(!old.isConnected, '真实 innerHTML 重绘确已分离旧卡');
    assert(s.buttons().every(b => b.disabled), '重绘后的新按钮仍禁用');
    assert(s.result() && s.result().innerHTML.includes('正在真实生成'), 'pending 卡挂到当前模型下');
    await s.start(false); assert.strictEqual(s.calls.length, 1);
    request.resolve({ok:true, real_call:{ok:true, files:[file], elapsed_ms:17, notes:['<script>仍需人工验收</script>']}});
    await running;
    const current = s.result(); assert(current && current.isConnected && current !== old);
    assert(current.innerHTML.includes('<audio') && current.innerHTML.includes(encodeURIComponent(file)));
    assert(!current.innerHTML.includes('<script>') && current.innerHTML.includes('&lt;script&gt;'));
    assert(s.buttons().every(b => !b.disabled)); assert.strictEqual(s.calls.length, 1);
    const completed = current.innerHTML; s.page.renderModels();
    assert(s.result() && s.result().isConnected); assert.strictEqual(s.result().innerHTML, completed);
  });
  await test('确认尚未完成时重绘也禁用新按钮，确认通过后挂回最新模型卡', async () => {
    const confirm = deferred(), request = deferred(); let confirmations = 0;
    const s = setup(() => { confirmations++; return confirm.promise; }, () => request.promise);
    const running = s.start(); const oldItem = s.card();
    assert.strictEqual(s.calls.length, 0); assert.strictEqual(s.result(), null);
    s.page.renderModels(); assert(!oldItem.isConnected);
    assert(s.buttons().every(b => b.disabled), '确认期重绘后同样保持锁');
    await s.start(); assert.strictEqual(confirmations, 1);
    confirm.resolve(true); await s.entered;
    assert(s.result() && s.result().isConnected, '确认后不能创建在旧的 detach 模型节点上');
    request.resolve({ok:true, real_call:{ok:true, files:[]}}); await running;
    assert(s.result().innerHTML.includes('本次生成成功')); assert.strictEqual(s.calls.length, 1);
  });
  await test('先运行得到旧结果，再在确认期重绘并取消，旧结果不被覆盖且不产生第二次请求', async () => {
    const cancel = deferred(); let confirmations = 0;
    const s = setup(() => ++confirmations === 1 ? Promise.resolve(true) : cancel.promise,
      () => ({ok:true, real_call:{ok:true, files:[], note:'先前可见结果 <保留>'}}));
    await s.start(); const oldHTML = s.result().innerHTML;
    const running = s.start(); s.page.renderModels();
    assert(s.result() && s.result().innerHTML === oldHTML, '确认期间刷新不删旧结果');
    assert(s.buttons().every(b => b.disabled));
    cancel.resolve(false); await running;
    assert.strictEqual(s.result().innerHTML, oldHTML); assert.strictEqual(s.calls.length, 1);
    assert(s.buttons().every(b => !b.disabled));
    s.page.renderModels(); assert.strictEqual(s.result().innerHTML, oldHTML);
  });
  await test('失败恢复的全部 files/urls/tasks 与停止建议可见，重绘后继续保留且安全转义', async () => {
    const files = ['C:\\离线\\完整 "一".wav', 'C:\\离线\\完整<二>.mp4'];
    const urls = ['https://stub.invalid/one?Signature=keep&part=1', 'https://stub.invalid/<two>?token=usable'];
    const s = setup(async () => true, () => ({ok:false, real_call:{ok:false, error:'交付失败 <script>bad</script>',
      hint:'不要再次生成 <保留>', files, urls, tasks:[{task_id:'task-<1>', query_url:'https://stub.invalid/tasks/1?token=***', status:'pending'}],
      phase:'delivery', submission_uncertain:false}}));
    await s.start();
    const result = s.result(); assert(result && result.isConnected);
    files.forEach(file => assert(result.innerHTML.includes(encodeURIComponent(file)), '每个本地文件均有路径编码预览'));
    urls.forEach(url => assert(result.innerHTML.includes(url.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')), '每条恢复 URL 均转义显示'));
    assert(result.innerHTML.includes('task-&lt;1&gt;')); assert(result.innerHTML.includes('不要再次生成'));
    assert(!result.innerHTML.includes('<script>') && !result.innerHTML.includes('本次生成成功'));
    const saved = result.innerHTML; s.page.renderModels(); assert.strictEqual(s.result().innerHTML, saved);
  });
  await test('免费预检请求跨实际重绘，最终检查仍挂当前卡且完成后可再重绘', async () => {
    const request = deferred(); const s = setup(async () => { throw Error('免费预检不应确认'); }, () => request.promise);
    const running = s.start(false); await s.entered; s.page.renderModels();
    assert(s.result() && s.result().innerHTML.includes('正在测试'));
    assert(s.buttons().every(b => b.disabled));
    request.resolve({ok:true, summary:'只读检查 <安全>', checks:[{name:'模型', status:'warn', detail:'未真实生成'}]}); await running;
    assert(s.result().innerHTML.includes('只读检查 &lt;安全&gt;'));
    const saved = s.result().innerHTML; s.page.renderModels(); assert.strictEqual(s.result().innerHTML, saved);
    assert.strictEqual(JSON.parse(s.calls[0].options.body).deep, false);
  });
  await test('请求异常发生在重绘后，错误显示在当前卡，解锁并保留到下一次重绘', async () => {
    const request = deferred(); const s = setup(async () => true, () => request.promise);
    const running = s.start(); await s.entered; s.page.renderModels(); request.reject(Error('offline <script>bad</script>')); await running;
    assert(s.result() && s.result().isConnected && s.result().innerHTML.includes('offline &lt;script&gt;'));
    assert(s.buttons().every(b => !b.disabled)); const saved = s.result().innerHTML;
    s.page.renderModels(); assert.strictEqual(s.result().innerHTML, saved);
  });
  await test('模型状态按 id 隔离，同模型单请求不阻挡另一模型并行且完成顺序不串卡', async () => {
    const first = deferred(), second = deferred();
    const s = setup(async () => true, (_, opts) => JSON.parse(opts.body).id === 'tts' ? first.promise : second.promise, ['tts','other']);
    const a = s.start(true, 'tts'); await s.entered; const b = s.start(false, 'other');
    s.page.renderModels(); await s.start(false, 'tts'); await s.start(true, 'other');
    assert.strictEqual(s.calls.length, 2); assert(s.buttons('tts').every(btn => btn.disabled)); assert(s.buttons('other').every(btn => btn.disabled));
    second.resolve({ok:true, summary:'第二模型先完成'}); await b;
    assert(s.result('other').innerHTML.includes('第二模型先完成'));
    first.resolve({ok:true, real_call:{ok:true, files:[], note:'第一模型后完成'}}); await a;
    assert(s.result('tts').innerHTML.includes('第一模型后完成'));
    s.page.renderModels(); assert(s.result('other').innerHTML.includes('第二模型先完成'));
    assert(s.result('tts').innerHTML.includes('第一模型后完成'));
  });
  await test('特殊字符模型 id 不参与选择器拼接，刷新后安全重挂当前结果', async () => {
    const id = 'tts"[]<>&'; const request = deferred();
    const s = setup(async () => true, () => request.promise, [id]);
    const running = s.start(); await s.entered; s.page.renderModels();
    assert(s.buttons().every(b => b.disabled));
    request.resolve({ok:true, real_call:{ok:true, files:[], note:'特殊 id 的结果'}}); await running;
    assert(s.result() && s.result().innerHTML.includes('特殊 id 的结果')); assert.strictEqual(s.calls.length, 1);
    s.page.renderModels(); assert(s.result().isConnected);
  });
  console.log(`通过 ${passed} 项实际重绘测试，失败 ${failed} 项`);
  if (failed) process.exitCode = 1;
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => clearTimeout(watchdog));
