const test = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { runInNewContext } = require('node:vm');

// Small DOM fixture; these tests exercise events rather than exported internals.
class Element {
  constructor(tag, attrs = {}, parent = null) {
    this.tagName = tag.toUpperCase();
    this.attrs = { ...attrs };
    this.parent = parent;
    this.dataset = {};
    this.classes = new Set();
    this.classList = {
      add: name => this.classes.add(name),
      remove: name => this.classes.delete(name),
      contains: name => this.classes.has(name),
    };
    if (['BUTTON', 'INPUT'].includes(this.tagName)) this.disabled = false;
    if (this.tagName === 'FORM') { this.elements = []; this.method = 'post'; }
    if (parent?.tagName === 'FORM') { this.form = parent; parent.elements.push(this); }
    if (attrs.href) this.href = new URL(attrs.href, 'https://rivals.test/teams/1/').href;
  }
  getAttribute(name) { return this.attrs[name] ?? null; }
  hasAttribute(name) { return name in this.attrs; }
  setAttribute(name, value) { this.attrs[name] = value; }
  removeAttribute(name) { delete this.attrs[name]; }
  matches(selector) {
    return selector.split(',').some(part => {
      part = part.trim();
      if (part.includes(':not([type])')) return this.tagName === 'BUTTON' && !this.hasAttribute('type');
      const [, tag, attr, value] = part.match(/^(\w+)?(?:\[([\w-]+)(?:="([^"]*)")?\])?$/) || [];
      return (tag || attr) && (!tag || this.tagName === tag.toUpperCase()) &&
        (!attr || (this.hasAttribute(attr) && (value === undefined || this.getAttribute(attr) === value)));
    });
  }
  closest(selector) { return this.matches(selector) ? this : this.parent?.closest(selector) || null; }
}

function setup() {
  const listeners = {}, windowListeners = {}, timers = [];
  const status = { hidden: true, textContent: '' };
  runInNewContext(readFileSync('assets/js/request-feedback.js', 'utf8'), {
    document: {
      querySelector: () => status,
      addEventListener(name, handler, capture = false) {
        (listeners[name] ||= []).push({ handler, capture });
      },
    },
    window: {
      location: new URL('https://rivals.test/teams/1/'),
      addEventListener: (name, handler) => { windowListeners[name] = handler; },
    },
    URL,
    setTimeout: handler => timers.push(handler),
  });
  function fire(name, target, options = {}) {
    const event = {
      target, button: 0, defaultPrevented: false, ...options,
      preventDefault() { this.defaultPrevented = true; },
      stopImmediatePropagation() { this.stopped = true; },
    };
    for (const { handler } of [...(listeners[name] || [])].sort((a, b) => Number(b.capture) - Number(a.capture))) {
      handler(event);
      if (event.stopped) break;
    }
    return event;
  }
  const ajax = (name, elt, xhr, extras = {}) => fire(`htmx:${name}`, elt, { detail: { elt, xhr, ...extras } });
  return {
    fire, ajax, status,
    flush: () => { while (timers.length) timers.shift()(); },
    pageshow: persisted => windowListeners.pageshow({ persisted }),
  };
}

test('comparison buttons reject duplicate clicks and requests until completion', () => {
  const s = setup(), button = new Element('button'), xhr = {};
  button.dataset.loadingLabel = 'Comparing teams…';
  s.ajax('beforeRequest', button, xhr);
  assert.equal(button.disabled, true);
  assert.equal(button.getAttribute('aria-busy'), 'true');
  assert.equal(s.status.textContent, 'Comparing teams…');
  assert.equal(s.fire('click', button).defaultPrevented, true);
  assert.equal(s.ajax('beforeRequest', button, {}).defaultPrevented, true);
  s.ajax('afterRequest', button, xhr);
  assert.equal(button.disabled, false);
  assert.equal(button.getAttribute('aria-busy'), null);
  assert.equal(s.status.hidden, true);
  assert.equal(s.ajax('beforeRequest', button, {}).defaultPrevented, false);
});

for (const outcome of ['timeout', 'sendError', 'sendAbort', 'onLoadError', 'swapError']) {
  test(`${outcome} releases all submit controls and permits retry`, () => {
    const s = setup(), form = new Element('form'), xhr = {};
    const first = new Element('button', { type: 'submit' }, form);
    const second = new Element('button', { type: 'submit' }, form);
    const disabled = new Element('button', { type: 'submit' }, form);
    disabled.disabled = true;
    s.ajax('beforeRequest', form, xhr);
    assert.equal(first.disabled, true);
    assert.equal(second.disabled, true);
    assert.equal(s.fire('submit', form).defaultPrevented, true);
    s.ajax(outcome, form, xhr);
    assert.equal(first.disabled, false);
    assert.equal(second.disabled, false);
    assert.equal(disabled.disabled, true);
    assert.equal(s.status.hidden, true);
  });
}

test('native forms preserve named submitter values during serialization', () => {
  const s = setup(), form = new Element('form');
  const preview = new Element('button', { type: 'submit', name: 'action', value: 'preview' }, form);
  const email = new Element('button', { type: 'submit', name: 'action', value: 'test' }, form);
  s.fire('submit', form, { submitter: preview });
  assert.equal(preview.disabled, false);
  assert.equal(preview.getAttribute('name'), 'action');
  assert.equal(preview.getAttribute('value'), 'preview');
  assert.equal(s.fire('click', email).defaultPrevented, true);
  assert.equal(s.fire('submit', form, { submitter: email }).defaultPrevented, true);
  s.flush();
  assert.equal(preview.disabled, true);
  assert.equal(email.disabled, true);
});

test('restoring from browser history clears stale disabled states', () => {
  const s = setup(), link = new Element('a', { href: '/teams/2/compare/' });
  s.fire('click', link);
  assert.equal(link.getAttribute('aria-disabled'), 'true');
  assert.equal(s.fire('click', link, { ctrlKey: true }).defaultPrevented, false);
  s.pageshow(false);
  assert.equal(link.getAttribute('aria-disabled'), 'true');
  s.pageshow(true);
  assert.equal(link.getAttribute('aria-disabled'), null);
  assert.equal(s.status.hidden, true);
  assert.equal(s.fire('click', link).defaultPrevented, false);
});

test('same comparison destination cannot be requested through another link', () => {
  const s = setup();
  s.fire('click', new Element('a', { href: '/teams/2/compare/' }));
  assert.equal(s.fire('click', new Element('a', { href: '/teams/2/compare/' })).defaultPrevented, true);
});

test('modified clicks, downloads, external links and local anchors stay native', () => {
  for (const [href, options, attrs] of [
    ['/teams/2/', { ctrlKey: true }, {}],
    ['/teams/2/', { metaKey: true }, {}],
    ['/teams/2/', { button: 1 }, {}],
    ['/report.png', {}, { download: '' }],
    ['https://example.com/', {}, {}],
    ['#', {}, {}], ['#lineup', {}, {}],
  ]) {
    const s = setup(), link = new Element('a', { href, ...attrs });
    assert.equal(s.fire('click', link, options).defaultPrevented, false);
    assert.equal(s.status.hidden, true);
  }
});

test('dialog forms, invalid/canceled submissions and local buttons never lock', () => {
  const s = setup(), form = new Element('form');
  form.method = 'dialog';
  s.fire('submit', form);
  const canceled = new Element('form');
  s.fire('submit', canceled, { defaultPrevented: true });
  s.fire('click', new Element('button', { type: 'button' }));
  assert.equal(s.status.hidden, true);
});

test('a replaced HTMX form unlocks through the request identity', () => {
  const s = setup(), old = new Element('form'), xhr = {};
  new Element('button', { type: 'submit' }, old);
  s.ajax('beforeRequest', old, xhr);
  s.ajax('afterRequest', new Element('div'), xhr);
  assert.equal(s.status.hidden, true);
});

test('polling containers and live search inputs do not lock user controls', () => {
  const s = setup();
  s.ajax('beforeRequest', new Element('div', { 'hx-trigger': 'every 60s' }), {});
  const input = new Element('input', { type: 'search' });
  s.ajax('beforeRequest', input, {});
  assert.equal(input.disabled, false);
  assert.equal(s.status.hidden, true);
});

test('finishing an older request cannot unlock a retried action', () => {
  const s = setup(), button = new Element('button'), first = {}, second = {};
  s.ajax('beforeRequest', button, first);
  s.ajax('timeout', button, first);
  s.ajax('beforeRequest', button, second);
  s.ajax('afterRequest', button, first);
  assert.equal(button.disabled, true);
  s.ajax('afterRequest', button, second);
  assert.equal(button.disabled, false);
});

test('cancellation by a later event handler restores the action', () => {
  const s = setup(), button = new Element('button');
  const event = s.ajax('beforeRequest', button, {});
  event.preventDefault();
  s.flush();
  assert.equal(button.disabled, false);
  assert.equal(s.status.hidden, true);
});
