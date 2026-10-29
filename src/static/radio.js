(() => {
  const player = document.getElementById('player');
  const button = document.getElementById('listen');
  const status = document.getElementById('playback-status');
  let listening = false;
  let session = null;
  let events = Promise.resolve();
  let starting = false;

  async function json(url) {
    const response = await fetch(url, {cache: 'no-store'});
    if (!response.ok) throw new Error('unavailable');
    return response.json();
  }
  const position = () => Number.isFinite(player.currentTime) ? player.currentTime : 0;
  function event(action) {
    const at = position();
    events = events.then(async () => {
      if (action !== 'start' && !session) return;
      try {
        const response = await fetch('/api/listening', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({action, session_id: session, position: at}),
          keepalive: action === 'stop',
        });
        if (!response.ok) return;
        const result = await response.json();
        session = action === 'stop' ? null : result.session_id;
      } catch (_) { /* Statistics must never interrupt playback. */ }
    });
  }
  function stopCounting() {
    if (listening) { listening = false; event('stop'); }
  }
  button.addEventListener('click', async () => {
    if (starting) return;
    if (!player.paused) { player.pause(); return; }
    starting = true;
    status.textContent = 'Подключение к эфиру…';
    // A fresh connection joins live radio instead of replaying a paused buffer.
    player.src = '/stream';
    try { await player.play(); }
    catch (_) { status.textContent = 'Эфир пока недоступен. Попробуйте ещё раз.'; }
    finally { starting = false; }
  });
  player.addEventListener('playing', () => {
    status.textContent = 'В эфире';
    button.textContent = 'Пауза';
    if (!listening) { listening = true; event('start'); }
  });
  player.addEventListener('pause', () => {
    stopCounting();
    button.textContent = 'Слушать эфир';
    status.textContent = 'Пауза';
    // Stop downloading while paused; the button creates a new live connection.
    if (player.hasAttribute('src')) { player.removeAttribute('src'); player.load(); }
  });
  player.addEventListener('waiting', () => { stopCounting(); status.textContent = 'Буферизация…'; });
  player.addEventListener('ended', () => { stopCounting(); button.textContent = 'Слушать эфир'; });
  player.addEventListener('error', () => {
    stopCounting();
    status.textContent = 'Связь с эфиром прервалась. Нажмите «Слушать эфир».';
    button.textContent = 'Слушать эфир';
  });
  setInterval(() => { if (listening && !player.paused) event('heartbeat'); }, 10000);
  window.addEventListener('pagehide', () => {
    if (session) navigator.sendBeacon('/api/listening', new Blob([
      JSON.stringify({action: 'stop', session_id: session, position: position()}),
    ], {type: 'application/json'}));
  });

  async function poll() {
    try {
      const current = await json('/api/now-playing');
      document.getElementById('now-playing').textContent = [current.artist, current.title].filter(Boolean).join(' — ');
      document.getElementById('album').textContent = current.album || '—';
    } catch (_) { document.getElementById('now-playing').textContent = 'Эфир временно недоступен'; }
    const queue = document.getElementById('queue');
    try {
      const data = await json('/api/queue?count=5');
      queue.replaceChildren(...data.tracks.map(track => {
        const li = document.createElement('li');
        li.textContent = `${track.artist} — ${track.title} · ${track.album}`;
        return li;
      }));
      if (!data.tracks.length) queue.textContent = 'Очередь пока пуста';
    } catch (_) { queue.textContent = 'Очередь временно недоступна'; }
  }
  async function schedule() {
    const container = document.getElementById('schedule');
    try {
      const data = await json('/api/schedule');
      document.getElementById('schedule-date').textContent = `${data.date} · ${data.timezone}`;
      const format = iso => new Date(iso).toLocaleTimeString('ru-RU', {
        timeZone: data.timezone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
      });
      container.replaceChildren(...data.slots.map(slot => {
        const row = document.createElement('div');
        row.className = 'album-slot';
        if (Date.now() >= Date.parse(slot.start_at) && Date.now() < Date.parse(slot.end_at)) row.classList.add('active');
        row.textContent = `${format(slot.start_at)}–${format(slot.end_at)} · ${slot.artist} — ${slot.album} (${slot.tracks} тр.)`;
        return row;
      }));
    } catch (_) { container.textContent = 'Расписание временно недоступно'; }
  }
  poll(); schedule();
  setInterval(poll, 5000);
  setInterval(schedule, 60000);
})();
