const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const settle = () => new Promise(resolve => setImmediate(resolve));
async function setup(visibility = 'visible', fail = false) {
  const calls = [], timers = [], handlers = new Map();
  const document = {
    visibilityState: visibility,
    querySelector: () => ({content: 'signed-page-token'}),
    addEventListener: (name, handler) => handlers.set(name, handler),
    getElementById: () => ({addEventListener: (name, handler) => handlers.set(name, handler)}),
  };
  vm.runInNewContext(fs.readFileSync('src/static/visits.js', 'utf8'), {
    document, window: {addEventListener: (name, handler) => handlers.set(name, handler)},
    setTimeout: callback => timers.push(callback),
    fetch: async (url, options) => { calls.push({url, ...options}); return {ok: !fail}; },
  });
  await settle();
  return {calls, timers, handlers, document};
}
test('a visible visit is confirmed without clicks, audio or cookie access', async () => {
  const ui = await setup();
  assert.equal(ui.calls.length, 1);
  assert.equal(JSON.parse(ui.calls[0].body).token, 'signed-page-token');
  await ui.handlers.get('pageshow')();
  assert.equal(ui.calls.length, 1);
});
test('a background page waits until it is shown; playback also confirms a visit', async () => {
  const ui = await setup('hidden');
  assert.equal(ui.calls.length, 0);
  ui.document.visibilityState = 'visible';
  await ui.handlers.get('visibilitychange')();
  assert.equal(ui.calls.length, 1);
  const audio = await setup('hidden');
  audio.handlers.get('playing')(); await settle();
  assert.equal(audio.calls.length, 1);
});
test('network failures retry the same confirmation, with bounded automatic retries', async () => {
  const ui = await setup('visible', true);
  while (ui.timers.length) { await ui.timers.shift()(); }
  assert.equal(ui.calls.length, 3);
  assert.equal(new Set(ui.calls.map(call => call.body)).size, 1);
});
