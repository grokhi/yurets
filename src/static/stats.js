(() => {
  const period = document.getElementById('period');
  const status = document.getElementById('status');
  const duration = seconds => `${Math.floor(seconds / 3600)} ч ${Math.floor(seconds % 3600 / 60)} мин`;
  const when = stamp => new Date(stamp * 1000).toLocaleString('ru-RU');
  const listener = id => `Слушатель ${id.slice(0, 8)}`;
  const country = code => {
    if (!code || code === 'unknown') return 'Не определена';
    try { return new Intl.DisplayNames(['ru'], {type: 'region'}).of(code); }
    catch (_) { return code; }
  };
  function rows(id, data) {
    const body = document.getElementById(id);
    body.replaceChildren(...data.map(values => {
      const row = document.createElement('tr');
      for (const value of values) {
        const cell = document.createElement('td');
        cell.textContent = String(value ?? '—');
        row.append(cell);
      }
      return row;
    }));
    if (!data.length) {
      const row = document.createElement('tr');
      const cell = document.createElement('td'); cell.textContent = 'Пока нет данных';
      row.append(cell); body.append(row);
    }
  }
  let generation = 0;
  async function load() {
    const current = ++generation;
    status.textContent = 'Обновление…';
    try {
      const response = await fetch(`/api/analytics?days=${period.value}`, {cache: 'no-store'});
      if (!response.ok) throw new Error();
      const data = await response.json();
      if (current !== generation) return;
      const totals = data.totals;
      const metrics = [
        [totals.visits, 'посещений'], [totals.visitors, 'браузеров'],
        [duration(totals.listening_seconds), 'прослушано'], [totals.listening_now, 'слушают сейчас'],
      ];
      document.getElementById('metrics').replaceChildren(...metrics.map(([value, label]) => {
        const metric = document.createElement('div'); metric.className = 'metric';
        const number = document.createElement('strong'); number.textContent = value;
        metric.append(number, document.createTextNode(label)); return metric;
      }));
      rows('listeners', data.listeners.map(s => [listener(s.visitor_id), duration(s.listening_seconds), s.sessions, country(s.country), s.ip, when(s.last_seen)]));
      rows('countries', data.countries.map(c => [country(c.country), c.visits, c.visitors]));
      rows('daily', data.daily.map(d => [d.day, d.visits, d.visitors]));
      rows('visits', data.visits.map(v => [when(v.at), listener(v.visitor_id), v.ip, country(v.country), v.user_agent]));
      document.getElementById('notes').textContent = `История хранится ${data.retention_days} дней. ` +
        (data.geoip_available ? 'Страна определяется приблизительно по IP.' : 'Определение стран ещё не подключено.');
      if (data.geoip_provider === 'DB-IP Lite') {
        const credit = document.createElement('a');
        credit.href = 'https://db-ip.com';
        credit.textContent = 'IP Geolocation by DB-IP';
        document.getElementById('notes').append(document.createTextNode(' '), credit);
      }
      status.textContent = `Обновлено: ${new Date().toLocaleTimeString('ru-RU')}`;
    } catch (_) { if (current === generation) status.textContent = 'Не удалось загрузить статистику. Проверьте вход и попробуйте снова.'; }
  }
  period.addEventListener('change', load);
  document.getElementById('refresh').addEventListener('click', load);
  load(); setInterval(load, 30000);
})();
