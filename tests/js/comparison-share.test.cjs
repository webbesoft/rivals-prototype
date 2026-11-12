const test = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { runInNewContext } = require('node:vm');

function setup({ data = {}, native = true, failExport = false, shareError } = {}) {
  const labels = [], revoked = [], shares = [];
  function element() {
    return {
      hidden: true, handlers: {},
      addEventListener(name, handler) { this.handlers[name] = handler; },
      removeAttribute(name) { delete this[name]; },
    };
  }
  const trigger = element();
  trigger.dataset = {
    mine: '<My XI>', rival: 'Rival XI', myManager: 'Alice', rivalManager: 'Bob',
    myPoints: '704', rivalPoints: '690', gameweek: '10', myCaptain: 'Salah',
    rivalCaptain: 'Haaland', myForm: '54.0', rivalForm: '51.0',
    myForecast: '62.1', rivalForecast: '59.2', scoreSource: 'FPL entry totals',
    timestamp: '2026-11-12 10:00:00', ...data,
  };
  const preview = element(), save = element(), share = element(), status = element();
  const parts = { '[data-share-preview]': preview, '[data-share-save]': save,
    '[data-share-native]': share, '[data-share-status]': status };
  const dialog = element();
  dialog.querySelector = name => parts[name];
  dialog.showModal = () => { dialog.open = true; };
  const context = {
    fillRect() {}, measureText: value => ({ width: String(value).length * 12 }),
    fillText: value => labels.push(value),
  };
  const canvas = {
    getContext: () => context,
    toBlob: callback => callback(failExport ? null : { type: 'image/png' }),
  };
  runInNewContext(readFileSync('assets/js/comparison-share.js', 'utf8'), {
    document: {
      querySelector: selector => selector === '[data-comparison-share]' ? trigger : dialog,
      createElement: () => canvas,
    },
    navigator: native ? {
      canShare: () => true,
      share: async payload => { shares.push(payload); if (shareError) throw shareError; },
    } : {},
    File: class { constructor(parts, name, options) { this.name = name; this.type = options.type; } },
    URL: { createObjectURL: () => 'blob:comparison', revokeObjectURL: url => revoked.push(url) },
  });
  return { trigger, dialog, canvas, preview, save, share, status, labels, shares, revoked };
}

test('creates a portrait PNG with actual scores and a downloadable safe filename', async () => {
  const s = setup();
  assert.equal(s.trigger.hidden, false);
  await s.trigger.handlers.click();
  assert.equal(s.canvas.width, 1080);
  assert.equal(s.canvas.height, 1350);
  for (const label of ['<My XI>', '704', '690', '14 pts ahead', 'Salah', 'GW11 forecast']) {
    assert.ok(s.labels.includes(label), label);
  }
  assert.equal(s.save.download, 'rivalspy-My-XI-vs-Rival-XI.png');
  assert.equal(s.save.href, s.preview.src);
  assert.equal(s.save.hidden, false);
  await s.share.handlers.click();
  assert.equal(s.shares[0].files[0].type, 'image/png');
});

test('keeps genuine zero points while treating missing totals as unavailable', async () => {
  const zero = setup({ data: { myPoints: '0', rivalPoints: '12' } });
  await zero.trigger.handlers.click();
  assert.ok(zero.labels.includes('0'));
  assert.ok(zero.labels.includes('12 pts behind'));
  const missing = setup({ data: { myPoints: '', rivalPoints: '12' } });
  await missing.trigger.handlers.click();
  assert.ok(missing.labels.includes('Totals not available'));
  assert.ok(!missing.labels.includes('12 pts behind'));
});

test('offers download when native file sharing is unsupported', async () => {
  const s = setup({ native: false });
  await s.trigger.handlers.click();
  assert.equal(s.share.hidden, true);
  assert.equal(s.save.hidden, false);
});

test('an export failure leaves a retry message without a broken download', async () => {
  const s = setup({ failExport: true });
  await s.trigger.handlers.click();
  assert.match(s.status.textContent, /try again/);
  assert.equal(s.save.hidden, true);
});

test('cancellation preserves the ready preview; other share failures offer saving', async () => {
  for (const name of ['AbortError', 'NotAllowedError']) {
    const s = setup({ shareError: { name } });
    await s.trigger.handlers.click();
    await s.share.handlers.click();
    assert.equal(s.share.disabled, false);
    assert.match(s.status.textContent, name === 'AbortError' ? /ready/ : /Save PNG/);
    assert.equal(s.save.hidden, false);
  }
});

test('closing releases the generated image and reopening creates a new one', async () => {
  const s = setup();
  await s.trigger.handlers.click();
  s.dialog.open = false;
  s.dialog.handlers.close();
  assert.deepEqual(s.revoked, ['blob:comparison']);
  assert.equal(s.preview.src, undefined);
  assert.equal(s.save.href, undefined);
  await s.trigger.handlers.click();
  assert.equal(s.preview.src, 'blob:comparison');
});
