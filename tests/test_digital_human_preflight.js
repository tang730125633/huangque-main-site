const test = require('node:test');
const assert = require('node:assert/strict');
const { createPreflightGate } = require('../site/workbench/digital-human-preflight.js');

test('failed preflight blocks every paid child entry and reports no-charge failure', async () => {
  const calls = [];
  let failure;
  const gate = createPreflightGate({
    request: async () => { calls.push('preflight'); throw Object.assign(new Error('forbidden'), { status: 503 }); },
    onFailure: (error) => { failure = error; },
  });
  await assert.rejects(gate.run(async () => { calls.push('start'); }), /forbidden/);
  assert.deepEqual(calls, ['preflight']);
  assert.equal(failure.status, 503);
});

test('successful preflight starts the original chain exactly once', async () => {
  const calls = [];
  const gate = createPreflightGate({
    request: async () => { calls.push('preflight'); return { ok: true, no_charge: true }; },
  });
  await gate.run(async () => { calls.push('start'); });
  assert.deepEqual(calls, ['preflight', 'start']);
  await assert.rejects(gate.run(async () => { calls.push('start-again'); }), /预检已执行/);
  assert.deepEqual(calls, ['preflight', 'start']);
});

test('network failure also blocks the chain and does not retry the paid start', async () => {
  const calls = [];
  const gate = createPreflightGate({
    request: async () => { calls.push('preflight'); throw new TypeError('network'); },
  });
  await assert.rejects(gate.run(async () => { calls.push('start'); }), /network/);
  assert.deepEqual(calls, ['preflight']);
});
