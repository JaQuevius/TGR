/**
 * Soundbite UI sound effects (hover + click), generated with Web Audio.
 * No audio files needed. Respects the Sound ON/OFF toggle.
 */

import { isSoundMuted } from './audio.js';

let ctx = null;

function getCtx() {
  if (!ctx) {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    ctx = new AC();
  }
  if (ctx.state === 'suspended') ctx.resume().catch(() => {});
  return ctx;
}

// One short tone that slides from startHz to endHz and fades out.
function tone({ type = 'sine', startHz, endHz, duration, volume, delay = 0 }) {
  const ac = getCtx();
  if (!ac || isSoundMuted()) return;
  const t0 = ac.currentTime + delay;
  const osc = ac.createOscillator();
  const gain = ac.createGain();
  osc.type = type;
  osc.frequency.setValueAtTime(startHz, t0);
  osc.frequency.exponentialRampToValueAtTime(endHz, t0 + duration);
  gain.gain.setValueAtTime(0.0001, t0);
  gain.gain.exponentialRampToValueAtTime(volume, t0 + 0.01);
  gain.gain.exponentialRampToValueAtTime(0.0001, t0 + duration);
  osc.connect(gain).connect(ac.destination);
  osc.start(t0);
  osc.stop(t0 + duration + 0.02);
}

// Hover: a soft, quick upward "tick"
export function playHover() {
  tone({ type: 'sine', startHz: 900, endHz: 1200, duration: 0.06, volume: 0.05 });
}

// Click: a bouncy two-note "pop"
export function playClick() {
  tone({ type: 'triangle', startHz: 520, endHz: 780, duration: 0.09, volume: 0.15 });
  tone({ type: 'triangle', startHz: 780, endHz: 1170, duration: 0.12, volume: 0.12, delay: 0.07 });
}

/**
 * Attach hover + click sounds to every button matching `selector`.
 * Hover only fires for a real mouse (phones have no hover, and tapping
 * would otherwise play both sounds at once).
 */
export function attachButtonSfx(selector) {
  // Browsers only allow audio after the first click/tap/key press.
  const unlock = () => getCtx();
  window.addEventListener('pointerdown', unlock, { once: true });
  window.addEventListener('keydown', unlock, { once: true });

  document.querySelectorAll(selector).forEach((btn) => {
    btn.addEventListener('pointerenter', (e) => {
      if (e.pointerType === 'mouse' && !btn.disabled) playHover();
    });
    btn.addEventListener('click', () => {
      if (btn.disabled) return;
      playClick();
      // Restart the bounce animation (see .btn-pop in styles.css)
      btn.classList.remove('btn-pop');
      void btn.offsetWidth;
      btn.classList.add('btn-pop');
    });
    btn.addEventListener('animationend', () => btn.classList.remove('btn-pop'));
  });
}
