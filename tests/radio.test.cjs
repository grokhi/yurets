const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor() {
    this.handlers = new Map(); this.children = []; this.textContent = '';
    this.paused = true; this.currentTime = 0; this.attributes = new Set();
    this.loads = 0; this.connections = 0; this.downloading = false;
    this.classList = {add() {}};
  }
  set innerHTML(_) { throw new Error('Do not interpret library metadata as HTML'); }
  set src(value) { this.attributes.add('src'); this._src = value; }
  get src() { return this._src; }
  get firstElementChild() { return this.children[0]; }
  hasAttribute(name) { return this.attributes.has(name); }
  removeAttribute(name) { this.attributes.delete(name); }
  addEventListener(name, handler) {
    const list = this.handlers.get(name) || []; list.push(handler); this.handlers.set(name, list);
  }
  async emit(name) { for (const handler of this.handlers.get(name) || []) await handler({}); }
  async play() {
    assert.equal(this.src, '/stream');
    this.paused = false; this.downloading = true; this.connections++;
    await this.emit('play');
    await this.emit('playing');
  }
  async pause() { this.paused = true; await this.emit('pause'); }
  load() { this.currentTime = 0; this.paused = true; this.downloading = false; this.loads++; }
  replaceChildren(...children) { this.children = children; }
  append(...children) { this.children.push(...children); }
}
const settle = () => new Promise(resolve => setImmediate(resolve));

async function setup({empty = false, media = false, timezone = 'UTC', start = '2026-10-29T00:00:00Z', end = '2026-10-30T00:00:00Z'} = {}) {
  const elements = new Map(); const intervals = new Map(); const events = [];
  const get = id => {
    assert.notEqual(id, 'listen', 'Playback must use the native player only');
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const html = fs.readFileSync('src/static/index.html', 'utf8');
  const audio = html.match(/<audio\b[^>]*>/)[0];
  assert.match(audio, /preload="none"/);
  get('player').src = audio.match(/src="([^"]+)"/)[1];
  const metadata = '<img src=x onerror=alert(1)>';
  const handlers = new Map();
  const mediaSession = {setActionHandler: (name, handler) => handlers.set(name, handler)};
  const zone = {value: timezone};
  const clock = {value: Date.parse('2026-10-29T12:00:00Z')};
  const requests = [], pending = [];
  const network = {hold: false};
  class ClockDate extends Date {
    constructor(...args) { super(...(args.length ? args : [clock.value])); }
    static now() { return clock.value; }
  }
  const context = {
    Intl: {DateTimeFormat: () => ({resolvedOptions: () => ({timeZone: zone.value})})},
    document: {getElementById: get, createElement: () => new Element(), addEventListener() {}},
    window: {addEventListener() {}, location: {href: 'https://radio.example/'}}, URL, navigator: {sendBeacon() {}, ...(media ? {mediaSession} : {})}, Blob, Date: ClockDate,
    MediaMetadata: class { constructor(values) { Object.assign(this, values); } },
    setInterval: (callback, ms) => intervals.set(ms, callback),
    fetch: async (url, options) => {
      let result;
      if (url === '/api/listening') {
        const event = JSON.parse(options.body); events.push(event);
        result = {session_id: event.action === 'start' ? `s${events.length}` : event.session_id};
      } else if (url === '/api/now-playing') result = {title: metadata, artist: 'Artist', album: 'Album', artwork_url: '/api/artwork/123'};
      else if (url.startsWith('/api/queue')) result = {tracks: empty ? [] : [{title: metadata, artist: 'Artist', album: 'Album'}]};
      else {
        requests.push(url);
        const tz = new URL(url, 'https://radio.example').searchParams.get('timezone');
        const parts = new Intl.DateTimeFormat('en-CA', {timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit'}).formatToParts(new Date(clock.value));
        const value = name => parts.find(part => part.type === name).value;
        const date = `${value('year')}-${value('month')}-${value('day')}`;
        const offset = {'UTC': 0, 'America/Los_Angeles': -7, 'Asia/Almaty': 5}[tz];
        const begin = Date.parse(`${date}T00:00:00Z`) - offset * 3600000;
        const finish = begin + 86400000;
        result = {date, timezone: tz, start_at: new Date(begin).toISOString(), end_at: new Date(finish).toISOString(),
          slots: empty || Date.parse(end) <= begin || Date.parse(start) >= finish ? [] : [{
            start_at: new Date(Math.max(begin, Date.parse(start))).toISOString(),
            end_at: new Date(Math.min(finish, Date.parse(end))).toISOString(), artist: 'Artist', album: metadata, tracks: 3,
          }]};
        if (network.hold) await new Promise(resolve => pending.push(resolve));
      }
      return {ok: true, json: async () => result};
    },
  };
  vm.runInNewContext(fs.readFileSync('src/static/radio.js', 'utf8'), context);
  await settle();
  return {player: get('player'), events, intervals, get, metadata, mediaSession, handlers, zone, clock, requests, network, pending};
}

test('opening the site does not play or count listening; table metadata is plain text', async () => {
  const ui = await setup();
  assert.equal(ui.events.length, 0);
  assert.equal(ui.player.connections, 0);
  assert.ok(ui.get('now-playing').textContent.includes(ui.metadata));
  assert.equal(ui.get('queue').children[0].children[1].textContent, ui.metadata);
  assert.equal(ui.get('schedule').children[0].children[2].textContent, ui.metadata);
});

test('native Play counts progress; Pause releases audio; next Play joins a fresh stream', async () => {
  const ui = await setup();
  await ui.player.play(); await settle();
  ui.player.currentTime = 10; ui.intervals.get(10000)(); await settle();
  ui.player.currentTime = 12; await ui.player.pause(); await settle();
  assert.deepEqual(ui.events.map(e => [e.action, e.position]), [['start', 0], ['heartbeat', 10], ['stop', 12]]);
  assert.equal(ui.player.downloading, false);
  assert.equal(ui.player.hasAttribute('src'), true);
  assert.equal(ui.player.loads, 1);
  ui.intervals.get(10000)(); await settle();
  assert.equal(ui.events.length, 3);
  await ui.player.play(); await settle();
  assert.equal(ui.player.connections, 2);
  assert.equal(ui.events.at(-1).action, 'start');
  assert.equal(ui.events.at(-1).position, 0);
});

test('buffering ends the session and recovery starts at the new media position', async () => {
  const ui = await setup();
  await ui.player.play(); await settle();
  ui.player.currentTime = 3; await ui.player.emit('waiting'); await settle();
  ui.intervals.get(10000)(); await settle();
  ui.player.currentTime = 4; await ui.player.emit('playing'); await settle();
  assert.deepEqual(ui.events.map(e => [e.action, e.position]), [['start', 0], ['stop', 3], ['start', 4]]);
});

test('connection failure stops accounting and native Play can retry', async () => {
  const ui = await setup();
  await ui.player.play(); await settle();
  ui.player.currentTime = 7; await ui.player.emit('error'); await settle();
  assert.equal(ui.player.downloading, false);
  assert.deepEqual(ui.events.map(e => [e.action, e.position]), [['start', 0], ['stop', 7]]);
  await ui.player.play(); await settle();
  assert.equal(ui.player.connections, 2);
  assert.equal(ui.events.at(-1).action, 'start');
});

test('empty queue and schedule render a single spanning table cell', async () => {
  const ui = await setup({empty: true});
  assert.equal(ui.get('queue').firstElementChild.firstElementChild.colSpan, 3);
  assert.equal(ui.get('schedule').firstElementChild.firstElementChild.colSpan, 4);
});


test('lock screen receives track metadata and its controls use the live player', async () => {
  const ui = await setup({media: true});
  assert.equal(ui.mediaSession.metadata.title, ui.metadata);
  assert.equal(ui.mediaSession.metadata.artist, 'Artist');
  assert.equal(ui.mediaSession.metadata.album, 'Album');
  assert.equal(ui.mediaSession.metadata.artwork[0].src, 'https://radio.example/api/artwork/123');
  assert.equal(ui.handlers.get('seekto'), null);
  await ui.handlers.get('play')(); await settle();
  assert.equal(ui.mediaSession.playbackState, 'playing');
  await ui.handlers.get('pause')(); await settle();
  assert.equal(ui.mediaSession.playbackState, 'paused');
  assert.equal(ui.player.downloading, false);
  ui.mediaSession.metadata = null;
  await ui.intervals.get(5000)();
  assert.equal(ui.mediaSession.metadata.title, ui.metadata);
});


test('schedule requests local day again when device timezone changes', async () => {
  const ui = await setup({timezone: 'America/Los_Angeles'});
  assert.match(ui.get('schedule-date').textContent, /^29\.10\.2026 · America\/Los_Angeles$/);
  assert.equal(ui.get('schedule').children[0].children[0].textContent, '00:00–17:00');
  assert.equal(ui.requests.at(-1), '/api/schedule?timezone=America%2FLos_Angeles');
  ui.zone.value = 'Asia/Almaty';
  await ui.intervals.get(1000)();
  assert.equal(ui.requests.at(-1), '/api/schedule?timezone=Asia%2FAlmaty');
  assert.equal(ui.get('schedule-date').textContent, '29.10.2026 · Asia/Almaty');
  assert.equal(ui.get('schedule').children[0].children[0].textContent, '05:00–24:00');
});

test('album crossing local midnight ends at 24:00 with only one date in heading', async () => {
  const ui = await setup({timezone: 'Asia/Almaty', start: '2026-10-29T18:30:00Z', end: '2026-10-29T19:30:00Z'});
  assert.equal(ui.get('schedule').children[0].children[0].textContent, '23:30–24:00');
  assert.equal(ui.get('schedule-date').textContent, '29.10.2026 · Asia/Almaty');
  ui.clock.value = Date.parse('2026-10-29T19:00:01Z');
  await ui.intervals.get(1000)();
  assert.equal(ui.get('schedule-date').textContent, '30.10.2026 · Asia/Almaty');
  assert.equal(ui.get('schedule').children[0].children[0].textContent, '00:00–00:30');
  assert.equal(ui.requests.length, 2);
});

test('slow response for an old timezone cannot replace current day', async () => {
  const ui = await setup();
  ui.network.hold = true;
  ui.zone.value = 'America/Los_Angeles';
  const old = ui.intervals.get(1000)();
  ui.zone.value = 'Asia/Almaty';
  const current = ui.intervals.get(1000)();
  ui.pending[1](); await current;
  assert.equal(ui.get('schedule-date').textContent, '29.10.2026 · Asia/Almaty');
  ui.pending[0](); await old;
  assert.equal(ui.get('schedule-date').textContent, '29.10.2026 · Asia/Almaty');
  assert.equal(ui.get('schedule').children[0].children[0].textContent, '05:00–24:00');
});
