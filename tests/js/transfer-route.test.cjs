const test = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { runInNewContext } = require('node:vm');

const players = [
  { id: 1, name: 'Goalkeeper', position: 1, club: 'ARS', points: [2, 2, 2, 2, 2, 2] },
  { id: 2, name: 'Defender', position: 2, club: 'CHE', points: [3, 3, 3, 3, 3, 3] },
  { id: 3, name: 'Midfielder', position: 3, club: 'LIV', points: [5, 4, 3, 2, 1, 0] },
  { id: 4, name: 'Forward', position: 4, club: 'MCI', points: [3, 3, 3, 3, 3, 3] },
  { id: 5, name: 'Other midfielder', position: 3, club: 'AVL', points: [3, 6, 6, 6, 6, 6] },
];

function setup({ sells = ['', ''], buys = ['', ''], loaded = true, horizon = 3, start = 11 } = {}) {
  const listeners = {};
  const changes = [];
  const horizonControl = { name: 'horizon', value: String(horizon) };
  const windowLabel = { textContent: '' };
  const form = { elements: { horizon: horizonControl }, addEventListener: (name, handler) => {
    assert.equal(name, 'change');
    changes.push(handler);
  } };
  const controls = {};
  for (const index of [1, 2]) {
    controls[`out_${index}`] = { name: `out_${index}`, value: sells[index - 1] };
    controls[`in_${index}`] = {
      value: buys[index - 1], disabled: false, options: [],
      replaceChildren(...options) { this.options = options; this.value = ''; },
    };
  }
  const route = {
    dataset: { startGameweek: String(start) },
    matches: selector => selector === '[data-transfer-route]',
    querySelector: selector => selector === '[data-transfer-window]' ? windowLabel : selector.startsWith('script')
      ? { textContent: JSON.stringify(players) }
      : controls[selector.match(/name="([^"]+)"/)[1]],
    closest: () => form,
  };
  const document = {
    addEventListener: (name, handler) => { listeners[name] = handler; },
    querySelectorAll: () => loaded ? [route] : [],
  };
  runInNewContext(readFileSync('assets/js/transfer-route.js', 'utf8'), {
    document,
    Option: function (text, value) { this.text = text; this.value = value; },
  });
  return {
    controls, changes, horizon: horizonControl, windowLabel,
    change: target => changes.forEach(handler => handler({ target })),
    load: () => listeners['htmx:load']({ detail: { elt: route } }),
  };
}

const values = buy => buy.options.map(option => option.value);

test('Buy waits for a Sell selection in both moves', () => {
  const { controls } = setup();
  for (const index of [1, 2]) {
    assert.equal(controls[`in_${index}`].disabled, true);
    assert.deepEqual(values(controls[`in_${index}`]), ['']);
  }
});

for (const position of [1, 2, 3, 4]) {
  test(`changing Sell to position ${position} immediately restricts Buy`, () => {
    const { controls, change } = setup();
    controls.out_1.value = String(position);
    change(controls.out_1);
    assert.deepEqual(values(controls.in_1), ['', ...players.filter(player => player.position === position).map(player => String(player.id))]);
    assert.equal(controls.in_1.disabled, false);
    assert.equal(controls.in_1.value, '');
    assert.deepEqual(values(controls.in_2), ['']);
  });
}

test('changing positions clears an incompatible Buy while preserving the other move', () => {
  const { controls, change } = setup({ sells: ['3', '2'], buys: ['5', '2'] });
  controls.out_1.value = '4';
  change(controls.out_1);
  assert.equal(controls.in_1.value, '');
  assert.deepEqual(values(controls.in_1), ['', '4']);
  assert.equal(controls.in_2.value, '2');
  assert.deepEqual(values(controls.in_2), ['', '2']);
});

test('saved selections survive loading and a same-position Sell change', () => {
  const { controls, change } = setup({ sells: ['3', '2'], buys: ['5', '2'] });
  assert.equal(controls.in_1.value, '5');
  assert.equal(controls.in_2.value, '2');
  controls.out_1.value = '5';
  change(controls.out_1);
  assert.equal(controls.in_1.value, '5');
});

test('clearing Sell resets and disables Buy', () => {
  const { controls, change } = setup({ sells: ['3', ''], buys: ['5', ''] });
  controls.out_1.value = '';
  change(controls.out_1);
  assert.equal(controls.in_1.value, '');
  assert.equal(controls.in_1.disabled, true);
  assert.deepEqual(values(controls.in_1), ['']);
});

test('squad corrections also reset the filtered lists after the form clears the moves', () => {
  const { controls, change } = setup({ sells: ['3', '2'], buys: ['5', '2'] });
  for (const control of Object.values(controls)) control.value = '';
  change({ name: 'owned_0' });
  assert.equal(controls.in_1.disabled, true);
  assert.equal(controls.in_2.disabled, true);
});

test('a lazy-loaded planner initialises once after HTMX inserts the form', () => {
  const { controls, changes, change, load } = setup({ loaded: false });
  assert.equal(changes.length, 0);
  load();
  load();
  assert.equal(changes.length, 1);
  controls.out_2.value = '3';
  change(controls.out_2);
  assert.deepEqual(values(controls.in_2), ['', '3', '5']);
});

test('Buy labels include the club and signed expected points difference versus Sell', () => {
  const { controls } = setup({ sells: ['3', '5'] });
  assert.equal(controls.in_1.options.find(option => option.value === '5').text, 'Other midfielder · AVL · +3.0 xPts');
  assert.equal(controls.in_2.options.find(option => option.value === '3').text, 'Midfielder · LIV · -3.0 xPts');
  assert.equal(controls.in_1.options.find(option => option.value === '3').text, 'Midfielder · LIV · +0.0 xPts');
});

test('changing Sell to another midfielder recalculates the points differences', () => {
  const { controls, change } = setup({ sells: ['3', ''], buys: ['5', ''] });
  controls.out_1.value = '5';
  change(controls.out_1);
  assert.equal(controls.in_1.options.find(option => option.value === '3').text, 'Midfielder · LIV · -3.0 xPts');
  assert.equal(controls.in_1.value, '5');
});

test('changing Plan ahead updates both point differences and the displayed gameweek range', () => {
  const { controls, horizon, windowLabel, change } = setup({ sells: ['3', '5'], buys: ['5', '3'] });
  assert.equal(windowLabel.textContent, 'GW11–13');
  horizon.value = '6';
  change(horizon);
  assert.equal(windowLabel.textContent, 'GW11–16');
  assert.equal(controls.in_1.options.find(option => option.value === '5').text, 'Other midfielder · AVL · +18.0 xPts');
  assert.equal(controls.in_2.options.find(option => option.value === '3').text, 'Midfielder · LIV · -18.0 xPts');
  assert.equal(controls.in_1.value, '5');
  assert.equal(controls.in_2.value, '3');
});

test('the displayed range stops at the last gameweek of the season', () => {
  const { windowLabel } = setup({ start: 38, horizon: 6 });
  assert.equal(windowLabel.textContent, 'GW38');
});
