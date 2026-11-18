const test = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { runInNewContext } = require('node:vm');

function app(mode, { cancelFirst = false } = {}) {
  let handler, calls = 0;
  const submissions = [];
  const options = { challenge_id: 'enrollment-attempt', creation_options: { publicKey: { challenge: 'enrollment-challenge' } } };
  const button = { textContent: 'Save a passkey', disabled: true };
  const status = {};
  const form = {
    dataset: { passkeyForm: mode }, action: '/accounts/passkeys/login/',
    elements: { credential: { value: '' }, challenge_id: { value: '' } },
    querySelector: selector => selector.startsWith('button') ? button : status,
    addEventListener: (_event, listener) => { handler = listener; },
  };
  const context = {
    document: {
      querySelectorAll: () => [form],
      getElementById: () => ({ textContent: JSON.stringify(options) }),
    },
    window: {
      isSecureContext: true, PublicKeyCredential: function () {},
      webauthnJSON: {
        create: async data => { assert.equal(data.publicKey.challenge, 'enrollment-challenge'); return { id: 'enrolled-key' }; },
        get: async data => {
          if (cancelFirst && calls === 1) throw { name: 'NotAllowedError' };
          return { id: `key-for-${data.publicKey.challenge}` };
        },
      },
    },
    fetch: async () => {
      calls++;
      return { ok: true, json: async () => ({ challenge_id: `login-attempt-${calls}`, request_options: { publicKey: { challenge: `challenge-${calls}` } } }) };
    },
    HTMLFormElement: { prototype: { submit() {
      submissions.push({ challenge_id: this.elements.challenge_id.value, credential: JSON.parse(this.elements.credential.value) });
    } } },
  };
  runInNewContext(readFileSync('assets/js/passkeys.js', 'utf8'), context);
  return { submissions, button, status, submit: () => handler({ preventDefault() {} }) };
}

test('enrollment posts the identifier belonging to its creation options', async () => {
  const page = app('register');
  await page.submit();
  assert.deepEqual(page.submissions, [{ challenge_id: 'enrollment-attempt', credential: { id: 'enrolled-key' } }]);
});

test('passkey login posts its fetched challenge identifier', async () => {
  const page = app('login');
  await page.submit();
  assert.deepEqual(page.submissions, [{ challenge_id: 'login-attempt-1', credential: { id: 'key-for-challenge-1' } }]);
});

test('a cancelled prompt sends nothing and a retry fetches a new challenge', async () => {
  const page = app('login', { cancelFirst: true });
  await page.submit();
  assert.equal(page.submissions.length, 0);
  assert.equal(page.button.disabled, false);
  await page.submit();
  assert.deepEqual(page.submissions, [{ challenge_id: 'login-attempt-2', credential: { id: 'key-for-challenge-2' } }]);
});
