(() => {
  const player = document.getElementById('player');
  const status = document.getElementById('playback-status');
  let listening = false;
  let session = null;
  let events = Promise.resolve();

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
  player.addEventListener('play', () => {
    status.textContent = 'Подключение к эфиру…';
  });
  player.addEventListener('playing', () => {
    if (player.paused) return;
    status.textContent = 'В эфире';
    if (!listening) { listening = true; event('start'); }
  });
  player.addEventListener('pause', () => {
    if (!player.paused) return;
    stopCounting();
    status.textContent = 'Пауза';
    // Keep src so native Play stays available. preload=none releases the current
    // request on load(); the next Play connects to live radio with no old buffer.
    player.load();
  });
  player.addEventListener('waiting', () => { stopCounting(); status.textContent = 'Буферизация…'; });
  player.addEventListener('ended', () => {
    stopCounting();
    player.load();
    status.textContent = 'Эфир остановлен. Нажмите ▶ в плеере.';
  });
  player.addEventListener('error', () => {
    stopCounting();
    player.load();
    status.textContent = 'Эфир недоступен. Нажмите ▶ в плеере, чтобы повторить.';
  });
  setInterval(() => { if (listening && !player.paused) event('heartbeat'); }, 10000);
  window.addEventListener('pagehide', () => {
    if (session) navigator.sendBeacon('/api/listening', new Blob([
      JSON.stringify({action: 'stop', session_id: session, position: position()}),
    ], {type: 'application/json'}));
  });

  function row(values) {
    const tr = document.createElement('tr');
    for (const value of values) {
      const td = document.createElement('td');
      td.textContent = String(value ?? '—');
      tr.append(td);
    }
    return tr;
  }
  function message(body, text, columns) {
    const tr = row([text]);
    tr.firstElementChild.colSpan = columns;
    body.replaceChildren(tr);
  }
  async function poll() {
    try {
      const current = await json('/api/now-playing');
      document.getElementById('now-playing').textContent = [current.artist, current.title].filter(Boolean).join(' — ');
      document.getElementById('album').textContent = current.album || '—';
    } catch (_) { document.getElementById('now-playing').textContent = 'Эфир временно недоступен'; }
    const queue = document.getElementById('queue');
    try {
      const data = await json('/api/queue?count=5');
      queue.replaceChildren(...data.tracks.map(track => row([track.artist, track.title, track.album])));
      if (!data.tracks.length) message(queue, 'Очередь пока пуста', 3);
    } catch (_) { message(queue, 'Очередь временно недоступна', 3); }
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
        const tr = row([`${format(slot.start_at)}–${format(slot.end_at)}`, slot.artist, slot.album, slot.tracks]);
        if (Date.now() >= Date.parse(slot.start_at) && Date.now() < Date.parse(slot.end_at)) tr.classList.add('active');
        return tr;
      }));
      if (!data.slots.length) message(container, 'Расписание пока пусто', 4);
    } catch (_) { message(container, 'Расписание временно недоступно', 4); }
  }
  poll(); schedule();
  setInterval(poll, 5000);
  setInterval(schedule, 60000);
})();
