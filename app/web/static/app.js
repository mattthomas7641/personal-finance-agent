// Page behaviour that used to live in inline onclick/onchange attributes. Keeping it in one file lets the
// Content-Security-Policy refuse every inline handler, so injected markup can't run script.
(() => {
  // Offline copy from the service worker: show it read-only.
  if (document.body.dataset.offline) {
    document.querySelectorAll("form button, form input, form select, form textarea, [hx-post]").forEach(el => el.disabled = true);
    document.querySelectorAll("[hx-get][hx-trigger*=every]").forEach(el => el.removeAttribute("hx-trigger"));
  }
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");

  document.addEventListener("change", e => {
    const el = e.target.closest("[data-autosubmit]");
    if (el && el.form) el.form.requestSubmit();
  });

  document.addEventListener("submit", e => {
    const form = e.target;
    const msg = form.dataset.confirm || e.submitter?.dataset.confirm;
    if (msg && !confirm(msg)) e.preventDefault();
  }, true);

  document.addEventListener("click", e => {
    const t = e.target;
    const open = t.closest("[data-open]");
    if (open) { const d = document.getElementById(open.dataset.open); if (d) d.open = true; }
    if (t.closest("[data-close-menu]")) { const m = document.getElementById("menu"); if (m) m.open = false; }
    const row = t.closest("[data-href]");
    if (row && !t.closest("a, button, input, select, textarea, label, summary")) location.href = row.dataset.href;
    const rc = t.closest("[data-reconnect]");
    if (rc && window.reconnect) window.reconnect(rc.dataset.reconnect);
  });
})();
