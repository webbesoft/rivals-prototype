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
