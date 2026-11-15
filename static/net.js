/**
 * Soundwich Outbox & Resilient Network Layer
 *
 * DOM-free persistent outbox and connection manager.
 * Stores actions in sessionStorage BEFORE transmitting and retries with
 * exponential backoff until acknowledged or expired.
 */

// Safe random ID generation supporting plain HTTP contexts (crypto.getRandomValues)
export function generateId(prefix = 'act') {
  if (typeof crypto !== 'undefined' && crypto.getRandomValues) {
    const bytes = new Uint8Array(8);
    crypto.getRandomValues(bytes);
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
    return `${prefix}-${hex}`;
  }
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

// Global debug knobs (controllable via ?debug=1 panel)
export const debugConfig = {
  simulateOffline: false,
  artificialLatencyMs: 0,
  pauseOutbox: false,
};

export function setDebugOption(key, value) {
  if (key in debugConfig) {
    debugConfig[key] = value;
  }
}

// ---------------------------------------------------------------------------
// Resilient API Fetch
// ---------------------------------------------------------------------------

let consecutiveFailures = 0;
let connectionListeners = new Set();

export function onConnectionStateChange(listener) {
  connectionListeners.add(listener);
  return () => connectionListeners.delete(listener);
}

function notifyConnectionChange() {
  const state = getConnectionState();
  for (const fn of connectionListeners) {
    try {
      fn(state);
    } catch (e) {
      console.error('Error in connection listener:', e);
    }
  }
}

/**
 * Returns current connection status: 'online' | 'reconnecting' | 'offline'
 */
export function getConnectionState() {
  if (debugConfig.simulateOffline) return 'offline';
  if (typeof navigator !== 'undefined' && navigator.onLine === false) return 'offline';
  if (consecutiveFailures === 0) return 'online';
  if (consecutiveFailures === 1) return 'reconnecting';
  return 'offline';
}

export function getConsecutiveFailures() {
  return consecutiveFailures;
}

export function _resetConnectionState() {
  consecutiveFailures = 0;
}

/**
 * Core HTTP fetch wrapper with artificial latency, offline simulation,
 * and connection state tracking.
 */
export async function apiFetch(url, options = {}) {
  // 1. Check simulated offline
  if (debugConfig.simulateOffline) {
    consecutiveFailures++;
    notifyConnectionChange();
    throw new TypeError('Failed to fetch (simulated offline)');
  }

  // 2. Artificial latency simulation
  if (debugConfig.artificialLatencyMs > 0) {
    await new Promise((resolve) => setTimeout(resolve, debugConfig.artificialLatencyMs));
  }

  try {
    const resp = await fetch(url, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        ...(options.headers || {}),
      },
    });

    if (consecutiveFailures > 0) {
      consecutiveFailures = 0;
      notifyConnectionChange();
    }

    return resp;
  } catch (err) {
    consecutiveFailures++;
    notifyConnectionChange();
    throw err;
  }
}

// ---------------------------------------------------------------------------
// Persistent Outbox
// ---------------------------------------------------------------------------

export class Outbox {
  /**
   * @param {Object} options
   * @param {string} options.storageKey - Key for sessionStorage
   * @param {Function} options.onActionResolved - Callback (action, outcome, snapshot)
   */
  constructor({ storageKey = 'soundbite_outbox', onActionResolved = null } = {}) {
    this.storageKey = storageKey;
    this.onActionResolved = onActionResolved;
    this.queue = [];
    this._flushTimer = null;
    this._isFlushing = false;
    this._token = null;
    this._code = null;

    this._load();
    this._startBackgroundFlush();
  }

  setSession(code, token) {
    this._code = code;
    this._token = token;
  }

  _load() {
    if (typeof sessionStorage === 'undefined') return;
    try {
      const raw = sessionStorage.getItem(this.storageKey);
      if (raw) {
        this.queue = JSON.parse(raw) || [];
      }
    } catch (e) {
      this.queue = [];
    }
  }

  _save() {
    if (typeof sessionStorage === 'undefined') return;
    try {
      if (this.queue.length === 0) {
        sessionStorage.removeItem(this.storageKey);
      } else {
        sessionStorage.setItem(this.storageKey, JSON.stringify(this.queue));
      }
    } catch (e) {}
  }

  /**
   * Enqueue a new action (buzz or answer).
   * Automatically persists to sessionStorage BEFORE sending.
   */
  enqueue(action) {
    const entry = {
      ...action,
      queuedAt: Date.now(),
      attempts: 0,
      nextRetry: Date.now(),
      status: 'pending',
    };

    // Replace if duplicate action ID exists
    const existingIdx = this.queue.findIndex(
      (item) => (item.buzz_id && item.buzz_id === entry.buzz_id) || (item.answer_id && item.answer_id === entry.answer_id)
    );

    if (existingIdx >= 0) {
      this.queue[existingIdx] = entry;
    } else {
      this.queue.push(entry);
    }

    this._save();
    this.triggerFlush();
    return entry;
  }

  getPendingCount() {
    return this.queue.length;
  }

  getPendingActions() {
    return [...this.queue];
  }

  clear() {
    this.queue = [];
    this._save();
  }

  remove(actionId) {
    this.queue = this.queue.filter(
      (item) => item.buzz_id !== actionId && item.answer_id !== actionId && item.id !== actionId
    );
    this._save();
  }

  triggerFlush() {
    if (this._isFlushing || debugConfig.pauseOutbox) return;
    this._flush().catch(() => {});
  }

  _startBackgroundFlush() {
    if (this._flushTimer) clearInterval(this._flushTimer);
    this._flushTimer = setInterval(() => {
      this.triggerFlush();
    }, 300);
  }

  stop() {
    if (this._flushTimer) {
      clearInterval(this._flushTimer);
      this._flushTimer = null;
    }
  }

  async _flush() {
    if (this._isFlushing || !this._code || !this._token || debugConfig.pauseOutbox) return;
    if (this.queue.length === 0) return;

    this._isFlushing = true;
    const now = Date.now();

    try {
      for (const item of [...this.queue]) {
        if (item.nextRetry > now) continue;

        let endpoint = '';
        let body = {};

        if (item.type === 'buzz') {
          endpoint = `/api/game/${this._code}/buzz`;
          body = { round_id: item.round_id, buzz_id: item.buzz_id, ts: item.ts };
        } else if (item.type === 'answer') {
          endpoint = `/api/game/${this._code}/answer`;
          body = { round_id: item.round_id, answer_id: item.answer_id, answer: item.answer, ts: item.ts };
        } else {
          // Unknown action, discard
          this.remove(item.id);
          continue;
        }

        item.attempts = (item.attempts || 0) + 1;

        try {
          const resp = await apiFetch(endpoint, {
            method: 'POST',
            body: JSON.stringify(body),
            headers: { 'X-Player-Token': this._token },
          });

          const data = await resp.json();

          // Server responded with an outcome!
          if (resp.ok && data.outcome) {
            // Remove from outbox once definitive
            this.remove(item.buzz_id || item.answer_id);
            if (this.onActionResolved) {
              this.onActionResolved(item, data.outcome, data.snapshot);
            }
          } else if (resp.status === 401 || resp.status === 404) {
            // Session dead, discard
            this.remove(item.buzz_id || item.answer_id);
          } else {
            // Backoff retry: 250ms to 2000ms
            const delay = Math.min(2000, 250 * Math.pow(1.5, item.attempts));
            item.nextRetry = Date.now() + delay;
          }
        } catch (fetchErr) {
          // Failed (offline or timeout): backoff
          const delay = Math.min(2000, 250 * Math.pow(1.5, item.attempts));
          item.nextRetry = Date.now() + delay;
        }
      }
    } finally {
      this._save();
      this._isFlushing = false;
    }
  }
}
