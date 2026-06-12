(function initializeManagementUi(global) {
  const namespace = global.NKB || {};

  function byId(id) {
    return document.getElementById(id);
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
