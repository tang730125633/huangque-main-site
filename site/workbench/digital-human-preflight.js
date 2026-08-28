'use strict';

function createPreflightGate({ request, onFailure }) {
  if (typeof request !== 'function') throw new TypeError('request is required');
  let started = false;
  return {
    run(start) {
      if (started) return Promise.reject(new Error('预检已执行'));
      started = true;
      return Promise.resolve()
        .then(() => request('/api/gen/digital-human-oneclick/heygen-preflight', { method: 'POST' }))
        .then(() => start())
        .catch((error) => {
          if (typeof onFailure === 'function') onFailure(error);
          throw error;
        });
    },
  };
}

if (typeof module !== 'undefined') module.exports = { createPreflightGate };
