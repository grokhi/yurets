(() => {
  const token = document.querySelector('meta[name="visit-token"]')?.content;
  if (!token) return;
  let counted = false;
  let pending = false;
  let attempts = 0;
  let played = false;
  async function report() {
    if (counted || pending || (!played && document.visibilityState !== 'visible')) return;
    pending = true;
    attempts++;
    try {
      const response = await fetch('/api/visit', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({token}), keepalive: true,
      });
      if (response.ok) counted = true; // Idempotent, including rejected automation.
    } catch (_) { /* Measurement must not interrupt the page or player. */ }
    finally { pending = false; }
    if (!counted && attempts < 3) setTimeout(report, attempts * 2000);
  }
  document.addEventListener('visibilitychange', report);
  window.addEventListener('pageshow', report);
  window.addEventListener('online', report);
  document.getElementById('player')?.addEventListener('playing', () => { played = true; report(); });
  report();
})();
