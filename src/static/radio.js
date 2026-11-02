(() => {
  const player = document.getElementById('player');
  const status = document.getElementById('playback-status');
  let listening = false;
  let session = null;
  let events = Promise.resolve();

  const mediaSession = navigator.mediaSession;
  function playbackState(value) {
    if (mediaSession) {
      try { mediaSession.playbackState = value; } catch (_) { /* Optional browser API. */ }
    }
  }
  if (mediaSession) {
    const actions = {
      play: async () => {
        try { await player.play(); }
        catch (_) { status.textContent = 'Эфир временно недоступен.'; }
      },
      pause: () => player.pause(),
      stop: () => player.pause(),
      seekbackward: null, seekforward: null, seekto: null,
      previoustrack: null, nexttrack: null,
    };
    for (const [action, handler] of Object.entries(actions)) {
      try { mediaSession.setActionHandler(action, handler); } catch (_) { /* Unsupported action. */ }
    }
  }
  function updateMedia(current) {
    if (!mediaSession || typeof MediaMetadata === 'undefined') return;
    try {
      mediaSession.metadata = new MediaMetadata({
        title: current.title || 'Радио Юрец',
        artist: current.artist || 'Радио Юрец',
        album: current.album || '',
        artwork: current.artwork_url ? [{src: new URL(current.artwork_url, window.location.href).href}] : [],
      });
    } catch (_) { /* Metadata must never interrupt playback. */ }
  }

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
    playbackState('playing');
    if (!listening) { listening = true; event('start'); }
  });
  player.addEventListener('pause', () => {
    if (!player.paused) return;
    stopCounting();
    status.textContent = 'Пауза';
    playbackState('paused');
    // Keep src so native Play stays available. preload=none releases the current
    // request on load(); the next Play connects to live radio with no old buffer.
    player.load();
  });
  player.addEventListener('waiting', () => { stopCounting(); status.textContent = 'Буферизация…'; });
  player.addEventListener('ended', () => {
    stopCounting();
    player.load();
    status.textContent = 'Эфир остановлен.';
    playbackState('none');
  });
  player.addEventListener('error', () => {
    stopCounting();
    player.load();
    status.textContent = 'Эфир временно недоступен.';
    playbackState('none');
  });
  setInterval(() => { if (listening && !player.paused) event('heartbeat'); }, 10000);
  window.addEventListener('pagehide', () => {
    if (session) navigator.sendBeacon('/api/listening', new Blob([
      JSON.stringify({action: 'stop', session_id: session, position: position()}),
    ], {type: 'application/json'}));
  });

  const tooltip = document.getElementById('cell-tooltip');
  function hideTooltip() { tooltip.hidden = true; }
  function showTooltip(cell) {
    hideTooltip();
    if (cell.scrollWidth <= cell.clientWidth) return;
    tooltip.textContent = cell.textContent;
    tooltip.style.left = '12px';
    tooltip.style.top = '0px';
    tooltip.hidden = false;
    const cellBox = cell.getBoundingClientRect();
    const tipBox = tooltip.getBoundingClientRect();
    tooltip.style.left = `${Math.max(12, Math.min(cellBox.left, window.innerWidth - tipBox.width - 12))}px`;
    const below = cellBox.bottom + 6;
    tooltip.style.top = `${below + tipBox.height <= window.innerHeight - 12
      ? below : Math.max(12, cellBox.top - tipBox.height - 6)}px`;
  }
  window.addEventListener('scroll', hideTooltip, true);
  window.addEventListener('resize', hideTooltip);
  window.addEventListener('blur', hideTooltip);
  document.addEventListener('keydown', event => { if (event.key === 'Escape') hideTooltip(); });

  function row(values, readable = []) {
    const tr = document.createElement('tr');
    for (const [index, value] of values.entries()) {
      const td = document.createElement('td');
      td.textContent = String(value ?? '—');
      if (readable.includes(index) && value) {
        td.addEventListener('mouseenter', () => showTooltip(td));
        td.addEventListener('mouseleave', hideTooltip);
      }
      tr.append(td);
    }
    return tr;
  }
  function message(body, text, columns) {
    const tr = row([text]);
    tr.firstElementChild.colSpan = columns;
    body.replaceChildren(tr);
  }
  let queueSignature = null;
  async function poll() {
    try {
      const current = await json('/api/now-playing');
      document.getElementById('now-playing').textContent = [current.artist, current.title].filter(Boolean).join(' — ');
      document.getElementById('album').textContent = current.album || '—';
      updateMedia(current);
    } catch (_) { document.getElementById('now-playing').textContent = 'Эфир временно недоступен'; }
    const queue = document.getElementById('queue');
    try {
      const data = await json('/api/queue?count=5');
      const signature = JSON.stringify(data.tracks);
      if (signature !== queueSignature) {
        hideTooltip();
        queue.replaceChildren(...data.tracks.map(track => row([track.artist, track.title, track.album], [0, 1, 2])));
        if (!data.tracks.length) message(queue, 'Очередь пока пуста', 3);
        queueSignature = signature;
      }
    } catch (_) {
      hideTooltip();
      queueSignature = null;
      message(queue, 'Очередь временно недоступна', 3);
    }
  }
  let requestedDay = null;
  let pendingDay = null;
  let scheduleRequest = 0;
  function deviceDay() {
    const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
    const date = new Date().toLocaleDateString('ru-RU', {timeZone});
    return {timeZone, date, key: `${timeZone}|${date}`};
  }
  function renderSchedule(data) {
    hideTooltip();
    const container = document.getElementById('schedule');
    const timeZone = data.timezone;
    const format = iso => new Date(iso).toLocaleTimeString('ru-RU', {
      timeZone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    });
    document.getElementById('schedule-date').textContent = `${data.date.split('-').reverse().join('.')} · ${timeZone}`;
    container.replaceChildren(...data.slots.map(slot => {
      const start = Date.parse(slot.start_at) === Date.parse(data.start_at) ? '00:00' : format(slot.start_at);
      const end = Date.parse(slot.end_at) === Date.parse(data.end_at) ? '24:00' : format(slot.end_at);
      const tr = row([`${start}–${end}`, slot.artist, slot.album, slot.tracks], [1, 2]);
      if (Date.now() >= Date.parse(slot.start_at) && Date.now() < Date.parse(slot.end_at)) tr.classList.add('active');
      return tr;
    }));
    if (!data.slots.length) message(container, 'Расписание пока пусто', 4);
  }
  async function schedule() {
    const day = deviceDay();
    if (pendingDay === day.key) return;
    const request = ++scheduleRequest;
    pendingDay = day.key;
    if (requestedDay !== day.key) {
      hideTooltip();
      document.getElementById('schedule-date').textContent = `${day.date} · ${day.timeZone}`;
      message(document.getElementById('schedule'), 'Загрузка…', 4);
    }
    requestedDay = day.key;
    try {
      const data = await json(`/api/schedule?timezone=${encodeURIComponent(day.timeZone)}`);
      // A slow response from the previous timezone/day must not replace the new one.
      if (request === scheduleRequest && day.key === deviceDay().key) renderSchedule(data);
    } catch (_) {
      if (request === scheduleRequest && day.key === deviceDay().key) {
        message(document.getElementById('schedule'), 'Расписание временно недоступно', 4);
      }
    } finally {
      if (request === scheduleRequest) pendingDay = null;
    }
  }
  function refreshTimezone() {
    if (deviceDay().key !== requestedDay) return schedule();
  }
  window.addEventListener('focus', schedule);
  window.addEventListener('pageshow', refreshTimezone);
  document.addEventListener('visibilitychange', refreshTimezone);
  setInterval(refreshTimezone, 1000);
  poll(); schedule();
  setInterval(poll, 5000);
  setInterval(schedule, 60000);
})();
