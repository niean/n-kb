const { ui: navigationUi } = window.NKB;
const { byId: navigationById } = navigationUi;

const SIDEBAR_PREF_KEY = 'nkb.sidebar.expanded';

function readSidebarPref() {
  try {
    const value = localStorage.getItem(SIDEBAR_PREF_KEY);
    if (value === '0') return false;
    if (value === '1') return true;
  } catch (_) { /* localStorage 不可用时使用默认值 */ }
  return true;
}

function writeSidebarPref(expanded) {
  try {
    localStorage.setItem(SIDEBAR_PREF_KEY, expanded ? '1' : '0');
  } catch (_) { /* 写入失败不回滚当前 UI */ }
}

function applySidebarExpanded(expanded) {
  document.body.classList.toggle('sidebar-expanded', expanded);
  const toggle = document.getElementById('sidebar-toggle');
  if (toggle) toggle.setAttribute('aria-expanded', String(expanded));
}

const tabNames = ['overview', 'documents', 'retrieval', 'health'];

function selectedTabFromHash() {
  const tabName = window.location.hash.slice(1);
  if (tabNames.includes(tabName)) {
    return tabName;
  }
  history.replaceState(null, '', '#overview');
  return 'overview';
}

function switchTab(tabName) {
  const nextTab = tabNames.includes(tabName) ? tabName : 'overview';
  document.querySelectorAll('.tab-content').forEach((tab) => tab.classList.remove('active'));
  document.querySelectorAll('.sidebar__item').forEach((item) => item.classList.remove('sidebar__item--active'));
  navigationById(`tab-${nextTab}`).classList.add('active');
  const nav = document.querySelector(`[data-tab="${nextTab}"]`);
  nav.classList.add('sidebar__item--active');
  navigationById('topbar-title').textContent = nav.querySelector('.sidebar__item-label').textContent;
}

function initNavigation() {
  applySidebarExpanded(readSidebarPref());
  const toggle = navigationById('sidebar-toggle');
  if (toggle) {
    toggle.addEventListener('click', () => {
      const nextExpanded = !document.body.classList.contains('sidebar-expanded');
      applySidebarExpanded(nextExpanded);
      writeSidebarPref(nextExpanded);
    });
  }
  document.querySelectorAll('.sidebar__item').forEach((link) => {
    link.addEventListener('click', () => switchTab(link.dataset.tab));
  });
  window.addEventListener('hashchange', () => switchTab(selectedTabFromHash()));
  switchTab(selectedTabFromHash());
}

window.NKB.navigation = {
  initNavigation,
};
