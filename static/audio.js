/**
 * Soundbite Resilient Audio Controller
 *
 * Handles both offline-capable audio Blobs and hidden YouTube IFrame playback.
 * Synchronizes start time with serverNow() and provides user-gesture unlocking
 * and per-device sound mute toggles.
 */

import { serverNow } from './clock.js';

let isMuted = false;
let audioUnlocked = false;
let currentSource = null;
let currentAudioElement = null;
let currentBlobUrl = null;
let ytPlayer = null;
let ytReady = false;
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
  if (ytPlayer && ytPlayer.mute && ytPlayer.unMute) {
    if (isMuted) ytPlayer.mute();
    else ytPlayer.unMute();
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

/**
 * Preload audio during countdown so it can play uninterrupted even if offline.
 */
export async function preloadAudio(source) {
  currentSource = source;
  stopAudio();

  if (!source) return;

  if (source.type === 'audio') {
    const audioUrl = `/audio/${encodeURIComponent(source.id)}`;
    try {
      const resp = await fetch(audioUrl);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
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
  } else if (source.type === 'youtube') {
    ensureYouTubeApiLoaded();
    cueYouTube(source.videoId, source.start || 0);
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

  if (currentSource.type === 'audio' && currentAudioElement) {
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
  } else if (currentSource.type === 'youtube') {
    playYouTube(startOffsetSec);
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

  if (currentBlobUrl) {
    try {
      URL.revokeObjectURL(currentBlobUrl);
    } catch (e) {}
    currentBlobUrl = null;
  }

  if (ytPlayer && ytPlayer.stopVideo) {
    try {
      ytPlayer.stopVideo();
    } catch (e) {}
  }
}

// ---------------------------------------------------------------------------
// YouTube IFrame API Support (Behind Opaque Visual Curtain)
// ---------------------------------------------------------------------------

let ytApiLoaded = false;

function ensureYouTubeApiLoaded() {
  if (ytApiLoaded) return;
  ytApiLoaded = true;

  if (window.YT && window.YT.Player) {
    ytReady = true;
    return;
  }

  const tag = document.createElement('script');
  tag.src = 'https://www.youtube.com/iframe_api';
  const firstScriptTag = document.getElementsByTagName('script')[0];
  if (firstScriptTag && firstScriptTag.parentNode) {
    firstScriptTag.parentNode.insertBefore(tag, firstScriptTag);
  } else {
    document.head.appendChild(tag);
  }

  window.onYouTubeIframeAPIReady = () => {
    ytReady = true;
    initYouTubePlayer();
  };
}

function initYouTubePlayer() {
  const container = document.getElementById('youtube-player-mount');
  if (!container) return;

  ytPlayer = new window.YT.Player('youtube-player-mount', {
    height: '200',
    width: '200',
    playerVars: {
      autoplay: 0,
      controls: 0,
      disablekb: 1,
      modestbranding: 1,
      rel: 0,
      playsinline: 1,
    },
    events: {
      onReady: () => {
        if (isMuted && ytPlayer.mute) ytPlayer.mute();
      },
      onError: (e) => {
        console.warn('YouTube Player error:', e);
      },
    },
  });
}

function cueYouTube(videoId, startSeconds = 0) {
  if (!ytPlayer || !ytPlayer.cueVideoById) {
    setTimeout(() => {
      if (ytPlayer && ytPlayer.cueVideoById) {
        ytPlayer.cueVideoById({ videoId, startSeconds });
      }
    }, 500);
    return;
  }
  ytPlayer.cueVideoById({ videoId, startSeconds });
}

function playYouTube(startOffsetSec = 0) {
  if (!ytPlayer || !ytPlayer.seekTo || !ytPlayer.playVideo) return;
  try {
    if (isMuted) ytPlayer.mute();
    else ytPlayer.unMute();
    ytPlayer.seekTo(startOffsetSec, true);
    ytPlayer.playVideo();
  } catch (err) {
    console.warn('YouTube playVideo failed:', err);
    if (onAudioBlockedListener) onAudioBlockedListener(true);
  }
}
