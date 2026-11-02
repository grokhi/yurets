(() => {
  const sitekey = document.querySelector('meta[name="turnstile-site-key"]')?.content;
  const token = document.querySelector('meta[name="visit-token"]')?.content;
  if (!sitekey || !token) return;
  let started = false, reported = false;
  async function report(response = null) {
    if (reported) return;
    reported = true;
    try {
      await fetch('/api/observation', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({token, response}), keepalive: true,
      });
    } catch (_) { /* Optional observation never affects visits or playback. */ }
  }
  function start() {
    if (started || document.visibilityState !== 'visible') return;
    started = true;
    const script = document.createElement('script');
    script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
    script.async = true;
    script.onerror = () => report();
    script.onload = () => {
      try {
        const target = document.createElement('div');
        document.body.append(target);
        window.turnstile.render(target, {
          sitekey, action: 'audience_observe', cData: token.replace('.', '_'),
          'response-field': false, retry: 'never',
          callback: response => report(response),
          'error-callback': () => { report(); return true; },
          'expired-callback': () => report(),
          'timeout-callback': () => report(),
        });
      } catch (_) { report(); }
    };
    document.head.append(script);
    setTimeout(() => report(), 30000);
  }
  document.addEventListener('visibilitychange', start);
  start();
})();
