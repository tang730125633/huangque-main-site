'use strict';

const assert = require('node:assert/strict');
const agent = require('../site/workbench/script-agent.js');

function response(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text() { return Promise.resolve(JSON.stringify(body)); },
  };
}

(async function () {
  const offer = agent.validProductionOffer({
    offer_id: 'director-production-1234567890abcdef',
    kind: 'script',
    expected_cost: 3,
    requires_confirmation: true,
    plan_digest: 'a'.repeat(64),
    quote_token: 'server_issued_confirmation_token_1234',
    expires_at: 2000000900,
    input: {
      request_id: 'director-production-1234567890abcdef',
      topic: '东鹏特饮', selling_points: '买三送一',
      style: '口播', duration: '30s', platform: '抖音',
    },
    summary: {
      topic: '东鹏特饮', style: '口播', duration: '30s', platform: '抖音',
    },
  });
  assert.ok(offer);
  assert.equal(offer.requires_confirmation, true);

  const calls = [];
  const win = {
    fetch(url, options) {
      calls.push({url, options});
      if (url === '/api/gen/director_agent/produce') {
        return Promise.resolve(response(200, {job_id: 81, cost: 3}));
      }
      if (url === '/api/gen/job/81') {
        return Promise.resolve(response(200, {
          status: 'done',
          result: {platform: '抖音', dur: '30s', scenes: [
            {dur: '3s', scene: '产品特写', line: '买三送一'},
          ]},
        }));
      }
      throw new Error('unexpected URL ' + url);
    },
  };

  const record = {offer, job_id: null, created_at: Date.now()};
  const result = await agent.resumeProduction(win, record);
  assert.equal(result.scenes[0].line, '买三送一');
  assert.equal(calls[0].url, '/api/gen/director_agent/produce');
  assert.equal(
    calls[0].options.headers['Idempotency-Key'],
    'director-production-1234567890abcdef',
  );
  assert.equal(
    JSON.parse(calls[0].options.body).offer_id,
    'director-production-1234567890abcdef',
  );
  assert.equal(
    JSON.parse(calls[0].options.body).quote_token,
    'server_issued_confirmation_token_1234',
  );
  assert.match(agent.formatScriptResult(result), /脚本已生产完成/);
  assert.match(agent.formatScriptResult(result), /买三送一/);

  for (const code of ['idempotency_in_progress', 'reconcile_pending']) {
    const recoveringWindow = {
      fetch() {
        return Promise.resolve(response(
          code === 'idempotency_in_progress' ? 409 : 503,
          {code, detail: 'original submission is recovering'},
        ));
      },
    };
    await assert.rejects(
      agent.resumeProduction(
        recoveringWindow, {offer, job_id: null, created_at: Date.now()},
      ),
      (error) => {
        assert.equal(error.data.code, code);
        assert.equal(error.terminal, false);
        assert.equal(error.uncertain, true);
        return true;
      },
    );
  }

  console.log('director agent confirmation frontend tests passed');
})().catch(function (error) {
  console.error(error);
  process.exitCode = 1;
});
