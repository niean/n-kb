const { ui: navigationUi } = window.NKB;
const { byId: navigationById } = navigationUi;

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
  navigationById('sidebar-toggle').addEventListener('click', () => {
    const expanded = document.body.classList.toggle('sidebar-expanded');
    navigationById('sidebar-toggle').setAttribute('aria-expanded', String(expanded));
  });
  document.querySelectorAll('.sidebar__item').forEach((link) => {
    link.addEventListener('click', () => switchTab(link.dataset.tab));
  });
  window.addEventListener('hashchange', () => switchTab(selectedTabFromHash()));
  switchTab(selectedTabFromHash());
}

window.NKB.navigation = {
  initNavigation,
};
