/* ReviewBay: the signed-in account, shared by every signed-in page.
 *
 * 1. A same-origin API call that comes back 401 means the session ended (it
 *    expired, or you signed out in another tab). Rather than leave a page of
 *    broken panels, go to the sign-in page and come back here afterwards.
 * 2. Shows who is signed in, with a sign-out button, at the end of the top bar.
 *
 * Load it before brands.js: brands.js fetches through the wrapper installed here.
 */
(function () {
  const realFetch = window.fetch.bind(window);

  function here() { return location.pathname + location.search; }

  window.fetch = async function (input, init) {
    const resp = await realFetch(input, init);
    if (resp.status === 401) {
      try {
        const url = new URL(typeof input === 'string' ? input : input.url, location.origin);
        if (url.origin === location.origin && !url.pathname.startsWith('/api/auth/')) {
          location.href = '/login?next=' + encodeURIComponent(here());
        }
      } catch (e) { /* not a URL we can reason about: leave it to the caller */ }
    }
    return resp;
  };

  let me = null;
  async function loadMe() {
    try {
      const r = await realFetch('/api/auth/me');
      if (r.ok) me = (await r.json()).user || null;
    } catch (e) { me = null; }
    return me;
  }

  async function renderMenu() {
    const bar = document.querySelector('.topbar');
    if (!bar || bar.querySelector('.acct')) return;
    const user = await loadMe();
    // The sign-in pages do not need a "Sign in" link to themselves.
    if (!user && (location.pathname === '/login' || location.pathname === '/signup')) return;
    const box = document.createElement('div');
    box.className = 'acct';
    if (user) {
      // textContent, never innerHTML: the address is user input.
      const who = document.createElement('span');
      who.className = 'acct-email';
      who.textContent = user.email;
      who.title = user.email;
      const out = document.createElement('button');
      out.className = 'acct-out';
      out.type = 'button';
      out.textContent = 'Sign out';
      out.addEventListener('click', async () => {
        out.disabled = true;
        try { await realFetch('/api/auth/logout', { method: 'POST' }); } catch (e) {}
        try { localStorage.removeItem('reviewbay.import.dismissed'); } catch (e) {}
        location.href = '/';
      });
      box.append(who, out);
    } else {
      const a = document.createElement('a');
      a.href = '/login';
      a.textContent = 'Sign in';
      box.append(a);
    }
    bar.appendChild(box);
  }

  window.RB_Account = {
    /** The signed-in user ({email, is_admin}) or null. */
    me() { return me ? Promise.resolve(me) : loadMe(); },
    /** A same-origin path to return to after signing in, or a safe default. */
    safeNext(value, fallback) {
      const v = typeof value === 'string' ? value : '';
      // Only same-site paths: "//evil.example" and "/\evil.example" are
      // protocol-relative redirects in some browsers.
      return v.startsWith('/') && !v.startsWith('//') && !v.includes('\\') ? v : (fallback || '/brands');
    },
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', renderMenu);
  else renderMenu();
})();
