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
      updateMedia(current);
    } catch (_) { document.getElementById('now-playing').textContent = 'Эфир временно недоступен'; }
    const queue = document.getElementById('queue');
    try {
      const data = await json('/api/queue?count=5');
      queue.replaceChildren(...data.tracks.map(track => row([track.artist, track.title, track.album])));
      if (!data.tracks.length) message(queue, 'Очередь пока пуста', 3);
    } catch (_) { message(queue, 'Очередь временно недоступна', 3); }
  }
  let scheduleData = null;
  let displayedTimeZone = null;
  function renderSchedule(data) {
    const container = document.getElementById('schedule');
    const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    displayedTimeZone = timeZone;
    const date = iso => new Date(iso).toLocaleDateString('ru-RU', {timeZone});
    const format = iso => new Date(iso).toLocaleTimeString('ru-RU', {
      timeZone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    });
    const first = data.slots[0], last = data.slots[data.slots.length - 1];
    const dates = first ? [...new Set([date(first.start_at), date(last.end_at)])].join(' — ') : '';
    document.getElementById('schedule-date').textContent = [dates, timeZone].filter(Boolean).join(' · ');
    container.replaceChildren(...data.slots.map(slot => {
      const startDate = date(slot.start_at), endDate = date(slot.end_at);
      const end = startDate === endDate ? format(slot.end_at) : `${endDate}, ${format(slot.end_at)}`;
      const tr = row([`${startDate}, ${format(slot.start_at)}–${end}`, slot.artist, slot.album, slot.tracks]);
      if (Date.now() >= Date.parse(slot.start_at) && Date.now() < Date.parse(slot.end_at)) tr.classList.add('active');
      return tr;
    }));
    if (!data.slots.length) message(container, 'Расписание пока пусто', 4);
  }
  async function schedule() {
    try {
      scheduleData = await json('/api/schedule');
      renderSchedule(scheduleData);
    } catch (_) { message(document.getElementById('schedule'), 'Расписание временно недоступно', 4); }
  }
  function refreshTimezone() {
    if (scheduleData && Intl.DateTimeFormat().resolvedOptions().timeZone !== displayedTimeZone) {
      renderSchedule(scheduleData);
    }
  }
  window.addEventListener('focus', refreshTimezone);
  window.addEventListener('pageshow', refreshTimezone);
  document.addEventListener('visibilitychange', refreshTimezone);
  setInterval(refreshTimezone, 1000);
  window.addEventListener('focus', schedule);
  poll(); schedule();
  setInterval(poll, 5000);
  setInterval(schedule, 60000);
})();
