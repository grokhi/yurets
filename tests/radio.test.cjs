const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor() {
    this.handlers = new Map(); this.children = []; this.textContent = '';
    this.paused = true; this.currentTime = 0; this.attributes = new Set();
    this.classList = {add() {}};
  }
  set innerHTML(_) { throw new Error('Do not interpret library metadata as HTML'); }
  set src(value) { this.attributes.add('src'); this._src = value; }
  hasAttribute(name) { return this.attributes.has(name); }
  removeAttribute(name) { this.attributes.delete(name); }
  addEventListener(name, handler) {
    const list = this.handlers.get(name) || []; list.push(handler); this.handlers.set(name, list);
  }
  async emit(name) { for (const handler of this.handlers.get(name) || []) await handler({}); }
  async play() { this.paused = false; await this.emit('playing'); }
  pause() { this.paused = true; this.emit('pause'); }
  load() { this.currentTime = 0; }
  replaceChildren(...children) { this.children = children; }
  append(...children) { this.children.push(...children); }
}
const settle = () => new Promise(resolve => setImmediate(resolve));

async function setup() {
  const elements = new Map(); const intervals = new Map(); const events = [];
  const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const metadata = '<img src=x onerror=alert(1)>';
  const context = {
    document: {getElementById: get, createElement: () => new Element()},
    window: {addEventListener() {}}, navigator: {sendBeacon() {}}, Blob, Date,
    setInterval: (callback, ms) => intervals.set(ms, callback),
    fetch: async (url, options) => {
      let result;
      if (url === '/api/listening') {
        const event = JSON.parse(options.body); events.push(event);
        result = {session_id: event.action === 'start' ? `s${events.length}` : event.session_id};
      } else if (url === '/api/now-playing') result = {title: metadata, artist: 'Artist', album: 'Album'};
      else if (url.startsWith('/api/queue')) result = {tracks: [{title: metadata, artist: 'Artist', album: 'Album'}]};
      else result = {date: '2026-10-29', timezone: 'UTC', slots: []};
      return {ok: true, json: async () => result};
    },
  };
  vm.runInNewContext(fs.readFileSync('src/static/radio.js', 'utf8'), context);
  await settle();
  return {player: get('player'), button: get('listen'), events, intervals, get, metadata};
}

test('opening the site does not start a listening session or download the stream', async () => {
  const ui = await setup();
  assert.equal(ui.events.length, 0);
  assert.equal(ui.player.hasAttribute('src'), false);
  assert.ok(ui.get('now-playing').textContent.includes(ui.metadata));
  assert.ok(ui.get('queue').children[0].textContent.includes(ui.metadata));
});

test('playing sends progress; pause stops accounting and releases the stream', async () => {
  const ui = await setup();
  await ui.button.emit('click'); await settle();
  ui.player.currentTime = 10; ui.intervals.get(10000)(); await settle();
  ui.player.currentTime = 12; await ui.button.emit('click'); await settle();
  assert.deepEqual(ui.events.map(e => [e.action, e.position]), [['start', 0], ['heartbeat', 10], ['stop', 12]]);
  assert.equal(ui.player.hasAttribute('src'), false);
  ui.intervals.get(10000)(); await settle();
  assert.equal(ui.events.length, 3);
});

test('buffering ends the session and recovery starts at the new media position', async () => {
  const ui = await setup();
  await ui.button.emit('click'); await settle();
  ui.player.currentTime = 3; await ui.player.emit('waiting'); await settle();
  ui.intervals.get(10000)(); await settle();
  ui.player.currentTime = 4; await ui.player.emit('playing'); await settle();
  assert.deepEqual(ui.events.map(e => [e.action, e.position]), [['start', 0], ['stop', 3], ['start', 4]]);
});
