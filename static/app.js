/**
 * Soundbite Client Controller
 *
 * Coordinates UI state, autonomous local round countdowns, resilient outbox flushing,
 * NTP clock sync, audio preloading, and server-authoritative buzzer racing.
 */

import { initClock, syncClock, startClockSync, serverNow, getClockOffset, getLastRTT, absorbSnapshotTime } from './clock.js';
import { Outbox, apiFetch, generateId, getConnectionState, onConnectionStateChange, debugConfig, setDebugOption } from './net.js';
import { preloadAudio, schedulePlayback, stopAudio, isSoundMuted, setSoundMuted, unlockAudio, setOnAudioBlocked } from './audio.js';

// DOM Helper
const $ = (id) => document.getElementById(id);

// App State
let session = null; // { code, pid, token, host, name }
let lastSnapshot = null;
let pollTimer = null;
let localTimerInterval = null;
let outbox = null;
let pendingEarlyAnswer = null; // Stored if user types before floor is confirmed
let currentRoundId = null;
let hasBuzzedThisRound = false;

// ---------------------------------------------------------------------------
// Initialization & Session Management
// ---------------------------------------------------------------------------

async function init() {
  setupDomListeners();
  setupDebugPanel();

  // Initialize NTP Clock
  await syncClock(4);
  startClockSync(30000);

  // Initialize Outbox
  outbox = new Outbox({
    storageKey: 'soundbite_outbox_queue',
    onActionResolved: handleActionResolved,
  });

  // Track connection state updates for the top header pill
  onConnectionStateChange((state) => {
    updateConnectionPill();
  });

  setOnAudioBlocked((blocked) => {
    $('sound-blocked-banner').classList.toggle('hidden', !blocked);
  });

  // Check sessionStorage (per-tab seat persistence)
  const savedSession = getStoredSession();
  if (savedSession) {
    session = savedSession;
    outbox.setSession(session.code, session.token);
    await enterLobby();
  } else {
    checkLastSessionRejoin();
  }
}

function getStoredSession() {
  try {
    const raw = sessionStorage.getItem('soundbite-session');
    return raw ? JSON.parse(raw) : null;
  } catch (e) {
    return null;
  }
}

function setSession(sess) {
  session = sess;
  sessionStorage.setItem('soundbite-session', JSON.stringify(sess));
  // Mirror to localStorage for explicit home-screen rejoin button
  localStorage.setItem('soundbite-last-session', JSON.stringify(sess));
  if (outbox) {
    outbox.setSession(sess.code, sess.token);
  }
}

function clearSession() {
  session = null;
  sessionStorage.removeItem('soundbite-session');
  if (outbox) outbox.clear();
  stopPolling();
  stopAudio();
}

function checkLastSessionRejoin() {
  try {
    const raw = localStorage.getItem('soundbite-last-session');
    if (raw) {
      const last = JSON.parse(raw);
      if (last && last.code && last.name) {
        $('rejoin-container').classList.remove('hidden');
        $('btn-rejoin-last').textContent = `Rejoin Lobby ${last.code} as ${last.name}`;
        $('btn-rejoin-last').onclick = () => {
          session = last;
          setSession(last);
          enterLobby();
        };
      }
    }
  } catch (e) {}
}

// ---------------------------------------------------------------------------
// Screen Transitions
// ---------------------------------------------------------------------------

function showScreen(screenId) {
  ['home', 'lobby', 'game'].forEach((id) => {
    $(id).classList.toggle('hidden', id !== screenId);
  });
}

function updateConnectionPill() {
  const pill = $('connection-pill');
  const state = getConnectionState();
  const pendingCount = outbox ? outbox.getPendingCount() : 0;

  pill.className = `pill conn-${state}`;
  let label = state === 'online' ? '● Online' : state === 'reconnecting' ? '○ Reconnecting' : '✕ Offline';

  if (pendingCount > 0) {
    label += ` (${pendingCount} queued)`;
  }
  pill.textContent = label;
}

// ---------------------------------------------------------------------------
// API Helpers & Polling
// ---------------------------------------------------------------------------

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (session && session.token) {
    headers['X-Player-Token'] = session.token;
  }

  const t0 = performance.now();
  const resp = await apiFetch(path, { ...options, headers });
  const t1 = performance.now();

  const data = await resp.json();

  if (data.server_time) {
    absorbSnapshotTime(data.server_time, t0, t1);
  }

  if (!resp.ok) {
    if (resp.status === 404 && data.error === 'lobby_gone') {
      clearSession();
      showScreen('home');
      $('join-error').textContent = 'That lobby has ended.';
      throw new Error('That lobby has ended.');
    }
    if (resp.status === 401 && data.error === 'bad_token') {
      clearSession();
      showScreen('home');
      $('join-error').textContent = 'Your session expired. Please rejoin.';
      throw new Error('Your session expired.');
    }
    throw new Error(data.error || 'Server error');
  }

  return data;
}

function determinePollInterval() {
  if (!lastSnapshot || !lastSnapshot.round) return 1000; // Lobby
  const phase = lastSnapshot.round.phase;
  if (phase === 'locking' || phase === 'answering') return 250;
  if (phase === 'countdown' || phase === 'open') return 400;
  return 1000;
}

function scheduleNextPoll() {
  clearTimeout(pollTimer);
  const interval = determinePollInterval();
  pollTimer = setTimeout(async () => {
    await pollGame();
    scheduleNextPoll();
  }, interval);
}

function stopPolling() {
  clearTimeout(pollTimer);
}

async function pollGame() {
  if (!session) return;
  try {
    const snapshot = await api(`/api/game/${session.code}`);
    handleSnapshot(snapshot);
  } catch (err) {
    updateConnectionPill();
  }
}

// ---------------------------------------------------------------------------
// Snapshot & Round Engine
// ---------------------------------------------------------------------------

function handleSnapshot(snapshot) {
  lastSnapshot = snapshot;
  updateConnectionPill();

  // Update session host flag if changed
  if (snapshot.me) {
    session.host = snapshot.me.host;
    session.pid = snapshot.me.pid;
  }

  renderPlayers(snapshot.players);

  const round = snapshot.round;
  if (!round) {
    // In Lobby
    showScreen('lobby');
    $('lobby-code').textContent = session.code;
    $('host-controls').classList.toggle('hidden', !session.host);
    $('waiting-card').classList.toggle('hidden', session.host);
    stopAudio();
    currentRoundId = null;
    hasBuzzedThisRound = false;
    pendingEarlyAnswer = null;
    return;
  }

  // Active or Revealed Round
  showScreen('game');
  $('game-code').textContent = session.code;
  $('game-mode-badge').textContent = round.mode === 'race' ? 'Typing Race' : 'Buzzer';

  // Handle Round ID Change (Fresh Round)
  if (currentRoundId !== round.round_id) {
    currentRoundId = round.round_id;
    hasBuzzedThisRound = false;
    pendingEarlyAnswer = null;
    $('answer-input').value = '';
    $('answer-status').textContent = '';
    $('reveal-card').classList.add('hidden');

    // Preload audio and schedule playback
    if (round.source) {
      preloadAudio(round.source);
      schedulePlayback(round.start_at, 12);
    }
  }

  renderRound(round);
}

function renderPlayers(players) {
  if (!players) return;

  const html = players
    .map((p, idx) => {
      const isOnline = p.connected;
      const crown = idx === 0 && p.score > 0 ? '🏆 ' : '';
      const dot = `<span class="dot ${isOnline ? 'online' : 'offline'}"></span>`;
      const eliminatedBadge = p.eliminated ? ' <small style="color:var(--orange)">(eliminated)</small>' : '';
      return `
      <div class="player ${isOnline ? '' : 'dimmed'}">
        <span>${dot}${crown}<b>${escapeHtml(p.name)}</b>${eliminatedBadge}</span>
        <span class="score">${p.score} pts</span>
      </div>
    `;
    })
    .join('');

  if ($('lobby-players')) $('lobby-players').innerHTML = html;
  if ($('game-scoreboard')) $('game-scoreboard').innerHTML = html;
  if ($('player-count')) $('player-count').textContent = `(${players.length})`;
}

function renderRound(round) {
  const mePid = session.pid;
  const isRaceMode = round.mode === 'race';

  // Events Feed
  if (round.events && round.events.length > 0) {
    const recent = round.events.slice(-5).reverse();
    $('game-feed').innerHTML = recent
      .map((e) => {
        if (e.type === 'floor_awarded') return `<div>⚡ <b>${escapeHtml(e.name || 'Someone')}</b> claimed the floor!</div>`;
        if (e.type === 'answer_correct') return `<div>🎉 <b>${escapeHtml(e.name || 'Someone')}</b> got it! (+${e.points})</div>`;
        if (e.type === 'answer_wrong') return `<div>❌ <b>${escapeHtml(e.name || 'Someone')}</b> missed (-50)</div>`;
        if (e.type === 'disconnected_timeout') return `<div>⏳ <b>${escapeHtml(e.name || 'Someone')}</b> disconnected without answering</div>`;
        return '';
      })
      .filter(Boolean)
      .join('');
  }

  // Reveal Phase
  if (round.phase === 'revealed' && round.reveal) {
    $('reveal-card').classList.remove('hidden');
    $('reveal-title').textContent = round.reveal.title;
    $('reveal-artist').textContent = round.reveal.artist;
    $('reveal-winner').textContent = round.reveal.winner ? `🏆 Won by ${round.reveal.winner}` : 'No one got it this round!';
    $('host-next-controls').classList.toggle('hidden', !session.host);
    $('buzz-btn').classList.add('hidden');
    $('answer-form').classList.add('hidden');
    return;
  }

  // -------------------------------------------------------------------------
  // Buzzer vs Race Mode UI Controls
  // -------------------------------------------------------------------------
  if (isRaceMode) {
    $('buzz-btn').classList.add('hidden');
    $('answer-form').classList.remove('hidden');
    $('answer-input').disabled = round.phase !== 'open';
    $('answer-submit-btn').disabled = round.phase !== 'open';
    return;
  }

  // BUZZER MODE
  $('buzz-btn').classList.remove('hidden');

  const mePlayer = lastSnapshot.players.find((p) => p.pid === mePid);
  const isEliminated = mePlayer ? mePlayer.eliminated : false;

  const now = serverNow();
  const isHoldingFloor = round.floor && round.floor.pid === mePid;
  const someoneElseHoldsFloor = round.floor && round.floor.pid !== mePid;

  // 1. Eliminated
  if (isEliminated) {
    setBuzzerState('eliminated', '❌ Eliminated this round', true);
    $('answer-form').classList.add('hidden');
    return;
  }

  // 2. You Hold the Floor!
  if (isHoldingFloor) {
    setBuzzerState('floor-holder', '🎤 YOU HAVE THE FLOOR!', true);
    $('answer-form').classList.remove('hidden');
    $('answer-input').disabled = false;
    $('answer-submit-btn').disabled = false;

    // If early answer was queued while confirming, fire it now!
    if (pendingEarlyAnswer) {
      submitAnswerNow(pendingEarlyAnswer);
      pendingEarlyAnswer = null;
    }
    return;
  }

  // 3. Someone Else Holds the Floor
  if (someoneElseHoldsFloor) {
    const holderName = round.floor.name || 'A player';
    const msg = round.floor.holder_connected ? `🎤 ${holderName} is answering...` : `⏳ ${holderName} lost connection, holding floor...`;
    setBuzzerState('floor-other', msg, true);
    $('answer-form').classList.add('hidden');
    pendingEarlyAnswer = null;
    return;
  }

  // 4. Locking in progress
  if (round.phase === 'locking') {
    setBuzzerState('locking', '🔒 Locking in buzzes...', true);
    // Allow typing early even during lock window
    if (hasBuzzedThisRound) {
      $('answer-form').classList.remove('hidden');
    }
    return;
  }

  // 5. Open / Countdown Window
  if (hasBuzzedThisRound) {
    setBuzzerState('confirming', '⏳ Buzzed, confirming...', true);
    // Open answer input immediately so player can start typing!
    $('answer-form').classList.remove('hidden');
    return;
  }

  // Autonomous local button enablement: check local clock
  const isWithinWindow = now >= round.start_at && now <= round.buzz_close;
  if (isWithinWindow) {
    setBuzzerState('waiting', '🔔 BUZZ IN', false);
    $('answer-form').classList.add('hidden');
  } else if (now < round.start_at) {
    setBuzzerState('waiting', 'Wait for countdown...', true);
    $('answer-form').classList.add('hidden');
  } else {
    setBuzzerState('waiting', 'Round Over', true);
    $('answer-form').classList.add('hidden');
  }
}

function setBuzzerState(stateClass, label, disabled) {
  const btn = $('buzz-btn');
  btn.className = `buzz-button buzz-${stateClass}`;
  btn.textContent = label;
  btn.disabled = Boolean(disabled);
}

// ---------------------------------------------------------------------------
// Autonomous Local Timer Loop
// ---------------------------------------------------------------------------

function startLocalTimerLoop() {
  if (localTimerInterval) clearInterval(localTimerInterval);
  localTimerInterval = setInterval(() => {
    if (!lastSnapshot || !lastSnapshot.round) return;
    const round = lastSnapshot.round;
    const now = serverNow();

    if (round.phase === 'countdown') {
      const leftMs = Math.max(0, round.start_at - now);
      $('round-timer').textContent = `Starts in ${(leftMs / 1000).toFixed(1)}s`;
    } else if (round.phase === 'open' || round.phase === 'locking') {
      const leftSec = Math.max(0, Math.ceil((round.buzz_close - now) / 1000));
      $('round-timer').textContent = leftSec > 0 ? `${leftSec}s left` : 'Time up';
    } else if (round.phase === 'answering' && round.floor && round.floor.deadline) {
      const leftSec = Math.max(0, Math.ceil((round.floor.deadline - now) / 1000));
      $('round-timer').textContent = `Answer: ${leftSec}s`;
    } else {
      $('round-timer').textContent = 'Round over';
    }

    // Refresh telemetry in debug panel if visible
    updateDebugTelemetry();
  }, 100);
}

startLocalTimerLoop();

// ---------------------------------------------------------------------------
// Action Handlers (Buzzing & Answering)
// ---------------------------------------------------------------------------

function handleBuzzPointerDown(evt) {
  evt.preventDefault();
  unlockAudio();

  if (!lastSnapshot || !lastSnapshot.round) return;
  const round = lastSnapshot.round;

  if (hasBuzzedThisRound) return;

  const now = serverNow();

  // Instant tactile feedback
  hasBuzzedThisRound = true;
  setBuzzerState('confirming', '⏳ Buzzed, confirming...', true);
  $('answer-form').classList.remove('hidden');
  $('answer-input').focus();

  // Enqueue buzz action in persistent outbox BEFORE sending!
  const buzzId = generateId('buzz');
  const action = {
    type: 'buzz',
    round_id: round.round_id,
    buzz_id: buzzId,
    ts: now,
  };

  outbox.enqueue(action);
}

function handleAnswerSubmit(evt) {
  evt.preventDefault();
  const answerText = $('answer-input').value.trim();
  if (!answerText || !lastSnapshot || !lastSnapshot.round) return;

  const round = lastSnapshot.round;
  const now = serverNow();

  if (round.mode === 'race') {
    // In race mode, send immediately
    submitAnswerNow(answerText);
    $('answer-input').value = '';
    return;
  }

  // In buzzer mode:
  // If we already hold the floor, submit right now!
  if (round.floor && round.floor.pid === session.pid) {
    submitAnswerNow(answerText);
    $('answer-input').value = '';
  } else {
    // Hold early answer until snapshot confirms floor
    pendingEarlyAnswer = answerText;
    $('answer-status').textContent = 'Answer saved, sending the instant floor confirms...';
  }
}

function submitAnswerNow(answerText) {
  if (!lastSnapshot || !lastSnapshot.round) return;
  const round = lastSnapshot.round;
  const answerId = generateId('ans');
  const now = serverNow();

  const action = {
    type: 'answer',
    round_id: round.round_id,
    answer_id: answerId,
    answer: answerText,
    ts: now,
  };

  $('answer-status').textContent = getConnectionState() === 'offline' ? 'Saved offline, sending when you reconnect...' : 'Sending answer...';

  outbox.enqueue(action);
}

function handleActionResolved(action, outcome, freshSnapshot) {
  if (freshSnapshot) {
    handleSnapshot(freshSnapshot);
  }

  if (action.type === 'buzz') {
    if (outcome.status === 'late') {
      $('answer-status').textContent = outcome.reason || 'Too late: another player got the floor first.';
      hasBuzzedThisRound = false;
      pendingEarlyAnswer = null;
    } else if (outcome.status === 'rejected') {
      $('answer-status').textContent = outcome.reason || 'Buzz rejected.';
      hasBuzzedThisRound = false;
    }
  } else if (action.type === 'answer') {
    if (outcome.status === 'correct') {
      $('answer-status').textContent = `Correct! +${outcome.points} points 🎉`;
    } else if (outcome.status === 'incorrect') {
      $('answer-status').textContent = `Wrong answer (${outcome.points} pts). Eliminated for this round.`;
    } else if (outcome.status === 'timeout') {
      $('answer-status').textContent = 'Answer timed out.';
    }
  }
}

// ---------------------------------------------------------------------------
// UI Form & Button Listeners
// ---------------------------------------------------------------------------

function setupDomListeners() {
  // Sound controls
  $('sound-toggle-btn').onclick = () => {
    unlockAudio();
    const muted = !isSoundMuted();
    setSoundMuted(muted);
    $('sound-toggle-btn').textContent = muted ? '🔇 Sound OFF' : '🔊 Sound ON';
  };

  $('enable-sound-btn').onclick = () => {
    unlockAudio();
    $('sound-blocked-banner').classList.add('hidden');
  };

  // Host Form
  $('host-form').onsubmit = async (e) => {
    e.preventDefault();
    unlockAudio();
    $('host-error').textContent = '';
    const name = $('host-name').value.trim();
    try {
      const data = await api('/api/host', {
        method: 'POST',
        body: JSON.stringify({ name }),
      });
      setSession({ code: data.code, pid: data.pid, token: data.token, host: true, name });
      await enterLobby();
    } catch (err) {
      $('host-error').textContent = err.message;
    }
  };

  // Join Form
  $('join-form').onsubmit = async (e) => {
    e.preventDefault();
    unlockAudio();
    $('join-error').textContent = '';
    const name = $('join-name').value.trim();
    const code = $('join-code').value.trim();
    try {
      const data = await api('/api/join', {
        method: 'POST',
        body: JSON.stringify({ code, name }),
      });
      setSession({ code: data.code, pid: data.pid, token: data.token, host: false, name });
      await enterLobby();
    } catch (err) {
      $('join-error').textContent = err.message;
    }
  };

  // Start Round Button
  $('btn-start-round').onclick = async () => {
    $('lobby-error').textContent = '';
    const mode = $('mode-select').value;
    const playlistId = $('playlist-select').value;
    try {
      const res = await api(`/api/game/${session.code}/round`, {
        method: 'POST',
        body: JSON.stringify({ mode, playlist_id: playlistId }),
      });
      handleSnapshot(res.snapshot);
    } catch (err) {
      $('lobby-error').textContent = err.message;
    }
  };

  // Next Round Button on Reveal Card
  $('btn-next-round').onclick = async () => {
    // Re-open round setup in lobby or start next track directly
    showScreen('lobby');
  };

  // End Game Button
  $('btn-finish-game').onclick = async () => {
    try {
      await api(`/api/game/${session.code}/finish`, { method: 'POST' });
    } catch (e) {}
  };

  // Leave Lobby
  $('btn-leave-lobby').onclick = async () => {
    try {
      await api(`/api/game/${session.code}/leave`, { method: 'POST' });
    } catch (e) {}
    clearSession();
    showScreen('home');
    checkLastSessionRejoin();
  };

  // BUZZ BUTTON — Triggered on pointerdown for instant tactile response!
  $('buzz-btn').addEventListener('pointerdown', handleBuzzPointerDown);

  // Answer Form
  $('answer-form').onsubmit = handleAnswerSubmit;
}

async function enterLobby() {
  showScreen('lobby');
  $('lobby-code').textContent = session.code;
  $('game-code').textContent = session.code;
  $('host-controls').classList.toggle('hidden', !session.host);
  $('waiting-card').classList.toggle('hidden', session.host);

  if (session.host) {
    await loadPlaylists();
  }

  await pollGame();
  scheduleNextPoll();
}

async function loadPlaylists() {
  try {
    const playlists = await api('/api/library');
    $('playlist-select').innerHTML = playlists
      .map((pl) => `<option value="${escapeHtml(pl.id)}">${escapeHtml(pl.name)}</option>`)
      .join('');
  } catch (err) {
    $('lobby-error').textContent = err.message;
  }
}

function escapeHtml(str) {
  return String(str || '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[c]));
}

// ---------------------------------------------------------------------------
// Debug Panel (?debug=1)
// ---------------------------------------------------------------------------

function setupDebugPanel() {
  const params = new URLSearchParams(window.location.search);
  const debugActive = params.get('debug') === '1';

  if (!debugActive) return;

  $('debug-panel').classList.remove('hidden');

  $('debug-offline-toggle').onchange = (e) => {
    setDebugOption('simulateOffline', e.target.checked);
    updateConnectionPill();
  };

  $('debug-pause-outbox').onchange = (e) => {
    setDebugOption('pauseOutbox', e.target.checked);
  };

  $('debug-latency-select').onchange = (e) => {
    setDebugOption('artificialLatencyMs', Number(e.target.value));
  };

  let min = false;
  $('debug-toggle-expand').onclick = () => {
    min = !min;
    $('debug-body').classList.toggle('hidden', min);
    $('debug-toggle-expand').textContent = min ? 'Max' : 'Min';
  };
}

function updateDebugTelemetry() {
  if ($('debug-panel').classList.contains('hidden')) return;
  $('debug-offset').textContent = Math.round(getClockOffset());
  $('debug-rtt').textContent = Math.round(getLastRTT());
  $('debug-conn').textContent = getConnectionState();
  $('debug-outbox').textContent = outbox ? outbox.getPendingCount() : 0;
  $('debug-server-time').textContent = serverNow();
}

// Boot application
init();
