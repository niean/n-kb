(function initializeObservationsOtel(global) {
  const namespace = global.NKB || {};
  const { api, ui } = namespace;
  const { el } = ui;
  const CONTAINER_ID = 'tab-observations-otel';
  const LANGFUSE_URL = 'http://langfuse.localhost/project/n-agent-local-project/sessions';
  let active = false;
  let currentPage = 1;
  let requestToken = 0;
  let lastSnapshot = null;
  let lastSuccessAt = null;

  function root() {
    return ui.byId(CONTAINER_ID);
  }

  function isCurrent(token) {
    const node = root();
    return active && token === requestToken && node && node.classList.contains('active');
  }

  function formatTime(value) {
    if (!value) return '-';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    const shanghai = new Date(date.getTime() + 8 * 3600 * 1000);
    const pad = (number) => (number < 10 ? `0${number}` : String(number));
    return `${shanghai.getUTCFullYear()}-${pad(shanghai.getUTCMonth() + 1)}-${pad(shanghai.getUTCDate())} ${pad(shanghai.getUTCHours())}:${pad(shanghai.getUTCMinutes())}:${pad(shanghai.getUTCSeconds())}`;
  }

  function badgeVariant(status) {
    const value = String(status || '');
    if (['ok', 'running', 'enabled', 'healthy'].includes(value)) return 'success';
    if (value.startsWith('error') || ['failed', 'unhealthy'].includes(value)) return 'danger';
    return 'warning';
  }

  function statusBadge(status) {
    return el('span', `badge badge--${badgeVariant(status)}`, status);
  }

  function panel(title) {
    const section = el('section', 'status-panel');
    const header = el('div', 'panel-header');
    header.appendChild(el('span', null, title));
    const body = el('div', 'panel-body');
    section.append(header, body);
    return { section, body };
  }

  function kvRow(label, value) {
    const row = el('div', 'policy-item');
    row.appendChild(el('span', 'policy-k', `${label}：`));
    row.appendChild(el('span', 'policy-v', value === undefined || value === null || value === '' ? '-' : value));
    return row;
  }

  function kvRowBadge(label, status) {
    const row = el('div', 'policy-item');
    row.appendChild(el('span', 'policy-k', `${label}：`));
    row.appendChild(status ? statusBadge(status) : el('span', 'policy-v', '-'));
    return row;
  }

  function table(headers, rows) {
    const tableNode = el('table', 'document-table');
    const thead = el('thead');
    const headerRow = el('tr');
    headers.forEach((header) => {
      headerRow.appendChild(el('th', header.numeric ? 'document-table__numeric' : null, header.label));
    });
    thead.appendChild(headerRow);
    const tbody = el('tbody');
    rows.forEach((cells) => {
      const row = el('tr');
      cells.forEach((cell, index) => {
        const cellNode = el('td', headers[index].numeric ? 'document-table__numeric' : null);
        if (cell && cell.nodeType) cellNode.appendChild(cell);
        else cellNode.textContent = cell === undefined || cell === null || cell === '' ? '-' : String(cell);
        row.appendChild(cellNode);
      });
      tbody.appendChild(row);
    });
    tableNode.append(thead, tbody);
    return tableNode;
  }

  function empty(body, message) {
    body.appendChild(el('div', 'muted empty-state', message));
  }

  function renderStatus(data) {
    const result = panel('OTel 自检状态');
    const grid = el('div', 'policy-meta');
    grid.appendChild(kvRowBadge('状态', data.status));
    grid.appendChild(kvRow('模式', data.mode));
    grid.appendChild(kvRow('实例 ID', data.instance_id));
    grid.appendChild(kvRow('采样率', data.sample_ratio));
    grid.appendChild(kvRow('semconv 版本', data.semconv_version));
    grid.appendChild(kvRow('导出 endpoint', data.exporter_endpoint));
    if (data.dropped_after_deadline) grid.appendChild(kvRow('关停截止后丢弃', data.dropped_after_deadline));
    result.body.appendChild(grid);
    return result.section;
  }

  function safeHref(value) {
    if (typeof value !== 'string' || /[\x00-\x1f\x7f]/.test(value)) return null;
    try {
      const parsed = new URL(value);
      if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) return null;
      return parsed.href;
    } catch (_) { return null; }
  }

  function renderLinks(data) {
    const result = panel('外部链接');
    const links = Object.assign({}, data.links || {}, { Langfuse: LANGFUSE_URL });
    const list = el('div', 'otel-links');
    Object.entries(links).forEach(([name, value]) => {
      const href = safeHref(value);
      if (!href) return;
      const link = el('a', 'otel-link', name);
      link.href = href;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      list.appendChild(link);
    });
    if (list.children.length) result.body.appendChild(list);
    else empty(result.body, '未配置外部链接');
    return result.section;
  }

  function renderSignals(data) {
    const result = panel('信号状态');
    const signals = data.signals && typeof data.signals === 'object' ? data.signals : {};
    const entries = Object.entries(signals);
    if (!entries.length) {
      empty(result.body, '无信号状态');
      return result.section;
    }
    const rows = entries.map(([name, signal]) => {
      const value = signal && typeof signal === 'object' ? signal : {};
      return [
        el('span', 'otel-strong', name),
        value.status ? statusBadge(value.status) : '-',
        formatTime(value.delivery),
        value.failure_count,
        formatTime(value.last_success),
        formatTime(value.last_failure),
        value.current_error,
      ];
    });
    result.body.appendChild(table([
      { label: '信号' }, { label: '状态' }, { label: '最近交付' },
      { label: '累计失败', numeric: true }, { label: '最近成功' }, { label: '最近失败' }, { label: '当前故障' },
    ], rows));
    return result.section;
  }

  function renderInstruments(data) {
    const result = panel('Instrument 清单(metric)');
    const instruments = Array.isArray(data.instruments) ? data.instruments : [];
    if (!instruments.length) {
      empty(result.body, '无 instrument');
      return result.section;
    }
    result.body.appendChild(table([
      { label: '名称' }, { label: '类型' }, { label: '单位' },
    ], instruments.map((item) => [item && item.name, item && item.kind, item && item.unit])));
    return result.section;
  }

  function renderSpans(data) {
    const result = panel('最近执行片段(span)');
    const spans = Array.isArray(data.spans) ? data.spans : [];
    if (!spans.length) empty(result.body, '暂无已采样 span');
    else {
      const rows = spans.map((span) => {
        const value = span && typeof span === 'object' ? span : {};
        const duration = value.duration_ms === undefined || value.duration_ms === null ? '-' : `${value.duration_ms}ms`;
        return [value.name, value.status ? statusBadge(value.status) : '-', duration];
      });
      result.body.appendChild(table([
        { label: '名称' }, { label: '状态' }, { label: '耗时', numeric: true },
      ], rows));
    }
    const pagination = data.pagination && typeof data.pagination === 'object' ? data.pagination : {};
    result.body.appendChild(ui.buildPager(
      Number(pagination.page) || 1,
      Number(pagination.pages) || 0,
      (page) => load(page),
      Number(pagination.total) || 0,
      Number(pagination.size) || 10,
    ));
    return result.section;
  }

  function renderNotes(data) {
    const notes = data.notes && typeof data.notes === 'object' ? data.notes : {};
    const entries = Object.entries(notes).filter(([, value]) => typeof value === 'string' && value);
    if (!entries.length) return null;
    const result = panel('说明');
    entries.forEach(([key, value]) => result.body.appendChild(kvRow(key, value)));
    return result.section;
  }

  function renderSnapshot(data) {
    const node = root();
    node.replaceChildren();
    [
      renderStatus(data),
      renderLinks(data),
      renderSignals(data),
      renderInstruments(data),
      renderSpans(data),
      renderNotes(data),
    ].forEach((section) => {
      if (section) node.appendChild(section);
    });
  }

  function buildLoading() {
    const result = el('section', 'status-panel');
    const body = el('div', 'panel-body');
    ui.renderLoading(body, '加载 OTel 自检数据...');
    result.appendChild(body);
    return result;
  }

  function buildError(page) {
    const result = el('section', 'status-panel');
    const body = el('div', 'panel-body');
    ui.renderError(body, '加载 OTel 自检数据失败');
    const retry = el('button', 'btn', '重试');
    retry.type = 'button';
    retry.addEventListener('click', () => load(page));
    body.appendChild(retry);
    result.appendChild(body);
    return result;
  }

  function renderPageRecovery() {
    const node = root();
    node.replaceChildren();
    const result = panel('最近执行片段(span)');
    empty(result.body, '数据已变化，当前页暂无记录');
    const back = el('button', 'btn', '回第一页');
    back.type = 'button';
    back.addEventListener('click', () => load(1));
    result.body.appendChild(back);
    node.appendChild(result.section);
  }

  async function load(page, corrected) {
    const target = Number(page);
    if (!active || !Number.isInteger(target) || target < 1 || target > 10) return;
    currentPage = target;
    const token = ++requestToken;
    const node = root();
    if (!node) return;
    if (!lastSnapshot) {
      node.replaceChildren();
      node.appendChild(buildLoading());
    }
    let snapshot;
    try {
      snapshot = await api.getObservationsOtel(target);
      if (!snapshot || typeof snapshot !== 'object' || Array.isArray(snapshot)) throw new Error('invalid_snapshot');
    } catch (_) {
      if (!isCurrent(token)) return;
      node.replaceChildren();
      if (lastSnapshot) {
        renderSnapshot(lastSnapshot);
        node.appendChild(el('p', 'error-state', `刷新失败，保留上次快照（${formatTime(lastSuccessAt)}）`));
      }
      node.appendChild(buildError(target));
      return;
    }
    if (!isCurrent(token)) return;
    const pagination = snapshot.pagination && typeof snapshot.pagination === 'object' ? snapshot.pagination : {};
    const responsePage = Number(pagination.page) || 1;
    const pages = Math.max(0, Number(pagination.pages) || 0);
    if (responsePage > pages && pages > 0) {
      if (!corrected) return load(pages, true);
      renderPageRecovery();
      return;
    }
    lastSnapshot = snapshot;
    lastSuccessAt = new Date().toISOString();
    renderSnapshot(snapshot);
    const updated = ui.byId('last-update');
    if (updated) updated.textContent = `Last updated: ${new Date().toLocaleString('zh-CN')}`;
  }

  function init() {
    if (active) return Promise.resolve();
    active = true;
    currentPage = 1;
    lastSnapshot = null;
    lastSuccessAt = null;
    return load(1);
  }

  function refresh() {
    if (!active) return Promise.resolve();
    return load(currentPage);
  }

  function deactivate() {
    active = false;
    requestToken += 1;
    lastSnapshot = null;
    lastSuccessAt = null;
  }

  namespace.observationsOtel = { init, refresh, deactivate };
  global.NKB = namespace;
}(window));
