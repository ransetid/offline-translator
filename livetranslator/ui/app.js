/* LiveTranslator 主界面 —— 无框架、无外部依赖（离线可用）。
   数据流：bootstrap() 拿快照 → SSE(/events) 推事件批 → 局部更新 DOM。 */
'use strict';

/* ============================== 工具 ============================== */
const T = window.LT_TOKEN;
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const isMac = /Mac/.test(navigator.platform);
const MOD = isMac ? '⌘' : 'Ctrl+';

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'on') for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
    else if (k === 'style' && typeof v === 'object') {
      for (const [sk, sv] of Object.entries(v)) sk.startsWith('--') ? el.style.setProperty(sk, sv) : (el.style[sk] = sv);
    }
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k === 'html') el.innerHTML = v;
    else if (k === 'value' && tag !== 'select') el.value = v;   // textarea 不认 value 属性
    else if (k in el && typeof v !== 'string') el[k] = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const SVGNS = 'http://www.w3.org/2000/svg';
function icon(name, cls = '') {
  const s = document.createElementNS(SVGNS, 'svg');
  s.setAttribute('class', `i ${cls}`.trim());
  s.setAttribute('aria-hidden', 'true');
  const u = document.createElementNS(SVGNS, 'use');
  u.setAttribute('href', `#i-${name}`);
  s.append(u);
  return s;
}
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
function fmtClock(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const hh = Math.floor(sec / 3600), mm = Math.floor(sec % 3600 / 60), ss = sec % 60;
  const p = n => String(n).padStart(2, '0');
  return hh ? `${hh}:${p(mm)}:${p(ss)}` : `${p(mm)}:${p(ss)}`;
}
function fmtDur(sec) {
  sec = Math.round(sec || 0);
  if (sec < 60) return `${sec} 秒`;
  const m = Math.round(sec / 60);
  return m < 60 ? `${m} 分钟` : `${Math.floor(m / 60)} 小时 ${m % 60} 分`;
}
function parseDate(s) { return s ? new Date(s.replace(' ', 'T')) : new Date(0); }
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function highlight(text, q) {
  const s = esc(text);
  if (!q) return s;
  const i = s.toLowerCase().indexOf(esc(q).toLowerCase());
  if (i < 0) return s;
  return s.slice(0, i) + '<mark>' + s.slice(i, i + esc(q).length) + '</mark>' + s.slice(i + esc(q).length);
}

async function api(name, args = {}) {
  let res;
  try {
    res = await fetch(`/api/${name}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-LT-Token': T },
      body: JSON.stringify(args),
    });
  } catch (e) { throw new Error('与本地服务的连接已断开'); }
  const j = await res.json().catch(() => ({ ok: false, error: `HTTP ${res.status}` }));
  if (!j.ok) throw new Error(j.error || '操作失败');
  return j.result;
}
async function tryApi(name, args, okMsg) {
  try { const r = await api(name, args); if (okMsg) toast(okMsg, 'ok'); return r; }
  catch (e) { toast(e.message, 'error'); return null; }
}

/* ============================== 状态 ============================== */
const S = {
  cfg: {}, langs: {}, pairs: [], templates: [], native: false, paths: {},
  session: { active: false }, entries: new Map(), order: [],
  sources: {}, backend: { checking: true }, jobs: new Map(),
  page: 'live', stick: true, unseen: 0, interim: null, levels: { mic: 0, media: 0 },
  lib: { items: [], sel: null, rec: null, q: '', filter: 'all', tab: 'summary', starOnly: false },
  termsDraft: null, settingsDraft: null, termsCount: 0,
};
const langName = l => (S.langs[l] || {}).name || l;
const langShort = l => (S.langs[l] || {}).short || l;
const pairOf = () => (S.cfg.translate_mode || 'zh-en').split('-');
const pairLabel = p => { const [a, b] = p.split('-'); return `${langName(a)} ⇄ ${langName(b)}`; };
const srcKeyOf = spk => (!spk ? undefined : spk.startsWith('麦克风') ? 'mic' : spk.startsWith('媒体') ? 'media' : undefined);
function spkColor(name) {
  if (!name) return 'var(--text-2)';
  if (name === '麦克风') return 'var(--spk-1)';
  let x = 0; for (const c of name) x = (x * 31 + c.charCodeAt(0)) >>> 0;
  return `var(--spk-${2 + x % 5})`;
}

/* ============================== 浮层：toast / 菜单 / 对话框 ============================== */
function toast(text, level = 'info', opts = {}) {
  const ico = { ok: 'check', error: 'alert', warn: 'alert', info: 'info' }[level] || 'info';
  const el = h('div', { class: `toast ${level}`, role: level === 'error' ? 'alert' : 'status' }, icon(ico), h('span', null, text));
  if (opts.action) el.append(h('button', { class: 'btn sm', on: { click: () => { opts.action.fn(); close(); } } }, opts.action.label));
  $('#toasts').append(el);
  const close = () => { el.classList.add('out'); setTimeout(() => el.remove(), 220); };
  setTimeout(close, opts.timeout || (level === 'error' ? 6000 : 3200));
  while ($('#toasts').children.length > 4) $('#toasts').firstChild.remove();
}

let openMenuEl = null, menuAnchor = null;
function closeMenu(refocus = true) {
  if (!openMenuEl) return;
  openMenuEl.remove(); openMenuEl = null;
  if (refocus) menuAnchor?.focus?.();
  menuAnchor = null;
}
function showMenu(anchor, items) {
  closeMenu(false);
  menuAnchor = anchor;
  const m = h('div', { class: 'menu', role: 'menu' });
  for (const it of items) {
    if (it === '-') { m.append(h('hr')); continue; }
    if (it.header) { m.append(h('div', { class: 'menu-label' }, it.header)); continue; }
    m.append(h('button', {
      role: 'menuitem', class: it.danger ? 'danger' : '', disabled: it.disabled,
      on: { click: e => { e.stopPropagation(); closeMenu(false); it.onClick(); } },
    }, it.icon ? icon(it.icon, 'sm') : null, it.label, it.hint ? h('span', { class: 'hint' }, it.hint) : null));
  }
  document.body.append(m);
  const r = anchor.getBoundingClientRect();
  const mw = m.offsetWidth, mh = m.offsetHeight;
  let x = r.left, y = r.bottom + 6;
  if (x + mw > innerWidth - 8) x = r.right - mw;
  if (y + mh > innerHeight - 8) y = r.top - mh - 6;
  m.style.left = `${Math.max(8, x)}px`; m.style.top = `${Math.max(8, y)}px`;
  openMenuEl = m;
  m.querySelector('button')?.focus();
  m.addEventListener('focusout', e => { if (!m.contains(e.relatedTarget)) setTimeout(() => closeMenu(false), 0); });
  m.addEventListener('keydown', e => {
    const bs = $$('button:not(:disabled)', m); const i = bs.indexOf(document.activeElement);
    if (e.key === 'ArrowDown') { bs[(i + 1) % bs.length].focus(); e.preventDefault(); }
    if (e.key === 'ArrowUp') { bs[(i - 1 + bs.length) % bs.length].focus(); e.preventDefault(); }
  });
}
document.addEventListener('mousedown', e => { if (openMenuEl && !openMenuEl.contains(e.target)) closeMenu(false); });

const modalStack = [];
function modal({ title, desc, body, actions = [], wide = false, onClose }) {
  const prevFocus = document.activeElement;
  const foot = h('div', { class: 'modal-foot' });
  const box = h('div', { class: `modal ${wide ? 'wide' : ''}`, role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
    h('div', { class: 'modal-head' }, h('h3', null, title), desc ? h('p', null, desc) : null),
    body ? h('div', { class: 'modal-body' }, body) : null, foot);
  const scrim = h('div', { class: 'scrim' }, box);
  let closed = false;
  const close = () => {
    if (closed) return; closed = true;
    scrim.remove(); const i = modalStack.indexOf(close); if (i >= 0) modalStack.splice(i, 1);
    onClose && onClose(); prevFocus?.focus?.();
  };
  box.addEventListener('keydown', e => {                 // 焦点困在对话框里
    if (e.key !== 'Tab') return;
    const f = $$('button:not(:disabled), input, select, textarea, [tabindex="0"]', box).filter(x => x.offsetParent !== null);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { last.focus(); e.preventDefault(); }
    else if (!e.shiftKey && document.activeElement === last) { first.focus(); e.preventDefault(); }
  });
  for (const a of actions) {
    if (a === '-') { foot.append(h('div', { class: 'grow' })); continue; }
    foot.append(h('button', {
      class: `btn ${a.kind || ''}`, on: {
        click: async () => { const r = a.onClick ? await a.onClick() : undefined; if (r !== false) close(); },
      },
    }, a.icon ? icon(a.icon) : null, a.label));
  }
  scrim.addEventListener('mousedown', e => { if (e.target === scrim) close(); });
  document.body.append(scrim);
  modalStack.push(close);
  setTimeout(() => (box.querySelector('input, textarea, select') || foot.querySelector('.btn.primary, .btn.danger') || foot.lastChild)?.focus(), 30);
  return close;
}
function confirmBox(title, desc, okLabel = '确定', kind = 'primary') {
  return new Promise(res => modal({
    title, desc, onClose: () => res(false),
    actions: [{ label: '取消', kind: 'ghost', onClick: () => res(false) }, { label: okLabel, kind, onClick: () => res(true) }],
  }));
}
function promptBox(title, desc, value = '', placeholder = '') {
  return new Promise(res => {
    const inp = h('input', { class: 'input', value, placeholder });
    let done = false;
    const finish = v => { if (!done) { done = true; res(v); } };
    const close = modal({
      title, desc, body: inp, onClose: () => finish(null),
      actions: [{ label: '取消', kind: 'ghost' }, { label: '确定', kind: 'primary', onClick: () => finish(inp.value.trim()) }],
    });
    inp.addEventListener('keydown', e => { if (e.key === 'Enter') { finish(inp.value.trim()); close(); } });
  });
}

/* ============================== Markdown（纪要用的最小子集） ============================== */
function md(src) {
  const lines = String(src || '').replace(/\r/g, '').split('\n');
  const out = []; let i = 0;
  const inline = s => esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>');
  while (i < lines.length) {
    const ln = lines[i];
    if (!ln.trim()) { i++; continue; }
    let m;
    if ((m = ln.match(/^(#{1,4})\s+(.*)/))) { const l = Math.min(4, m[1].length); out.push(`<h${l}>${inline(m[2])}</h${l}>`); i++; continue; }
    if (/^(-{3,}|\*{3,})\s*$/.test(ln)) { out.push('<hr>'); i++; continue; }
    if (/^\s*\|.*\|\s*$/.test(ln) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      const row = r => r.trim().replace(/^\||\|$/g, '').split('|').map(c => inline(c.trim()));
      const head = row(ln); i += 2; const body = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) body.push(row(lines[i++]));
      out.push(`<table><thead><tr>${head.map(c => `<th>${c}</th>`).join('')}</tr></thead><tbody>${body.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table>`);
      continue;
    }
    if (/^\s*>/.test(ln)) {
      const buf = []; while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ''));
      out.push(`<blockquote>${inline(buf.join(' '))}</blockquote>`); continue;
    }
    if (/^\s*([-*+]|\d+[.)])\s+/.test(ln)) {
      const ordered = /^\s*\d/.test(ln); const items = [];
      while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) {
        let t = lines[i++].replace(/^\s*([-*+]|\d+[.)])\s+/, '');
        const task = t.match(/^\[( |x|X)\]\s*(.*)/);
        items.push(task ? `<li class="task ${task[1] !== ' ' ? 'done' : ''}">${inline(task[2])}</li>` : `<li>${inline(t)}</li>`);
      }
      out.push(ordered ? `<ol>${items.join('')}</ol>` : `<ul>${items.join('')}</ul>`); continue;
    }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|\s*([-*+]|\d+[.)])\s|\s*>|\s*\|)/.test(lines[i])) buf.push(lines[i++]);
    if (!buf.length) buf.push(lines[i++]);
    out.push(`<p>${buf.map(inline).join('<br>')}</p>`);
  }
  return out.join('\n');
}

/* ============================== 主题 / 偏好 ============================== */
const mq = matchMedia('(prefers-color-scheme: light)');
function applyPrefs() {
  const t = S.cfg.ui_theme || 'dark';
  document.documentElement.dataset.theme = t === 'system' ? (mq.matches ? 'light' : 'dark') : t;
  document.documentElement.style.setProperty('--read-scale', S.cfg.ui_font_scale || 1);
  $('#btn-pin').classList.toggle('is-on', !!S.cfg.ui_on_top);
  $('#btn-pin').setAttribute('aria-pressed', String(!!S.cfg.ui_on_top));
  $$('#view-seg button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.view === (S.cfg.ui_view || 'timeline'))));
  const sel = $('#pair-select');
  if (sel.options.length !== S.pairs.length) sel.replaceChildren(...S.pairs.map(p => h('option', { value: p }, pairLabel(p))));
  sel.value = S.cfg.translate_mode;
}
mq.addEventListener?.('change', applyPrefs);
async function setPref(key, value) {
  S.cfg[key] = value; applyPrefs();
  const r = await tryApi('set_pref', { key, value });
  if (r && r.config) S.cfg = r.config;
}

/* ============================== 导航 ============================== */
function go(page) {
  if (S.page === 'settings' && page !== 'settings' && S.settingsDraft && Object.keys(S.settingsDraft).length) {
    toast('设置有未保存的更改', 'warn');
  }
  S.page = page;
  $$('.page').forEach(p => p.classList.toggle('active', p.id === `page-${page}`));
  $$('.rail-btn[data-nav]').forEach(b => (b.dataset.nav === page ? b.setAttribute('aria-current', 'page') : b.removeAttribute('aria-current')));
  if (page === 'live') requestAnimationFrame(() => scrollBottom(S.stick));
  if (page === 'library') loadLibrary();
  if (page === 'terms') renderTerms();
  if (page === 'settings') renderSettings();
}
$$('.rail-btn[data-nav]').forEach(b => b.addEventListener('click', () => go(b.dataset.nav)));
$('#nav-help').addEventListener('click', () => showOnboarding());
$('#nav-backend').addEventListener('click', () => showOnboarding());

/* ============================== 后端状态 ============================== */
function backendSummary() {
  const b = S.backend || {};
  if (b.checking && !b.ok) return { cls: 'warn', label: '检查中', text: b.note || '正在连接模型后端…' };
  if (!b.ok) return { cls: 'bad', label: '离线', text: b.error || '连不上模型后端' };
  if (!b.asr_ok) return { cls: 'warn', label: '缺模型', text: `识别模型「${S.cfg.asr_model}」未加载` };
  if (!b.chat_ok) return { cls: 'warn', label: '缺模型', text: `翻译模型「${S.cfg.chat_model}」未加载` };
  return { cls: 'ok', label: '就绪', text: '模型后端在线' };
}
function renderBackend() {
  const s = backendSummary();
  $('#backend-dot').className = `dot ${s.cls}`;
  $('#backend-label').textContent = s.label;
  $('#nav-backend').title = s.text;
  if (!S.session.active && !S.order.length) renderTranscript();
  renderConsoleHint();
}

/* ============================== 实时页：控制台 ============================== */
function renderConsole() {
  const c = $('#console');
  if (c.contains(document.activeElement) && document.activeElement.type === 'range') return updateConsoleLive();
  const src = S.sources;
  if (!src.mic) return;
  const mic = src.mic, med = src.media;
  const card = (s, ico, name, sub, extra) => {
    const gain = Math.round((s.gain ?? 1) * 100);
    const slider = h('input', {
      type: 'range', min: 0, max: 300, step: 5, value: gain, 'aria-label': `${name}音量`,
      style: { '--fill': `${gain / 3}%` },
      on: {
        input: e => { e.target.style.setProperty('--fill', `${e.target.value / 3}%`); e.target.nextSibling.textContent = `${e.target.value}%`; },
        change: e => api('set_source', { key: s.key, gain: e.target.value / 100 }),
      },
    });
    return h('div', { class: `src-card ${s.enabled ? '' : 'off'}`, id: `src-${s.key}` },
      h('div', { class: 'src-head' },
        h('div', { class: 'src-ico' }, icon(ico)),
        h('div', { class: 'grow' }, h('div', { class: 'src-name' }, name), h('div', { class: 'src-sub', title: sub }, sub)),
        h('label', { class: 'switch', title: s.enabled ? '点击静音该音源' : '点击开启' },
          h('input', {
            type: 'checkbox', checked: s.enabled, 'aria-label': `启用${name}`,
            on: { change: e => api('set_source', { key: s.key, enabled: e.target.checked }) },
          }), h('span'))),
      h('div', { class: `meter ${s.enabled ? '' : 'off'}`, title: '电平（竖线 = 静音阈值，低于它不算说话）' },
        h('i'), h('span', { class: 'thr', style: { left: `${clamp(s.threshold / 0.15 * 100, 0, 100)}%` } })),
      h('div', { class: 'src-row' }, h('label', null, '音量'), slider, h('output', null, `${gain}%`)),
      extra);
  };
  const micSub = mic.available ? (mic.device || '默认输入设备') : (mic.error ? '未连接 · ' + mic.error : '未连接');
  let medSub;
  if (med.kind === 'sck') medSub = med.available ? (med.active_app ? `仅 ${med.active_app}` : '全部系统声音') : (med.needs_permission ? '需要屏幕录制权限' : (med.error || '未启动'));
  else if (med.kind === 'device') medSub = `回环设备 · ${med.device}`;
  else medSub = med.error || '不可用';

  const extra = [];
  if (med.kind === 'sck' && med.available) {
    const sel = h('select', {
      class: 'select', 'aria-label': '捕获来源',
      on: {
        focus: () => api('refresh_apps').then(apps => { if (apps) { S.sources.media.apps = apps; fillApps(sel); } }),
        change: e => { toast('正在切换捕获来源…'); api('set_capture_app', { bundle: e.target.value }).catch(err => toast(err.message, 'error')); },
      },
    });
    const fillApps = el => {
      const cur = S.sources.media.capture_app || '';
      const apps = S.sources.media.apps || [];
      el.replaceChildren(h('option', { value: '' }, '全部系统声音'),
        ...apps.map(a => h('option', { value: a.bundle }, a.name)),
        ...(cur && !apps.some(a => a.bundle === cur) ? [h('option', { value: cur }, `${cur}（未运行）`)] : []));
      el.value = cur;
    };
    fillApps(sel);
    extra.push(h('div', { class: 'src-row' }, h('label', null, '来源'), sel));
  }
  extra.push(h('div', { class: 'check-row', title: '用声纹把不同的人标成「说话人1/2…」，结束后可在记录库命名' },
    h('span', null, '区分说话人'),
    h('label', { class: 'switch' }, h('input', {
      type: 'checkbox', checked: !!med.diarize, 'aria-label': '区分说话人',
      on: { change: e => api('set_diarize', { on: e.target.checked }) },
    }), h('span'))));
  if (med.needs_permission) {
    extra.push(h('div', { class: 'callout warn' }, icon('alert'),
      h('div', { class: 'grow' }, h('b', null, '需要「屏幕录制」权限'), h('p', null, '用于抓取会议软件、视频的声音（音频不出本机）。授权后点「重试」，个别系统需重启 App。'),
        h('div', { class: 'row', style: { marginTop: '8px' } },
          h('button', { class: 'btn sm primary', on: { click: () => api('request_permission') } }, '去授权'),
          h('button', { class: 'btn sm', on: { click: async () => { const r = await tryApi('retry_media'); if (r && r.available) toast('系统声音已连接', 'ok'); } } }, '重试')))));
  }
  const focusLabel = c.contains(document.activeElement) ? document.activeElement.getAttribute('aria-label') : null;
  c.replaceChildren(
    h('div', { class: 'sec-label' }, '音源'),
    card(mic, 'mic', '麦克风', micSub),
    card(med, 'media', '系统声音', medSub, extra),
    h('div', { id: 'console-hint' }),
  );
  if (focusLabel) c.querySelector(`[aria-label="${CSS.escape(focusLabel)}"]`)?.focus();   // 重建后焦点留在原控件
  renderConsoleHint();
}
function updateConsoleLive() {
  for (const k of ['mic', 'media']) {
    const s = S.sources[k]; const el = $(`#src-${k}`);
    if (!s || !el) continue;
    el.classList.toggle('off', !s.enabled);
  }
}
function renderConsoleHint() {
  const el = $('#console-hint'); if (!el) return;
  const s = backendSummary();
  el.replaceChildren();
  if (s.cls !== 'ok') {
    el.append(h('div', { class: `callout ${s.cls === 'bad' ? 'bad' : 'warn'}` }, icon('alert'),
      h('div', { class: 'grow' }, h('b', null, s.cls === 'bad' ? '模型后端离线' : '模型未就绪'), h('p', null, s.text),
        h('div', { class: 'row', style: { marginTop: '8px' } },
          h('button', { class: 'btn sm', on: { click: () => showOnboarding() } }, '检查环境'),
          h('button', { class: 'btn sm ghost', on: { click: () => { api('check_backend').then(b => { S.backend = b; renderBackend(); }); } } }, '刷新')))));
  }
  el.append(h('div', { class: 'sec-label', style: { marginTop: '12px' } }, '提示'),
    h('div', { class: 'callout' }, icon('info'), h('div', { class: 'grow dim', style: { fontSize: '12px', lineHeight: '1.6' } },
      '电平条上的竖线是静音阈值：说话时电平应明显越过它。环境吵或识别到杂音，到「设置 → 音频」点「校准」。')));
}

/* 电平表 + 波形（rAF 驱动，不重建 DOM） */
let lvlRaf = 0;
function paintLevels() {
  lvlRaf = 0;
  for (const k of ['mic', 'media']) {
    const el = $(`#src-${k}`); if (!el) continue;
    const v = S.levels[k] || 0; const s = S.sources[k] || {};
    const m = el.querySelector('.meter i');
    m.style.width = `${Math.round(v * 100)}%`;
    el.querySelector('.meter').classList.toggle('hot', v > 0.9);
    el.classList.toggle('speaking', s.enabled && v * 0.15 > (s.threshold || 0.01));
  }
  const v = Math.max(S.levels.mic || 0, S.levels.media || 0);
  $$('#wave i').forEach((b, i) => { const k = [0.55, 0.85, 1, 0.75, 0.5][i]; b.style.height = `${4 + Math.round(16 * Math.min(1, v * 1.6) * k)}px`; });
}

/* ============================== 实时页：会话 ============================== */
function renderSession() {
  const a = !!S.session.active;
  const b = $('#rec-btn');
  b.classList.toggle('recording', a);
  $('#rec-label').textContent = a ? '结束并生成纪要' : '开始录制';
  b.title = a ? `结束会议（${MOD}R）` : `开始录制（${MOD}R）`;
  $('#nav-live-badge').classList.toggle('hidden', !a);
  $('#session-stats').style.opacity = a || S.order.length ? 1 : .45;
  updateStats();
}
function updateStats() {
  $('#stat-count').textContent = S.order.length;
  const lat = S.order.map(id => S.entries.get(id)?.latency).filter(Boolean);
  $('#stat-latency').textContent = lat.length ? `${(lat.reduce((a, b) => a + b, 0) / lat.length).toFixed(1)}s` : '—';
}
let recBusy = false;
async function toggleRecording() {
  if (recBusy) return;
  recBusy = true; $('#rec-btn').classList.add('busy');
  try {
    if (S.session.active) {
      await api('end_session');
      toast('会议已结束，正在后台生成纪要。现在就可以开始下一场。', 'ok', { timeout: 4500 });
    } else {
      let r = await api('start_session');
      if (!r.ok && r.reason === 'backend') {
        const yes = await confirmBox('模型后端还没准备好',
          `${r.message}。仍然开始的话只会录音；之后可在记录库里对这条记录「从录音重新识别」。`, '仍然开始录音');
        if (!yes) return;
        r = await api('start_session', { force: true });
      }
      if (r.ok) { S.session = r.session; clearLive(); }
    }
  } catch (e) { toast(e.message, 'error'); }
  finally { recBusy = false; $('#rec-btn').classList.remove('busy'); renderSession(); }
}
$('#rec-btn').addEventListener('click', toggleRecording);
function clearLive() {
  S.entries.clear(); S.order = []; S.unseen = 0; S.stick = true; S.interim = null;
  renderTranscript(); renderInterim(); updateStats();
}

/* ============================== 实时页：转录 ============================== */
function entryEl(e, ctx = {}) {
  const tgt = e.target || (e.lang === pairOf()[0] ? pairOf()[1] : pairOf()[0]);
  const el = h('article', { class: `entry ${e.starred ? 'starred' : ''}`, dataset: { id: e.id }, tabindex: -1 });
  const timeTxt = ctx.record ? fmtClock(e.t_start) : (e.time || '').slice(0, 5);
  el.append(
    h('div', { class: 'entry-meta' },
      h('span', { class: 'spk', style: { '--spk': spkColor(e.speaker) }, title: e.speaker }, e.speaker || ''),
      h('time', { class: ctx.record && ctx.onSeek ? 'seek' : '', title: ctx.onSeek ? '从这里播放' : '', on: ctx.onSeek ? { click: () => ctx.onSeek(e) } : null }, timeTxt)),
    h('div', { class: 'entry-body' }),
    h('div', { class: 'entry-actions' },
      h('button', { class: 'btn ghost sm icon star-btn', title: e.starred ? '取消标记' : `标记为重点${ctx.record ? '' : `（${MOD}B）`}`, 'aria-pressed': String(!!e.starred), on: { click: () => toggleStar(e, ctx) } }, icon('star', 'sm')),
      h('button', { class: 'btn ghost sm icon', title: '复制', on: { click: () => copyText(`${e.source}\n${e.translation || ''}`.trim()) } }, icon('copy', 'sm')),
      h('button', { class: 'btn ghost sm icon', title: '纠错', on: { click: () => editEntry(el, e, ctx) } }, icon('edit', 'sm')),
      !ctx.record ? h('button', { class: 'btn ghost sm icon', title: '重新翻译', on: { click: () => api('retranslate', { id: e.id }) } }, icon('refresh', 'sm')) : null));
  fillEntryBody(el, e, tgt, ctx.q);
  return el;
}
function fillEntryBody(el, e, tgt, q) {
  const body = el.querySelector('.entry-body');
  tgt = tgt || e.target;
  const src = h('p', { class: 'src selectable' }, h('span', { class: 'chip lang tag', style: { color: `var(--lang-${e.lang})` } }, langShort(e.lang)));
  src.insertAdjacentHTML('beforeend', highlight(e.source, q));
  let dst;
  if (e.error) dst = h('p', { class: 'dst err' }, `翻译失败：${e.error}`);
  else if (!e.translation && e.translating) dst = h('p', { class: 'dst pending' }, `正在翻译成${langName(tgt)}…`);
  else if (!e.translation) dst = h('p', { class: 'dst pending' }, '（无译文）');
  else {
    dst = h('p', { class: 'dst selectable' });
    dst.insertAdjacentHTML('beforeend', highlight(e.translation || '', q));
    if (e.translating) dst.append(h('span', { class: 'caret' }));
  }
  if (e.edited) (e.translation ? dst : src).append(h('span', { class: 'edited' }, '已修改'));
  body.replaceChildren(src, dst);
  el.classList.toggle('starred', !!e.starred);
  const sb = el.querySelector('.star-btn'); if (sb) sb.setAttribute('aria-pressed', String(!!e.starred));
}
async function copyText(t) {
  try { await navigator.clipboard.writeText(t); toast('已复制', 'ok'); }
  catch { const r = await tryApi('copy', { text: t }); if (r && r.ok) toast('已复制', 'ok'); }
}
async function toggleStar(e, ctx) {
  const v = !e.starred;
  const r = await tryApi('update_entry', { id: e.id, record_id: ctx.record || null, starred: v });
  if (!r) return false;
  e.starred = v;
  const el = ctx.root ? ctx.root.querySelector(`.entry[data-id="${e.id}"]`) : findEntryEl(e.id);
  if (el && !el.querySelector('.entry-edit')) fillEntryBody(el, e, null, ctx.q);
  if (ctx.record) updateStarTab();
  return true;
}
function updateStarTab() {
  const rec = S.lib.rec; if (!rec) return;
  const n = rec.transcript.filter(x => x.starred).length;
  const tab = $('#tab-starred'); if (tab) tab.textContent = `重点 · ${n}`;
  const it = S.lib.items.find(x => x.id === rec.meta.id); if (it) { it.starred_count = n; renderLibList(); }
}
function editEntry(el, e, ctx) {
  const body = el.querySelector('.entry-body');
  const s = h('textarea', { class: 'textarea', rows: 2, value: e.source, 'aria-label': '原文' });
  const t = h('textarea', { class: 'textarea', rows: 2, value: e.translation || '', 'aria-label': '译文' });
  const save = async () => {
    if (!s.value.trim()) { toast('原文不能为空', 'warn'); s.focus(); return; }
    const r = await tryApi('update_entry', { id: e.id, record_id: ctx.record || null, source: s.value, translation: t.value });
    if (!r) return;                                   // 失败：保留编辑框，别丢用户输入
    Object.assign(e, { source: s.value.trim(), translation: t.value.trim(), edited: true });
    fillEntryBody(el, e, null, ctx.q);
  };
  body.replaceChildren(h('div', { class: 'entry-edit' },
    h('label', { class: 'muted', style: { fontSize: '11px' } }, '原文'), s,
    h('label', { class: 'muted', style: { fontSize: '11px' } }, '译文'), t,
    h('div', { class: 'row' },
      h('span', { class: 'muted grow', style: { fontSize: '11px' } }, `${MOD}↩ 保存 · Esc 取消`),
      h('button', { class: 'btn sm ghost', on: { click: () => fillEntryBody(el, e, null, ctx.q) } }, '取消'),
      h('button', { class: 'btn sm primary', on: { click: save } }, '保存'))));
  const keys = ev => { if (ev.key === 'Enter' && (ev.metaKey || ev.ctrlKey)) save(); if (ev.key === 'Escape') { ev.stopPropagation(); fillEntryBody(el, e, null, ctx.q); } };
  s.addEventListener('keydown', keys); t.addEventListener('keydown', keys);
  s.focus();
}

function colFor(e) {
  const [a, b] = pairOf();
  return e.lang === a ? a : b;
}
function findEntryEl(id) { return $(`#transcript .entry[data-id="${id}"]`); }

function renderTranscript() {
  const box = $('#transcript');
  const view = S.cfg.ui_view || 'timeline';
  box.classList.toggle('columns', view === 'columns' && S.order.length > 0);
  box.replaceChildren();
  if (!S.order.length) { box.append(S.session.active ? listeningEmpty() : readyPanel()); return; }
  if (view === 'columns') {
    const [a, b] = pairOf();
    for (const l of [a, b]) {
      box.append(h('div', { class: 'lang-col', dataset: { lang: l } },
        h('div', { class: 'lang-col-head' }, h('span', { class: 'chip lang', style: { color: `var(--lang-${l})` } }, langName(l)),
          h('span', { class: 'muted', style: { fontSize: '12px' } }, `发言 → ${langName(l === a ? b : a)}`), h('div', { class: 'grow' }),
          h('button', { class: 'btn ghost sm icon', title: '只看这一栏', on: { click: ev => soloCol(ev.currentTarget.closest('.lang-col')) } }, icon('expand', 'sm'))),
        h('div', { class: 'lang-col-body' })));
    }
  }
  for (const id of S.order) appendEntryDom(S.entries.get(id), false, true);
  scrollBottom(true);
}
function soloCol(col) {
  const solo = !col.classList.contains('solo');
  $$('#transcript .lang-col').forEach(c => { c.classList.toggle('solo', solo && c === col); c.classList.toggle('hidden', solo && c !== col); });
}
function appendEntryDom(e, animate = true, fromRender = false) {
  const box = $('#transcript');
  if (!fromRender && !box.querySelector('.entry') && !box.querySelector('.lang-col')) { renderTranscript(); return; }
  const el = entryEl(e);
  if (!animate) el.style.animation = 'none';
  if (box.classList.contains('columns')) {
    const col = box.querySelector(`.lang-col[data-lang="${colFor(e)}"] .lang-col-body`) || box.querySelector('.lang-col-body');
    col.append(el);
  } else box.append(el);
}
let autoScrollAt = 0;
function scrollBottom(force) {
  const box = $('#transcript');
  const targets = box.classList.contains('columns') ? $$('.lang-col-body', box) : [box];
  for (const t of targets) if (force || S.stick) { autoScrollAt = Date.now(); t.scrollTop = t.scrollHeight; }
}
$('#transcript').addEventListener('scroll', e => {
  if (Date.now() - autoScrollAt < 350) return;      // 自己滚到底时产生的 scroll 事件不算「用户往上翻」
  const t = e.target; const near = t.scrollHeight - t.scrollTop - t.clientHeight < 120;
  S.stick = near; if (near) { S.unseen = 0; $('#new-pill').classList.add('hidden'); }
}, true);
$('#new-pill').addEventListener('click', () => { S.stick = true; S.unseen = 0; $('#new-pill').classList.add('hidden'); scrollBottom(true); });

function onEntry(e) {
  if (S.entries.has(e.id)) return onEntryUpdate(e);
  S.entries.set(e.id, e); S.order.push(e.id);
  if (S.interim && S.interim.id === e.id) { S.interim = null; renderInterim(); }
  if (S.order.length === 1) renderTranscript(); else appendEntryDom(e);
  if (S.stick) scrollBottom(); else { S.unseen++; const p = $('#new-pill'); p.classList.remove('hidden'); p.querySelector('span').textContent = `${S.unseen} 条新内容`; }
  updateStats(); renderSession();
}
function onEntryUpdate(e) {
  const cur = S.entries.get(e.id); if (!cur) return;
  Object.assign(cur, e);
  const el = findEntryEl(e.id); if (el && !el.querySelector('.entry-edit')) fillEntryBody(el, cur);
}
function onTranslation(d) {
  const e = S.entries.get(d.id); if (!e) return;
  e.translation = d.translation; e.translating = !d.done;
  if (d.done) { e.error = d.error || null; e.latency = d.latency; if (d.target) e.target = d.target; updateStats(); }
  const el = findEntryEl(d.id);
  if (el && !el.querySelector('.entry-edit')) fillEntryBody(el, e);
  if (S.stick) scrollBottom();
}

function renderInterim() {
  const strip = $('#live-strip'); const t = $('#live-text');
  const it = S.interim;
  strip.classList.toggle('on', !!it);
  if (it) {
    t.replaceChildren(h('span', { class: 'who', style: { '--spk': spkColor(it.speaker) } }, it.speaker), it.source);
  } else t.textContent = S.session.active ? '正在聆听… 说话时识别结果会先出现在这里' : '开始录制后，正在说的话会先出现在这里';
}

function readyPanel() {
  const s = backendSummary();
  const pair = h('select', { class: 'select', style: { width: '200px' }, 'aria-label': '语言对', on: { change: e => setPref('translate_mode', e.target.value) } },
    ...S.pairs.map(p => h('option', { value: p, selected: p === S.cfg.translate_mode }, pairLabel(p))));
  const checks = [];
  if (s.cls !== 'ok') checks.push(h('div', { class: `check ${s.cls}` }, h('span', { class: 'ico' }, icon(s.cls === 'bad' ? 'x' : 'alert', 'sm')),
    h('div', { class: 'grow' }, h('b', null, s.cls === 'bad' ? '模型后端未连接' : '模型未就绪'), h('span', null, s.text)),
    h('button', { class: 'btn sm', on: { click: () => showOnboarding() } }, '去解决')));
  const med = S.sources.media || {};
  if (med.needs_permission) checks.push(h('div', { class: 'check warn' }, h('span', { class: 'ico' }, icon('alert', 'sm')),
    h('div', { class: 'grow' }, h('b', null, '系统声音未授权'), h('span', null, '只能识别麦克风；要翻译会议软件/视频的声音需授权「屏幕录制」')),
    h('button', { class: 'btn sm', on: { click: () => api('request_permission') } }, '去授权')));
  return h('div', { class: 'ready' },
    h('div', { class: 'empty' }, h('div', { class: 'art' }, icon('live', 'lg'))),
    h('h2', null, S.order.length ? '' : '准备好开会了'),
    h('p', null, '麦克风和本机播放的声音会被实时识别、自动双语对照，结束后一键生成纪要。全程离线，音频不出本机。'),
    h('div', { class: 'pair-pick' }, h('span', { class: 'dim' }, '翻译方向'), pair),
    h('div', null, h('button', { class: 'btn primary lg', on: { click: toggleRecording } }, h('span', { class: 'dot', style: { background: 'currentColor' } }), '开始录制')),
    checks.length ? h('div', { class: 'checks' }, checks) : null,
    h('div', { class: 'tips' },
      h('span', null, h('span', { class: 'kbd' }, `${MOD}R`), ' 开始/结束'),
      h('span', null, h('span', { class: 'kbd' }, `${MOD}B`), ' 标记上一句'),
      h('span', null, h('span', { class: 'kbd' }, `${MOD}⇧S`), ' 字幕条'),
      h('span', null, '或把音视频文件', h('a', { href: '#', style: { color: 'var(--accent)' }, on: { click: ev => { ev.preventDefault(); importFile(); } } }, '导入'), '进来')));
}
function listeningEmpty() {
  return h('div', { class: 'empty' }, h('div', { class: 'art' }, icon('wave', 'lg')),
    h('h3', null, '正在聆听'), h('p', null, '开口说话或播放会议声音。说完一句（停顿约 1 秒）后，原文和译文会出现在这里。'));
}

/* 顶栏其余按钮 */
$('#pair-select').addEventListener('change', e => setPref('translate_mode', e.target.value).then(() => S.order.length && renderTranscript()));
$$('#view-seg button').forEach(b => b.addEventListener('click', () => setPref('ui_view', b.dataset.view).then(renderTranscript)));
$('#btn-pin').addEventListener('click', async () => { S.cfg.ui_on_top = !S.cfg.ui_on_top; applyPrefs(); await tryApi('set_on_top', { on: S.cfg.ui_on_top }); });
$('#btn-subtitle').addEventListener('click', openSubtitle);
$('#btn-import').addEventListener('click', importFile);
$('#lib-import').addEventListener('click', importFile);
$('#lib-open-dir').addEventListener('click', () => api('reveal', { path: S.paths.records }));

async function openSubtitle() {
  const r = await tryApi('open_overlay');
  if (r && !r.ok) { window.open(`/overlay`, 'lt-overlay', 'width=1000,height=160'); }
}
async function importFile() {
  const r = await tryApi('pick_and_import');
  if (!r) return;
  if (r.ok) { toast(`开始导入 ${r.name}，完成后会出现在记录库`, 'ok'); return; }
  if (r.cancelled && !r.native) {
    const p = await promptBox('导入音频 / 视频', '浏览器模式下无法弹出文件选择框，请粘贴文件的完整路径：', '', '/Users/you/Downloads/meeting.m4a');
    if (p) { const r2 = await tryApi('import_path', { path: p }); if (r2 && r2.ok) toast(`开始导入 ${r2.name}`, 'ok'); else if (r2) toast(r2.error, 'error'); }
  }
}

/* ============================== 任务托盘 ============================== */
function renderJobs() {
  const box = $('#jobs');
  const now = Date.now() / 1000;
  const list = [...S.jobs.values()].filter(j => j.status === 'running' || (j.finished && now - j.finished < 6 && j.status !== 'done'));
  box.replaceChildren(...list.map(j => h('div', { class: 'job' },
    h('div', { class: 'row' }, h('span', { class: 'ttl' }, j.title),
      j.status === 'running' && j.cancellable ? h('button', { class: 'btn ghost sm', on: { click: () => api('cancel_job', { id: j.id }) } }, '取消') :
        j.status === 'failed' ? h('span', { class: 'chip bad' }, '失败') : null),
    h('div', { class: `progress ${j.progress < 0.02 && j.status === 'running' ? 'indet' : ''}` }, h('i', { style: { width: `${Math.round((j.progress || 0) * 100)}%` } })),
    h('div', { class: 'msg' }, j.message || ''))));
  const running = [...S.jobs.values()].filter(j => j.status === 'running').length;
  const b = $('#nav-lib-badge'); b.textContent = running || ''; b.classList.toggle('hidden', !running);
}
function onJob(j) {
  const prev = S.jobs.get(j.id);
  S.jobs.set(j.id, j);
  renderJobs();
  if (j.status !== 'running' && prev?.status === 'running') {
    if (j.status === 'done') toast(j.message || `${j.title}完成`, 'ok', j.record_id ? { action: { label: '查看', fn: () => openRecord(j.record_id) }, timeout: 6000 } : {});
    else if (j.status === 'failed') toast(`${j.title}失败：${j.message}`, 'error');
    setTimeout(renderJobs, 6500);
  }
}

/* ============================== 记录库 ============================== */
const STATUS = {
  done: null, summarizing: ['warn', '生成中'], failed: ['bad', '纪要失败'], interrupted: ['warn', '未完成'],
  pending: ['warn', '待生成'], recording: ['ok', '录制中'],
};
async function loadLibrary() {
  const items = await tryApi('list_records', { query: S.lib.q });
  if (!items) return;
  S.lib.items = items;
  renderLibList();
  if (S.lib.sel && !items.some(x => x.id === S.lib.sel)) { S.lib.sel = null; S.lib.rec = null; }
  if (!S.lib.sel && items.length && !S.lib.rec) selectRecord(items[0].id);
  if (!items.length) renderDetail();
}
const reloadLibrary = debounce(() => { if (S.page === 'library') loadLibrary(); }, 300);
function dateGroup(d) {
  const now = new Date(); const day = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const diff = (day - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 864e5;
  if (diff <= 0) return '今天'; if (diff === 1) return '昨天'; if (diff < 7) return '本周';
  if (d.getMonth() === now.getMonth() && d.getFullYear() === now.getFullYear()) return '本月';
  return `${d.getFullYear()} 年 ${d.getMonth() + 1} 月`;
}
function renderLibList() {
  const box = $('#lib-items');
  let items = S.lib.items;
  if (S.lib.filter === 'starred') items = items.filter(x => x.starred_count > 0);
  if (S.lib.filter === 'attention') items = items.filter(x => ['failed', 'interrupted', 'pending'].includes(x.status));
  $('#lib-count').textContent = S.lib.items.length ? `共 ${S.lib.items.length} 条` : '';
  if (!items.length) {
    box.replaceChildren(h('div', { class: 'empty' }, h('div', { class: 'art' }, icon(S.lib.q ? 'search' : 'library', 'lg')),
      h('h3', null, S.lib.q ? '没有匹配的记录' : (S.lib.filter !== 'all' ? '这里是空的' : '还没有记录')),
      h('p', null, S.lib.q ? '换个关键词试试，搜索覆盖标题、原文、译文和纪要。' : '在「实时」页开始一场会议，或导入一段录音。')));
    return;
  }
  let g = null; const kids = [];
  for (const it of items) {
    const d = parseDate(it.created_at); const grp = dateGroup(d);
    if (grp !== g) { g = grp; kids.push(h('div', { class: 'lib-group' }, grp)); }
    const st = STATUS[it.status];
    const t = h('div', { class: `t ${it.title ? '' : 'untitled'}` });
    t.innerHTML = highlight(it.title || `未命名会议 · ${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`, S.lib.q);
    const snip = it.match ? h('div', { class: 'snip', html: highlight(it.match, S.lib.q) }) : null;
    kids.push(h('div', {
      class: 'lib-item', role: 'option', tabindex: 0, 'aria-selected': String(it.id === S.lib.sel), dataset: { id: it.id },
      on: { click: () => selectRecord(it.id), keydown: e => { if (e.key === 'Enter') selectRecord(it.id); } },
    }, t, h('div', { class: 'm' },
      h('span', { class: 'num' }, `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`),
      h('span', null, fmtDur(it.duration_s)),
      it.entry_count != null ? h('span', null, `${it.entry_count} 句`) : null,
      it.starred_count ? h('span', { style: { color: 'var(--warn)' } }, `★ ${it.starred_count}`) : null,
      it.app && it.app.startsWith('import:') ? h('span', null, '导入') : null,
      st ? h('span', { class: `chip ${st[0]}` }, st[1]) : null), snip));
  }
  box.replaceChildren(...kids);
}
$('#lib-search').addEventListener('input', debounce(e => { S.lib.q = e.target.value.trim(); loadLibrary(); }, 250));
$$('#lib-filter button').forEach(b => b.addEventListener('click', () => {
  S.lib.filter = b.dataset.f; $$('#lib-filter button').forEach(x => x.setAttribute('aria-pressed', String(x === b))); renderLibList();
}));
$('#lib-items').addEventListener('keydown', e => {
  if (!['ArrowDown', 'ArrowUp'].includes(e.key)) return;
  const els = $$('.lib-item', $('#lib-items')); const i = els.findIndex(x => x.dataset.id === S.lib.sel);
  const n = els[clamp(i + (e.key === 'ArrowDown' ? 1 : -1), 0, els.length - 1)];
  if (n) { selectRecord(n.dataset.id); n.focus(); e.preventDefault(); }
});

async function openRecord(id) { go('library'); S.lib.q = ''; $('#lib-search').value = ''; await loadLibrary(); selectRecord(id); }
async function selectRecord(id, keepTab) {
  S.lib.sel = id;
  $$('.lib-item').forEach(x => x.setAttribute('aria-selected', String(x.dataset.id === id)));
  const rec = await tryApi('get_record', { id });
  if (!rec || S.lib.sel !== id) return;
  const prevId = S.lib.rec?.meta?.id;
  const same = prevId === id;
  const keep = same && player ? player.state() : null;
  const scroll = same ? ($('#detail-scroll')?.scrollTop || 0) : 0;
  S.lib.rec = rec;
  if (!keepTab && !same) S.lib.tab = rec.summary ? 'summary' : 'transcript';
  renderDetail();
  if (keep && player) player.restore(keep);           // 刷新详情不打断回放位置 / 倍速
  if (scroll && $('#detail-scroll')) $('#detail-scroll').scrollTop = scroll;
}

let player = null;
function renderDetail() {
  const box = $('#lib-detail');
  if (player) { player.audio.pause(); player = null; }
  const rec = S.lib.rec;
  if (!rec) { box.replaceChildren(h('div', { class: 'empty' }, h('div', { class: 'art' }, icon('library', 'lg')), h('h3', null, '选择一条记录'), h('p', null, '左侧选择会议，查看纪要、逐句转录并回放录音。'))); return; }
  const m = rec.meta; const id = m.id;
  const d = parseDate(m.created_at);
  const listItem = S.lib.items.find(x => x.id === id) || {};
  const status = listItem.status || m.summary_status;
  const runningJob = [...S.jobs.values()].find(j => j.record_id === id && j.status === 'running');

  const title = h('input', {
    class: 'title-edit', value: m.title || '', placeholder: '未命名会议（点击命名）', 'aria-label': '标题',
    on: {
      keydown: e => { if (e.key === 'Enter') e.target.blur(); if (e.key === 'Escape') { e.target.value = m.title || ''; e.target.blur(); } },
      blur: async e => { const v = e.target.value.trim(); if (v !== (m.title || '')) { const r = await tryApi('rename_record', { id, title: v }, '已重命名'); if (r) { m.title = v; reloadLibrary(); } } },
    },
  });
  const langs = (m.langs || []).map(l => h('span', { class: 'chip lang', style: { color: `var(--lang-${l})` } }, langName(l)));
  const spk = (m.speaker_names || []).length;
  const metaRow = h('div', { class: 'meta-row' },
    h('span', null, d.toLocaleString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit' })),
    h('span', { class: 'sep' }), h('span', null, fmtDur(m.duration_s)),
    h('span', { class: 'sep' }), h('span', null, `${rec.transcript.length} 句`),
    spk ? [h('span', { class: 'sep' }), h('span', null, `${spk} 位说话人`)] : null,
    m.source_file ? [h('span', { class: 'sep' }), h('span', null, `导入自 ${m.source_file}`)] : null,
    langs.length ? h('span', { class: 'sep' }) : null, langs);

  const banners = [];
  if (runningJob) banners.push(h('div', { class: 'callout' }, icon('sparkle'), h('div', { class: 'grow' }, h('b', null, runningJob.title), h('p', null, runningJob.message),
    h('div', { class: 'progress', style: { marginTop: '8px' } }, h('i', { style: { width: `${Math.round(runningJob.progress * 100)}%` } })))));
  else if (status === 'interrupted' || status === 'pending') banners.push(h('div', { class: 'callout warn' }, icon('alert'),
    h('div', { class: 'grow' }, h('b', null, '这场会议没有正常结束'), h('p', null, '可能是上次异常退出。可以补齐翻译并生成纪要；如果转录也缺失，会从录音重新识别。')),
    h('button', { class: 'btn primary sm', on: { click: () => runJob('recover', id) } }, '恢复并生成纪要')));
  else if (status === 'failed') banners.push(h('div', { class: 'callout bad' }, icon('alert'),
    h('div', { class: 'grow' }, h('b', null, '纪要生成失败'), h('p', null, m.summary_error || '模型后端可能当时不在线。')),
    h('button', { class: 'btn sm', on: { click: () => runJob('resummarize', id) } }, '重试')));
  if (!rec.transcript.length && rec.sources.length && !runningJob && status !== 'interrupted') banners.push(h('div', { class: 'callout warn' }, icon('alert'),
    h('div', { class: 'grow' }, h('b', null, '没有转录内容'), h('p', null, '录音还在。模型后端就绪后可以从录音重新识别。')),
    h('button', { class: 'btn primary sm', on: { click: () => runJob('reprocess', id) } }, '从录音重新识别')));

  const actions = h('div', { class: 'action-row' },
    h('button', { class: 'btn', on: { click: ev => exportMenu(ev.currentTarget, id) } }, icon('export'), '导出', icon('down', 'sm')),
    h('button', { class: 'btn', on: { click: ev => regenMenu(ev.currentTarget, id) } }, icon('sparkle'), '重新生成纪要', icon('down', 'sm')),
    h('button', { class: 'btn', on: { click: () => speakersDialog(id) } }, icon('users'), '说话人'),
    rec.summary ? h('button', { class: 'btn ghost', on: { click: () => copyText(rec.summary) } }, icon('copy'), '复制纪要') : null,
    h('div', { class: 'grow' }),
    h('button', { class: 'btn ghost icon', title: '更多', on: { click: ev => moreMenu(ev.currentTarget, id) } }, icon('more')));

  const head = h('div', { class: 'detail-head' }, title, metaRow, ...banners, actions);
  const kids = [head];
  if (rec.sources.length) kids.push(buildPlayer(rec));
  const nStar = rec.transcript.filter(e => e.starred).length;
  const tabs = h('div', { class: 'tabs', role: 'tablist' },
    h('button', { role: 'tab', 'aria-selected': String(S.lib.tab === 'summary'), on: { click: () => { S.lib.tab = 'summary'; renderDetailBody(); syncTabs(); } } }, '纪要'),
    h('button', { role: 'tab', 'aria-selected': String(S.lib.tab === 'transcript'), on: { click: () => { S.lib.tab = 'transcript'; renderDetailBody(); syncTabs(); } } }, `转录 · ${rec.transcript.length}`),
    nStar ? h('button', { role: 'tab', id: 'tab-starred', 'aria-selected': String(S.lib.tab === 'starred'), on: { click: () => { S.lib.tab = 'starred'; renderDetailBody(); syncTabs(); } } }, `重点 · ${nStar}`) : null);
  const syncTabs = () => $$('button', tabs).forEach((b, i) => b.setAttribute('aria-selected', String(['summary', 'transcript', 'starred'][i] === S.lib.tab)));
  kids.push(tabs, h('div', { class: 'detail-scroll', id: 'detail-scroll' }));
  box.replaceChildren(...kids);
  renderDetailBody();
}
function renderDetailBody() {
  const sc = $('#detail-scroll'); if (!sc) return;
  const rec = S.lib.rec; const id = rec.meta.id;
  if (S.lib.tab === 'summary') {
    if (rec.summary) { sc.replaceChildren(h('div', { class: 'md selectable', html: md(rec.summary) })); }
    else sc.replaceChildren(h('div', { class: 'empty' }, h('div', { class: 'art' }, icon('sparkle', 'lg')), h('h3', null, '还没有纪要'),
      h('p', null, rec.transcript.length ? '用当前的转录生成一份结构化纪要。' : '先需要有转录内容。'),
      rec.transcript.length ? h('button', { class: 'btn primary', on: { click: () => runJob('resummarize', id) } }, '生成纪要') : null));
    return;
  }
  let list = rec.transcript;
  if (S.lib.tab === 'starred') list = list.filter(e => e.starred);
  const ctx = { record: id, q: S.lib.q, root: sc, onSeek: rec.sources.length ? e => player && player.seek(e.t_start, true, e.src || srcKeyOf(e.speaker)) : null };
  sc.replaceChildren(...list.map(e => entryEl(e, ctx)));
  if (!list.length) sc.append(h('div', { class: 'empty' }, h('h3', null, '没有内容')));
}
async function runJob(kind, id, extra = {}) {
  const r = await tryApi(kind, { id, ...extra });
  if (r && r.job) { toast('已开始，完成后会通知你', 'info'); setTimeout(() => selectRecord(id, true), 200); }
}
function exportMenu(anchor, id) {
  const ex = fmt => tryApi('export_record', { id, fmt }).then(r => r && toast(`已导出：${r.path.split('/').pop()}`, 'ok'));
  showMenu(anchor, [
    { header: '导出并在访达中显示' },
    { label: 'Markdown', hint: '.md', icon: 'library', onClick: () => ex('md') },
    { label: 'Word 文档', hint: '.docx', icon: 'book', onClick: () => ex('docx') },
    { label: '纯文本', hint: '.txt', icon: 'list', onClick: () => ex('txt') },
    '-',
    { label: 'SRT 字幕', hint: '.srt', icon: 'sub', onClick: () => ex('srt') },
    { label: 'WebVTT 字幕', hint: '.vtt', icon: 'sub', onClick: () => ex('vtt') },
  ]);
}
function regenMenu(anchor, id) {
  showMenu(anchor, [{ header: '选择纪要模板' }, ...S.templates.map(t => ({
    label: t, hint: t === (S.lib.rec.meta.summary_template || S.cfg.summary_template) ? '当前' : '', onClick: () => runJob('resummarize', id, { template: t }),
  }))]);
}
function moreMenu(anchor, id) {
  showMenu(anchor, [
    { label: '在访达中显示', icon: 'folder', onClick: () => api('reveal_record', { id }) },
    { label: '从录音重新识别', icon: 'refresh', disabled: !S.lib.rec.sources.length, onClick: async () => {
      if (await confirmBox('从录音重新识别？', '会用当前的识别 / 翻译模型重新处理整段录音，并覆盖现有的转录（手动纠错和标记会丢失）。', '重新识别')) runJob('reprocess', id);
    } },
    '-',
    { label: '删除记录', icon: 'trash', danger: true, onClick: async () => {
      if (await confirmBox('删除这条记录？', '录音、转录和纪要都会被永久删除，无法恢复。', '删除', 'danger')) {
        const r = await tryApi('delete_record', { id }, '已删除');
        if (r) { S.lib.sel = null; S.lib.rec = null; loadLibrary(); }
      }
    } },
  ]);
}
async function speakersDialog(id) {
  const list = await tryApi('speakers', { id });
  if (!list) return;
  if (!list.length) { toast('这条记录没有说话人信息', 'warn'); return; }
  const inputs = {};
  const body = h('div', { class: 'col', style: { gap: '10px' } }, ...list.map(s => {
    const inp = h('input', { class: 'input', placeholder: '输入真实姓名', value: '' });
    inputs[s.id] = inp;
    return h('div', { class: 'check', style: { alignItems: 'flex-start' } },
      h('div', { class: 'grow' },
        h('b', { style: { color: spkColor(s.id) } }, s.id),
        h('span', null, `${s.count} 句 · “${s.sample}…”`),
        s.suggest ? h('div', { style: { marginTop: '6px' } }, h('button', { class: 'chip ok', style: { border: 0 }, on: { click: () => { inp.value = s.suggest.name; } } }, `可能是 ${s.suggest.name}（相似度 ${s.suggest.sim}）· 点击采用`)) : null),
      h('div', { style: { width: '170px' } }, inp));
  }));
  modal({
    title: '给说话人命名', desc: '命名后会改写本条记录；有声纹的说话人还会被记住，以后的会议能自动建议名字。', body, wide: true,
    actions: [{ label: '取消', kind: 'ghost' }, {
      label: '应用', kind: 'primary', onClick: async () => {
        const mapping = {}; for (const [k, v] of Object.entries(inputs)) if (v.value.trim()) mapping[k] = v.value.trim();
        if (!Object.keys(mapping).length) return;
        const r = await tryApi('name_speakers', { id, mapping }, '已更新说话人');
        if (r) selectRecord(id, true);
      },
    }],
  });
}

/* 播放器：每路音源一条 wav；offset = 该音轨相对会议起点的起始时间 */
function buildPlayer(rec) {
  const audio = new Audio(); audio.preload = 'metadata';
  let src = rec.sources[0];
  const setSrc = (s, at, play) => {
    const t = at ?? (audio.currentTime + src.offset); const wasPlaying = play ?? !audio.paused;
    src = s; audio.src = `${s.url}?token=${T}`; audio.playbackRate = +speed.value;
    if (srcSel) srcSel.value = rec.sources.indexOf(s);
    audio.addEventListener('loadedmetadata', () => { audio.currentTime = Math.max(0, t - s.offset); if (wasPlaying) audio.play().catch(() => {}); }, { once: true });
  };
  audio.src = `${src.url}?token=${T}`;
  const playBtn = h('button', { class: 'play', title: '播放 / 暂停（空格）', 'aria-label': '播放或暂停', on: { click: () => (audio.paused ? audio.play().catch(e => toast(`无法播放：${e.message}`, 'error')) : audio.pause()) } }, icon('play', 'fill'));
  const time = h('span', { class: 'time num' }, '00:00 / ' + fmtClock(src.duration));
  const bar = h('input', { type: 'range', min: 0, max: 1000, value: 0, 'aria-label': '进度', style: { '--fill': '0%' } });
  let dragging = false;
  bar.addEventListener('input', () => { dragging = true; bar.style.setProperty('--fill', `${bar.value / 10}%`); time.firstChild.textContent = `${fmtClock(bar.value / 1000 * (audio.duration || src.duration))} / ${fmtClock(audio.duration || src.duration)}`; });
  bar.addEventListener('change', () => { audio.currentTime = bar.value / 1000 * (audio.duration || src.duration); dragging = false; });
  const speed = h('select', { class: 'select', title: '倍速', 'aria-label': '播放倍速', on: { change: e => { audio.playbackRate = +e.target.value; } } },
    ...[1, 1.25, 1.5, 2].map(v => h('option', { value: v }, `${v}×`)));
  const srcSel = rec.sources.length > 1 ? h('select', { class: 'select', title: '音轨', 'aria-label': '音轨', on: { change: e => setSrc(rec.sources[+e.target.value]) } },
    ...rec.sources.map((s, i) => h('option', { value: i }, s.key === 'mic' ? '麦克风' : s.key === 'media' ? '系统声音' : s.label))) : null;
  let lastId = null;
  audio.addEventListener('play', () => playBtn.replaceChildren(icon('pause')));
  audio.addEventListener('pause', () => playBtn.replaceChildren(icon('play', 'fill')));
  audio.addEventListener('timeupdate', () => {
    const dur = audio.duration || src.duration || 1;
    if (!dragging) { bar.value = Math.round(audio.currentTime / dur * 1000); bar.style.setProperty('--fill', `${audio.currentTime / dur * 100}%`); time.firstChild.textContent = `${fmtClock(audio.currentTime)} / ${fmtClock(dur)}`; }
    const t = audio.currentTime + src.offset;
    const cur = rec.transcript.find(e => e.t_start <= t && t < (e.t_end ?? e.t_start + 3));
    const cid = cur ? String(cur.id) : null;
    if (cid !== lastId) {
      lastId = cid;
      $$('#detail-scroll .entry.playing').forEach(x => x.classList.remove('playing'));
      const el = cid && $(`#detail-scroll .entry[data-id="${cid}"]`);
      if (el) { el.classList.add('playing'); if (!audio.paused) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }
    }
  });
  audio.addEventListener('error', () => { time.firstChild.textContent = '音频无法播放'; });
  player = {
    audio, rec,
    state: () => ({ t: audio.currentTime + src.offset, rate: audio.playbackRate, key: src.key, playing: !audio.paused }),
    restore(st) { speed.value = String(st.rate); const s = rec.sources.find(x => x.key === st.key) || src; setSrc(s, st.t, false); },
    seek(t, play, key) {
      const want = rec.sources.find(x => x.key === key);
      if (want && want !== src) { setSrc(want, t, play); return; }   // 媒体的句子就放媒体音轨
      audio.currentTime = Math.max(0, t - src.offset); if (play) audio.play().catch(() => {});
    },
  };
  return h('div', { class: 'player' }, playBtn, time, bar, srcSel, speed);
}

/* ============================== 术语表 ============================== */
async function renderTerms() {
  if (!S.termsDraft) {
    const t = await tryApi('get_terms'); if (!t) return;
    S.termsDraft = t.map(x => ({ ...x, aliases: (x.aliases || []).join('，') }));
    S.termsSaved = JSON.stringify(S.termsDraft);
  }
  const q = ($('#terms-search').value || '').trim().toLowerCase();
  const page = $('#terms-page');
  const dirty = JSON.stringify(S.termsDraft) !== S.termsSaved;
  $('#terms-count').textContent = `${S.termsDraft.filter(t => t.term).length} 条`;
  const rows = S.termsDraft.map((t, i) => ({ t, i })).filter(({ t }) => !q || [t.term, t.aliases, t.translation].join(' ').toLowerCase().includes(q));
  const inp = (t, k, ph) => h('input', { class: 'input', value: t[k] || '', placeholder: ph, 'aria-label': { term: '规范写法', aliases: '别名', translation: '指定译文' }[k], on: { input: e => { t[k] = e.target.value; markTermsDirty(); } } });
  const table = h('table', { class: 'terms-table' },
    h('thead', null, h('tr', null, h('th', { style: { width: '26%' } }, '规范写法'), h('th', null, '常见错写 / 别名（逗号分隔）'), h('th', { style: { width: '26%' } }, '指定译文（可选）'), h('th', { style: { width: '40px' } }))),
    h('tbody', null, rows.map(({ t, i }) => h('tr', null,
      h('td', null, inp(t, 'term', '如：比特币')), h('td', null, inp(t, 'aliases', '如：BTC，比特弊')), h('td', null, inp(t, 'translation', '如：Bitcoin')),
      h('td', null, h('button', { class: 'btn ghost sm icon', title: '删除', on: { click: () => { S.termsDraft.splice(i, 1); renderTerms(); } } }, icon('trash', 'sm')))))));
  page.replaceChildren(h('div', { class: 'form-wrap' },
    h('div', { class: 'callout', style: { marginBottom: '20px' } }, icon('info'), h('div', { class: 'grow' },
      h('b', null, '术语表让人名、产品名、黑话又准又统一'),
      h('p', null, '识别结果里出现「别名」会被替换成「规范写法」；填了「指定译文」的词，翻译时会严格按它来。'))),
    S.termsDraft.length ? table : h('div', { class: 'empty' }, h('div', { class: 'art' }, icon('book', 'lg')), h('h3', null, '还没有术语'),
      h('p', null, '把常被识别错的人名、公司名、行业词加进来。'), h('button', { class: 'btn primary', on: { click: addTerm } }, icon('plus'), '添加第一条'))),
  h('div', { class: `save-bar ${dirty ? 'show' : ''}`, id: 'terms-bar' },
    h('span', { class: 'grow dim' }, '有未保存的更改'),
    h('button', { class: 'btn ghost', on: { click: () => { S.termsDraft = null; renderTerms(); } } }, '放弃'),
    h('button', { class: 'btn primary', on: { click: saveTerms } }, '保存')));
}
function markTermsDirty() { $('#terms-bar')?.classList.toggle('show', JSON.stringify(S.termsDraft) !== S.termsSaved); }
function addTerm() {
  if (!S.termsDraft) return;
  if (S.termsDraft[0] && !S.termsDraft[0].term.trim()) { $('#terms-page tbody input')?.focus(); return; }   // 已有一条空行
  S.termsDraft.unshift({ term: '', aliases: '', translation: '' }); $('#terms-search').value = ''; renderTerms();
  setTimeout(() => $('#terms-page tbody input')?.focus(), 20);
}
async function saveTerms() {
  if (!S.termsDraft) return;
  const terms = S.termsDraft.filter(t => t.term.trim()).map(t => ({ term: t.term, aliases: t.aliases.split(/[,，]/).map(s => s.trim()).filter(Boolean), translation: t.translation }));
  const r = await tryApi('save_terms', { terms }, '术语表已保存');
  if (r) { S.termsDraft = null; S.termsCount = r.terms.length; renderTerms(); }
}
$('#terms-add').addEventListener('click', addTerm);
$('#terms-search').addEventListener('input', debounce(renderTerms, 150));

/* ============================== 设置 ============================== */
const SETTINGS = () => [
  { title: '翻译', desc: '语言对决定翻译方向：检测到前一种语言就译成后一种，其他语言一律译成前一种。', fields: [
    { k: 'translate_mode', label: '语言对', type: 'select', options: S.pairs.map(p => [p, pairLabel(p)]) },
    { k: 'translate_context', label: '参考上一句翻译', hint: '代词、省略句更准。小模型（<7B）可能串句，建议大模型开启。', type: 'switch' },
  ] },
  { title: '会议纪要', fields: [
    { k: 'summary_template', label: '默认模板', hint: '结束会议时用它生成纪要；记录库里可换模板重新生成。', type: 'select', options: S.templates.map(t => [t, t]) },
    { k: 'summary_language', label: '纪要语言', type: 'select', options: [['zh', '中文'], ['en', 'English'], ['auto', '跟随会议主语言']] },
  ] },
  { title: '模型后端', desc: '识别和翻译都调用本机的 OpenAI 兼容接口（如 oMLX），音频不会离开这台电脑。', fields: [
    { k: 'omlx_url', label: '后端地址', type: 'text', placeholder: 'http://127.0.0.1:16578' },
    { k: 'api_key', label: 'API Key', hint: 'oMLX 里设置的密钥；没有可留空。', type: 'password' },
    { k: 'asr_model', label: '识别模型', hint: 'SenseVoiceSmall 快、边说边出字；Whisper 更准但稍慢。', type: 'model' },
    { k: 'chat_model', label: '翻译 / 纪要模型', type: 'model' },
    { type: 'test' },
    { k: 'self_host_llm', label: '内置翻译模型兜底', hint: '后端没加载对话模型时，自动用 mlx_lm 在本机起一个（需安装 mlx-lm）。', type: 'switch' },
    { k: 'llm_model_repo', label: '兜底模型', type: 'text' },
  ] },
  { title: '音频', desc: '静音阈值：电平低于它不算说话。环境安静时点「校准」，会自动给出建议值。', fields: [
    { k: 'silence_threshold_mic', label: '麦克风静音阈值', type: 'threshold', src: 'mic' },
    { k: 'silence_threshold_sys', label: '系统声音静音阈值', type: 'threshold', src: 'media' },
    { k: 'system_fallback_device_keyword', label: '回退输入设备', hint: '没有屏幕录制权限时，用名字含这个关键词的输入设备抓系统声音（如 BlackHole）。', type: 'text' },
    { k: 'system_audio_planar', label: '系统声音声道修正', hint: '系统声音听起来不对（重叠/变调）时再打开。', type: 'switch' },
  ] },
  { title: '说话人区分', desc: '仅对系统声音生效：用声纹把不同的人标成「说话人1、2…」。', fields: [
    { k: 'diarize_enabled', label: '启用', type: 'switch' },
    { k: 'diarize_threshold', label: '同一人判定阈值', hint: '越高越容易分成不同的人。', type: 'range', min: 0.3, max: 0.9, step: 0.01 },
    { k: 'diarize_max_speakers', label: '最多人数', type: 'number', min: 2, max: 20 },
    { k: 'diarize_global_threshold', label: '跨会议匹配阈值', hint: '记录库给说话人「建议名字」时用，越高越保守。', type: 'range', min: 0.5, max: 0.95, step: 0.01 },
  ] },
  { title: '外观', desc: '这里的修改立即生效。', live: true, fields: [
    { k: 'ui_theme', label: '主题', type: 'seg', options: [['dark', '深色'], ['light', '浅色'], ['system', '跟随系统']] },
    { k: 'ui_on_top', label: '窗口置顶', type: 'switch' },
    { k: 'ui_font_scale', label: '阅读字号', type: 'range', min: 0.85, max: 1.5, step: 0.05, fmt: v => `${Math.round(v * 100)}%` },
    { k: 'subtitle_font_size', label: '字幕条字号', type: 'range', min: 16, max: 48, step: 1, fmt: v => `${v}px` },
    { k: 'subtitle_opacity', label: '字幕条不透明度', type: 'range', min: 0.4, max: 1, step: 0.02, fmt: v => `${Math.round(v * 100)}%` },
    { k: 'subtitle_show_source', label: '字幕条显示原文', type: 'switch' },
  ] },
  { title: '存储', fields: [{ k: 'output_dir', label: '记录保存位置', hint: '录音、转录、纪要都在这里的 records/ 下。', type: 'folder' }] },
];
let testState = null;
function renderSettings() {
  S.settingsDraft = S.settingsDraft || {};
  $('#cfg-path').textContent = S.paths.config || '';
  const val = k => (k in S.settingsDraft ? S.settingsDraft[k] : S.cfg[k]);
  const set = (k, v, live) => {
    if (live) { setPref(k, v); return; }
    if (String(v) === String(S.cfg[k])) delete S.settingsDraft[k]; else S.settingsDraft[k] = v;
    $('#settings-bar')?.classList.toggle('show', Object.keys(S.settingsDraft).length > 0);
  };
  const errs = S.settingsErrors || {};
  const ctl = (f, live) => {
    const v = val(f.k);
    switch (f.type) {
      case 'switch': return h('div', { class: 'ctl end' }, h('label', { class: 'switch' }, h('input', { type: 'checkbox', checked: !!v, 'aria-label': f.label, on: { change: e => set(f.k, e.target.checked, live) } }), h('span')));
      case 'select': return h('div', { class: 'ctl' }, h('select', { class: 'select', 'aria-label': f.label, on: { change: e => set(f.k, e.target.value, live) } }, ...f.options.map(([a, b]) => h('option', { value: a, selected: a === v }, b))));
      case 'seg': return h('div', { class: 'ctl end' }, h('div', { class: 'seg' }, ...f.options.map(([a, b]) => h('button', { 'aria-pressed': String(a === v), on: { click: e => { set(f.k, a, live); $$('button', e.target.parentNode).forEach(x => x.setAttribute('aria-pressed', String(x === e.target))); } } }, b))));
      case 'range': {
        const fmt = f.fmt || (x => (+x).toFixed(2));
        const out = h('output', null, fmt(v));
        const pct = x => `${(x - f.min) / (f.max - f.min) * 100}%`;
        return h('div', { class: 'ctl' }, h('div', { class: 'range-ctl' }, h('input', {
          type: 'range', min: f.min, max: f.max, step: f.step, value: v, style: { '--fill': pct(v) }, 'aria-label': f.label,
          on: { input: e => { out.textContent = fmt(+e.target.value); e.target.style.setProperty('--fill', pct(+e.target.value)); if (live) { S.cfg[f.k] = +e.target.value; applyPrefs(); } }, change: e => set(f.k, +e.target.value, live) },
        }), out));
      }
      case 'number': return h('div', { class: 'ctl' }, h('input', { class: `input ${errs[f.k] ? 'invalid' : ''}`, type: 'number', min: f.min, max: f.max, value: v, 'aria-label': f.label, style: { maxWidth: '120px' }, on: { input: e => set(f.k, e.target.value === '' ? '' : +e.target.value) } }));
      case 'threshold': {
        const inp = h('input', { class: `input ${errs[f.k] ? 'invalid' : ''}`, type: 'number', step: 0.001, min: 0.0005, max: 0.2, value: v, 'aria-label': f.label, style: { maxWidth: '120px' }, on: { input: e => set(f.k, e.target.value === '' ? '' : +e.target.value) } });
        const btn = h('button', { class: 'btn', on: { click: async () => {
          btn.disabled = true; btn.textContent = '请保持安静 3 秒…';
          const r = await tryApi('calibrate', { key: f.src });
          btn.disabled = false; btn.textContent = '校准';
          if (r && r.ok) { inp.value = r.threshold; set(f.k, r.threshold); toast(`环境噪声 ${r.noise}，建议阈值 ${r.threshold}（记得保存）`, 'ok', { timeout: 5000 }); }
          else if (r) toast(r.error, 'warn');
        } } }, '校准');
        return h('div', { class: 'ctl' }, inp, btn);
      }
      case 'model': {
        const listId = `dl-${f.k}`;
        return h('div', { class: 'ctl' }, h('input', { class: 'input', value: v, list: listId, 'aria-label': f.label, on: { input: e => set(f.k, e.target.value.trim()) } }),
          h('datalist', { id: listId }, ...(S.backend.models || []).map(m => h('option', { value: m }))));
      }
      case 'folder': {
        const inp = h('input', { class: 'input', value: v, 'aria-label': f.label, on: { input: e => set(f.k, e.target.value) } });
        return h('div', { class: 'ctl' }, inp,
          h('button', { class: 'btn', on: { click: async () => { const r = await tryApi('pick_folder', { start: inp.value }); if (r && r.path) { inp.value = r.path; set(f.k, r.path); } } } }, '选择…'),
          h('button', { class: 'btn ghost icon', title: '在访达中打开', on: { click: () => api('reveal', { path: inp.value }) } }, icon('folder')));
      }
      default: return h('div', { class: 'ctl' }, h('input', { class: `input ${errs[f.k] ? 'invalid' : ''}`, type: f.type === 'password' ? 'password' : 'text', value: v ?? '', placeholder: f.placeholder || '', 'aria-label': f.label, on: { input: e => set(f.k, e.target.value) } }));
    }
  };
  const testRow = () => {
    const res = h('div', { class: 'test-result' });
    if (testState) {
      const r = testState;
      res.append(r.ok ? h('div', { class: 'col', style: { gap: '4px' } },
        h('span', { style: { color: 'var(--accent)' } }, `✓ 连接成功，已加载 ${r.models.length} 个模型`),
        h('span', { style: { color: r.asr_ok ? 'var(--text-2)' : 'var(--warn)' } }, r.asr_ok ? '✓ 识别模型已加载' : '⚠ 识别模型不在已加载列表里'),
        h('span', { style: { color: r.chat_ok ? 'var(--text-2)' : 'var(--warn)' } }, r.chat_ok ? '✓ 翻译模型已加载' : '⚠ 翻译模型不在已加载列表里')) :
        h('span', { style: { color: 'var(--danger)' } }, `✕ ${r.error}`));
    }
    return h('div', { class: 'field' }, h('div', { class: 'lbl' }, h('b', null, '连接测试'), h('span', null, '用上面填写的值试连，不会保存。')),
      h('div', null, h('div', { class: 'ctl' }, h('button', { class: 'btn', on: { click: async e => {
        e.target.disabled = true;
        testState = await tryApi('test_connection', { url: val('omlx_url'), api_key: val('api_key'), asr_model: val('asr_model'), chat_model: val('chat_model') });
        if (testState && testState.models) S.backend.models = testState.models;
        renderSettings();
      } } }, '测试连接')), res));
  };
  const secs = SETTINGS().map(sec => h('section', { class: 'fsec' }, h('h3', null, sec.title), sec.desc ? h('p', null, sec.desc) : null,
    h('div', { class: 'fgroup' }, ...sec.fields.map(f => f.type === 'test' ? testRow() :
      h('div', { class: 'field' }, h('div', { class: 'lbl' }, h('b', null, f.label), f.hint ? h('span', null, f.hint) : null),
        h('div', null, ctl(f, sec.live), errs[f.k] ? h('div', { class: 'err' }, errs[f.k]) : null))))));
  const scroll = $('#settings-page').scrollTop;
  $('#settings-page').replaceChildren(h('div', { class: 'form-wrap' }, ...secs,
    h('div', { class: 'muted', style: { fontSize: '12px' } }, `LiveTranslator ${S.version || ''} · 数据目录 `, h('a', { href: '#', style: { color: 'var(--text-2)' }, on: { click: e => { e.preventDefault(); api('reveal', { path: S.paths.data }); } } }, S.paths.data))),
  h('div', { class: `save-bar ${Object.keys(S.settingsDraft).length ? 'show' : ''}`, id: 'settings-bar' },
    h('span', { class: 'grow dim' }, '有未保存的更改'),
    h('button', { class: 'btn ghost', on: { click: () => { S.settingsDraft = {}; S.settingsErrors = {}; renderSettings(); } } }, '放弃'),
    h('button', { class: 'btn primary', on: { click: saveSettings } }, '保存')));
  $('#settings-page').scrollTop = scroll;
}
async function saveSettings() {
  try {
    const r = await api('save_settings', { values: S.settingsDraft });
    if (!r.ok) { S.settingsErrors = r.errors; renderSettings(); toast('有字段需要修正', 'error'); return; }
    S.cfg = r.config; S.settingsDraft = {}; S.settingsErrors = {};
    applyPrefs(); renderSettings(); toast('设置已保存', 'ok');
    if (S.page !== 'live') renderTranscript();
  } catch (e) { toast(e.message, 'error'); }
}

/* ============================== 使用向导 / 环境检查 ============================== */
async function showOnboarding(first = false) {
  const list = h('div', { class: 'col', style: { gap: '8px' } }, h('div', { class: 'check pending' }, h('span', { class: 'ico' }, icon('refresh', 'sm')), h('div', { class: 'grow' }, h('b', null, '正在检查环境…'))));
  const refresh = async () => {
    const o = await tryApi('onboarding'); if (!o) return;
    S.backend = o.backend; renderBackend();
    const b = o.backend;
    const row = (ok, title, desc, fix, level) => h('div', { class: `check ${ok ? 'ok' : (level || 'bad')}` },
      h('span', { class: 'ico' }, icon(ok ? 'check' : (level === 'warn' ? 'alert' : 'x'), 'sm')),
      h('div', { class: 'grow' }, h('b', null, title), h('span', null, desc)), !ok && fix ? fix : null);
    const btn = (label, fn) => h('button', { class: 'btn sm', on: { click: fn } }, label);
    const toSettings = () => { closeAll(); go('settings'); };
    list.replaceChildren(
      row(b.ok, '模型后端在线', b.ok ? `${S.cfg.omlx_url}` : (b.error || '请打开 oMLX 并启动服务'), h('div', { class: 'row' }, btn('打开 oMLX', () => api('open_omlx')), btn('改地址', toSettings))),
      row(b.ok && b.asr_ok, `识别模型：${S.cfg.asr_model}`, b.asr_ok ? '已加载' : '在 oMLX 里加载它，或在设置里换成已加载的模型', btn('去设置', toSettings)),
      row(b.ok && b.chat_ok, `翻译模型：${b.chat_model || S.cfg.chat_model}`, b.chat_ok ? (b.self_hosted_chat ? '由内置 mlx_lm 提供' : '已加载') : '在 oMLX 里加载一个对话模型（如 gemma）', btn('去设置', toSettings)),
      row(o.mic, '麦克风', o.mic ? '可用' : '未检测到输入设备，或尚未授权麦克风', null),
      row(o.media, '系统声音（会议软件 / 视频）', o.media ? '可用' : (o.media_needs_permission ? '需要授权「屏幕录制」' : '不可用 · 可选，只用麦克风也能工作'),
        o.media_needs_permission ? btn('去授权', () => api('request_permission')) : null, 'warn'),
      row(o.speaker_model, '说话人模型', o.speaker_model ? '已就绪' : '首次运行会自动下载（约 28MB）· 可选', null, 'warn'),
    );
  };
  const body = h('div', null,
    h('div', { class: 'sec-label', style: { marginTop: 0, marginBottom: '8px' } }, '环境检查'), list,
    h('div', { class: 'sec-label', style: { marginTop: '20px', marginBottom: '8px' } }, '三步上手'),
    h('ol', { class: 'steps' },
      h('li', null, '选好翻译方向，点「开始录制」。说话或播放会议声音，原文和译文实时出现。'),
      h('li', null, '重要的话点 ★ 标记（或按 ', h('span', { class: 'kbd' }, `${MOD}B`), '），纪要会优先体现。想看大字幕就切到「字幕条」。'),
      h('li', null, '点「结束并生成纪要」。在记录库里回放录音、纠错、给说话人命名、导出 Word / 字幕。')),
    h('div', { class: 'sec-label', style: { marginTop: '20px', marginBottom: '8px' } }, '快捷键'),
    h('div', { class: 'shortcuts' },
      ...[[`${MOD}R`, '开始 / 结束'], [`${MOD}B`, '标记最近一句'], [`${MOD}⇧S`, '字幕条'], [`${MOD}1~4`, '切换页面'], [`${MOD}F`, '搜索记录'], ['空格', '记录库播放/暂停']]
        .map(([k, d]) => h('div', null, h('span', null, d), h('span', { class: 'kbd' }, k)))));
  const closeAll = modal({
    title: first ? '欢迎使用 LiveTranslator' : '环境检查与帮助',
    desc: first ? '本地离线的双语会议助手：实时识别 + 翻译 + 自动纪要，音频不出本机。先确认下面几项就绪。' : null,
    body, wide: true,
    actions: [{ label: '重新检查', kind: 'ghost', icon: 'refresh', onClick: () => { refresh(); return false; } }, '-',
      { label: first ? '开始使用' : '完成', kind: 'primary', onClick: () => { if (first) api('set_pref', { key: 'onboarded', value: true }); } }],
    onClose: () => { if (first) api('set_pref', { key: 'onboarded', value: true }); },
  });
  refresh();
}

/* ============================== 快捷键 ============================== */
document.addEventListener('keydown', e => {
  const mod = e.metaKey || e.ctrlKey;
  const typing = !!e.target.closest?.('input,textarea,select,button,[role=option],[role=tab],[contenteditable]');
  const inEditor = !!e.target.closest?.('.entry-edit, .modal');
  if (mod && inEditor && ['b', 'f', '1', '2', '3', '4', 'r'].includes(e.key.toLowerCase())) { if (e.key.toLowerCase() === 'r') e.preventDefault(); return; }
  if (e.key === 'Escape') { if (openMenuEl) { closeMenu(); return; } if (modalStack.length) { modalStack[modalStack.length - 1](); return; } }
  if (mod && e.key.toLowerCase() === 'r') { e.preventDefault(); toggleRecording(); return; }
  if (mod && e.shiftKey && e.key.toLowerCase() === 's') { e.preventDefault(); openSubtitle(); return; }
  if (mod && e.key.toLowerCase() === 'b') {
    e.preventDefault(); const id = S.order[S.order.length - 1];
    if (id) { const en = S.entries.get(id); if (!en.starred) toggleStar(en, {}).then(ok => ok && toast('已标记最近一句', 'ok')); else toast('最近一句已经标记过了'); }
    return;
  }
  if (mod && ['1', '2', '3', '4'].includes(e.key)) { e.preventDefault(); go(['live', 'library', 'terms', 'settings'][+e.key - 1]); return; }
  if (mod && e.key.toLowerCase() === 'f') { e.preventDefault(); go('library'); setTimeout(() => $('#lib-search').focus(), 30); return; }
  if (mod && e.key.toLowerCase() === 's' && S.page === 'settings') { e.preventDefault(); if (Object.keys(S.settingsDraft || {}).length) saveSettings(); return; }
  if (mod && e.key.toLowerCase() === 's' && S.page === 'terms') { e.preventDefault(); saveTerms(); return; }
  if (e.key === ' ' && !typing && S.page === 'library' && player) { e.preventDefault(); player.audio.paused ? player.audio.play().catch(() => {}) : player.audio.pause(); }
});

/* ============================== 事件流 ============================== */
function handle(ev) {
  const d = ev.data;
  switch (ev.type) {
    case 'entry': onEntry(d); break;
    case 'entry_update': onEntryUpdate(d); break;
    case 'translation': onTranslation(d); break;
    case 'interim': S.interim = d; renderInterim(); break;
    case 'interim_clear': if (S.interim && S.interim.id === d.id) { S.interim = null; renderInterim(); } break;
    case 'session':
      if (d.active && (!S.session.active || S.session.id !== d.id)) { S.session = d; clearLive(); }
      else S.session = d.active ? d : { active: false };
      if (!d.active) { S.interim = null; renderInterim(); }
      renderSession(); if (!S.order.length) renderTranscript(); break;
    case 'sources': S.sources = d; renderConsole(); if (!S.session.active && !S.order.length) renderTranscript(); break;
    case 'backend': S.backend = d; renderBackend(); break;
    case 'job': onJob(d); if (S.page === 'library' && S.lib.rec && d.record_id === S.lib.rec.meta.id) refreshDetailSoon(); break;
    case 'records_changed': reloadLibrary(); if (S.lib.rec && d.id === S.lib.rec.meta.id && !d.deleted) refreshDetailSoon(); break;
    case 'toast': toast(d.text, d.level); break;
    case 'config': S.cfg = d; applyPrefs(); break;
    case 'quitting': document.body.append(h('div', { class: 'quitting' }, h('div', { class: 'empty' }, h('div', { class: 'art' }, icon('sparkle', 'lg')), h('h3', null, '正在收尾'), h('p', null, d.text), h('div', { class: 'progress indet' }, h('i'))))); break;
  }
}
const refreshDetailSoon = debounce(() => {
  if (S.page !== 'library' || !S.lib.sel) return;
  if (document.activeElement && $('#lib-detail').contains(document.activeElement) && /INPUT|TEXTAREA/.test(document.activeElement.tagName)) return;
  if (player && !player.audio.paused) return;
  selectRecord(S.lib.sel, true);
}, 400);

let es = null, lastElapsed = null;
function connect() {
  es = new EventSource(`/events?token=${encodeURIComponent(T)}&client=main`);
  es.onmessage = m => {
    let p; try { p = JSON.parse(m.data); } catch { return; }
    for (const ev of p.events) handle(ev);
    S.levels = p.levels || S.levels;
    if (!lvlRaf) lvlRaf = requestAnimationFrame(paintLevels);
    if (p.elapsed != null) { const t = fmtClock(p.elapsed); if (t !== lastElapsed) { lastElapsed = t; $('#timer').textContent = t; } }
    else if (lastElapsed !== null && !S.session.active) { lastElapsed = null; }
    const pend = $('#stat-pending');
    pend.classList.toggle('hidden', !p.pending || p.pending < 2);
    pend.textContent = p.pending ? `处理中 ${p.pending}` : '';
  };
  let opened = false;
  es.onopen = async () => {
    if (opened) await resync();          // 断线期间的事件丢了：重新拉一次快照
    opened = true; renderBackend();
  };
  es.onerror = () => {                   // EventSource 会自动重连，这里只提示
    $('#backend-dot').className = 'dot bad'; $('#backend-label').textContent = '重连中';
  };
}

async function resync() {
  const b = await api('bootstrap').catch(() => null); if (!b) return;
  S.session = b.session; S.sources = b.sources; S.backend = b.backend; S.cfg = b.config;
  S.entries.clear(); S.order = [];
  for (const e of b.entries) { S.entries.set(e.id, e); S.order.push(e.id); }
  for (const j of b.jobs) S.jobs.set(j.id, j);
  applyPrefs(); renderConsole(); renderSession(); renderTranscript(); renderInterim(); renderJobs();
  if (S.page === 'library') loadLibrary();
}

/* ============================== 启动 ============================== */
async function boot() {
  let b;
  for (let i = 0; i < 20; i++) { try { b = await api('bootstrap'); break; } catch { await new Promise(r => setTimeout(r, 300)); } }
  if (!b) { document.body.innerHTML = '<div class="empty"><h3>无法连接本地服务</h3><p>请重启 App。</p></div>'; return; }
  Object.assign(S, { cfg: b.config, langs: b.langs, pairs: b.lang_pairs, templates: b.templates, native: b.native, paths: b.paths, version: b.version,
    session: b.session, sources: b.sources, backend: b.backend, termsCount: b.terms_count });
  for (const j of b.jobs) S.jobs.set(j.id, j);
  for (const e of b.entries) { S.entries.set(e.id, e); S.order.push(e.id); }
  applyPrefs(); renderConsole(); renderSession(); renderTranscript(); renderInterim(); renderBackend(); renderJobs();
  if (!S.native) $('#btn-pin').classList.add('hidden');
  connect();
  if (!S.cfg.onboarded) setTimeout(() => showOnboarding(true), 400);
}
boot();
