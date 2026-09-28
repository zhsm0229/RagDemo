/* 原型静态验证：1) 抽取内联脚本做语法检查 2) 用 Vue 编译器逐个编译模板 3) 校验 el-* 组件名存在于 Element Plus */
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const dir = path.dirname(fileURLToPath(import.meta.url));
const html = fs.readFileSync(path.join(dir, 'index.html'), 'utf8');

// 1. 提取最后一个 <script>（内联应用脚本）做语法检查
const scriptMatch = html.match(/<script>\n([\s\S]*?)<\/script>\s*<\/body>/);
if (!scriptMatch) { console.error('FAIL: 未找到内联应用脚本'); process.exit(1); }
const appScript = scriptMatch[1];
fs.writeFileSync(path.join(dir, '.app-extracted.js'), appScript);
try { new Function(appScript); console.log('PASS 应用脚本 JS 语法'); }
catch (e) { console.error('FAIL 应用脚本语法:', e.message); process.exit(1); }

// 2. 加载 Vue 完整构建（含编译器），逐个编译 template 字符串
const vueCode = fs.readFileSync(path.join(dir, 'lib', 'vue.global.prod.js'), 'utf8');
const ctx = { console, setTimeout, navigator: { userAgent: 'node' } };
// 浏览器里存在 document；Node 下给可工作的桩：Vue 编译器遇 & 时走 innerHTML/textContent 解码实体
ctx.document = {
  createElement: () => {
    let html = '';
    const obj = {
      set innerHTML(v) { html = String(v); },
      get innerHTML() { return html; },
      setAttribute() {}, appendChild() {}, removeChild() {},
    };
    Object.defineProperty(obj, 'textContent', { get: () => html });
    Object.defineProperty(obj, 'childNodes', { get: () => [{ nodeValue: html }] });
    // Vue decodeHtmlBrowser 属性分支：<div foo="..."> 后取 children[0].getAttribute('foo')
    Object.defineProperty(obj, 'children', {
      get: () => [{ getAttribute: () => html.replace(/^<div foo="/, '').replace(/">$/, '').replace(/&quot;/g, '"') }],
    });
    return obj;
  },
  createTextNode: () => ({}),
};
ctx.window = ctx; ctx.globalThis = ctx; ctx.self = ctx;
vm.createContext(ctx);
try { vm.runInContext(vueCode, ctx); } catch (e) { console.error('FAIL Vue 加载:', e.message); process.exit(1); }
const Vue = ctx.Vue;
if (!Vue || !Vue.compile) { console.error('FAIL Vue.compile 不可用'); process.exit(1); }

const tplRe = /template: `([\s\S]*?)`/g;
let m, count = 0, fail = 0;
while ((m = tplRe.exec(appScript)) !== null) {
  count++;
  try { Vue.compile(m[1]); console.log('PASS 模板 #' + count + '（' + m[1].trim().slice(0, 24).replace(/\n/g, ' ') + '...）'); }
  catch (e) { fail++; console.error('FAIL 模板 #' + count + ':', e.message.split('\n')[0]); }
}
console.log(`模板编译：${count - fail}/${count} 通过`);

// 3. 校验模板中使用的 el-* 组件在 Element Plus 全量包中存在
const ep = fs.readFileSync(path.join(dir, 'lib', 'element-plus.full.min.js'), 'utf8');
const usedTags = new Set();
const tagRe = /<(el-[a-z-]+)/g;
let tm;
const tplAll = appScript;
while ((tm = tagRe.exec(tplAll)) !== null) usedTags.add(tm[1]);
const missing = [];
for (const tag of usedTags) {
  const camel = tag.replace(/^el-/, '').split('-').map(s => s[0].toUpperCase() + s.slice(1)).join('');
  if (!ep.includes(camel)) missing.push(tag + ' (' + camel + ')');
}
if (missing.length) { console.error('FAIL 组件缺失:', missing.join(', ')); fail++; }
else console.log(`PASS 组件名校验（${usedTags.size} 种 el-* 组件全部存在于 Element Plus 2.8.8）`);

fs.unlinkSync(path.join(dir, '.app-extracted.js'));
process.exit(fail ? 1 : 0);
