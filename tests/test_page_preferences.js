#!/usr/bin/env node
// 页面偏好持久化回归：执行实际初始化函数，无服务、无网络、无个人配置。
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
const key = 'media-router.ui.test-fail-fast.v1';
function extract(name) {
  const start = html.indexOf('function ' + name + '(');
  assert(start >= 0, '页面必须实现 ' + name + '，否则刷新无法恢复选择');
  let depth = 0;
  for (let i = html.indexOf('{', start); i < html.length; i++) {
    if (html[i] === '{') depth++;
    if (html[i] === '}' && --depth === 0) return html.slice(start, i + 1);
  }
  throw Error('函数括号不完整');
}
function setup(saved = new Map(), options = {}) {
  const listeners = {}, writes = [], reads = [];
  const control = {checked:!!options.browserRestored, addEventListener(name, fn) { listeners[name] = fn; }};
  const note = {textContent:''};
  const storage = {
    getItem(k) { reads.push(k); if (options.readError) throw Error('storage disabled'); return saved.has(k) ? saved.get(k) : null; },
    setItem(k, value) { if (options.writeError) throw Error('quota exceeded'); writes.push([k,value]); saved.set(k,value); },
  };
  const context = {el:id => id === 'test-fail-fast' ? control : id === 'test-fail-fast-persistence' ? note : null,
    fetch:denyNetwork, console};
  Object.defineProperty(context, 'localStorage', {get() { if (options.accessError) throw Error('security denied'); return storage; }});
  vm.createContext(context);
  vm.runInContext(extract('initTestFailFastPreference') + '\ninitTestFailFastPreference();', context);
  return {control,note,writes,reads,change(value) {control.checked = value; listeners.change();}};
}
let passed = 0;
function test(name, fn) {fn(); passed++; console.log('[PASS] ' + name);}
test('首次访问默认关闭，不受浏览器表单恢复残值影响，不初始化写入存储', () => {
  const s = setup(new Map(), {browserRestored:true});
  assert.strictEqual(s.control.checked,false);
  assert.deepStrictEqual(s.writes,[]);
  assert.deepStrictEqual(s.reads,[key]);
});
test('开启后重新初始化模拟刷新仍开启，关闭再刷新仍关闭', () => {
  const saved = new Map();
  const first = setup(saved); first.change(true);
  const second = setup(saved); assert.strictEqual(second.control.checked,true);
  second.change(false);
  const third = setup(saved); assert.strictEqual(third.control.checked,false);
  assert.deepStrictEqual(first.writes,[[key,'true']]);
  assert.deepStrictEqual(second.writes,[[key,'false']]);
  assert(saved.size===1 && !saved.has('token'));
});
test('只接受精确 true/false 字符串，损坏或旧值不误开启', () => {
  for (const value of ['false','0','1','TRUE','yes','null','{}','', '"true"']) {
    const s = setup(new Map([[key,value]]));
    assert.strictEqual(s.control.checked,false,value);
    assert.deepStrictEqual(s.writes,[]);
  }
  assert.strictEqual(setup(new Map([[key,'true']])).control.checked,true);
});
test('浏览器禁止访问或读取 storage 时仍可使用并显示不能持久化', () => {
  for (const options of [{accessError:true},{readError:true}]) {
    const s = setup(new Map(),options);
    assert.strictEqual(s.control.checked,false);
    assert(s.note.textContent.includes('无法保存') && s.note.textContent.includes('刷新后可能恢复为上次保存的选择'));
    s.change(true);
    assert.strictEqual(s.control.checked,true);
  }
});
test('写入配额异常不撤销用户选择、不抛错并显示提示', () => {
  const s = setup(new Map(),{writeError:true});
  s.change(true);
  assert.strictEqual(s.control.checked,true);
  assert(s.note.textContent.includes('无法保存'));
  assert.deepStrictEqual(s.writes,[]);
});
test('已有开启记录而关闭保存失败时，提示刷新可能恢复上次选择', () => {
  const saved = new Map([[key,'true']]);
  const first = setup(saved,{writeError:true});
  assert.strictEqual(first.control.checked,true);
  first.change(false);
  assert.strictEqual(first.control.checked,false);
  assert.strictEqual(saved.get(key),'true');
  assert(first.note.textContent.includes('刷新后可能恢复为上次保存的选择'));
  assert.strictEqual(setup(saved).control.checked,true);
});
test('存储失败后再次成功保存可恢复正常提示，不发送 API 请求', () => {
  const saved = new Map();
  const options = {writeError:true};
  const s = setup(saved,options);
  s.change(true); options.writeError=false; s.change(false);
  assert.strictEqual(saved.get(key),'false');
  assert(s.note.textContent.includes('本浏览器') && !s.note.textContent.includes('无法保存'));
});
assert(html.includes('initTestFailFastPreference();'), '页面启动时必须实际调用初始化');
console.log('通过 ' + passed + ' 项偏好持久化测试');
