const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function setup({key = 'public-key', visibility = 'visible'} = {}) {
  const scripts = [], calls = [], timers = [], handlers = new Map();
  let widget;
  const document = {
    visibilityState: visibility,
    querySelector: selector => ({content: selector.includes('site-key') ? key : '1.signature'}),
    createElement: tag => ({tag}),
    head: {append: node => scripts.push(node)}, body: {append: () => {}},
    addEventListener: (name, callback) => handlers.set(name, callback),
  };
  vm.runInNewContext(fs.readFileSync('src/static/observation.js', 'utf8'), {
    document, window: {turnstile: {render: (_, options) => { widget = options; }}},
    setTimeout: callback => timers.push(callback),
    fetch: async (url, options) => { calls.push({url, body: JSON.parse(options.body)}); },
  });
  return {scripts, calls, timers, handlers, document, widget: () => widget};
}
test('missing configuration makes no external requests', () => {
  const ui = setup({key: ''});
  assert.equal(ui.scripts.length, 0);
  assert.equal(ui.calls.length, 0);
});
test('hidden page waits; result goes only to observation, exactly once', () => {
  const ui = setup({visibility: 'hidden'});
  assert.equal(ui.scripts.length, 0);
  ui.document.visibilityState = 'visible';
  ui.handlers.get('visibilitychange')();
  ui.scripts[0].onload();
  assert.equal(ui.widget().cData, '1_signature');
  ui.widget().callback('provider-token');
  ui.timers[0]();
  assert.deepEqual(ui.calls, [{url: '/api/observation', body: {token: '1.signature', response: 'provider-token'}}]);
});
test('blocked script and widget errors report unknown without audience events', () => {
  for (const failure of ['script', 'widget', 'timeout']) {
    const ui = setup();
    if (failure === 'script') ui.scripts[0].onerror();
    else if (failure === 'widget') { ui.scripts[0].onload(); ui.widget()['error-callback'](); }
    else ui.timers[0]();
    assert.deepEqual(ui.calls, [{url: '/api/observation', body: {token: '1.signature', response: null}}]);
  }
});
