(function initializeManagementNavigation(global) {
  const { ui: navigationUi } = global.NKB;
  const { byId: navigationById } = navigationUi;

  const SIDEBAR_PREF_KEY = 'nkb.sidebar.expanded';
  const SUBMENU_PREF_KEY = 'nkb.sidebar.submenu.observations';
  const tabNames = ['overview', 'documents', 'retrieval', 'health', 'observations-otel'];
  let currentTab = null;
  let onTabChange = null;
  let submenuOpen = true;
  let collapsedOpen = false;

  function readPref(key) {
    try {
      const value = localStorage.getItem(key);
      if (value === '0') return false;
      if (value === '1') return true;
    } catch (_) { /* localStorage 不可用时使用默认值 */ }
    return true;
  }

  function writePref(key, expanded) {
    try {
      localStorage.setItem(key, expanded ? '1' : '0');
    } catch (_) { /* 写入失败不回滚当前 UI */ }
  }

  function applySidebarExpanded(expanded) {
    collapsedOpen = false;
    document.body.classList.toggle('sidebar-expanded', expanded);
    const toggle = document.getElementById('sidebar-toggle');
    if (toggle) toggle.setAttribute('aria-expanded', String(expanded));
    applySubmenuState(currentTab === 'observations-otel' ? true : readPref(SUBMENU_PREF_KEY));
  }

  function syncSubmenuVisibility() {
    const submenu = document.querySelector('[data-submenu-of="observations"]');
    const parent = document.querySelector('[data-menu-parent="observations"]');
    const expanded = document.body.classList.contains('sidebar-expanded');
    const visible = expanded ? submenuOpen : collapsedOpen;
    if (submenu) {
      submenu.classList.toggle('sidebar__submenu--open', visible);
      submenu.hidden = !visible;
    }
    if (parent) {
      parent.classList.toggle('sidebar__item--parent-open', visible);
      parent.setAttribute('aria-expanded', String(visible));
    }
  }

  function applySubmenuState(open) {
    submenuOpen = Boolean(open);
    syncSubmenuVisibility();
  }

  function selectedTabFromHash() {
    const tabName = window.location.hash.slice(1);
    if (tabName === 'observations') {
      history.replaceState(null, '', '#observations-otel');
      return 'observations-otel';
    }
    if (tabNames.includes(tabName)) return tabName;
    history.replaceState(null, '', '#overview');
    return 'overview';
  }

  function switchTab(tabName) {
    const nextTab = tabNames.includes(tabName) ? tabName : 'overview';
    if (nextTab === currentTab) return;
    const previous = currentTab;
    currentTab = nextTab;
    collapsedOpen = false;
    document.querySelectorAll('.tab-content').forEach((tab) => tab.classList.remove('active'));
    document.querySelectorAll('.sidebar__item').forEach((item) => {
      item.classList.remove('sidebar__item--active');
      item.removeAttribute('aria-current');
    });
    navigationById(`tab-${nextTab}`).classList.add('active');
    const nav = document.querySelector(`[data-tab="${nextTab}"]`);
    nav.classList.add('sidebar__item--active');
    nav.setAttribute('aria-current', 'page');
    applySubmenuState(nextTab === 'observations-otel' ? true : readPref(SUBMENU_PREF_KEY));
    navigationById('topbar-title').textContent = nav.querySelector('.sidebar__item-label').textContent;
    if (typeof onTabChange === 'function') onTabChange({ previous, current: nextTab });
  }

  function navigateTo(tabName) {
    if (!tabNames.includes(tabName)) return;
    if (window.location.hash !== `#${tabName}`) window.location.hash = `#${tabName}`;
    switchTab(tabName);
  }

  function initNavigation(callback) {
    onTabChange = typeof callback === 'function' ? callback : null;
    applySidebarExpanded(readPref(SIDEBAR_PREF_KEY));
    submenuOpen = readPref(SUBMENU_PREF_KEY);
    applySubmenuState(submenuOpen);
    const toggle = navigationById('sidebar-toggle');
    if (toggle) {
      toggle.addEventListener('click', () => {
        const nextExpanded = !document.body.classList.contains('sidebar-expanded');
        applySidebarExpanded(nextExpanded);
        writePref(SIDEBAR_PREF_KEY, nextExpanded);
      });
    }
    const parent = document.querySelector('[data-menu-parent="observations"]');
    const group = document.querySelector('[data-tab-group="observations"]');
    function openCollapsed() {
      if (document.body.classList.contains('sidebar-expanded')) return;
      collapsedOpen = true;
      syncSubmenuVisibility();
    }
    function closeCollapsed(event) {
      if (document.body.classList.contains('sidebar-expanded')) return;
      if (event && event.relatedTarget && group && group.contains(event.relatedTarget)) return;
      collapsedOpen = false;
      syncSubmenuVisibility();
    }
    if (group) {
      group.addEventListener('mouseenter', openCollapsed);
      group.addEventListener('mouseleave', closeCollapsed);
      group.addEventListener('focusin', openCollapsed);
      group.addEventListener('focusout', closeCollapsed);
    }
    if (parent) {
      parent.addEventListener('click', (event) => {
        event.preventDefault();
        if (!document.body.classList.contains('sidebar-expanded')) {
          collapsedOpen = !collapsedOpen;
          syncSubmenuVisibility();
          return;
        }
        const nextOpen = !submenuOpen;
        applySubmenuState(nextOpen);
        writePref(SUBMENU_PREF_KEY, nextOpen);
      });
    }
    document.querySelectorAll('[data-tab]').forEach((link) => {
      link.addEventListener('click', (event) => {
        event.preventDefault();
        navigateTo(link.dataset.tab);
      });
    });
    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return;
      collapsedOpen = false;
      applySubmenuState(false);
      if (document.activeElement && typeof document.activeElement.blur === 'function') document.activeElement.blur();
    });
    window.addEventListener('hashchange', () => switchTab(selectedTabFromHash()));
    switchTab(selectedTabFromHash());
  }

  global.NKB.navigation = { initNavigation };
}(window));
