const test = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { runInNewContext } = require('node:vm');

function setup() {
  const listeners = {};
  const status = { hidden: true, textContent: '' };
  const panel = {
    dataset: { loadingMessage: 'Loading lineup…', errorMessage: 'Please try again.' },
    attributes: {},
    setAttribute(key, value) { this.attributes[key] = value; },
    querySelector() { return status; },
  };
  const elt = { closest: () => panel };
  runInNewContext(readFileSync('assets/js/team-panels.js', 'utf8'), {
    document: { addEventListener: (name, handler) => { listeners[name] = handler; } },
  });
  const fire = (name, detail) => listeners[`htmx:${name}`]({ detail, target: elt });
  return { panel, status, elt, fire };
}

test('shows loading without an explicit target and clears after the form is replaced', () => {
  const { panel, status, elt, fire } = setup();
  const xhr = {};
  fire('beforeRequest', { elt, xhr });
  assert.equal(panel.attributes['aria-busy'], 'true');
  assert.equal(status.hidden, false);
  assert.equal(status.textContent, 'Loading lineup…');
  elt.closest = () => null;
  fire('afterRequest', { elt, xhr, successful: true });
  assert.equal(panel.attributes['aria-busy'], 'false');
  assert.equal(status.hidden, true);
});

test('an older request cannot clear a newer loading state; errors stay visible', () => {
  const { panel, status, elt, fire } = setup();
  const first = {};
  const latest = {};
  fire('beforeRequest', { target: elt, xhr: first });
  fire('beforeRequest', { target: elt, xhr: latest });
  fire('afterRequest', { target: elt, xhr: first, successful: false });
  assert.equal(panel.attributes['aria-busy'], 'true');
  assert.equal(status.textContent, 'Loading lineup…');
  fire('afterRequest', { target: elt, xhr: latest, successful: false });
  assert.equal(panel.attributes['aria-busy'], 'false');
  assert.equal(status.hidden, false);
  assert.equal(status.textContent, 'Please try again.');
});

test('a timeout without a successful flag clears loading and explains the failure', () => {
  const { panel, status, elt, fire } = setup();
  panel.dataset.timeoutMessage = 'Comparing your options took too long. Reload this page to try again.';
  const xhr = {};
  fire('beforeRequest', { elt, xhr });
  fire('afterRequest', { elt, xhr });
  fire('timeout', { elt, xhr });
  assert.equal(panel.attributes['aria-busy'], 'false');
  assert.equal(status.hidden, false);
  assert.equal(status.textContent, panel.dataset.timeoutMessage);
});

test('an older timeout cannot replace the current loading message', () => {
  const { panel, status, elt, fire } = setup();
  const first = {}, latest = {};
  fire('beforeRequest', { elt, xhr: first });
  fire('beforeRequest', { elt, xhr: latest });
  fire('timeout', { elt, xhr: first });
  assert.equal(panel.attributes['aria-busy'], 'true');
  assert.equal(status.textContent, 'Loading lineup…');
});

for (const name of ['onLoadError', 'swapError']) {
  test(`${name} clears loading even when afterRequest is never emitted`, () => {
    const { panel, status, elt, fire } = setup();
    const xhr = {};
    fire('beforeRequest', { elt, xhr });
    fire(name, { elt, xhr, successful: true });
    assert.equal(panel.attributes['aria-busy'], 'false');
    assert.equal(status.hidden, false);
    assert.equal(status.textContent, 'Please try again.');
  });
}
