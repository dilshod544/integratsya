// Общий скрипт dashboard/*.html (кроме login.html):
// - сессия / UX-скрытие пунктов без права;
// - window.CMAI_AUTH для карты и вкладок аналитики.
(function () {
  const PAGE_HTML = {
    atm_map: ['/dashboard/map.html', '/dashboard/atm-card.html'],
    atm_analytics: ['/dashboard/analytics.html'],
    atm_savings: ['/dashboard/savings.html'],
    atm_plan: ['/dashboard/incassation-plan.html', '/dashboard/equipment-replacement.html'],
    atm_calendar: ['/dashboard/incassation.html'],
    treasury: ['/dashboard/branch-cash.html'],
    hr_cashiers: ['/dashboard/cashiers.html', '/dashboard/cashier-detail.html'],
  };
  // Импорт и служебное — только admin (сервер тоже режет)
  const ADMIN_HTML = [
    '/dashboard/import.html',
    '/dashboard/import-atms.html',
    '/dashboard/import-branches.html',
    '/dashboard/import-cashiers.html',
    '/dashboard/import-vash.html',
    '/dashboard/import-cash-equipment.html',
    '/dashboard/import-branch-balances.html',
    '/dashboard/import-sqb-rates.html',
    '/dashboard/data-console.html',
    '/dashboard/atm-monitor.html',
  ];
  const FREE_PATHS = new Set(['/', '/dashboard/index.html', '/dashboard/login.html']);

  function pageKeyForPath(path) {
    for (const [key, list] of Object.entries(PAGE_HTML)) {
      if (list.includes(path)) return key;
    }
    return null;
  }

  function applyHomeCards(me) {
    const allowed = new Set(me.pages || []);
    document.querySelectorAll('[data-perm]').forEach((el) => {
      const need = (el.getAttribute('data-perm') || '').split(/\s+/).filter(Boolean);
      if (!need.length) return;
      const ok = me.is_admin
        ? true
        : need.includes('__admin__')
          ? false
          : need.some((k) => allowed.has(k));
      el.style.display = ok ? '' : 'none';
    });
    document.querySelectorAll('[data-perm-section]').forEach((sec) => {
      const grid = sec.nextElementSibling;
      if (!grid || !grid.classList.contains('grid')) return;
      const visible = [...grid.querySelectorAll('.card, a.card')].some((c) => c.style.display !== 'none');
      sec.style.display = visible ? '' : 'none';
      grid.style.display = visible ? '' : 'none';
    });
  }

  function hideRestrictedLinks(me) {
    if (me.is_admin) return;
    const allowed = new Set(me.pages || []);
    document.querySelectorAll('a[href]').forEach((a) => {
      let path;
      try {
        path = new URL(a.getAttribute('href'), location.origin).pathname;
      } catch (e) {
        return;
      }
      if (FREE_PATHS.has(path)) return;
      if (ADMIN_HTML.some((p) => path === p || path.startsWith(p))) {
        a.style.display = 'none';
        return;
      }
      const key = pageKeyForPath(path);
      if (key && !allowed.has(key)) a.style.display = 'none';
    });
  }

  function applyAnalyticsTabs(me) {
    if (!location.pathname.endsWith('/analytics.html')) return;
    const allowed = new Set(me.pages || []);
    const hasAnalytics = me.is_admin || allowed.has('atm_analytics');
    const tabPerm = {
      status: 'atm_analytics',
      forecast: 'atm_analytics',
      withdrawals: 'atm_analytics',
      over400: 'atm_analytics',
      encash: 'atm_analytics',
      abc: 'atm_analytics',
      efficiency: 'atm_analytics',
      kassa: 'treasury',
      vash: 'map_vash',
    };
    let firstVisible = null;
    document.querySelectorAll('.section-tab[data-tab]').forEach((btn) => {
      const need = tabPerm[btn.dataset.tab];
      const ok = me.is_admin || (need ? allowed.has(need) : hasAnalytics);
      btn.style.display = ok ? '' : 'none';
      if (ok && !firstVisible) firstVisible = btn;
    });
    const active = document.querySelector('.section-tab.active');
    if (active && active.style.display === 'none' && firstVisible) {
      const tab = firstVisible.dataset.tab;
      if (typeof window.switchTab === 'function') window.switchTab(tab);
      else firstVisible.click();
    }
  }

  function applyMapLayers(me) {
    if (!location.pathname.endsWith('/map.html')) return;
    const allowed = new Set(me.pages || []);
    const canBranches = me.is_admin || allowed.has('map_branches');
    const canVash = me.is_admin || allowed.has('map_vash');
    window.CMAI_AUTH = window.CMAI_AUTH || {};
    window.CMAI_AUTH.canBranches = canBranches;
    window.CMAI_AUTH.canVash = canVash;
    if (!canBranches) {
      ['kpi-branches', 'bm-branches', 'bm-warehouse', 'branches-legend'].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.style.display = 'none';
      });
    }
    if (!canVash) {
      ['kpi-vash', 'bm-vash', 'vash-legend'].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.style.display = 'none';
      });
    }
    // Кнопка меню филиалов/ВАШ — спрятать, если оба слоя запрещены
    const menuBtn = document.getElementById('branches-toggle-btn');
    if (menuBtn && !canBranches && !canVash) menuBtn.style.display = 'none';
  }

  function renderUserBadge(me) {
    const initial = String(me.username || '?').trim().charAt(0).toUpperCase() || '?';
    const wrap = document.createElement('div');
    wrap.id = 'cmai-user-badge';
    wrap.innerHTML = `
      <style>
        #cmai-user-badge {
          position: fixed; top: 12px; right: 12px; z-index: 10050;
          font: 600 13px/1.25 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        }
        /* Avatar ustiga chiqmasin — header/nav o‘ng tomonda joy */
        body.has-cmai-user-badge header,
        body.has-cmai-user-badge .app-header,
        body.has-cmai-user-badge .topbar {
          padding-right: 64px !important;
        }
        body.has-cmai-user-badge header .top-nav,
        body.has-cmai-user-badge .top-nav {
          margin-right: 4px;
        }
        #cmai-user-badge .cmai-trigger {
          width: 40px; height: 40px; border-radius: 50%;
          border: 2px solid rgba(255,255,255,.85);
          background: linear-gradient(135deg, #2563eb, #0f766e);
          color: #fff; font-weight: 800; font-size: 15px;
          display: grid; place-items: center; cursor: pointer;
          box-shadow: 0 8px 22px rgba(15,23,42,.22);
          padding: 0;
        }
        #cmai-user-badge .cmai-trigger:hover,
        #cmai-user-badge.open .cmai-trigger {
          box-shadow: 0 10px 26px rgba(37,99,235,.35);
          transform: translateY(-1px);
        }
        #cmai-user-badge .cmai-menu {
          display: none;
          position: absolute; top: calc(100% + 8px); right: 0;
          min-width: 200px;
          background: #fff; color: #0f172a;
          border: 1px solid #e2e8f0; border-radius: 14px;
          box-shadow: 0 18px 40px rgba(15,23,42,.18);
          padding: 8px; overflow: hidden;
        }
        #cmai-user-badge.open .cmai-menu { display: block; }
        #cmai-user-badge .cmai-head {
          padding: 10px 10px 12px; border-bottom: 1px solid #f1f5f9; margin-bottom: 6px;
        }
        #cmai-user-badge .cmai-name { font-weight: 800; font-size: 14px; }
        #cmai-user-badge .cmai-role { color: #64748b; font-size: 12px; margin-top: 3px; font-weight: 650; }
        #cmai-user-badge .cmai-item {
          display: block; width: 100%; text-align: left;
          border: 0; background: transparent; border-radius: 10px;
          padding: 10px 10px; cursor: pointer;
          font: 750 13px/1 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          color: #0f172a; text-decoration: none;
        }
        #cmai-user-badge .cmai-item:hover { background: #f8fafc; }
        #cmai-user-badge .cmai-item.logout { color: #b91c1c; }
        #cmai-user-badge .cmai-item.logout:hover { background: #fef2f2; }
      </style>
      <button type="button" class="cmai-trigger" id="cmai-profile-btn" aria-haspopup="menu" aria-expanded="false" title="${escapeHtml(me.username)}">
        ${escapeHtml(initial)}
      </button>
      <div class="cmai-menu" role="menu" id="cmai-profile-menu">
        <div class="cmai-head">
          <div class="cmai-name">${escapeHtml(me.username)}</div>
          <div class="cmai-role">${escapeHtml(me.role_label || me.role_name || '')}</div>
        </div>
        ${me.is_admin ? '<a class="cmai-item" role="menuitem" href="/admin/">Админ-панель</a>' : ''}
        <button type="button" class="cmai-item logout" role="menuitem" id="auth-nav-logout">Выйти</button>
      </div>
    `;
    document.body.classList.add('has-cmai-user-badge');
    document.body.appendChild(wrap);

    const btn = document.getElementById('cmai-profile-btn');
    const toggle = (open) => {
      const next = open == null ? !wrap.classList.contains('open') : !!open;
      wrap.classList.toggle('open', next);
      btn.setAttribute('aria-expanded', next ? 'true' : 'false');
    };
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      toggle();
    });
    document.addEventListener('click', (e) => {
      if (!wrap.contains(e.target)) toggle(false);
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') toggle(false);
    });
    document.getElementById('auth-nav-logout').addEventListener('click', async () => {
      try { await fetch('/api/auth/logout', { method: 'POST' }); } catch (err) {}
      location.href = '/dashboard/login.html';
    });
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  async function init() {
    let me;
    try {
      const res = await fetch('/api/auth/me');
      if (!res.ok) {
        location.href = '/dashboard/login.html?next=' + encodeURIComponent(location.pathname + location.search);
        return;
      }
      me = await res.json();
      if (me.auth_disabled) {
        window.CMAI_AUTH = { auth_disabled: true, is_admin: true, pages: [], canBranches: true, canVash: true };
        return;
      }
      if (me.must_change_password) {
        location.href = '/dashboard/login.html?change=1';
        return;
      }
    } catch (err) {
      return;
    }
    window.CMAI_AUTH = {
      is_admin: !!me.is_admin,
      pages: me.pages || [],
      canBranches: !!(me.is_admin || (me.pages || []).includes('map_branches')),
      canVash: !!(me.is_admin || (me.pages || []).includes('map_vash')),
    };
    hideRestrictedLinks(me);
    applyHomeCards(me);
    applyAnalyticsTabs(me);
    applyMapLayers(me);
    renderUserBadge(me);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
