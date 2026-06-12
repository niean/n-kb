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
  badge.className = variant ? `badge ${variant}` : 'badge';
  badge.textContent = value;
  parent.appendChild(badge);
}

function renderJson(parent, value) {
  const pre = document.createElement('pre');
  pre.textContent = JSON.stringify(value, null, 2);
  parent.appendChild(pre);
}

function renderEmpty(parent, message) {
  const empty = document.createElement('div');
  empty.className = 'muted';
  empty.textContent = message;
  parent.appendChild(empty);
}

async function fetchJson(url, options) {
  return fetchJsonFromFetch(fetch(url, options));
}

async function fetchJsonFromFetch(responsePromise) {
  const response = await responsePromise;
  const data = response.status === 204 ? null : await response.json();
  if (!response.ok) {
    throw new Error(data && data.error ? data.error.code : 'request_failed');
  }
  return data;
}

let currentDocuments = [];
let currentHealth = null;

function setLastUpdate() {
  byId('last-update').textContent = `Last updated: ${new Date().toLocaleString('zh-CN')}`;
}

function switchTab(tabName) {
  document.querySelectorAll('.tab-content').forEach((tab) => tab.classList.remove('active'));
  document.querySelectorAll('.sidebar__item').forEach((item) => item.classList.remove('sidebar__item--active'));
  byId(`tab-${tabName}`).classList.add('active');
  const nav = document.querySelector(`[data-tab="${tabName}"]`);
  nav.classList.add('sidebar__item--active');
  byId('topbar-title').textContent = nav.querySelector('.sidebar__item-label').textContent;
}

function renderStats(target, stats) {
  clear(target);
  stats.forEach((stat) => {
    const card = document.createElement('div');
    card.className = 'stat-card';
    const label = document.createElement('div');
    label.className = 'label';
    label.textContent = stat.label;
    const value = document.createElement('div');
    value.className = 'value';
    value.textContent = String(stat.value);
    const sub = document.createElement('div');
    sub.className = 'sub';
    sub.textContent = stat.sub || '';
    card.append(label, value, sub);
    target.appendChild(card);
  });
}

function healthEntries(health) {
  if (!health || typeof health !== 'object') return [];
  return Object.entries(health).map(([name, value]) => {
    if (value && typeof value === 'object' && 'status' in value) {
      return [name, value.status];
    }
    if (name === 'status') return [name, value];
    return [name, JSON.stringify(value)];
  });
}

function renderOverview() {
  const indexed = currentDocuments.filter((doc) => doc.status === 'indexed').length;
  const failed = currentDocuments.filter((doc) => doc.status === 'failed').length;
  const pending = currentDocuments.length - indexed - failed;
  const healthStatus = healthEntries(currentHealth).some(([, status]) => status === 'error') ? 'error' : 'ok';
  renderStats(byId('overview-stats'), [
    { label: 'Total Documents', value: currentDocuments.length, sub: '全部文档' },
    { label: 'Indexed', value: indexed, sub: '已入库' },
    { label: 'Pending / Failed', value: `${pending} / ${failed}`, sub: '待处理 / 失败' },
    { label: 'Dependencies', value: healthStatus, sub: '依赖健康' },
  ]);

  const docTarget = byId('overview-documents');
  clear(docTarget);
  currentDocuments.slice(-5).reverse().forEach((doc) => appendText(docTarget, doc.title, vdbStatusText(doc.status)));
  if (!currentDocuments.length) renderEmpty(docTarget, '暂无文档');

  const healthTarget = byId('overview-health');
  clear(healthTarget);
  healthEntries(currentHealth).forEach(([name, status]) => appendText(healthTarget, name, status));
  if (!healthEntries(currentHealth).length) renderEmpty(healthTarget, '暂无健康信息');
}

async function loadHealth() {
  const target = byId('dependency-health');
  clear(target);
  try {
    currentHealth = await fetch('/health/dependencies').then((response) => response.json());
    const stats = healthEntries(currentHealth).map(([name, status]) => ({ label: name, value: status, sub: 'dependency' }));
    renderStats(byId('health-stats'), stats.length ? stats : [{ label: 'Dependencies', value: 'unknown', sub: 'no data' }]);
    healthEntries(currentHealth).forEach(([name, status]) => {
      const row = document.createElement('div');
      row.className = 'row';
      const key = document.createElement('span');
      key.className = 'key';
      const indicator = document.createElement('span');
      indicator.className = `indicator ${status === 'ok' ? 'green' : 'red'}`;
      key.append(indicator, document.createTextNode(` ${name}`));
      const val = document.createElement('span');
      val.className = 'val';
      val.textContent = status;
      row.append(key, val);
      target.appendChild(row);
    });
    renderJson(target, currentHealth);
    renderOverview();
    setLastUpdate();
  } catch (error) {
    currentHealth = { status: 'error' };
    renderEmpty(target, error.message);
    renderOverview();
  }
}

async function loadDocuments() {
  const target = byId('document-list');
  clear(target);
  const params = new URLSearchParams();
  const tags = byId('filter-tags').value.trim();
  const status = byId('filter-status').value.trim();
  if (tags) params.set('tags', tags);
  if (status) params.set('status', status);
  const suffix = params.toString() ? `?${params.toString()}` : '';
  try {
    currentDocuments = await fetch('/documents' + suffix).then((response) => response.json());
    currentDocuments.forEach((doc) => {
      const item = document.createElement('div');
      item.className = 'item';
      appendText(item, 'Title:', doc.title);
      appendBadge(item, vdbStatusText(doc.status), statusClass(doc.status));
      appendText(item, 'VDB:', vdbStatusText(doc.status));
      appendText(item, 'ID:', doc.id);
      appendText(item, 'Source:', doc.source.display_name || doc.source.uri);
      appendText(item, 'Tags:', JSON.stringify(doc.tags || {}));
      const actions = document.createElement('div');
      actions.className = 'item-actions';
      const detailButton = document.createElement('button');
      detailButton.className = 'btn';
      detailButton.type = 'button';
      detailButton.textContent = 'Detail';
      detailButton.addEventListener('click', () => showDocument(doc.id));
      const indexButton = document.createElement('button');
      indexButton.className = 'btn';
      indexButton.type = 'button';
      indexButton.textContent = 'Index';
      indexButton.addEventListener('click', () => indexDocument(doc.id));
      const deleteButton = document.createElement('button');
      deleteButton.className = 'btn danger';
      deleteButton.type = 'button';
      deleteButton.textContent = 'Delete';
      deleteButton.addEventListener('click', () => deleteDocument(doc.id));
      actions.append(detailButton, indexButton, deleteButton);
      item.appendChild(actions);
      target.appendChild(item);
    });
    if (!currentDocuments.length) renderEmpty(target, '暂无文档');
    renderOverview();
    setLastUpdate();
  } catch (error) {
    renderEmpty(target, error.message);
  }
}

function renderChunks(chunks) {
  const target = byId('document-chunks');
  clear(target);
  if (!chunks.length) {
    renderEmpty(target, '暂无 chunk，可能尚未入库或入库失败');
    return;
  }
  chunks.forEach((chunk) => {
    const card = document.createElement('div');
    card.className = 'chunk-card';
    appendBadge(card, `#${chunk.ordinal}`, 'success');
    appendText(card, 'chunk_id:', chunk.id);
    appendText(card, 'token_count:', chunk.token_count);
    appendText(card, 'metadata:', JSON.stringify(chunk.metadata || {}));
    const text = document.createElement('pre');
    text.textContent = chunk.text;
    card.appendChild(text);
    target.appendChild(card);
  });
}

async function showDocument(documentId) {
  const detail = byId('document-detail');
  const content = byId('document-content');
  const chunksTarget = byId('document-chunks');
  clear(detail);
  clear(chunksTarget);
  content.textContent = '';
  try {
    const [doc, body, chunks] = await Promise.all([
      fetchJson(`/documents/${encodeURIComponent(documentId)}`),
      fetchJson(`/documents/${encodeURIComponent(documentId)}/content`),
      fetchJson(`/documents/${encodeURIComponent(documentId)}/chunks`),
    ]);
    appendText(detail, 'Title:', doc.title);
    appendBadge(detail, vdbStatusText(doc.status), statusClass(doc.status));
    appendText(detail, 'Source:', doc.source.display_name || doc.source.uri);
    appendText(detail, 'Tags:', JSON.stringify(doc.tags || {}));
    content.textContent = body.text;
    renderChunks(chunks);
  } catch (error) {
    renderEmpty(detail, error.message);
  }
}

async function indexDocument(documentId) {
  const detail = byId('document-detail');
  clear(detail);
  try {
    const job = await fetchJson(`/documents/${encodeURIComponent(documentId)}/index`, { method: 'POST' });
    renderJson(detail, job);
    await loadDocuments();
  } catch (error) {
    renderEmpty(detail, error.message);
  }
}

async function deleteDocument(documentId) {
  const detail = byId('document-detail');
  const content = byId('document-content');
  const chunks = byId('document-chunks');
  clear(detail);
  if (!window.confirm('Delete this document?')) {
    return;
  }
  try {
    await fetchJson(`/documents/${encodeURIComponent(documentId)}`, { method: 'DELETE' });
    detail.textContent = 'Document deleted';
    content.textContent = '';
    clear(chunks);
    await loadDocuments();
  } catch (error) {
    renderEmpty(detail, error.message);
  }
}

async function uploadDocument(event) {
  event.preventDefault();
  const target = byId('upload-result');
  clear(target);
  const formData = new FormData();
  const file = byId('upload-file').files[0];
  formData.append('file', file);
  formData.append('source', byId('upload-source').value);
  formData.append('tags', byId('upload-tags').value);
  try {
    const doc = await fetchJson('/documents', { method: 'POST', body: formData });
    renderJson(target, doc);
    await loadDocuments();
  } catch (error) {
    renderEmpty(target, error.message);
  }
}

async function searchRetrieval(event) {
  event.preventDefault();
  const target = byId('retrieval-results');
  clear(target);
  const minScore = byId('retrieval-min-score').value.trim();
  const sourceKind = byId('retrieval-source-kind').value.trim();
  const documentStatus = byId('retrieval-document-status').value.trim();
  const filters = { tags: parseTags(byId('retrieval-tags').value) };
  if (sourceKind) filters.source_kind = sourceKind;
  if (documentStatus) filters.document_status = documentStatus;
  const body = {
    query: byId('retrieval-query').value,
    top_k: Number(byId('retrieval-top-k').value || 5),
    filters,
  };
  if (minScore) {
    body.min_score = Number(minScore);
  }
  try {
    const data = await fetchJsonFromFetch(fetch('/retrieval/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }));
    if (!data.results.length) {
      renderEmpty(target, '暂无检索结果');
      return;
    }
    data.results.forEach((result) => {
      const item = document.createElement('div');
      item.className = 'result-card';
      appendBadge(item, `score ${result.score}`, 'success');
      appendText(item, 'document_id:', result.document_id);
      appendText(item, 'chunk_id:', result.chunk_id);
      appendText(item, 'snippet:', result.snippet);
      appendText(item, 'source:', JSON.stringify(result.source || {}));
      appendText(item, 'tags:', JSON.stringify(result.tags || {}));
      appendText(item, 'metadata:', JSON.stringify(result.metadata || {}));
      target.appendChild(item);
    });
  } catch (error) {
    renderEmpty(target, error.message);
  }
}

function initNavigation() {
  byId('sidebar-toggle').addEventListener('click', () => {
    const expanded = document.body.classList.toggle('sidebar-expanded');
    byId('sidebar-toggle').setAttribute('aria-expanded', String(expanded));
  });
  document.querySelectorAll('.sidebar__item').forEach((button) => {
    button.addEventListener('click', () => switchTab(button.dataset.tab));
  });
}

async function loadAll() {
  await Promise.all([loadDocuments(), loadHealth()]);
}

initNavigation();
byId('refresh-health').addEventListener('click', loadHealth);
byId('refresh-documents').addEventListener('click', loadDocuments);
byId('upload-form').addEventListener('submit', uploadDocument);
byId('retrieval-form').addEventListener('submit', searchRetrieval);
loadAll();
