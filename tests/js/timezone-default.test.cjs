const test = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { runInNewContext } = require('node:vm');

function detect({ zone = 'Africa/Harare', status = 204, offline = false, editing = false, editingBeforeScript = false, trackerAvailable = true } = {}) {
  const requests = [];
  let reloads = 0, respond;
  const listeners = {};
  const input = { value: '' };
  const edit = () => {
    input.value = 'Unsaved changes';
    for (const listener of listeners.input || []) listener({ target: input });
  };
  const completion = new Promise(resolve => { respond = resolve; });
  const context = {
    document: {
      documentElement: { dataset: {} },
      currentScript: { dataset: { timezoneUrl: '/accounts/profile/timezone/', csrfToken: 'csrf-token' } },
      addEventListener: (name, handler) => { (listeners[name] ||= []).push(handler); },
    },
    Intl: { DateTimeFormat: () => ({ resolvedOptions: () => ({ timeZone: zone }) }) },
    URLSearchParams,
    window: { location: { reload: () => { reloads++; input.value = ''; } } },
    fetch: (url, options) => {
      requests.push({ url, options });
      if (editing) edit();
      return offline ? Promise.reject(new Error('offline')) : completion;
    },
  };
  const tracker = readFileSync('templates/cotton/layouts/base.html', 'utf8')
    .match(/<script id="timezone-edit-tracker">([\s\S]*?)<\/script>/);
  if (trackerAvailable && tracker) runInNewContext(tracker[1], context);
  if (editingBeforeScript) edit();
  runInNewContext(readFileSync('assets/js/timezone-default.js', 'utf8'), context);
  respond({ ok: status >= 200 && status < 300 });
  return { requests, input, reloads: () => reloads };
}

const settled = () => new Promise(resolve => setImmediate(resolve));

test('browser detection saves a CSRF-protected default and refreshes localized content', async () => {
  const app = detect();
  await settled();
  assert.equal(app.requests.length, 1);
  const { url, options } = app.requests[0];
  assert.equal(url, '/accounts/profile/timezone/');
  assert.equal(options.method, 'POST');
  assert.equal(options.mode, 'same-origin');
  assert.equal(options.credentials, 'same-origin');
  assert.equal(options.headers['X-CSRFToken'], 'csrf-token');
  assert.equal(options.body.get('timezone'), 'Africa/Harare');
  assert.equal(options.body.get('source'), 'browser');
  assert.equal(app.reloads(), 1);
});

test('a pending default never reloads over a user editing a form', async () => {
  const app = detect({ editing: true });
  await settled();
  assert.equal(app.reloads(), 0);
  assert.equal(app.input.value, 'Unsaved changes');
});

test('input before the deferred script runs survives a successful timezone save', async () => {
  const app = detect({ editingBeforeScript: true });
  await settled();
  assert.equal(app.requests.length, 1);
  assert.equal(app.reloads(), 0);
  assert.equal(app.input.value, 'Unsaved changes');
});

test('a successful save reloads only when early edit tracking is available', async () => {
  const app = detect({ trackerAvailable: false });
  await settled();
  assert.equal(app.requests.length, 1);
  assert.equal(app.reloads(), 0);
});

for (const options of [{ status: 400 }, { status: 403 }, { offline: true }]) {
  test(`failed detection remains usable without a reload: ${JSON.stringify(options)}`, async () => {
    const app = detect(options);
    await settled();
    assert.equal(app.reloads(), 0);
  });
}

test('a browser without a detected timezone uses the editable server fallback', async () => {
  const app = detect({ zone: '' });
  await settled();
  assert.equal(app.requests.length, 0);
  assert.equal(app.reloads(), 0);
});
