/** ИИ-помощник страницы «Экономия».
 *  Модель видит только обезличенный текст и плейсхолдеры {{ключ}};
 *  реальные значения приходят с нашего сервера в `values` и подставляются здесь. */
(function () {
  'use strict';
  const API = location.origin;
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const icon = (name) => `<i data-lucide="${name}"></i>`;
  const days = () => (document.getElementById('days') || {}).value || 90;
  const SUGGEST = [
    'Почему системе хватает меньше наличных?',
    'Что нужно задать, чтобы увидеть экономию в сумах?',
    'Как считается простой без денег?',
    'Где больше всего лишних денег?',
  ];

  const root = document.createElement('div');
  root.className = 'ai-root';
  root.innerHTML = `
    <button class="ai-fab" type="button" aria-label="ИИ-помощник" aria-expanded="false" aria-controls="ai-panel">
      <span class="ai-ico ai-ico-open">${icon('sparkles')}</span><span class="ai-ico ai-ico-close">${icon('x')}</span>
    </button>
    <div class="ai-fab-tip">Спросить ИИ про экономию</div>
    <section class="ai-panel" id="ai-panel" role="dialog" aria-label="ИИ-помощник">
      <div class="ai-head">
        <div class="ai-avatar">${icon('sparkles')}</div>
        <div><div class="ai-title">ИИ-помощник</div><div class="ai-sub">GLM 5.3 Flash · объясняет экономию</div></div>
        <div class="ai-sp"></div>
        <button class="ai-icon-btn" type="button" data-act="reset" title="Новый диалог">${icon('rotate-ccw')}</button>
        <button class="ai-icon-btn" type="button" data-act="close" title="Закрыть (Esc)">${icon('chevron-down')}</button>
      </div>
      <details class="ai-privacy">
        <summary>${icon('shield-check')} Цифры и названия не уходят в ИИ ${icon('chevron-down').replace('<i ', '<i class="ai-chev" ')}</summary>
        <ul>
          <li>ИИ не получает ни одной цифры, TID, адреса или имени — только описание методики.</li>
          <li>Он пишет ответ с метками вроде <b>{{экономия}}</b>, а значения подставляются здесь, на вашей стороне. Они выделены <span class="ai-val">так</span>.</li>
          <li>Числа и названия в ваших вопросах заменяются на [число] и [скрыто] — под каждым вопросом можно посмотреть, что именно ушло.</li>
          <li>Запросы идут только к провайдерам, которые не хранят и не собирают данные.</li>
        </ul>
      </details>
      <div class="ai-feed" aria-live="polite"></div>
      <div class="ai-chips"></div>
      <div class="ai-input">
        <div class="ai-box">
          <textarea rows="1" placeholder="Спросите про экономию…" aria-label="Вопрос"></textarea>
          <button class="ai-send" type="button" aria-label="Отправить" disabled>${icon('arrow-up')}</button>
        </div>
        <div class="ai-hint"><span>Enter — отправить, Shift+Enter — новая строка</span></div>
      </div>
    </section>`;
  document.body.appendChild(root);

  const $ = (s) => root.querySelector(s);
  const fab = $('.ai-fab'), feed = $('.ai-feed'), chips = $('.ai-chips'), ta = $('textarea'), send = $('.ai-send');
  let history = [];          // {role, content}: вопросы как есть, ответы — с плейсхолдерами
  let explained = false, busy = false, explainedDays = null;

  const icons = () => window.lucide && lucide.createIcons();
  const scroll = () => { feed.scrollTop = feed.scrollHeight; };

  // Мини-markdown: абзацы, списки «- », **жирный**; плейсхолдеры → значения.
  function render(raw, values) {
    const fill = (s) => esc(s)
      .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
      .replace(/\{\{\s*([a-z0-9_]+)\s*\}\}/g, (_, k) => {
        const v = values && values[k];
        if (v == null) return '<span class="ai-val empty">—</span>';
        const empty = /^(нет данных|не посчитано|пока нет)/.test(v);
        return `<span class="ai-val${empty ? ' empty' : ''}" title="Подставлено локально — в ИИ не отправлялось">${esc(v)}</span>`;
      });
    const isItem = (l) => /^\s*[-•*]\s+/.test(l);
    return raw.trim().split(/\n{2,}/).map((block) => {
      // Подряд идущие строки «- …» — список, остальные — абзац с переносами.
      const out = [];
      let text = [], items = [];
      const flush = () => {
        if (text.length) out.push(`<p>${text.map(fill).join('<br>')}</p>`);
        if (items.length) out.push(`<ul>${items.map((l) => `<li>${fill(l.replace(/^\s*[-•*]\s+/, ''))}</li>`).join('')}</ul>`);
        text = []; items = [];
      };
      block.split('\n').forEach((l) => {
        if (isItem(l)) { if (text.length) { const t = items; items = []; flush(); items = t; } items.push(l); }
        else { if (items.length) flush(); text.push(l); }
      });
      flush();
      return out.join('');
    }).join('');
  }

  function add(html, cls) {
    const el = document.createElement('div');
    el.className = cls;
    el.innerHTML = html;
    feed.appendChild(el);
    icons();
    scroll();
    return el;
  }

  function showChips() {
    chips.innerHTML = history.some((m) => m.role === 'user' && !m.synthetic) ? ''
      : SUGGEST.map((q) => `<button class="ai-chip" type="button">${esc(q)}</button>`).join('');
    chips.querySelectorAll('.ai-chip').forEach((b) => { b.onclick = () => ask(b.textContent); });
  }

  async function call(body) {
    const r = await fetch(`${API}/api/savings/ai`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ days: +days(), ...body }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || 'ИИ сейчас недоступен');
    return data;
  }

  function error(msg, retry) {
    const el = add(`${icon('circle-alert')}<div>${esc(msg)}${retry ? '<br><button type="button">Повторить</button>' : ''}</div>`, 'ai-err');
    if (retry) el.querySelector('button').onclick = () => { el.remove(); retry(); };
  }

  function setBusy(v) { busy = v; send.disabled = v || !ta.value.trim(); }

  async function explain() {
    setBusy(true);
    const sk = add(`<div class="ai-cap">${icon('loader-circle')} Считаю экономию…</div><span></span><span></span><span></span>`, 'ai-skel');
    try {
      const d = await call({ explain: true });
      sk.remove();
      add(`<div class="ai-cap">${icon('piggy-bank')} Сколько экономит система</div>${render(d.raw, d.values)}`, 'ai-msg bot explain');
      history.push({ role: 'user', content: 'Объясни, сколько экономит система.', synthetic: true }, { role: 'assistant', content: d.raw });
      explained = true;
      explainedDays = days();
    } catch (e) {
      sk.remove();
      error(e.message, explain);
    } finally {
      setBusy(false);
      showChips();
    }
  }

  async function ask(text) {
    text = (text || '').trim();
    if (!text || busy) return;
    ta.value = '';
    grow();
    chips.innerHTML = '';
    add(esc(text), 'ai-msg me');
    const sent = document.createElement('details');
    sent.className = 'ai-sent';
    history.push({ role: 'user', content: text });
    setBusy(true);
    const typing = add('<i></i><i></i><i></i>', 'ai-typing');
    try {
      const d = await call({ messages: history.map(({ role, content }) => ({ role, content })) });
      typing.remove();
      const shown = (d.sent || []).slice(-1)[0];
      if (shown != null) {
        sent.innerHTML = `<summary>что ушло в ИИ</summary><div>${esc(shown)}</div>`;
        feed.appendChild(sent);
      }
      add(render(d.raw, d.values), 'ai-msg bot');
      history.push({ role: 'assistant', content: d.raw });
    } catch (e) {
      typing.remove();
      history.pop();
      error(e.message, () => ask(text));
    } finally {
      setBusy(false);
      ta.focus();
    }
  }

  function reset() {
    history = [];
    explained = false;
    feed.innerHTML = '';
    chips.innerHTML = '';
    explain();
  }

  function open(v) {
    root.classList.toggle('open', v);
    fab.setAttribute('aria-expanded', String(v));
    if (v) {
      root.classList.add('seen');
      try { localStorage.setItem('ai-seen', '1'); } catch (_) { /* нет хранилища */ }
      if (!explained && !busy) explain();
      setTimeout(() => ta.focus(), 250);
    }
  }

  function grow() { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 120) + 'px'; }

  fab.onclick = () => open(!root.classList.contains('open'));
  $('[data-act="close"]').onclick = () => open(false);
  $('[data-act="reset"]').onclick = () => { if (!busy) reset(); };
  ta.addEventListener('input', () => { grow(); send.disabled = busy || !ta.value.trim(); });
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(ta.value); }
  });
  send.onclick = () => ask(ta.value);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && root.classList.contains('open')) open(false); });

  // Сменили период — предложить обновить объяснение.
  const sel = document.getElementById('days');
  if (sel) sel.addEventListener('change', () => {
    if (!explained || feed.querySelector('.ai-stale') || String(explainedDays) === String(days())) return;
    const b = add(`${icon('refresh-cw')} Период изменён — обновить объяснение`, 'ai-stale');
    b.onclick = () => { if (!busy) reset(); };
  });

  try { if (localStorage.getItem('ai-seen')) root.classList.add('seen'); } catch (_) { /* нет хранилища */ }
  icons();
})();
