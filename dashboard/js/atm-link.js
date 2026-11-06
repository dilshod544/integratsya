// Ссылка на карточку банкомата: единый способ для всех страниц дашборда.
// У банкомата три идентификатора, поэтому ссылка указывает, какой из них передан:
//   id       — внутренний ID BTech (точнее всего: TID в BTech бывает не уникален);
//   tid      — TID из BTech;
//   terminal — terminal_id реестра, сервер сопоставит его с BTech так же, как карта.
(function () {
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function atmHref(ref) {
    ref = ref || {};
    const key = ref.id != null && ref.id !== '' ? 'id' : ref.tid ? 'tid' : ref.terminal ? 'terminal' : null;
    return key ? `/dashboard/atm-card.html?${key}=${encodeURIComponent(ref[key])}` : null;
  }

  // label — обычный текст, экранируется здесь. stopPropagation: многие строки и карточки сами кликабельны.
  function atmLink(label, ref) {
    const href = atmHref(ref);
    if (!href) return esc(label);
    return `<a class="atm-link" href="${esc(href)}" onclick="event.stopPropagation()" title="Открыть страницу банкомата">${esc(label)}</a>`;
  }

  const style = document.createElement('style');
  style.textContent = 'a.atm-link{color:#2563eb;text-decoration:none;font-weight:inherit}a.atm-link:hover{text-decoration:underline}';
  document.head.appendChild(style);

  window.atmHref = atmHref;
  window.atmLink = atmLink;
})();
