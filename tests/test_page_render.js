#!/usr/bin/env node
// 页面渲染冒烟测试：零依赖，不启浏览器，用假 DOM 执行 config.html 里的真实函数。
// 运行：node tests/test_page_render.js

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");
const HTML = fs.readFileSync(path.join(ROOT, "assets", "config.html"), "utf8");

function extract(name) {
  const start = HTML.indexOf("function " + name + "(");
  if (start < 0) throw new Error("找不到函数 " + name);
  let i = HTML.indexOf("{", start);
  let depth = 0;
  for (; i < HTML.length; i++) {
    if (HTML[i] === "{") depth++;
    else if (HTML[i] === "}") {
      depth--;
      if (depth === 0) return HTML.slice(start, i + 1);
    }
  }
  throw new Error("函数括号不配对：" + name);
}

function extractConst(name) {
  const start = HTML.indexOf("const " + name + " = {");
  if (start < 0) throw new Error("找不到常量 " + name);
  let i = HTML.indexOf("{", start);
  let depth = 0;
  for (; i < HTML.length; i++) {
    if (HTML[i] === "{") depth++;
    else if (HTML[i] === "}") {
      depth--;
      if (depth === 0) return HTML.slice(start, i + 1) + ";";
    }
  }
  throw new Error("常量括号不配对：" + name);
}

const FUNCS = [
  "esc", "kindName", "updateKindCounts", "renderModelGroups",
  "routingInfo", "shareText", "metricOf", "tierHeader",
  "kindSummaryHtml", "renderProbability", "renderModelCard", "renderModels",
];
const SRC = extractConst("KIND_NAMES") + "\n" + FUNCS.map(extract).join("\n");

function M(id, extra) {
  return Object.assign({
    id, model: id + "-model", label: id + "-model",
    provider: "openai", vendor: "v1", vendor_label: "厂商",
    priority: 1, weight: 50, enabled: true, supports: ["text2img"],
    needs_api_key: true, api_key_ready: true, cooling: false,
    cooldown_remaining_seconds: 0, from_base: false,
  }, extra || {});
}

const NATIVE = {
  id: "builtin-imagen", model: "builtin-imagen", label: "builtin-imagen",
  provider: "native", vendor: "", priority: 1000000, weight: 100,
  enabled: true, supports: ["text2img", "img2img"],
  needs_api_key: false, api_key_ready: true, cooling: false,
  cooldown_remaining_seconds: 0, from_base: true,
};

function run(models, kinds) {
  const code = `
    const state = {
      kinds: ${JSON.stringify(kinds || ["image", "video"])},
      models: ${JSON.stringify(models)},
    };
    const pick = {activeKind: "image", kind: "image"};
    const els = {};
    function el(id) {
      return els[id] || (els[id] = {innerHTML: "", textContent: "", style: {}});
    }
    const document = {querySelectorAll(){ return []; }};
    ${SRC}
    renderModelGroups();
    renderModels();
    renderProbability();
    __out = {
      tabs: els["kind-tabs"].innerHTML,
      pages: els["model-list"].innerHTML,
      image: els["model-list-image"].innerHTML,
      video: els["model-list-video"].innerHTML,
      audio: (els["model-list-audio"] || {innerHTML: ""}).innerHTML,
      summary: els["prob-summary"].innerHTML,
      count: els["model-count"].textContent,
      info: routingInfo("image"),
    };
  `;
  const sandbox = {};
  vm.runInNewContext(code, sandbox);
  return sandbox.__out;
}

function cardIn(html, name) {
  const parts = html.split('<div class="mitem');
  const hit = parts.slice(1).find(p => p.includes(">" + name + "</span>"));
  if (!hit) throw new Error("找不到卡片：" + name);
  return '<div class="mitem' + hit;
}

let passed = 0;
let failed = 0;
function ok(cond, msg) {
  if (cond) { passed++; console.log("  [PASS] " + msg); }
  else { failed++; console.error("  [FAIL] " + msg); }
}

console.log("页面渲染冒烟测试");

// 1. 正常双模型 + native
let out = run({
  image: [M("pro"), M("base", {priority: 3}), NATIVE],
  video: [ NATIVE ],
});
ok(out.tabs.includes('<span class="kt">图像模型</span>')
   && out.tabs.includes('<span class="kt">视频模型</span>'), "图像/视频页签渲染");
ok(out.tabs.includes('data-kind="image"') && out.tabs.includes('class="kind-tab on"'), "默认激活图像页");
ok((out.pages.match(/model-page on/g) || []).length === 1, "只显示当前模型页");
ok(out.summary.includes("首选档（优先级 1）") && out.summary.includes("备用档：优先级 3"), "摘要区分首选/备用");
ok(cardIn(out.image, "pro-model").includes("首选概率 100%"), "首选模型显示概率");
ok(cardIn(out.image, "base-model").includes("备用档") && !cardIn(out.image, "base-model").includes("100%"),
   "备用模型不显示 100%");
ok(!out.image.includes("pbar"), "模型卡不再画重复占比条");
ok(cardIn(out.image, "builtin-imagen").includes("最后兜底"), "native 标注最后兜底");
// 内置兜底不是真模型：不吃也不吐，别给它贴「输入 … → 输出 …」的功能标签
ok(!cardIn(out.image, "builtin-imagen").includes("badge mod"),
   "内置兜底不显示输入/输出模态标签");
ok(cardIn(out.image, "pro-model").includes("badge mod"), "真模型仍然显示模态标签");
ok(!out.summary.includes("全局权重") && !out.summary.includes("严格顺位"),
   "摘要只讲按优先级分档（另两种策略已下线）");
ok(out.count === "3 个模型", "当前页模型计数正确");

// 1b. 语音类目：第三个页签 + 语音模型卡
out = run(
  { image: [M("pro")], video: [], audio: [M("tts", {
      kind: "audio", supports: ["text2audio"],
      input_modalities: ["text"], output_modalities: ["audio"],
    })] },
  ["image", "video", "audio"],
);
ok(out.tabs.includes("语音模型"), "存在「语音模型」第三个页签");
ok((out.audio || "").includes("输出") && (out.audio || "").includes("音频"),
   "语音模型卡显示「输出 音频」");
ok((out.audio || "").includes("输入") && (out.audio || "").includes("文本"),
   "语音模型卡显示「输入 文本」");
// 新格式用「输入 / 输出」全称 + 「/」分隔多模态，旧的「入 … · … → 出」不再出现
ok(!(out.audio || "").includes(">入 "), "模态标签不再用单字「入」");
ok((out.image || "").includes("输入") && (out.image || "").includes("输出"),
   "图像模型卡也用「输入 / 输出」全称");
// 语音类目还没有适配器：卡片不提供「测试」，避免给出误导性的探测结论
ok(!(out.audio || "").includes('data-act="test-model"'), "语音模型卡没有「测试」按钮");
ok((out.image.match(/data-act="test-model"/g) || []).length >= 1, "图像模型卡仍有「测试」按钮");

// 2. 缺 Key / 熔断
out = run({image: [M("nokey", {api_key_ready: false}), M("base", {priority: 3})], video: []});
ok(cardIn(out.image, "nokey-model").includes("缺 Key"), "缺 Key 卡片有徽标");
ok(!cardIn(out.image, "nokey-model").includes("首选概率"), "缺 Key 卡片不显示概率");
ok(out.info.shares["nokey"] === undefined, "缺 Key 模型不进入占比分母");
ok(out.summary.includes("1 个缺 Key") && out.summary.includes("本次不参与"), "摘要说明缺 Key 已排除");

out = run({image: [M("cool", {cooling: true, cooldown_remaining_seconds: 42}),
                   M("base", {priority: 3})], video: []});
ok(cardIn(out.image, "cool-model").includes("熔断中") && cardIn(out.image, "cool-model").includes("42s"),
   "熔断卡片显示剩余冷却");
ok(out.info.shares["cool"] === undefined, "熔断模型不进入占比分母");
ok(out.summary.includes("1 个熔断中"), "摘要说明熔断已排除");

// 3. 极端兜底
out = run({image: [M("a", {cooling: true}), M("b", {priority: 3, cooling: true})], video: []});
ok(out.info.coolingRelaxed === true && out.summary.includes("全部可用真实模型熔断中"),
   "全部真实模型熔断时说明会强制尝试");

out = run({image: [M("a", {cooling: true}), NATIVE], video: []});
ok(out.info.nativeFallback === true && out.summary.includes("走内置兜底"),
   "真实模型全熔断但有 native 时走兜底");
ok(cardIn(out.image, "a-model").includes("熔断中") && !cardIn(out.image, "a-model").includes("首选概率"),
   "兜底场景下冷却模型不显示概率");

out = run({image: [M("a", {api_key_ready: false}), M("b", {priority: 3, api_key_ready: false})], video: []});
ok(out.info.poolEmpty === true && out.summary.includes("没有可用的真实模型"),
   "全部缺 Key 时明确报不可用");

// 4. 策略只剩一种：页面上不该再出现另外两种
out = run({image: [M("a")], video: []});
ok(!out.summary.includes("全局权重") && !out.summary.includes("严格顺位"),
   "摘要里不再出现「全局权重 / 严格顺位」");
ok(out.summary.includes("首选档（优先级 1）"), "摘要仍按优先级分档说明");

// 5. P2 计数：停用模型要区分「启用 / 共」
out = run({image: [M("on"), M("off", {enabled: false})], video: []});
ok(out.count === "1 个启用 / 共 2 个", "计数区分启用数与总数");

console.log(`\n通过 ${passed} 项，失败 ${failed} 项`);
process.exit(failed ? 1 : 0);
