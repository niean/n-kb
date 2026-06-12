(function initializeManagementApi(global) {
  const namespace = global.NKB || {};

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

  function listDocuments(filters) {
    const params = new URLSearchParams();
    const tags = filters && filters.tags ? filters.tags.trim() : '';
    const status = filters && filters.status ? filters.status.trim() : '';
    if (tags) params.set('tags', tags);
    if (status) params.set('status', status);
    const suffix = params.toString() ? `?${params.toString()}` : '';
    return fetchJson('/documents' + suffix);
  }

  function getDocument(documentId) {
    return fetchJson(`/documents/${encodeURIComponent(documentId)}`);
  }

  function getDocumentContent(documentId) {
    return fetchJson(`/documents/${encodeURIComponent(documentId)}/content`);
  }

  function getDocumentChunks(documentId) {
    return fetchJson(`/documents/${encodeURIComponent(documentId)}/chunks`);
  }

  function indexDocument(documentId) {
    return fetchJson(`/documents/${encodeURIComponent(documentId)}/index`, { method: 'POST' });
  }

  function deleteDocument(documentId) {
    return fetchJson(`/documents/${encodeURIComponent(documentId)}`, { method: 'DELETE' });
  }

  function uploadDocument(formData) {
    return fetchJson('/documents', { method: 'POST', body: formData });
  }

  function searchRetrieval(body) {
    return fetchJson('/retrieval/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }

  function getDependencyHealth() {
    return fetchJson('/health/dependencies');
  }

  global.NKB = namespace;
  window.NKB.api = {
    fetchJson,
    fetchJsonFromFetch,
    listDocuments,
    getDocument,
    getDocumentContent,
    getDocumentChunks,
    indexDocument,
    deleteDocument,
    uploadDocument,
    searchRetrieval,
    getDependencyHealth,
  };
}(window));
