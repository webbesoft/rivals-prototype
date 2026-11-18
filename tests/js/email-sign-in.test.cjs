const test = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { runInNewContext } = require('node:vm');

const script = readFileSync('assets/js/email-sign-in.js', 'utf8');

test('returning sign-in submits the CSRF form without another click', () => {
  let submissions = 0;
  runInNewContext(script, {
    document: {
      querySelector(selector) {
        assert.equal(selector, 'form[data-auto-sign-in]');
        return { requestSubmit() { submissions++; } };
      },
    },
  });
  assert.equal(submissions, 1);
});

test('pages without automatic sign-in do not submit', () => {
  runInNewContext(script, { document: { querySelector: () => null } });
});
