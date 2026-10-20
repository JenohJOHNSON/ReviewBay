/* ReviewBay: the brands in YOUR ACCOUNT, shared by every page.
 *
 * Brands used to live in this browser's localStorage, so a brand you searched
 * existed only on the browser you searched it from. They now belong to your
 * account on the server: the same list from any browser, and after any deploy.
 *
 * The list loads once per page. Call `await RB_Brands.ready()` before reading
 * it; after that `list()`, `has()` and `fillSelect()` are synchronous.
 */
(function () {
  // The pre-accounts localStorage key, read only to offer importing it.
  const LEGACY_KEY = 'reviewbay.brands.v3';
  let cache = null;
  let pending = null;

  async function load() {
    const r = await fetch('/api/brands');
    if (!r.ok) throw new Error('could not load your brands (' + r.status + ')');
    const j = await r.json();
    cache = Array.isArray(j.brands) ? j.brands.filter((b) => typeof b === 'string' && b.trim()) : [];
    return cache;
  }

  function lower(s) { return (s || '').trim().toLowerCase(); }

  window.RB_Brands = {
    /** Resolves to the account's brand names, newest first. Loads once. */
    ready() {
      if (!pending) {
        pending = load().catch(() => { pending = null; cache = cache || []; return cache; });
      }
      return pending;
    },

    /** Re-read the list from the server (after adding or deleting). */
    refresh() { pending = null; return this.ready(); },

    list() { return (cache || []).slice(); },

    has(name) { return this.canonical(name) !== null; },

    /** The account's spelling of `name`, or null if it is not one of yours. */
    canonical(name) {
      const key = lower(name);
      return this.list().find((b) => lower(b) === key) || null;
    },

    /** Fill a <select> with the account's brands. Returns the count added. */
    fillSelect(sel, { chosen = '', placeholder = null } = {}) {
      if (!sel) return 0;
      const brands = this.list();
      sel.innerHTML = '';
      if (placeholder) {
        const o = document.createElement('option');
        o.value = ''; o.textContent = placeholder;
        sel.appendChild(o);
      }
      brands.forEach((b) => {
        const o = document.createElement('option');
        o.value = b; o.textContent = b;
        if (lower(b) === lower(chosen)) o.selected = true;
        sel.appendChild(o);
      });
      return brands.length;
    },

    /** Brands this browser remembered before accounts existed, not yet imported. */
    legacy() {
      try {
        const raw = JSON.parse(localStorage.getItem(LEGACY_KEY) || '[]');
        const mine = new Set(this.list().map(lower));
        return Array.isArray(raw)
          ? raw.filter((x) => typeof x === 'string' && x.trim() && !mine.has(lower(x)))
          : [];
      } catch (e) {
        return [];
      }
    },

    clearLegacy() {
      try { localStorage.removeItem(LEGACY_KEY); } catch (e) {}
    },
  };
})();
