const test = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { runInNewContext } = require('node:vm');

function worker(fetch = async () => ({ status: 200 })) {
  const listeners = {}, stored = [], deleted = [];
  const offline = { body: 'offline' };
  const cache = {
    add: async request => stored.push(request),
    match: async url => url === '/offline/' ? offline : undefined,
  };
  runInNewContext(readFileSync('templates/pwa/service-worker.js', 'utf8')
    .replace('{{ offline_url|escapejs }}', '/offline/'), {
    self: {
      location: new URL('https://rivals.test/service-worker.js'),
      clients: { claim: async () => {} },
      addEventListener: (name, handler) => { listeners[name] = handler; },
    },
    caches: {
      open: async () => cache,
      keys: async () => ['rivalspy-pwa-v0', 'rivalspy-pwa-v1', 'another-app'],
      delete: async key => deleted.push(key),
    },
    fetch, URL,
    Request: class extends Request {
      constructor(url, options) { super(new URL(url, 'https://rivals.test'), options); }
    },
  });
  function navigate(overrides = {}) {
    let response;
    listeners.fetch({
      request: { method: 'GET', mode: 'navigate', url: 'https://rivals.test/dashboard/', ...overrides },
      respondWith: promise => { response = promise; },
    });
    return response;
  }
  return { listeners, stored, deleted, offline, navigate };
}

test('install precaches only the anonymous offline page', async () => {
  const app = worker();
  let completion;
  app.listeners.install({ waitUntil: promise => { completion = promise; } });
  await completion;
  assert.equal(app.stored.length, 1);
  assert.equal(app.stored[0].url, 'https://rivals.test/offline/');
  assert.equal(app.stored[0].credentials, 'omit');
});

test('offline navigation falls back without caching private responses', async () => {
  const app = worker(async () => { throw new TypeError('offline'); });
  assert.equal(await app.navigate(), app.offline);
  assert.deepEqual(app.stored, []);
});

test('HTTP errors are preserved and online pages are never cached', async () => {
  const response = { status: 503 };
  const app = worker(async () => response);
  assert.equal(await app.navigate(), response);
  assert.deepEqual(app.stored, []);
});

test('HTMX, API, POST and cross-origin requests pass through', () => {
  const app = worker(() => { throw new Error('must not intercept'); });
  for (const override of [
    { mode: 'cors' }, { mode: 'same-origin', url: 'https://rivals.test/api/teams/' },
    { method: 'POST' }, { url: 'https://other.test/' },
  ]) assert.equal(app.navigate(override), undefined);
});

test('activation only removes older RivalSpy PWA caches', async () => {
  const app = worker();
  let completion;
  app.listeners.activate({ waitUntil: promise => { completion = promise; } });
  await completion;
  assert.deepEqual(app.deleted, ['rivalspy-pwa-v0']);
});

test('secure pages register the root worker after load', async () => {
  let onLoad, registration;
  runInNewContext(readFileSync('assets/js/pwa.js', 'utf8'), {
    navigator: { serviceWorker: { register: async (...args) => { registration = args; } } },
    window: { isSecureContext: true, addEventListener: (name, fn) => { onLoad = fn; } },
    console,
  });
  assert.equal(registration, undefined);
  onLoad();
  assert.equal(registration[0], '/service-worker.js');
  assert.equal(registration[1].scope, '/');
  assert.equal(registration[1].updateViaCache, 'none');
});

test('unsupported browsers and insecure pages do not attempt registration', () => {
  for (const navigator of [{}, { serviceWorker: {} }]) {
    runInNewContext(readFileSync('assets/js/pwa.js', 'utf8'), {
      navigator,
      window: { isSecureContext: false, addEventListener: () => assert.fail('registered') },
    });
  }
});
