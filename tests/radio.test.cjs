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

async function setup({empty = false, media = false} = {}) {
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
  const context = {
    document: {getElementById: get, createElement: () => new Element()},
    window: {addEventListener() {}, location: {href: 'https://radio.example/'}}, URL, navigator: {sendBeacon() {}, ...(media ? {mediaSession} : {})}, Blob, Date,
    MediaMetadata: class { constructor(values) { Object.assign(this, values); } },
    setInterval: (callback, ms) => intervals.set(ms, callback),
    fetch: async (url, options) => {
      let result;
      if (url === '/api/listening') {
        const event = JSON.parse(options.body); events.push(event);
        result = {session_id: event.action === 'start' ? `s${events.length}` : event.session_id};
      } else if (url === '/api/now-playing') result = {title: metadata, artist: 'Artist', album: 'Album', artwork_url: '/api/artwork/123'};
      else if (url.startsWith('/api/queue')) result = {tracks: empty ? [] : [{title: metadata, artist: 'Artist', album: 'Album'}]};
      else result = {date: '2026-10-29', timezone: 'UTC', slots: empty ? [] : [{start_at: '2026-10-29T00:00:00Z', end_at: '2026-10-29T01:00:00Z', artist: 'Artist', album: metadata, tracks: 3}]};
      return {ok: true, json: async () => result};
    },
  };
  vm.runInNewContext(fs.readFileSync('src/static/radio.js', 'utf8'), context);
  await settle();
  return {player: get('player'), events, intervals, get, metadata, mediaSession, handlers};
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
