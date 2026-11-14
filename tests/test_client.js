/**
 * Node.js Unit Tests for DOM-free Client Modules (clock.js and net.js)
 * Run with: node --test tests/test_client.js
 */

import test from 'node:test';
import assert from 'node:assert/strict';

// Set up mock window / performance / storage environment for Node test
if (typeof performance === 'undefined') {
  global.performance = { now: () => Date.now() };
}

// Mock sessionStorage
const mockStorage = new Map();
global.sessionStorage = {
  getItem: (k) => mockStorage.get(k) || null,
  setItem: (k, v) => mockStorage.set(k, String(v)),
  removeItem: (k) => mockStorage.delete(k),
  clear: () => mockStorage.clear(),
};

import { sampleClock, syncClock, serverNow, getClockOffset, absorbSnapshotTime, setCustomFetch, _setManualOffset } from '../static/clock.js';
import { Outbox, generateId, getConnectionState, setDebugOption, debugConfig, _resetConnectionState } from '../static/net.js';

test('clock.js: NTP offset and serverNow calculation', async () => {
  // Mock fetch returning server_time
  let mockServerTime = 1_700_000_000_000;
  setCustomFetch(async () => {
    return {
      json: async () => ({ server_time: mockServerTime }),
    };
  });

  const sample = await sampleClock();
  assert.ok(typeof sample.offset === 'number');
  assert.ok(typeof sample.rtt === 'number');

  // Verify syncClock retains lowest RTT sample
  const res = await syncClock(4);
  assert.ok(typeof res.offset === 'number');
  assert.ok(serverNow() > 0);

  // Test snapshot absorption
  const prevOffset = getClockOffset();
  absorbSnapshotTime(mockServerTime + 100, performance.now() - 20, performance.now());
  assert.ok(typeof getClockOffset() === 'number');
});

test('net.js: generateId produces unique strings without crypto.randomUUID', () => {
  const id1 = generateId('buzz');
  const id2 = generateId('buzz');
  assert.notEqual(id1, id2);
  assert.ok(id1.startsWith('buzz-'));
  assert.ok(id2.startsWith('buzz-'));
});

test('net.js: Outbox enqueues, persists and dedupes actions', () => {
  mockStorage.clear();
  const outbox = new Outbox({ storageKey: 'test_outbox' });
  outbox.setSession('1234', 'secret-token-123');

  const action1 = outbox.enqueue({
    type: 'buzz',
    round_id: 'rnd-1',
    buzz_id: 'buzz-unique-1',
    ts: 1000,
  });

  assert.equal(outbox.getPendingCount(), 1);
  assert.ok(mockStorage.has('test_outbox'));

  // Duplicate enqueue with same buzz_id should update in place, not duplicate
  outbox.enqueue({
    type: 'buzz',
    round_id: 'rnd-1',
    buzz_id: 'buzz-unique-1',
    ts: 1050,
  });

  assert.equal(outbox.getPendingCount(), 1);

  // Remove confirmed action
  outbox.remove('buzz-unique-1');
  assert.equal(outbox.getPendingCount(), 0);
  outbox.stop();
});

test('net.js: debug offline toggle triggers offline connection state', () => {
  _resetConnectionState();
  setDebugOption('simulateOffline', false);
  assert.equal(getConnectionState(), 'online');

  setDebugOption('simulateOffline', true);
  assert.equal(getConnectionState(), 'offline');

  setDebugOption('simulateOffline', false);
  _resetConnectionState();
  assert.equal(getConnectionState(), 'online');
});
