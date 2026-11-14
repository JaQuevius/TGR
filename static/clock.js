/**
 * Soundbite NTP-Style Clock Synchronization
 *
 * DOM-free clock sync module that computes high-precision offset between
 * local performance.now() and server epoch milliseconds.
 *
 * serverNow() = performance.now() + offset
 */

let clockOffset = 0;
let lastRTT = 0;
let syncIntervalTimer = null;
let customFetch = null;

export function setCustomFetch(fn) {
  customFetch = fn;
}

function doFetch(url, opts) {
  if (customFetch) return customFetch(url, opts);
  return fetch(url, opts);
}

/**
 * Perform a single NTP ping-pong sample.
 * Returns { offset, rtt }.
 */
export async function sampleClock() {
  const t0 = performance.now();
  const resp = await doFetch('/api/time', { cache: 'no-store' });
  const data = await resp.json();
  const t1 = performance.now();
  const rtt = t1 - t0;
  const serverTime = Number(data.server_time);

  // NTP estimation: serverTime corresponds to midpoint (t0 + rtt / 2)
  const offset = serverTime - (t0 + rtt / 2);
  return { offset, rtt };
}

/**
 * Takes 6 samples and retains the one with the lowest RTT (least jitter).
 */
export async function syncClock(sampleCount = 6) {
  let bestSample = null;

  for (let i = 0; i < sampleCount; i++) {
    try {
      const sample = await sampleClock();
      if (!bestSample || sample.rtt < bestSample.rtt) {
        bestSample = sample;
      }
    } catch (e) {
      // Ignore transient sample failures during offline periods
    }
  }

  if (bestSample) {
    clockOffset = bestSample.offset;
    lastRTT = bestSample.rtt;
  }
  return { offset: clockOffset, rtt: lastRTT };
}

/**
 * Starts periodic background re-sync every 30 seconds.
 */
export function startClockSync(intervalMs = 30000) {
  stopClockSync();
  syncIntervalTimer = setInterval(() => {
    syncClock(3).catch(() => {});
  }, intervalMs);
}

export function stopClockSync() {
  if (syncIntervalTimer) {
    clearInterval(syncIntervalTimer);
    syncIntervalTimer = null;
  }
}

/**
 * Returns estimated server epoch milliseconds.
 * Never use Date.now() for authoritative game timestamps!
 */
export function serverNow() {
  return Math.round(performance.now() + clockOffset);
}

export function getClockOffset() {
  return clockOffset;
}

export function getLastRTT() {
  return lastRTT;
}

/**
 * Opportunistically incorporates a server_time from a game snapshot as a free sample.
 */
export function absorbSnapshotTime(serverTime, t0, t1) {
  if (!serverTime || typeof serverTime !== 'number') return;
  const rtt = Math.max(0, t1 - t0);
  // Only accept if RTT is reasonably low (< 1500ms)
  if (rtt < 1500) {
    const estOffset = serverTime - (t0 + rtt / 2);
    if (clockOffset === 0) {
      clockOffset = estOffset;
      lastRTT = rtt;
    } else {
      // Smooth moving average
      clockOffset = clockOffset * 0.8 + estOffset * 0.2;
      lastRTT = lastRTT * 0.8 + rtt * 0.2;
    }
  }
}

// For unit testing only
export function _setManualOffset(offset, rtt = 0) {
  clockOffset = offset;
  lastRTT = rtt;
}
