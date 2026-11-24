const API = location.origin;
const CACHE = 'v=3';

let PAGES = [];
let ROLES = [];
let USERS = [];
let selectedId = null; // null = create mode when form open
let editingId = null;  // number | 'new' | null

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function showMsg(text, kind) {
  const el = document.getElementById('msg');
  el.hidden = false;
  el.textContent = text;
  el.className = 'msg ' + (kind || 'ok');
  clearTimeout(showMsg._t);
  showMsg._t = setTimeout(() => { el.hidden = true; }, 4200);
}

async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : ('Ошибка ' + res.status));
  return data;
}

async function loadMe() {
  try {
    const me = await api('/api/auth/me');
    if (!me.auth_disabled && !me.is_admin) {
      document.body.innerHTML = '<div style="padding:48px;text-align:center;color:#64748b">Нет доступа</div>';
      throw new Error('forbidden');
    }
    document.getElementById('who').textContent = me.username || 'admin';
    return me;
  } catch (e) {
    if (e.message !== 'forbidden') location.href = '/dashboard/login.html?next=/admin/';
    throw e;
  }
}

document.getElementById('logout-link').onclick = async (e) => {
  e.preventDefault();
  await api('/api/auth/logout', { method: 'POST' });
  location.href = '/dashboard/login.html';
};

const overlay = document.getElementById('overlay');
const modal = document.getElementById('modal');
function openModal(html) {
  modal.innerHTML = html;
  overlay.hidden = false;
}
function closeModal() {
  overlay.hidden = true;
  modal.innerHTML = '';
}
overlay.onclick = (e) => { if (e.target === overlay) closeModal(); };

function childrenOf(parentKey) {
  return PAGES.filter((p) => p.parent === parentKey);
}

function buildPermTree(selected) {
  const selectedSet = new Set(selected || []);
  const groups = [];
  const seen = new Set();
  PAGES.forEach((p) => {
    if (seen.has(p.group)) return;
    seen.add(p.group);
    groups.push({ id: p.group, label: p.group_label });
  });

  return groups.map((g) => {
    const roots = PAGES.filter((p) => p.group === g.id && !p.parent);
    return `
      <div class="perm-group-title">${esc(g.label)}</div>
      ${roots.map((p) => `
        <div class="perm-card">
          <label class="perm-row">
            <input type="checkbox" class="perm-check" data-key="${esc(p.key)}" ${selectedSet.has(p.key) ? 'checked' : ''} />
            <span>
              <span class="title">${esc(p.label)} <span class="tag">карточка</span></span>
              <div class="hint">${esc(p.hint || '')}</div>
            </span>
          </label>
          ${childrenOf(p.key).map((c) => `
            <label class="perm-row child">
              <input type="checkbox" class="perm-check" data-key="${esc(c.key)}" data-parent-key="${esc(p.key)}" ${selectedSet.has(c.key) ? 'checked' : ''} />
              <span>
                <span class="title">${esc(c.label)} <span class="tag">внутри</span></span>
                <div class="hint">${esc(c.hint || '')}</div>
              </span>
            </label>
          `).join('')}
        </div>
      `).join('')}
    `;
  }).join('');
}

function wirePermTree(root) {
  root.querySelectorAll('.perm-check').forEach((inp) => {
    inp.onchange = () => {
      const key = inp.dataset.key;
      const parentKey = inp.dataset.parentKey;
      if (inp.checked && parentKey) {
        const parent = root.querySelector(`.perm-check[data-key="${parentKey}"]`);
        if (parent) parent.checked = true;
      }
      if (!inp.checked && !parentKey) {
        root.querySelectorAll(`.perm-check[data-parent-key="${key}"]`).forEach((c) => { c.checked = false; });
      }
    };
  });
}

function collectPages() {
  return Array.from(document.querySelectorAll('#perm-tree .perm-check:checked')).map((i) => i.dataset.key);
}

function setPages(pages) {
  const set = new Set(pages || []);
  document.querySelectorAll('#perm-tree .perm-check').forEach((inp) => {
    inp.checked = set.has(inp.dataset.key);
  });
}

function renderPresets() {
  const el = document.getElementById('presets');
  const templates = ROLES.filter((r) => !r.is_admin);
  el.innerHTML = templates.map((r) =>
    `<button type="button" class="preset" data-pages='${esc(JSON.stringify(r.pages || []))}'>${esc(r.label)}</button>`
  ).join('') + `<button type="button" class="preset" data-pages="[]">Очистить</button>`;
  el.querySelectorAll('.preset').forEach((btn) => {
    btn.onclick = () => {
      document.getElementById('f-admin').checked = false;
      syncAdminUi();
      try { setPages(JSON.parse(btn.dataset.pages || '[]')); }
      catch (_) { setPages([]); }
    };
  });
}

function syncAdminUi() {
  const isAdmin = document.getElementById('f-admin').checked;
  document.getElementById('perms-wrap').classList.toggle('disabled', isAdmin);
}

function renderUsers() {
  const q = (document.getElementById('user-q').value || '').trim().toLowerCase();
  const list = USERS.filter((u) => !q || u.username.toLowerCase().includes(q));
  document.getElementById('user-list').innerHTML = list.map((u) => `
    <button type="button" class="user-item ${editingId === u.id ? 'active' : ''}" data-id="${u.id}">
      <div class="name">${esc(u.username)}</div>
      <div class="meta">
        <span class="pill ${u.is_admin ? 'pill-admin' : 'pill-ok'}">${u.is_admin ? 'админ' : `${(u.pages || []).length} доступов`}</span>
        <span class="pill ${u.is_active ? 'pill-ok' : 'pill-off'}">${u.is_active ? 'активен' : 'выкл'}</span>
        ${u.must_change_password ? '<span class="pill pill-warn">одноразовый пароль</span>' : ''}
      </div>
    </button>
  `).join('') || '<div style="padding:20px;color:#64748b;font-size:13px">Пользователей нет</div>';

  document.querySelectorAll('.user-item').forEach((btn) => {
    btn.onclick = () => openEditor(Number(btn.dataset.id));
  });
}

async function loadUsers() {
  const { users } = await api('/api/admin/users');
  USERS = users;
  renderUsers();
}

async function loadMeta() {
  const [{ pages }, { roles }] = await Promise.all([
    api('/api/admin/pages'),
    api('/api/admin/roles'),
  ]);
  PAGES = pages;
  ROLES = roles;
}

function showTemp(title, username, temp) {
  let secret = String(temp || '');
  openModal(`
    <h3>${esc(title)}</h3>
    <p class="sub">Одноразовый пароль показывается один раз. Скопируйте и закройте — из DOM удалится.</p>
    <div style="font-size:11px;font-weight:800;color:#64748b;text-transform:uppercase;letter-spacing:.04em">Логин</div>
    <code>${esc(username)}</code>
    <div style="font-size:11px;font-weight:800;color:#64748b;text-transform:uppercase;letter-spacing:.04em">Пароль</div>
    <code id="tmp-pass">${esc(secret)}</code>
    <div class="temp-actions">
      <button class="btn primary" type="button" id="copy-pass">Копировать пароль</button>
      <button class="btn ghost" type="button" id="copy-both">Логин + пароль</button>
    </div>
    <div class="row-actions">
      <button class="btn primary" type="button" id="tmp-ok">Готово</button>
    </div>
  `);
  const wipe = () => {
    secret = '';
    const el = document.getElementById('tmp-pass');
    if (el) el.textContent = '••••••••';
    closeModal();
  };
  document.getElementById('tmp-ok').onclick = wipe;
  document.getElementById('copy-pass').onclick = async () => {
    if (!secret) return;
    await navigator.clipboard.writeText(secret);
    showMsg('Пароль скопирован', 'ok');
  };
  document.getElementById('copy-both').onclick = async () => {
    if (!secret) return;
    await navigator.clipboard.writeText(`${username}\n${secret}`);
    showMsg('Скопировано', 'ok');
  };
}

function closeEditor() {
  editingId = null;
  document.getElementById('editor-form').hidden = true;
  document.getElementById('editor-empty').hidden = false;
  renderUsers();
}

function openCreate() {
  editingId = 'new';
  document.getElementById('editor-empty').hidden = true;
  document.getElementById('editor-form').hidden = false;
  document.getElementById('editor-mode').textContent = 'Новый пользователь';
  document.getElementById('editor-title').textContent = 'Логин и видимость сразу';
  document.getElementById('f-username').value = '';
  document.getElementById('f-username').disabled = false;
  document.getElementById('f-active').checked = true;
  document.getElementById('f-admin').checked = false;
  document.getElementById('btn-reset').hidden = true;
  document.getElementById('btn-delete').hidden = true;
  document.getElementById('perm-tree').innerHTML = buildPermTree([]);
  wirePermTree(document.getElementById('perm-tree'));
  renderPresets();
  syncAdminUi();
  renderUsers();
}

function openEditor(id) {
  const u = USERS.find((x) => x.id === id);
  if (!u) return;
  editingId = id;
  document.getElementById('editor-empty').hidden = true;
  document.getElementById('editor-form').hidden = false;
  document.getElementById('editor-mode').textContent = 'Редактирование';
  document.getElementById('editor-title').textContent = u.username;
  document.getElementById('f-username').value = u.username;
  document.getElementById('f-username').disabled = true;
  document.getElementById('f-active').checked = !!u.is_active;
  document.getElementById('f-admin').checked = !!u.is_admin;
  document.getElementById('btn-reset').hidden = false;
  document.getElementById('btn-delete').hidden = false;
  document.getElementById('perm-tree').innerHTML = buildPermTree(u.pages || []);
  wirePermTree(document.getElementById('perm-tree'));
  renderPresets();
  syncAdminUi();
  renderUsers();
}

document.getElementById('btn-new').onclick = openCreate;
document.getElementById('btn-new-2').onclick = openCreate;
document.getElementById('btn-cancel').onclick = closeEditor;
document.getElementById('f-admin').onchange = syncAdminUi;
document.getElementById('user-q').oninput = renderUsers;

document.getElementById('btn-reset').onclick = async () => {
  if (typeof editingId !== 'number') return;
  const u = USERS.find((x) => x.id === editingId);
  if (!u || !confirm(`Сбросить пароль для ${u.username}?`)) return;
  try {
    const user = await api(`/api/admin/users/${editingId}/reset-password`, { method: 'POST' });
    await loadUsers();
    openEditor(editingId);
    showTemp('Пароль сброшен', user.username, user.temporary_password);
  } catch (e) {
    showMsg(e.message, 'error');
  }
};

document.getElementById('btn-delete').onclick = async () => {
  if (typeof editingId !== 'number') return;
  const u = USERS.find((x) => x.id === editingId);
  if (!u || !confirm(`Удалить ${u.username}?`)) return;
  try {
    await api(`/api/admin/users/${editingId}`, { method: 'DELETE' });
    showMsg('Удалено', 'ok');
    closeEditor();
    await loadUsers();
  } catch (e) {
    showMsg(e.message, 'error');
  }
};

document.getElementById('editor-form').onsubmit = async (e) => {
  e.preventDefault();
  const username = document.getElementById('f-username').value.trim();
  const isAdmin = document.getElementById('f-admin').checked;
  const isActive = document.getElementById('f-active').checked;
  const pages = isAdmin ? [] : collectPages();
  if (!username) {
    showMsg('Введите логин', 'error');
    return;
  }
  if (!isAdmin && pages.length === 0) {
    if (!confirm('Не отмечена ни одна видимость. Создать пользователя без доступа к модулям?')) return;
  }
  const btn = document.getElementById('btn-save');
  btn.disabled = true;
  try {
    if (editingId === 'new') {
      const user = await api('/api/admin/users', {
        method: 'POST',
        body: JSON.stringify({ username, is_admin: isAdmin, pages, is_active: isActive }),
      });
      // is_active on create is always 1 in backend — patch if needed
      if (!isActive) {
        await api(`/api/admin/users/${user.id}`, {
          method: 'PATCH',
          body: JSON.stringify({ is_active: false }),
        });
      }
      await loadUsers();
      openEditor(user.id);
      showTemp('Пользователь создан', user.username, user.temporary_password);
    } else if (typeof editingId === 'number') {
      await api(`/api/admin/users/${editingId}`, {
        method: 'PATCH',
        body: JSON.stringify({
          is_admin: isAdmin,
          is_active: isActive,
          pages: isAdmin ? null : pages,
        }),
      });
      showMsg('Сохранено', 'ok');
      await loadUsers();
      openEditor(editingId);
    }
  } catch (err) {
    showMsg(err.message, 'error');
  } finally {
    btn.disabled = false;
  }
};

(async function boot() {
  await loadMe();
  await loadMeta();
  await loadUsers();
})();
