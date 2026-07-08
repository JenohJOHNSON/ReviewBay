/* ReviewBay: per-browser history of the brands THIS visitor has added.
 *
 * Kept in localStorage, never on the server, so a fresh browser (say, an
 * interviewer opening the link) starts with a clean slate, while you keep your
 * own list on your own machine. No accounts, no login, for now.
 *
 * Every page reads its brand list from here instead of a global server list.
 */
(function () {
  const KEY = 'reviewbay.brands.v3';

  function read() {
    try {
      const raw = JSON.parse(localStorage.getItem(KEY) || '[]');
      return Array.isArray(raw) ? raw.filter((x) => typeof x === 'string' && x.trim()) : [];
    } catch (e) {
      return [];
    }
  }

  function write(list) {
    try { localStorage.setItem(KEY, JSON.stringify(list)); } catch (e) {}
  }

  window.RB_Brands = {
    /** Most-recent-first list of brand names this browser has added. */
    list() { return read(); },

    /** Add (or bump to front) a brand; case-insensitive de-dupe. Returns the new list. */
    add(name) {
      name = (name || '').trim();
      if (!name) return read();
      const lower = name.toLowerCase();
      const next = [name, ...read().filter((b) => b.toLowerCase() !== lower)];
      write(next);
      return next;
    },

    /** Forget a brand (removes it from this browser only). Returns the new list. */
    remove(name) {
      const lower = (name || '').trim().toLowerCase();
      const next = read().filter((b) => b.toLowerCase() !== lower);
      write(next);
      return next;
    },

    /** Clear this browser's remembered brands. */
    clear() {
      write([]);
      return [];
    },

    has(name) {
      const lower = (name || '').trim().toLowerCase();
      return read().some((b) => b.toLowerCase() === lower);
    },

    /** Fill a <select> with the remembered brands. Returns the count added. */
    fillSelect(sel, { chosen = '', placeholder = null } = {}) {
      if (!sel) return 0;
      const brands = read();
      sel.innerHTML = placeholder ? `<option value="">${placeholder}</option>` : '';
      brands.forEach((b) => {
        const o = document.createElement('option');
        o.value = b; o.textContent = b;
        if (b.toLowerCase() === (chosen || '').toLowerCase()) o.selected = true;
        sel.appendChild(o);
      });
      return brands.length;
    },
  };
})();
