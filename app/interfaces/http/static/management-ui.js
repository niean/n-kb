(function initializeManagementUi(global) {
  const namespace = global.NKB || {};

  function byId(id) {
    return document.getElementById(id);
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function formatDateTime(value) {
    if (!value) return '-';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString('zh-CN');
  }

  // N-Agent 页码窗口组件；page/pages 均来自服务端，组件不访问 API。
  function buildPager(page, pages, onPageChange, total, pageSize) {
    const current = Math.max(1, Math.min(10, Number(page) || 1));
    const totalPages = Math.max(0, Math.min(10, Number(pages) || 0));
    const size = Math.max(1, Number(pageSize) || 10);
    const count = Math.max(0, Number(total) || 0);
    const pager = el('div', 'observations-pager');
    const info = el('span', 'observations-pager__info');
    if (!totalPages || !count) info.textContent = '暂无记录';
    else info.textContent = `第 ${(current - 1) * size + 1}-${Math.min(current * size, count)} 条 / 共 ${count} 条`;
    pager.appendChild(info);
    const controls = el('div', 'observations-pager__ctrl');
    function pageButton(label, target, disabled, primary) {
      const button = el('button', `btn${primary ? ' btn--primary' : ''}`, label);
      button.type = 'button';
      button.disabled = disabled;
      if (disabled) button.classList.add('btn--disabled');
      button.setAttribute('aria-label', label === '<' ? '上一页' : label === '>' ? '下一页' : `第 ${label} 页`);
      button.addEventListener('click', () => {
        if (!disabled && Number.isInteger(target) && target >= 1 && target <= totalPages) onPageChange(target);
      });
      controls.appendChild(button);
    }
    pageButton('<', current - 1, current <= 1 || !totalPages, false);
    const candidates = new Set([1, totalPages, current - 1, current, current + 1]);
    const visible = [...candidates].filter((value) => value >= 1 && value <= totalPages).sort((a, b) => a - b);
    let previous = 0;
    visible.forEach((value) => {
      if (previous && value - previous > 1) controls.appendChild(el('span', 'observations-pager__dots', '...'));
      pageButton(String(value), value, false, value === current);
      previous = value;
    });
    pageButton('>', current + 1, current >= totalPages || !totalPages, false);
    pager.appendChild(controls);
    return pager;
  }

  function clear(element) {
    element.replaceChildren();
  }

  function parseTags(value) {
    const tags = {};
    value.split(',').map((item) => item.trim()).filter(Boolean).forEach((item) => {
      const parts = item.split('=');
      if (parts.length >= 2) {
        const key = parts.shift().trim();
        const tagValue = parts.join('=').trim();
        if (key && tagValue) {
          tags[key] = tagValue;
        }
      }
    });
    return tags;
  }

  function vdbStatusText(status) {
    if (status === 'indexed') return '入库成功';
    if (status === 'failed') return '入库失败';
    return '未入库';
  }

  function statusClass(status) {
    if (status === 'indexed' || status === 'ok') return 'success';
    if (status === 'failed' || status === 'error') return 'danger';
    return 'warning';
  }

  function badgeClass(variant) {
    if (!variant) return 'badge';
    if (variant.startsWith('badge--')) return `badge ${variant}`;
    return `badge ${variant} badge--${variant}`;
  }

  function appendText(parent, label, value) {
    const line = document.createElement('div');
    line.className = 'row';
    const key = document.createElement('span');
    key.className = 'key';
    key.textContent = label;
    const val = document.createElement('span');
    val.className = 'val';
    val.textContent = value == null || value === '' ? '-' : String(value);
    line.append(key, val);
    parent.appendChild(line);
  }

  function appendBadge(parent, value, variant) {
    const badge = document.createElement('span');
    badge.className = badgeClass(variant);
    badge.textContent = value;
    parent.appendChild(badge);
  }

  function renderJson(parent, value) {
    const pre = document.createElement('pre');
    pre.textContent = JSON.stringify(value, null, 2);
    parent.appendChild(pre);
  }

  function renderState(parent, message, className) {
    const state = document.createElement('div');
    state.className = className;
    state.textContent = message;
    parent.appendChild(state);
  }

  function renderEmpty(parent, message) {
    renderState(parent, message, 'muted empty-state');
  }

  function renderLoading(parent, message) {
    renderState(parent, message || '加载中...', 'muted loading-state');
  }

  function renderError(parent, message) {
    renderState(parent, message, 'muted error-state');
  }

  global.NKB = namespace;
  window.NKB.ui = {
    byId,
    clear,
    el,
    formatDateTime,
    buildPager,
    parseTags,
    vdbStatusText,
    statusClass,
    appendText,
    appendBadge,
    renderJson,
    renderEmpty,
    renderLoading,
    renderError,
  };
}(window));
