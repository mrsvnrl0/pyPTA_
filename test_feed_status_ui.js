'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {effectiveState, eligibility} = require('./adaptive_crypto/static/feed_status.js');

test('elapsed diagnostics turn stale at the validity boundary', () => {
  const row = {status: 'ready', valid_until_ms: 2000};
  assert.equal(effectiveState(row, 1999, false), 'ready');
  assert.equal(effectiveState(row, 2000, false), 'stale');
  assert.equal(effectiveState({status: 'ready'}, 1000, false), 'stale');
});
test('lost diagnostics connection cannot leave a Current badge', () => {
  assert.equal(effectiveState({status: 'ready', valid_until_ms: 2000}, 1000, true), 'disconnected');
  assert.equal(effectiveState({status: 'unavailable'}, 1000, true), 'unavailable');
  assert.equal(effectiveState({status: 'unknown'}, 1000, false), 'unknown');
});
test('retry eligibility is not presented as a promised completed update', () => {
  assert.equal(eligibility({next_eligible_ms: 2000}, 1000), 'Refresh eligible in 1s');
  assert.match(eligibility({next_eligible_ms: 2000}, 2000), /^Eligible now; refresh starts on a GEX request or eligible scan/);
  assert.equal(eligibility({refreshing: true}, 1000), 'Refresh in progress');
  assert.equal(eligibility({next_check: 'Next scanner cycle'}, 1000), 'Next scanner cycle');
  assert.equal(eligibility({}, 1000), 'No retry time recorded');
});
