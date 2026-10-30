'use strict';
// FE 行为验证桩：在 Node 中加载 N-KB 管理页 JS，用最小 DOM/fetch 桩驱动真实代码，
// 由 pytest 断言 JSON 输出。用法：node harness.js <static_root> <scenario>
const fs = require('fs');
const path = require('path');

const STATIC_ROOT = process.argv[2];
const SCENARIO = process.argv[3];

// ---------------------------------------------------------------------------
// 极简 DOM 桩（无任何 HTML 解析器：动态 HTML 注入在此模型中不可能，只能走 createElement）
// ---------------------------------------------------------------------------
const createdTags = [];

class FakeTextNode {
  constructor(text) {
    this.nodeType = 3;
    this._text = String(text);
    this.parentNode = null;
  }
}

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.nodeType = 1;
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this._listeners = {};
    this._classes = new Set();
    this._text = '';
    this.attributes = {};
    this.value = '';
    this.disabled = false;
    this.href = '';
    this.target = '';
    this.rel = '';
  }
  get className() {
    return [...this._classes].join(' ');
  }
  set className(v) {
    this._classes = new Set(String(v).split(/\s+/).filter(Boolean));
  }
  get classList() {
    const self = this;
    return {
      add: (...names) => names.forEach((n) => self._classes.add(n)),
      remove: (...names) => names.forEach((n) => self._classes.delete(n)),
      toggle: (n, force) => {
        const target = force === undefined ? !self._classes.has(n) : Boolean(force);
        if (target) self._classes.add(n);
        else self._classes.delete(n);
        return target;
      },
      contains: (n) => self._classes.has(n),
    };
  }
  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  append(...items) {
    items.forEach((item) => this.appendChild(item));
  }
  replaceChildren() {
    this.children = [];
  }
  addEventListener(type, fn) {
    (this._listeners[type] = this._listeners[type] || []).push(fn);
  }
  dispatch(type, event) {
    (this._listeners[type] || []).slice().forEach((fn) => fn(event || { preventDefault() {}, target: this }));
  }
  setAttribute(k, v) {
    this.attributes[k] = String(v);
  }
  getAttribute(k) {
    return k in this.attributes ? this.attributes[k] : null;
  }
  removeAttribute(k) {
    delete this.attributes[k];
  }
  contains(node) {
    if (node === this) return true;
    return this.children.some((child) => child.nodeType === 1 && child.contains(node));
  }
  * walk() {
    for (const child of this.children) {
      if (child.nodeType === 1) {
        yield child;
        yield* child.walk();
      }
    }
  }
  querySelector(sel) {
    return this.querySelectorAll(sel)[0] || null;
  }
  querySelectorAll(sel) {
    return [...this.walk()].filter((el) => matchesSelector(el, sel));
  }
  get textContent() {
    if (this.children.length) {
      return this.children.map((c) => (c.nodeType === 3 ? c._text : c.textContent)).join('');
    }
    return this._text;
  }
  set textContent(v) {
    this.children = [];
    this._text = String(v);
  }
}

function matchesSelector(el, sel) {
  return String(sel)
    .trim()
    .split(/\s+/)
    .every((token) => {
      if (token === '[data-tab]') return el.dataset.tab !== undefined;
      if (token.startsWith('[data-tab=')) {
        const value = token.slice(token.indexOf('"') + 1, token.lastIndexOf('"'));
        return el.dataset.tab === value;
      }
      if (token.startsWith('[data-menu-parent=')) {
        const value = token.slice(token.indexOf('"') + 1, token.lastIndexOf('"'));
        return el.dataset.menuParent === value;
      }
      if (token.startsWith('[data-tab-group=')) {
        const value = token.slice(token.indexOf('"') + 1, token.lastIndexOf('"'));
        return el.dataset.tabGroup === value;
      }
      if (token.startsWith('[data-submenu-of=')) {
        const value = token.slice(token.indexOf('"') + 1, token.lastIndexOf('"'));
        return el.dataset.submenuOf === value;
      }
      if (token.startsWith('.')) {
        return token.slice(1).split('.').every((c) => el._classes.has(c));
      }
      return false;
    });
}

const registry = new Map();
function registerElement(el) {
  if (el.id) registry.set(el.id, el);
  return el;
}

function docQueryAll(sel) {
  // 所有元素均挂在 document.body 子树下，直接遍历即可覆盖全部候选
  const self = document.body;
  return [...self.walk()].filter((el) => matchesSelector(el, sel));
}

const document = {
  createElement: (tag) => {
    createdTags.push(tag.toUpperCase());
    return new FakeElement(tag);
  },
  createTextNode: (text) => new FakeTextNode(text),
  getElementById: (id) => registry.get(id) || null,
  querySelector: (sel) => docQueryAll(sel)[0] || null,
  querySelectorAll: (sel) => docQueryAll(sel),
  _listeners: {},
  addEventListener(type, fn) {
    (this._listeners[type] = this._listeners[type] || []).push(fn);
  },
  dispatch(type, event) {
    (this._listeners[type] || []).slice().forEach((fn) => fn(event || { preventDefault() {} }));
  },
  body: new FakeElement('body'),
};

const storageMap = new Map();
let storageThrows = false;
const localStorage = {
  getItem: (k) => {
    if (storageThrows) throw new Error('storage denied');
    return storageMap.has(k) ? storageMap.get(k) : null;
  },
  setItem: (k, v) => {
    if (storageThrows) throw new Error('storage denied');
    storageMap.set(k, String(v));
  },
};
const historyEntries = [];
let historyIndex = -1;
let replacingHistory = false;
const history = {
  replaceState(_state, _title, url) {
    replacingHistory = true;
    window.location.hash = url;
    replacingHistory = false;
    historyEntries[historyIndex] = url;
  },
  pushState(_state, _title, url) {
    window.location.hash = url;
  },
  go(delta) {
    const target = historyIndex + delta;
    if (target < 0 || target >= historyEntries.length) return;
    historyIndex = target;
    replacingHistory = true;
    window.location.hash = historyEntries[historyIndex];
    replacingHistory = false;
  },
};
const hashListeners = [];
const location = {
  _hash: '',
  get hash() { return this._hash; },
  set hash(value) {
    const next = String(value || '');
    if (next === this._hash) return;
    this._hash = next;
    if (!replacingHistory) {
    if (historyIndex < historyEntries.length - 1) historyEntries.splice(historyIndex + 1);
    historyEntries.push(next);
    historyIndex = historyEntries.length - 1;
    }
    hashListeners.slice().forEach((fn) => fn());
  },
};
const window = {
  location,
  addEventListener: (type, fn) => {
    if (type === 'hashchange') hashListeners.push(fn);
  },
  confirm: () => true,
  localStorage,
};

const fetchCalls = [];
let fetchResponder = (url) => Promise.resolve(jsonResponse(defaultBody(url)));
function fetch(url, options) {
  fetchCalls.push({ url: String(url), options: options || null });
  return fetchResponder(url, options);
}

// ---------------------------------------------------------------------------
// 构建与 index.html 对应的最小元素树
// ---------------------------------------------------------------------------
function buildDom() {
  const tabs = ['overview', 'documents', 'retrieval', 'health', 'observations-otel'];
  const labels = { overview: '概览', documents: '文档', retrieval: '检索', health: '健康', 'observations-otel': 'OTel' };
  const sidebar = new FakeElement('aside');
  sidebar.id = 'app-sidebar';
  const group = new FakeElement('div');
  group.className = 'sidebar__group';
  group.dataset.tabGroup = 'observations';
  const parent = new FakeElement('button');
  parent.className = 'sidebar__item sidebar__item--parent';
  parent.dataset.menuParent = 'observations';
  const submenu = new FakeElement('div');
  submenu.className = 'sidebar__submenu';
  submenu.id = 'submenu-observations';
  submenu.dataset.submenuOf = 'observations';
  group.append(parent, submenu);
  tabs.forEach((tab, index) => {
    const anchor = new FakeElement('a');
    anchor.className = 'sidebar__item' + (index === 0 ? ' sidebar__item--active' : '');
    anchor.dataset.tab = tab;
    const label = new FakeElement('span');
    label.className = 'sidebar__item-label';
    label.textContent = labels[tab];
    anchor.appendChild(label);
    if (tab === 'observations-otel') submenu.appendChild(anchor);
    else sidebar.appendChild(anchor);
  });
  sidebar.appendChild(group);
  const toggle = new FakeElement('button');
  toggle.id = 'sidebar-toggle';
  registerElement(toggle);
  sidebar.appendChild(toggle);
  document.body.appendChild(sidebar);

  const main = new FakeElement('main');
  tabs.forEach((tab, index) => {
    const div = new FakeElement('div');
    div.id = `tab-${tab}`;
    div.className = 'tab-content' + (index === 0 ? ' active' : '');
    main.appendChild(div);
    registerElement(div);
  });
  document.body.appendChild(main);

  [
    'topbar-title',
    'last-update',
    'overview-stats',
    'overview-documents',
    'overview-health',
    'health-stats',
    'dependency-health',
    'document-list',
    'filter-tags',
    'filter-status',
    'refresh-health',
    'refresh-documents',
    'upload-form',
    'upload-file',
    'upload-source',
    'upload-tags',
    'retrieval-form',
    'retrieval-query',
    'retrieval-top-k',
    'retrieval-min-score',
    'retrieval-tags',
    'retrieval-source-kind',
    'retrieval-document-status',
  ].forEach((id) => {
    const el = new FakeElement('div');
    el.id = id;
    main.appendChild(el);
    registerElement(el);
  });
}

// ---------------------------------------------------------------------------
// 加载被测 JS（真实前端代码）
// ---------------------------------------------------------------------------
function loadFrontendScripts() {
  for (const name of ['management-api.js', 'management-ui.js', 'management-navigation.js', 'observations-otel.js', 'app.js']) {
    const source = fs.readFileSync(path.join(STATIC_ROOT, name), 'utf8');
    // 直接 eval：document/window/history/localStorage/fetch 均为闭包内桩实现
    eval(source);
  }
}

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------
function jsonResponse(body) {
  return { ok: true, status: 200, json: () => Promise.resolve(body) };
}

function httpErrorResponse(status, code) {
  // 非 200 响应桩：fetchJsonFromFetch 会抛 data.error.code
  return { ok: false, status, json: () => Promise.resolve({ error: { code } }) };
}

function defaultBody(url) {
  if (url.startsWith('/health')) return { status: 'ok' };
  return [];
}

async function flush(times) {
  for (let i = 0; i < (times || 6); i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

function signal(delivery, err) {
  const failed = delivery === 'failed';
  return {
    status: failed ? 'degraded' : delivery === 'disabled' ? 'disabled' : 'healthy',
    delivery,
    attempt_count: failed || delivery === 'succeeded' ? 1 : 0,
    success_count: delivery === 'succeeded' ? 1 : 0,
    failure_count: failed ? 1 : 0,
    last_success: delivery === 'succeeded' ? '2026-10-30T00:00:00Z' : null,
    last_failure: failed ? '2026-10-30T00:01:00Z' : null,
    current_error: err || null,
  };
}

function spanOf(index) {
  return {
    name: `span-${index}`,
    trace_id: `trace-${index}`,
    span_id: `sid-${index}`,
    parent_span_id: index > 0 ? `sid-${index - 1}` : null,
    status: 'UNSET',
    outcome: 'ok',
    error_type: null,
    duration_ms: index,
  };
}

function healthySnapshot(spans, overrides) {
  return {
    status: 'healthy',
    mode: 'export',
    instance_id: 'inst-abc',
    sample_ratio: 1,
    semconv_version: '1.29.0',
    exporter_endpoint: 'http://collector:4317',
    signals: {
      traces: signal('succeeded'),
      metrics: signal('succeeded'),
      logs: signal('failed', 'boom'),
      profile: signal('disabled'),
    },
    spans,
    pagination: { page: 1, size: 10, total: spans.length, pages: Math.ceil(spans.length / 10) },
    grafana: { base_url: null, traces_base_url: null },
    dropped_after_deadline: 0,
    diagnostic_error: null,
    differing_fields: [],
    notes: { sampling: '仅展示本进程自检快照' },
    ...(overrides || {}),
  };
}

function observationsUrls() {
  return fetchCalls.map((c) => c.url).filter((url) => url.startsWith('/observations/otel'));
}

function findAnchor(tab) {
  const anchor = docQueryAll(`[data-tab="${tab}"]`)[0];
  if (!anchor) throw new Error(`anchor not found: ${tab}`);
  return anchor;
}

function findButton(root, text) {
  for (const el of root.walk()) {
    if (el.tagName === 'BUTTON' && el.textContent === text) return el;
  }
  return null;
}

function dumpTree(el) {
  return [...el.walk()].map((node) => ({
    tag: node.tagName,
    classes: node.className,
    text: node._text,
    disabled: node.disabled === true ? true : undefined,
  }));
}

function dumpAnchors(el) {
  return [...el.walk()]
    .filter((node) => node.tagName === 'A' && node.href)
    .map((node) => ({ href: node.href, target: node.target, rel: node.rel, text: node.textContent }));
}

function dump() {
  const body = registry.get('tab-observations-otel');
  return {
    fetchUrls: observationsUrls(),
    createdTags: [...new Set(createdTags)],
    statsText: '',
    bodyText: body.textContent,
    elements: dumpTree(body),
    anchors: dumpAnchors(body),
  };
}

function openObservationsTab() {
  findAnchor('observations-otel').dispatch('click');
}

function activeTab() {
  const active = docQueryAll('.tab-content.active')[0];
  return active ? active.id.replace('tab-', '') : '';
}

async function scenarioNavigation() {
  await flush();
  const alias = { current: activeTab(), requests: observationsUrls().length };
  findAnchor('observations-otel').dispatch('click');
  await flush();
  const repeatRequests = observationsUrls().length;
  docQueryAll('[data-menu-parent="observations"]')[0].dispatch('click');
  const parentCurrent = activeTab();
  const parentButton = docQueryAll('[data-menu-parent="observations"]')[0];
  const expandedBeforeEscape = parentButton.getAttribute('aria-expanded');
  document.dispatch('keydown', { key: 'Escape' });
  const expandedAfterEscape = parentButton.getAttribute('aria-expanded');
  registry.get('sidebar-toggle').dispatch('click');
  const group = docQueryAll('[data-tab-group="observations"]')[0];
  group.dispatch('mouseenter');
  const collapsedHoverExpanded = parentButton.getAttribute('aria-expanded');
  document.dispatch('keydown', { key: 'Escape' });
  const collapsedAfterEscape = parentButton.getAttribute('aria-expanded');
  group.dispatch('focusin');
  const collapsedFocusExpanded = parentButton.getAttribute('aria-expanded');
  group.dispatch('focusout', { relatedTarget: null });
  const collapsedAfterFocusOut = parentButton.getAttribute('aria-expanded');
  registry.get('sidebar-toggle').dispatch('click');
  findAnchor('documents').dispatch('click');
  await flush();
  history.go(-1);
  await flush();
  const previous = { current: activeTab() };
  const back = { current: previous.current };
  history.go(1);
  await flush();
  const forward = { current: activeTab() };
  storageThrows = true;
  let storageFailure = false;
  try {
    findAnchor('documents').dispatch('click');
    docQueryAll('[data-menu-parent="observations"]')[0].dispatch('click');
    storageFailure = true;
  } catch (_) { /* storage must not interrupt navigation */ }
  return { alias, repeatRequests, parentCurrent, expandedBeforeEscape, expandedAfterEscape, collapsedHoverExpanded, collapsedAfterEscape, collapsedFocusExpanded, collapsedAfterFocusOut, back, forward, storageFailure, historyEntries, historyIndex };
}

// ---------------------------------------------------------------------------
// 场景
// ---------------------------------------------------------------------------
async function scenarioMalicious() {
  const evilSpan = {
    name: '<script>alert("xss")</script>',
    trace_id: 'trace<&1>',
    span_id: 'span"2',
    parent_span_id: '<img src=x>',
    status: 'ERROR',
    outcome: 'failed',
    error_type: 'Boom<Error>',
    duration_ms: 3,
  };
  const snap = healthySnapshot([evilSpan], { status: 'degraded', exporter_endpoint: '<b>export deadline exceeded</b>' });
  snap.signals.traces.current_error = '<img src=x onerror=alert(2)>';
  fetchResponder = (url) =>
    url.startsWith('/observations/otel') ? Promise.resolve(jsonResponse(snap)) : Promise.resolve(jsonResponse(defaultBody(url)));
  openObservationsTab();
  await flush();
  return dump();
}

async function scenarioNetworkFailure() {
  let failing = true;
  const okSnap = healthySnapshot([spanOf(1)]);
  fetchResponder = (url) => {
    if (url.startsWith('/observations/otel')) {
      if (failing) return Promise.reject(new Error('network down'));
      return Promise.resolve(jsonResponse(okSnap));
    }
    return Promise.resolve(jsonResponse(defaultBody(url)));
  };
  openObservationsTab();
  await flush();
  const before = dump();
  const retry = findButton(registry.get('tab-observations-otel'), '重试');
  if (!retry) throw new Error('retry button missing after network failure');
  failing = false;
  retry.dispatch('click');
  await flush();
  const after = dump();
  return { before, after };
}

async function scenarioHttpError() {
  // 非 200（422/500 等）与网络异常走同一失败态：展示错误码并可 Retry 恢复
  let failing = true;
  const okSnap = healthySnapshot([spanOf(1)]);
  fetchResponder = (url) => {
    if (url.startsWith('/observations/otel')) {
      if (failing) return Promise.resolve(httpErrorResponse(500, 'exporter_unavailable'));
      return Promise.resolve(jsonResponse(okSnap));
    }
    return Promise.resolve(jsonResponse(defaultBody(url)));
  };
  openObservationsTab();
  await flush();
  const before = dump();
  const retry = findButton(registry.get('tab-observations-otel'), '重试');
  if (!retry) throw new Error('retry button missing after http error');
  failing = false;
  retry.dispatch('click');
  await flush();
  const after = dump();
  return { before, after };
}

async function scenarioStaleResponse() {
  const resolvers = [];
  fetchResponder = () => new Promise((resolve) => resolvers.push(resolve));
  openObservationsTab();
  await flush(3);
  window.NKB.observationsOtel.refresh();
  await flush(3);
  if (resolvers.length < 2) throw new Error(`expected 2 pending requests, got ${resolvers.length}`);
  const snapNew = healthySnapshot([{ ...spanOf(1), name: 'new-span' }]);
  const snapOld = healthySnapshot([{ ...spanOf(1), name: 'old-span' }]);
  resolvers[1](jsonResponse(snapNew));
  await flush(3);
  const afterNew = dump();
  resolvers[0](jsonResponse(snapOld));
  await flush(3);
  const afterOld = dump();
  return { afterNew, afterOld };
}

async function scenarioPagination() {
  const page1 = healthySnapshot(
    Array.from({ length: 10 }, (_, i) => spanOf(i)),
    { pagination: { page: 1, size: 10, total: 12, pages: 2 } },
  );
  const page2 = healthySnapshot([spanOf(10), spanOf(11)], { pagination: { page: 2, size: 10, total: 12, pages: 2 } });
  let obsCalls = 0;
  fetchResponder = (url) => {
    if (url.startsWith('/observations/otel')) {
      obsCalls += 1;
      return Promise.resolve(jsonResponse(obsCalls === 1 ? page1 : page2));
    }
    return Promise.resolve(jsonResponse(defaultBody(url)));
  };
  openObservationsTab();
  await flush();
  const firstPage = dump();
  findButton(registry.get('tab-observations-otel'), '>').dispatch('click');
  await flush();
  const secondPage = dump();
  findAnchor('documents').dispatch('click');
  await flush(3);
  findAnchor('observations-otel').dispatch('click');
  await flush();
  const backToFirst = dump();
  return { firstPage, secondPage, backToFirst };
}

async function scenarioGrafana() {
  const snap = healthySnapshot([], {
    links: {
      Metric: 'https://grafana.example.com/?org=2#main',
      Trace: 'https://grafana.example.com/explore?orgId=1',
    },
  });
  fetchResponder = (url) =>
    url.startsWith('/observations/otel') ? Promise.resolve(jsonResponse(snap)) : Promise.resolve(jsonResponse(defaultBody(url)));
  openObservationsTab();
  await flush();
  return dump();
}

async function scenarioGrafanaUnsafe() {
  const snap = healthySnapshot([], { links: { Metric: 'javascript:alert(1)', Trace: 'https://user:pass@grafana.example.com/explore' } });
  fetchResponder = (url) =>
    url.startsWith('/observations/otel') ? Promise.resolve(jsonResponse(snap)) : Promise.resolve(jsonResponse(defaultBody(url)));
  openObservationsTab();
  await flush();
  return dump();
}

async function scenarioEmptyDisabled() {
  const disabled = signal('disabled');
  const snap = {
    status: 'disabled',
    mode: 'disabled',
    instance_id: null,
    sample_ratio: null,
    semconv_version: null,
    signals: { traces: disabled, metrics: disabled, logs: disabled, profile: disabled },
    spans: [],
    pagination: { page: 1, size: 10, total: 0, pages: 0 },
    grafana: { base_url: null, traces_base_url: null },
    dropped_after_deadline: 0,
    diagnostic_error: null,
    differing_fields: [],
  };
  fetchResponder = (url) =>
    url.startsWith('/observations/otel') ? Promise.resolve(jsonResponse(snap)) : Promise.resolve(jsonResponse(defaultBody(url)));
  openObservationsTab();
  await flush();
  return dump();
}

async function scenarioInstruments() {
  const snap = healthySnapshot([spanOf(0)], {
    sample_ratio: 0,
    dropped_after_deadline: 2,
    instruments: [{ name: 'gen_ai.client.operation.duration', kind: 'histogram', unit: 's' }],
  });
  fetchResponder = (url) =>
    url.startsWith('/observations/otel') ? Promise.resolve(jsonResponse(snap)) : Promise.resolve(jsonResponse(defaultBody(url)));
  openObservationsTab();
  await flush();
  return dump();
}

async function scenarioInvalidSnapshot() {
  fetchResponder = (url) =>
    url.startsWith('/observations/otel') ? Promise.resolve(jsonResponse('secret-response')) : Promise.resolve(jsonResponse(defaultBody(url)));
  openObservationsTab();
  await flush();
  return dump();
}

async function scenarioLeaveAndReturn() {
  const pending = [];
  let reentered = false;
  fetchResponder = (url) => {
    if (!url.startsWith('/observations/otel')) return Promise.resolve(jsonResponse(defaultBody(url)));
    if (reentered) return Promise.resolve(jsonResponse(healthySnapshot([{ ...spanOf(2), name: 'returned-span' }])));
    return new Promise((resolve) => pending.push(resolve));
  };
  openObservationsTab();
  await flush(3);
  findAnchor('documents').dispatch('click');
  await flush(3);
  pending[0](jsonResponse(healthySnapshot([{ ...spanOf(1), name: 'left-span' }])));
  await flush(3);
  const afterLeave = dump();
  reentered = true;
  findAnchor('observations-otel').dispatch('click');
  await flush();
  return { afterLeave, afterReturn: dump() };
}

async function scenarioStaleFailure() {
  const resolvers = [];
  const rejecters = [];
  fetchResponder = (url) => {
    if (!url.startsWith('/observations/otel')) return Promise.resolve(jsonResponse(defaultBody(url)));
    return new Promise((resolve, reject) => { resolvers.push(resolve); rejecters.push(reject); });
  };
  openObservationsTab();
  await flush(3);
  window.NKB.observationsOtel.refresh();
  await flush(3);
  resolvers[1](jsonResponse(healthySnapshot([{ ...spanOf(2), name: 'latest-span' }])));
  await flush(3);
  rejecters[0](new Error('stale-secret-error'));
  await flush(3);
  return dump();
}

async function scenarioRefreshCurrentPage() {
  const page1 = healthySnapshot(Array.from({ length: 10 }, (_, i) => spanOf(i)), {
    pagination: { page: 1, size: 10, total: 12, pages: 2 },
  });
  const page2 = healthySnapshot([{ ...spanOf(10), name: 'page-two' }], {
    pagination: { page: 2, size: 10, total: 12, pages: 2 },
  });
  const refreshed = healthySnapshot([{ ...spanOf(11), name: 'refreshed-page-two' }], {
    pagination: { page: 2, size: 10, total: 12, pages: 2 },
  });
  fetchResponder = (url) => {
    if (!url.startsWith('/observations/otel')) return Promise.resolve(jsonResponse(defaultBody(url)));
    const calls = observationsUrls().length;
    return Promise.resolve(jsonResponse(calls === 1 ? page1 : calls === 2 ? page2 : refreshed));
  };
  openObservationsTab();
  await flush();
  findButton(registry.get('tab-observations-otel'), '>').dispatch('click');
  await flush();
  window.NKB.observationsOtel.refresh();
  await flush();
  return dump();
}

async function scenarioPageShrink() {
  const initial = healthySnapshot(Array.from({ length: 10 }, (_, i) => spanOf(i)), {
    pagination: { page: 1, size: 10, total: 100, pages: 10 },
  });
  const shrunk = healthySnapshot([], { pagination: { page: 10, size: 10, total: 12, pages: 2 } });
  const corrected = healthySnapshot([{ ...spanOf(11), name: 'corrected-page-two' }], {
    pagination: { page: 2, size: 10, total: 12, pages: 2 },
  });
  fetchResponder = (url) => {
    if (!url.startsWith('/observations/otel')) return Promise.resolve(jsonResponse(defaultBody(url)));
    const calls = observationsUrls().length;
    return Promise.resolve(jsonResponse(calls === 1 ? initial : calls === 2 ? shrunk : corrected));
  };
  openObservationsTab();
  await flush();
  findButton(registry.get('tab-observations-otel'), '10').dispatch('click');
  await flush();
  return dump();
}

async function scenarioPageTwoRetry() {
  const page1 = healthySnapshot(Array.from({ length: 10 }, (_, i) => spanOf(i)), {
    pagination: { page: 1, size: 10, total: 12, pages: 2 },
  });
  const page2 = healthySnapshot([spanOf(10)], { pagination: { page: 2, size: 10, total: 12, pages: 2 } });
  let calls = 0;
  fetchResponder = (url) => {
    if (!url.startsWith('/observations/otel')) return Promise.resolve(jsonResponse(defaultBody(url)));
    calls += 1;
    if (calls === 3) return Promise.reject(new Error('page-two-secret'));
    return Promise.resolve(jsonResponse(calls === 1 ? page1 : page2));
  };
  openObservationsTab();
  await flush();
  findButton(registry.get('tab-observations-otel'), '>').dispatch('click');
  await flush();
  window.NKB.observationsOtel.refresh();
  await flush();
  const failed = dump();
  findButton(registry.get('tab-observations-otel'), '重试').dispatch('click');
  await flush();
  return { failed, recovered: dump() };
}

async function scenarioRepeatedShrink() {
  const initial = healthySnapshot([], { pagination: { page: 1, size: 10, total: 30, pages: 3 } });
  const shrunk = healthySnapshot([], { pagination: { page: 3, size: 10, total: 12, pages: 2 } });
  const shrunkAgain = healthySnapshot([], { pagination: { page: 2, size: 10, total: 1, pages: 1 } });
  const first = healthySnapshot([{ ...spanOf(0), name: 'back-on-first-page' }]);
  let calls = 0;
  fetchResponder = (url) => {
    if (!url.startsWith('/observations/otel')) return Promise.resolve(jsonResponse(defaultBody(url)));
    calls += 1;
    return Promise.resolve(jsonResponse([initial, shrunk, shrunkAgain, first][Math.min(calls - 1, 3)]));
  };
  openObservationsTab();
  await flush();
  findButton(registry.get('tab-observations-otel'), '3').dispatch('click');
  await flush();
  const recovery = dump();
  findButton(registry.get('tab-observations-otel'), '回第一页').dispatch('click');
  await flush();
  return { recovery, first: dump() };
}

async function scenarioInvalidVariants() {
  const variants = [null, [], { ok: true, status: 200, json: () => Promise.reject(new Error('bad json')) }];
  let index = 0;
  fetchResponder = (url) => {
    if (!url.startsWith('/observations/otel')) return Promise.resolve(jsonResponse(defaultBody(url)));
    const variant = variants[Math.min(index++, variants.length - 1)];
    return Promise.resolve(variant && variant.json ? variant : jsonResponse(variant));
  };
  const states = [];
  openObservationsTab();
  await flush();
  states.push(dump());
  for (let i = 0; i < 2; i += 1) {
    findButton(registry.get('tab-observations-otel'), '重试').dispatch('click');
    await flush();
    states.push(dump());
  }
  return { states };
}

async function scenarioCompleteSignals() {
  const snap = healthySnapshot([{...spanOf(1), trace_url:'https://grafana.example/obs/explore?trace=abc', attributes:{'nkb.result_count':0, 'http.route':'<img src=x>'}}]);
  snap.links = {Metric:'https://grafana.example/obs/d/nkb-overview', Log:'https://grafana.example/obs/d/nkb-logs', Trace:'javascript:alert(1)', Profile:'https://grafana.example/obs/d/nkb-profile'};
  snap.metric_snapshot = {status:'healthy', collected_at:'2026-10-30T01:00:00Z', collection_count:2, collection_interval_millis:15000, truncated:true, truncation_scope:['points'], stale:false, instruments:[{name:'nkb.retrieval.results',kind:'histogram',unit:'{result}',window:'provider_lifetime',points:[{attributes:{outcome:'success'},aggregation_temporality:'CUMULATIVE',count:1,sum:0,min:0,max:0,start_time:'2026-10-30T00:00:00Z',end_time:'2026-10-30T01:00:00Z'}]}]};
  snap.logs = {status:'healthy',capacity:100,retained_count:100,returned_count:1,evicted_count:5,items:[{timestamp:'2026-10-30T01:00:00Z',severity:'INFO',event:'nkb.retrieval.completed',trace_id:'a'.repeat(32),trace_url:'https://grafana.example/obs/explore?trace=abc',attributes:{outcome:'success',result_count:0}}]};
  if (SCENARIO === 'summary-states') {
    snap.metric_snapshot.status = 'degraded';
    snap.metric_snapshot.stale = true;
    snap.metric_snapshot.error = 'metric_collection_failed';
    snap.metric_snapshot.instruments.push({name:'counter-value',kind:'counter',unit:'1',window:'provider_lifetime',points:[{attributes:{outcome:'success'},value:0}]},
      {name:'gauge-unknown',kind:'gauge',unit:'By',window:'instantaneous',points:[{attributes:{},value:null}]},
      {name:'overflow-point',kind:'counter',unit:'1',points:[{attributes:{},value:8,overflow:true}]});
    snap.signals.metrics.dropped_queue_full = 3;
    snap.signals.metrics.unknown_diagnostic = 'HIDDEN-DIAGNOSTIC';
    snap.logs.status = 'degraded';
  }
  let count = 0;
  fetchResponder = (url) => {
    if (!url.startsWith('/observations/otel')) return Promise.resolve(jsonResponse(defaultBody(url)));
    if (++count > 1) return Promise.reject(new Error('SECRET'));
    return Promise.resolve(jsonResponse(snap));
  };
  openObservationsTab();
  await flush();
  const before = dump();
  window.NKB.observationsOtel.refresh();
  await flush();
  return {before, after:dump()};
}

const scenarios = {
  "complete-signals": scenarioCompleteSignals,
  "summary-states": scenarioCompleteSignals,
  malicious: scenarioMalicious,
  navigation: scenarioNavigation,
  'network-failure': scenarioNetworkFailure,
  'http-error': scenarioHttpError,
  'stale-response': scenarioStaleResponse,
  pagination: scenarioPagination,
  grafana: scenarioGrafana,
  'grafana-unsafe': scenarioGrafanaUnsafe,
  'empty-disabled': scenarioEmptyDisabled,
  instruments: scenarioInstruments,
  'invalid-snapshot': scenarioInvalidSnapshot,
  'leave-return': scenarioLeaveAndReturn,
  'stale-failure': scenarioStaleFailure,
  'refresh-page': scenarioRefreshCurrentPage,
  'page-shrink': scenarioPageShrink,
  'page-two-retry': scenarioPageTwoRetry,
  'repeated-shrink': scenarioRepeatedShrink,
  'invalid-variants': scenarioInvalidVariants,
};

async function main() {
  buildDom();
  if (SCENARIO === 'navigation') window.location.hash = '#observations';
  loadFrontendScripts();
  const runner = scenarios[SCENARIO];
  if (!runner) throw new Error(`unknown scenario: ${SCENARIO}`);
  const result = await runner();
  process.stdout.write(JSON.stringify(result));
}

main().catch((error) => {
  process.stderr.write(String(error && error.stack ? error.stack : error));
  process.exitCode = 1;
});
