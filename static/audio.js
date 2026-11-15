/**
 * Soundbite Resilient Audio Controller
 *
 * Handles offline-capable audio Blobs.
 * Synchronizes start time with serverNow() and provides user-gesture unlocking
 * and per-device sound mute toggles.
 */

import { serverNow } from './clock.js';

let isMuted = false;
let audioUnlocked = false;
let currentSource = null;
let currentAudioElement = null;
let currentAudioBuffer = null;
let currentBufferSource = null;
let audioContext = null;
let currentBlobUrl = null;
let scheduledPlayTimer = null;

// Callbacks
let onAudioBlockedListener = null;

export function setOnAudioBlocked(fn) {
  onAudioBlockedListener = fn;
}

export function isSoundMuted() {
  return isMuted;
}

export function setSoundMuted(muted) {
  isMuted = muted;
  if (currentAudioElement) {
    currentAudioElement.muted = isMuted;
  }
}

/**
 * Call on first user interaction (e.g. Host/Join button click or "Enable Sound" tap)
 * to unlock browser autoplay policy.
 */
export function unlockAudio() {
  if (audioUnlocked) return;
  audioUnlocked = true;

  // Create and play silent 1-sample audio buffer to unlock Web Audio / HTML5 audio
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    if (ctx.state === 'suspended') {
      ctx.resume();
    }
  } catch (e) {}

  const silent = new Audio();
  silent.src = 'data:audio/wav;base64,UklGRigAAABXQVZFZm10IBIAAAABAAEARKwAAIhYAQACABAAAABkYXRhAgAAAAEA';
  silent.play().catch(() => {});
}

export function resumeAudio() {
  if (currentSource && currentSource.type === 'audio' && currentSource.reverse && currentAudioBuffer) {
    startPlayback(0);
    return;
  }
  if (currentAudioElement) {
    currentAudioElement.play().catch(() => {
      if (onAudioBlockedListener) onAudioBlockedListener(true);
    });
  }
}

/**
 * Preload audio during countdown so it can play uninterrupted even if offline.
 */
export async function preloadAudio(source) {
  currentSource = source;
  stopAudio();

  if (!source) return;

  if (source.type !== 'audio') {
    throw new Error(`Unsupported audio source type: ${source.type}`);
  }

  // Lobby uploads come with their own URL and need the player token
  const audioUrl = source.url || `/audio/${encodeURIComponent(source.id)}`;
  try {
    const headers = source.token ? { 'X-Player-Token': source.token } : {};
    const resp = await fetch(audioUrl, { headers });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    if (source.reverse) {
      audioContext = audioContext || new (window.AudioContext || window.webkitAudioContext)();
      currentAudioBuffer = await audioContext.decodeAudioData(await resp.arrayBuffer());
      for (let channel = 0; channel < currentAudioBuffer.numberOfChannels; channel++) {
        const samples = currentAudioBuffer.getChannelData(channel);
        for (let left = 0, right = samples.length - 1; left < right; left++, right--) {
          const value = samples[left];
          samples[left] = samples[right];
          samples[right] = value;
        }
      }
      return;
    }
    const blob = await resp.blob();
    currentBlobUrl = URL.createObjectURL(blob);
    currentAudioElement = new Audio(currentBlobUrl);
    currentAudioElement.muted = isMuted;
    currentAudioElement.preload = 'auto';
  } catch (err) {
    console.warn('Failed to preload audio into blob, using direct stream:', err);
    currentAudioElement = new Audio(audioUrl);
    currentAudioElement.muted = isMuted;
  }
}

/**
 * Starts audio playback at exact start_at epoch milliseconds using serverNow().
 * If joining mid-track, seeks to the elapsed position.
 */
export function schedulePlayback(startAtMs, durationSec = 12) {
  clearTimeout(scheduledPlayTimer);

  const now = serverNow();
  const delayMs = startAtMs - now;

  if (delayMs > 0) {
    scheduledPlayTimer = setTimeout(() => {
      startPlayback(0);
    }, delayMs);
  } else {
    // Round already started: seek to offset
    const elapsedSec = Math.max(0, (now - startAtMs) / 1000);
    if (elapsedSec < durationSec) {
      startPlayback(elapsedSec);
    }
  }
}

function startPlayback(startOffsetSec = 0) {
  if (!currentSource) return;

  if (currentSource.reverse && currentAudioBuffer && audioContext) {
    try {
      if (currentBufferSource) currentBufferSource.stop();
      currentBufferSource = audioContext.createBufferSource();
      currentBufferSource.buffer = currentAudioBuffer;
      currentBufferSource.connect(audioContext.destination);
      if (audioContext.state === 'suspended') audioContext.resume();
      currentBufferSource.start(0, Math.min(startOffsetSec, currentAudioBuffer.duration));
    } catch (err) {
      console.warn('Reversed audio playback failed:', err);
      if (onAudioBlockedListener) onAudioBlockedListener(true);
    }
    return;
  }
  if (!currentAudioElement) return;

  try {
    currentAudioElement.currentTime = startOffsetSec;
    currentAudioElement.muted = isMuted;
    const promise = currentAudioElement.play();
    if (promise !== undefined) {
      promise.catch((err) => {
        console.warn('Autoplay blocked:', err);
        if (onAudioBlockedListener) onAudioBlockedListener(true);
      });
    }
  } catch (e) {
    if (onAudioBlockedListener) onAudioBlockedListener(true);
  }
}

export function stopAudio() {
  clearTimeout(scheduledPlayTimer);

  if (currentAudioElement) {
    try {
      currentAudioElement.pause();
      currentAudioElement.currentTime = 0;
    } catch (e) {}
    currentAudioElement = null;
  }
  if (currentBufferSource) {
    try {
      currentBufferSource.stop();
    } catch (e) {}
    currentBufferSource = null;
  }
  currentAudioBuffer = null;

  if (currentBlobUrl) {
    try {
      URL.revokeObjectURL(currentBlobUrl);
    } catch (e) {}
    currentBlobUrl = null;
  }
}
