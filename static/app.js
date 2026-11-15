/**
 * Soundwich Client Controller
 *
 * Coordinates UI state, autonomous local round countdowns, resilient outbox flushing,
 * NTP clock sync, audio preloading, and server-authoritative buzzer racing.
 */

import { syncClock, startClockSync, serverNow, getClockOffset, getLastRTT, absorbSnapshotTime } from './clock.js';
import { Outbox, apiFetch, generateId, getConnectionState, onConnectionStateChange, debugConfig, setDebugOption } from './net.js';
import { preloadAudio, schedulePlayback, stopAudio, isSoundMuted, setSoundMuted, unlockAudio, resumeAudio, setOnAudioBlocked } from './audio.js';
import { attachButtonSfx, playClick } from './sfx.js';

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
let activeRoundSettings = null;
let autoAdvanceRoundId = null;
let autoAdvanceTimer = null;
// Song direction / speed options (Sped up and Slowed down also shift the pitch)
const AUDIO_EFFECT_RATES = { normal: 1, reverse: 1, fast: 1.35, slow: 0.75 };
// Slider settings for "how fast?" / "how slow?"
const SPEED_SLIDERS = {
  fast: { min: 1.1, max: 2, step: 0.05, value: 1.35, label: 'How fast?', minLabel: 'A little', maxLabel: 'Chipmunk 🐿️' },
  slow: { min: 0.5, max: 0.9, step: 0.05, value: 0.75, label: 'How slow?', minLabel: 'Sloth 🦥', maxLabel: 'A little' },
};
let rouletteGuessPid = null; // Music Roulette: my guess, shown instantly before the server confirms
let rouletteRenderKey = '';  // avoids rebuilding the guess buttons on every poll
// ---------------------------------------------------------------------------
// Initialization & Session Management
// ---------------------------------------------------------------------------

async function init() {
  setupDomListeners();
  setupDebugPanel();
  attachButtonSfx('#home button:not(.secondary), #lobby button'); // hover + click sounds on home + lobby buttons
  const qrCode = new URLSearchParams(location.search).get('code');    
  if (qrCode) $('join-code').value = qrCode;

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

  let data = {};
  try {
    data = await resp.json();
  } catch (err) {
    if (resp.ok) throw new Error('The server returned an invalid response.');
  }

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
    const error = new Error(data.error || 'Server error');
    error.status = resp.status;
    throw error;
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
  if (session.local) return;
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
  const uploadsChanged = lastSnapshot && lastSnapshot.upload_count !== snapshot.upload_count;
  lastSnapshot = snapshot;
  updateConnectionPill();

  // Update session host flag if changed
  if (snapshot.me) {
    session.host = snapshot.me.host;
    session.pid = snapshot.me.pid;
  }
  if (session.host && uploadsChanged) loadPlaylists();

  renderPlayers(snapshot.players);
  renderRouletteCount(snapshot);
  if (snapshot.lobby_mode) applyLobbyMode(snapshot.lobby_mode);

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
    activeRoundSettings = null;
    autoAdvanceRoundId = null;
    clearTimeout(autoAdvanceTimer);
    return;
  }

  // Active or Revealed Round
  showScreen('game');
  $('game-code').textContent = session.code;
  const modeLabels = { race: '⌨️ Typing Race', buzzer: '🔔 Buzzer', roulette: '🎲 Music Roulette' };
  $('game-mode-badge').textContent = modeLabels[round.mode] || 'Buzzer';
  $('round-heading').textContent = round.mode === 'roulette' ? 'Who uploaded this song?' : 'Name that sound';

  // Handle Round ID Change (Fresh Round)
  if (currentRoundId !== round.round_id) {
    currentRoundId = round.round_id;
    activeRoundSettings = {
      mode: round.mode,
      playlistId: round.playlist_id || $('playlist-select').value,
      reverseAudio: Boolean(round.reverse_audio),
      audioEffect: round.audio_effect || (round.reverse_audio ? 'reverse' : 'normal'),
      audioRate: round.audio_rate || null,
    };
    hasBuzzedThisRound = false;
    pendingEarlyAnswer = null;
    rouletteGuessPid = null;
    rouletteRenderKey = '';
    $('answer-input').value = '';
    $('answer-status').textContent = '';
    $('reveal-card').classList.add('hidden');

    // Preload audio and schedule playback
    if (round.source) {
      preloadAudio({
        ...round.source,
        reverse: Boolean(round.reverse_audio),
        rate: activeRoundSettings.audioRate || AUDIO_EFFECT_RATES[activeRoundSettings.audioEffect] || 1,
        token: session.token,
      });
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

function scheduleAutomaticNextRound(round) {
  if (!session.host || autoAdvanceRoundId === round.round_id || !activeRoundSettings) return;
  if (serverNow() < round.buzz_close) return;

  autoAdvanceRoundId = round.round_id;
  clearTimeout(autoAdvanceTimer);
  autoAdvanceTimer = setTimeout(async () => {
    const sentMode = activeRoundSettings ? activeRoundSettings.mode : null;
    try {
      const res = await api(`/api/game/${session.code}/round`, {
        method: 'POST',
        body: JSON.stringify({
          mode: activeRoundSettings.mode,
          playlist_id: activeRoundSettings.playlistId,
          reverse_audio: activeRoundSettings.reverseAudio,
          audio_effect: activeRoundSettings.audioEffect,
          audio_rate: activeRoundSettings.audioRate,
        }),
      });
      handleSnapshot(res.snapshot);
      if (res.outcome === 'game_finished') {
        // handleSnapshot above has already cleared activeRoundSettings, so use the mode we sent
        $('lobby-error').textContent = sentMode === 'roulette'
          ? "Every Music Roulette song has been played! Add more and start again."
          : 'That playlist is finished. Start a new game whenever you are ready.';
      }
    } catch (err) {
      $('answer-status').textContent = `Next song could not start: ${err.message}`;
      autoAdvanceRoundId = null;
    }
  }, 2000);
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
        if (e.type === 'roulette_guess') return `<div>🎲 <b>${escapeHtml(e.name || 'Someone')}</b> locked in a guess</div>`;
        return '';
      })
      .filter(Boolean)
      .join('');
  }

  // Reveal Phase
  if (round.phase === 'revealed' && round.reveal) {
    $('reveal-card').classList.remove('hidden');
    $('reveal-title').textContent = round.reveal.title;
    $('reveal-winner').textContent = round.reveal.winner ? `🏆 Won by ${round.reveal.winner}` : 'No one got it this round!';
    renderRouletteReveal(round);
    $('host-next-controls').classList.toggle('hidden', !session.host);
    $('buzz-btn').classList.add('hidden');
    $('answer-form').classList.add('hidden');
    $('roulette-choices').classList.add('hidden');
    $('play-sound-btn').classList.add('hidden');
    scheduleAutomaticNextRound(round);
    return;
  }

  // -------------------------------------------------------------------------
  // Music Roulette vs Buzzer vs Race Mode UI Controls
  // -------------------------------------------------------------------------
  if (round.mode === 'roulette') {
    $('buzz-btn').classList.add('hidden');
    $('answer-form').classList.add('hidden');
    $('play-sound-btn').classList.remove('hidden');
    renderRouletteChoices(round);
    return;
  }
  $('roulette-choices').classList.add('hidden');

  if (isRaceMode) {
    $('buzz-btn').classList.add('hidden');
    $('answer-form').classList.remove('hidden');
    $('play-sound-btn').classList.remove('hidden');
    $('answer-input').disabled = round.phase !== 'open';
    $('answer-submit-btn').disabled = round.phase !== 'open';
    return;
  }

  // BUZZER MODE
  $('buzz-btn').classList.remove('hidden');
  $('play-sound-btn').classList.remove('hidden');

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

// ---------------------------------------------------------------------------
// Music Roulette
// ---------------------------------------------------------------------------

function renderRouletteChoices(round) {
  const box = $('roulette-choices');
  box.classList.remove('hidden');
  const info = round.roulette || {};
  const myGuess = info.my_guess || rouletteGuessPid;
  const isOpen = round.phase === 'open';

  // Only rebuild the buttons when something that affects them changes
  const key = JSON.stringify([info.you_uploaded, myGuess, isOpen, info.choices]);
  if (key === rouletteRenderKey) return;
  rouletteRenderKey = key;

  if (info.you_uploaded) {
    box.innerHTML = `<p class="roulette-note">🎧 This one's yours! Sit back while everyone guesses.</p>`;
    return;
  }

  const choices = (info.choices || []).filter((c) => c.pid !== session.pid);
  box.innerHTML = choices
    .map((c) => {
      const picked = c.pid === myGuess;
      const disabled = !isOpen || Boolean(myGuess);
      return `<button type="button" class="roulette-choice${picked ? ' picked' : ''}" data-pid="${escapeHtml(c.pid)}"${disabled ? ' disabled' : ''}>${picked ? '👉 ' : ''}${escapeHtml(c.name)}</button>`;
    })
    .join('');

  box.querySelectorAll('.roulette-choice').forEach((btn) => {
    btn.onclick = () => submitRouletteGuess(btn.dataset.pid, btn.textContent.trim());
  });

  const waitMsg = 'Listen first - guessing opens when the song starts.';
  if (!myGuess && round.phase === 'countdown') {
    $('answer-status').textContent = waitMsg;
  } else if ($('answer-status').textContent === waitMsg) {
    $('answer-status').textContent = '';
  }
}

function submitRouletteGuess(pid, name) {
  if (rouletteGuessPid || !lastSnapshot || !lastSnapshot.round) return;
  unlockAudio();
  playClick();
  rouletteGuessPid = pid;
  rouletteRenderKey = '';
  renderRouletteChoices(lastSnapshot.round);
  submitAnswerNow(pid); // goes through the offline-safe outbox like any answer
  $('answer-status').textContent = `Locked in: ${name}. Waiting for the reveal...`;
}

function renderRouletteReveal(round) {
  const el = $('reveal-owner');
  if (round.mode !== 'roulette' || !round.reveal || !round.reveal.owner) {
    el.classList.add('hidden');
    return;
  }
  const guesses = (round.reveal.guesses || [])
    .map((g) => `<li>${g.correct ? '✅' : '❌'} <b>${escapeHtml(g.name)}</b> guessed ${escapeHtml(g.guess_name)}${g.points ? ` (+${g.points})` : ''}</li>`)
    .join('');
  el.innerHTML = `🎲 Uploaded by ${escapeHtml(round.reveal.owner)}${guesses ? `<ul>${guesses}</ul>` : '<ul><li>Nobody guessed in time!</li></ul>'}`;
  el.classList.remove('hidden');
}

function renderRouletteCount(snapshot) {
  if (!$('roulette-count') || snapshot.my_roulette_count === undefined) return;
  const mine = snapshot.my_roulette_count;
  const all = snapshot.roulette_count || 0;
  $('roulette-count').textContent = `You have ${mine} song${mine === 1 ? '' : 's'} in Music Roulette · ${all} in this lobby`;
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
      if (round.phase === 'revealed') scheduleAutomaticNextRound(round);
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
  } else if (action.type === 'answer' && outcome.roulette) {
    if (outcome.status === 'correct') {
      $('answer-status').textContent = `✅ You got it! +${outcome.points} points`;
    } else if (outcome.status === 'incorrect') {
      $('answer-status').textContent = '❌ Not them! Wait for the reveal...';
    } else if (outcome.status === 'rejected') {
      $('answer-status').textContent = outcome.reason || 'Guess not accepted.';
      // Let them try again if the guess never counted (e.g. sent before guessing opened)
      if (lastSnapshot && lastSnapshot.round && !(lastSnapshot.round.roulette || {}).my_guess) {
        rouletteGuessPid = null;
        rouletteRenderKey = '';
        renderRound(lastSnapshot.round);
      }
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
  // Colour theme picker (saved on this device)
  const themeSelect = $('theme-select');
  themeSelect.value = document.documentElement.dataset.theme || 'toast';
  themeSelect.onchange = () => {
    document.documentElement.dataset.theme = themeSelect.value;
    try { localStorage.setItem('soundbite_theme', themeSelect.value); } catch (e) {}
  };

  // Sound controls
  $('sound-toggle-btn').onclick = () => {
    unlockAudio();
    const muted = !isSoundMuted();
    setSoundMuted(muted);
    resumeAudio();
    $('sound-toggle-btn').textContent = muted ? '🔇 Sound OFF' : '🔊 Sound ON';
  };

  $('enable-sound-btn').onclick = () => {
    unlockAudio();
    resumeAudio();
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
      setSession({ code: data.code, pid: data.pid || data.player_id, token: data.token || null, host: true, name });
      await enterLobby();
    } catch (err) {
      if (err instanceof TypeError || err.status === 404 || err.status === 405 || err.status === 501) {
        startLocalDemo(name);
      } else {
        $('host-error').textContent = err.message;
      }
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
      setSession({ code: data.code, pid: data.pid || data.player_id, token: data.token || null, host: false, name });
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
    const audioEffect = $('audio-direction-select').value; // normal | reverse | fast | slow
    const reverseAudio = audioEffect === 'reverse';
    if (session.local) {
      $('lobby-error').textContent = 'Local demo lobby created. Start the Python server for shared rounds across devices.';
      return;
    }
    try {
      const res = await api(`/api/game/${session.code}/round`, {
        method: 'POST',
        body: JSON.stringify({
          mode,
          playlist_id: playlistId,
          reverse_audio: reverseAudio,
          audio_effect: audioEffect,
          audio_rate: SPEED_SLIDERS[audioEffect] ? Number($('speed-range').value) : null,
        }),
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
      const res = await api(`/api/game/${session.code}/finish`, { method: 'POST' });
      handleSnapshot(res.snapshot);
    } catch (err) {
      $('answer-status').textContent = `Could not end game: ${err.message}`;
    }
  };

  // Leave Lobby
  $('btn-leave-lobby').onclick = async () => {
    try {
      await api(`/api/game/${session.code}/leave`, { method: 'POST' });
    } catch (err) {
      $('join-error').textContent = `Could not leave lobby cleanly: ${err.message}`;
    }
    clearSession();
    showScreen('home');
    checkLastSessionRejoin();
  };

  $('btn-leave-match').onclick = async () => {
    try {
      await api(`/api/game/${session.code}/leave`, { method: 'POST' });
    } catch (err) {
      $('answer-status').textContent = `Could not leave match cleanly: ${err.message}`;
    }
    clearSession();
    showScreen('home');
    checkLastSessionRejoin();
  };

  // BUZZ BUTTON — Triggered on pointerdown for instant tactile response!
  $('buzz-btn').addEventListener('pointerdown', handleBuzzPointerDown);

  // Answer Form
  $('answer-form').onsubmit = handleAnswerSubmit;
  $('play-sound-btn').onclick = () => {
    unlockAudio();
    resumeAudio();
    $('play-sound-btn').textContent = '🔊 Sound playing';
  };

  $('upload-form').onsubmit = async (event) => {
    event.preventDefault();
    const count = await uploadMp3s($('mp3-files'), 'upload', $('upload-status'));
    if (count) {
      $('upload-status').textContent = `${count} MP3${count === 1 ? '' : 's'} added to the lobby.`;
      if (session.host) await loadPlaylists();
    }
  };

  // Music Roulette uploads: saved on the host laptop in Music roulette/<your name>/
  $('roulette-form').onsubmit = async (event) => {
    event.preventDefault();
    const count = await uploadMp3s($('roulette-files'), 'roulette-upload', $('roulette-status'));
    if (count) {
      $('roulette-status').textContent = `${count} song${count === 1 ? '' : 's'} added to Music Roulette 🎲`;
      await pollGame(); // refresh the song counts
    }
  };

  // Host picks a mode: update this screen now, and tell the server so every phone follows
  $('mode-select').onchange = async () => {
    const mode = $('mode-select').value;
    applyLobbyMode(mode);
    if (session.local) return;
    try {
      const res = await api(`/api/game/${session.code}/mode`, {
        method: 'POST',
        body: JSON.stringify({ mode }),
      });
      handleSnapshot(res.snapshot);
    } catch (err) {
      $('lobby-error').textContent = `Could not change mode: ${err.message}`;
    }
  };

  // Sped up / Slowed down: show a slider asking how fast or how slow
  $('audio-direction-select').onchange = () => {
    const cfg = SPEED_SLIDERS[$('audio-direction-select').value];
    $('speed-field').classList.toggle('hidden', !cfg);
    if (!cfg) return;
    const slider = $('speed-range');
    Object.assign(slider, { min: cfg.min, max: cfg.max, step: cfg.step });
    slider.value = cfg.value;
    $('speed-label').textContent = cfg.label;
    $('speed-min-label').textContent = cfg.minLabel;
    $('speed-max-label').textContent = cfg.maxLabel;
    $('speed-value').textContent = `${Number(slider.value).toFixed(2)}×`;
  };
  $('speed-range').oninput = () => {
    $('speed-value').textContent = `${Number($('speed-range').value).toFixed(2)}×`;
  };

  // 🎲 Random: pick any of the game modes at random, then apply it like a manual pick
  $('btn-random-mode').onclick = () => {
    const options = Array.from($('mode-select').options);
    const choice = options[Math.floor(Math.random() * options.length)];
    $('mode-select').value = choice.value;
    $('mode-select').dispatchEvent(new Event('change'));
    $('lobby-error').textContent = `🔀 Rolled: ${choice.textContent}`;
    const btn = $('btn-random-mode');
    btn.classList.remove('rolling');
    void btn.offsetWidth; // restart the wobble animation
    btn.classList.add('rolling');
  };
}

/** Shows the Music Roulette card (for everyone) only when Music Roulette is the chosen mode. */
function applyLobbyMode(mode) {
  const roulette = mode === 'roulette';
  $('roulette-form').classList.toggle('hidden', !roulette);
  // Music Roulette doesn't use the song library dropdown
  $('playlist-field').classList.toggle('hidden', roulette);
  $('roulette-mode-hint').classList.toggle('hidden', !roulette);
  // Keep the host's dropdown in step (e.g. after rejoining), unless they're using it right now
  if ($('mode-select').value !== mode && document.activeElement !== $('mode-select')) {
    $('mode-select').value = mode;
  }
}

/** Uploads the chosen MP3s one by one. Returns how many succeeded (0 on error). */
async function uploadMp3s(input, endpoint, statusEl) {
  const files = Array.from(input.files || []);
  if (!files.length) {
    statusEl.textContent = 'Choose at least one MP3 file.';
    return 0;
  }

  try {
    let done = 0;
    for (const file of files) {
      statusEl.textContent = `Uploading ${done + 1} of ${files.length}...`;
      const formData = new FormData();
      formData.append('file', file, file.name);
      const response = await fetch(`/api/game/${session.code}/${endpoint}`, {
        method: 'POST',
        headers: { 'X-Player-Token': session.token },
        body: formData,
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Upload failed.');
      done += 1;
    }
    input.value = '';
    return done;
  } catch (err) {
    statusEl.textContent = err.message;
    return 0;
  }
}

async function enterLobby() {
  showScreen('lobby');
  $('lobby-code').textContent = session.code;
  renderJoinQr();
  $('game-code').textContent = session.code;
  $('host-controls').classList.toggle('hidden', !session.host);
  $('waiting-card').classList.toggle('hidden', session.host);

  if (session.host) {
    await loadPlaylists();
  }

  if (session.local) {
    const players = [{ pid: session.pid, name: session.name, score: 0, connected: true }];
    handleSnapshot({ players, me: { pid: session.pid, host: true }, round: null });
    return;
  }

  await pollGame();
  scheduleNextPoll();
}

async function loadPlaylists() {
  try {
    // Every folder playlist under static/audio, plus "Player uploads" once anyone has uploaded
    const playlists = session.local ? [] : await api(`/api/game/${session.code}/library`);
    $('playlist-select').innerHTML = playlists
      .map((pl) => `<option value="${escapeHtml(pl.id)}" selected>${escapeHtml(pl.name)}</option>`)
      .join('');
  } catch (err) {
    $('lobby-error').textContent = err.message;
  }
}

function startLocalDemo(name) {
  const code = String(Math.floor(1000 + Math.random() * 9000));
  setSession({
    code,
    pid: generateId('local'),
    token: null,
    host: true,
    name,
    local: true,
  });
  $('host-error').textContent = '';
  enterLobby();
}

async function renderJoinQr() {
  // Always point the QR code at the Phone LAN address, even if this page was opened via localhost.
  let origin = location.origin;
  try {
    const resp = await fetch('/api/join-link');
    if (resp.ok) origin = (await resp.json()).url || origin;
  } catch (err) {
    // Fall back to the current page's address.
  }
  const link = `${origin}/?code=${session.code}`;
  const qr = qrcode(0, 'M');
  qr.addData(link);
  qr.make();
  $('join-qr').innerHTML = qr.createSvgTag({ cellSize: 6, margin: 2 });
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
