const { api, ui, navigation } = window.NKB;
const {
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
} = ui;

let currentDocuments = [];
let currentHealth = null;

function setLastUpdate() {
  byId('last-update').textContent = `Last updated: ${new Date().toLocaleString('zh-CN')}`;
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

function formatDateTime(value) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN');
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
  renderLoading(target, '正在加载依赖健康状态...');
  try {
    currentHealth = await api.getDependencyHealth();
    clear(target);
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
    clear(target);
    renderError(target, error.message);
    renderOverview();
  }
}

async function loadDocuments() {
  const target = byId('document-list');
  clear(target);
  renderLoading(target, '正在加载文档列表...');
  const tags = byId('filter-tags').value.trim();
  const status = byId('filter-status').value.trim();
  try {
    currentDocuments = await api.listDocuments({ tags, status });
    clear(target);
    if (!currentDocuments.length) {
      renderEmpty(target, '暂无文档');
    } else {
      const table = document.createElement('table');
      table.className = 'document-table';
      const thead = document.createElement('thead');
      const headerRow = document.createElement('tr');
      ['文件名', '操作'].forEach((label) => {
        const th = document.createElement('th');
        th.textContent = label;
        headerRow.appendChild(th);
      });
      thead.appendChild(headerRow);
      const tbody = document.createElement('tbody');
      currentDocuments.forEach((doc) => {
        const row = document.createElement('tr');
        const titleCell = document.createElement('td');
        titleCell.textContent = doc.title;
        const actionsCell = document.createElement('td');
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
        actionsCell.appendChild(actions);
        row.append(titleCell, actionsCell);
        tbody.appendChild(row);
      });
      table.append(thead, tbody);
      target.appendChild(table);
    }
    renderOverview();
    setLastUpdate();
  } catch (error) {
    clear(target);
    renderError(target, error.message);
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
  renderLoading(detail, '正在加载文档详情...');
  renderLoading(content, '正在加载文档原文...');
  renderLoading(chunksTarget, '正在加载 chunk 列表...');
  try {
    const [doc, body, chunks] = await Promise.all([
      api.getDocument(documentId),
      api.getDocumentContent(documentId),
      api.getDocumentChunks(documentId),
    ]);
    clear(detail);
    appendText(detail, '文件名称:', doc.title);
    appendText(detail, '录入时间:', formatDateTime(doc.created_at));
    appendText(detail, 'VDB入库状态:', vdbStatusText(doc.status));
    appendText(detail, 'ID:', doc.id);
    appendText(detail, '来源:', doc.source.display_name || doc.source.uri);
    appendText(detail, 'Tags:', JSON.stringify(doc.tags || {}));
    appendBadge(detail, vdbStatusText(doc.status), statusClass(doc.status));
    content.textContent = body.text;
    renderChunks(chunks);
  } catch (error) {
    const message = error.message || '加载文档失败';
    clear(detail);
    clear(chunksTarget);
    content.textContent = `Unable to load document content: ${message}`;
    renderError(detail, message);
    renderError(chunksTarget, message);
  }
}

async function indexDocument(documentId) {
  const detail = byId('document-detail');
  clear(detail);
  renderLoading(detail, '正在触发文档入库...');
  try {
    const job = await api.indexDocument(documentId);
    clear(detail);
    renderJson(detail, job);
    await loadDocuments();
  } catch (error) {
    clear(detail);
    renderError(detail, error.message);
  }
}

async function deleteDocument(documentId) {
  const detail = byId('document-detail');
  const content = byId('document-content');
  const chunks = byId('document-chunks');
  if (!window.confirm(`Delete document ${documentId}?`)) {
    return;
  }
  clear(detail);
  renderLoading(detail, '正在删除文档...');
  try {
    await api.deleteDocument(documentId);
    clear(detail);
    detail.textContent = 'Document deleted';
    content.textContent = '';
    clear(chunks);
    await loadDocuments();
  } catch (error) {
    clear(detail);
    renderError(detail, error.message);
  }
}

async function uploadDocument(event) {
  event.preventDefault();
  const target = byId('upload-result');
  clear(target);
  renderLoading(target, '正在上传文档...');
  const formData = new FormData();
  const file = byId('upload-file').files[0];
  formData.append('file', file);
  formData.append('source', byId('upload-source').value);
  formData.append('tags', byId('upload-tags').value);
  try {
    const doc = await api.uploadDocument(formData);
    clear(target);
    renderJson(target, doc);
    await loadDocuments();
  } catch (error) {
    clear(target);
    renderError(target, error.message);
  }
}

async function searchRetrieval(event) {
  event.preventDefault();
  const target = byId('retrieval-results');
  clear(target);
  renderLoading(target, '正在检索...');
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
    const data = await api.searchRetrieval(body);
    clear(target);
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
    clear(target);
    renderError(target, error.message);
  }
}

async function loadAll() {
  await Promise.all([loadDocuments(), loadHealth()]);
}

function init() {
  navigation.initNavigation();
  byId('refresh-health').addEventListener('click', loadHealth);
  byId('refresh-documents').addEventListener('click', loadDocuments);
  byId('upload-form').addEventListener('submit', uploadDocument);
  byId('retrieval-form').addEventListener('submit', searchRetrieval);
  loadAll();
}

window.NKB.app = { init };
window.NKB.app.init();
