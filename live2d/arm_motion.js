(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.AmadeusArmMotion = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const clamp = value => Math.max(0, Math.min(1, value));
  // Zero velocity and acceleration at both ends; cross the blended sleeve
  // region briskly instead of holding a translucent intermediate pose.
  const ease = t => t * t * t * (t * (t * 6 - 15) + 10);

  function createController() {
    let state = null;
    return {
      reset() { state = null; },
      update(target, { now, mode = "continuous", modelId = "" }) {
        target = clamp(target);
        const key = `${modelId}:${mode}`;
        if (!state || state.key !== key) {
          const value = mode === "endpoints" ? (target >= 0.5 ? 1 : 0) : target;
          state = { key, value, goal: value, from: value, start: now, duration: 0,
            lastTime: now, holdUntil: now, pending: value, pendingSince: now };
          return value;
        }
        const elapsed = Math.max(0, now - state.lastTime);
        state.lastTime = now;
        if (mode !== "endpoints") {
          state.value += (target - state.value) * (1 - Math.pow(0.88, elapsed / (1000 / 60)));
          return state.value;
        }

        const progress = state.duration ? clamp((now - state.start) / state.duration) : 1;
        state.value = state.from + (state.goal - state.from) * ease(progress);
        // Small idle/talk variations around 0.5 must not switch whole arm sets.
        const desired = state.goal === 0 ? (target >= 0.58 ? 1 : 0) : (target <= 0.42 ? 0 : 1);
        if (desired !== state.pending) {
          state.pending = desired;
          state.pendingSince = now;
        }
        // Finish an arm movement, then hold it before accepting a reversal.
        // Only the current target is kept; obsolete gestures are never queued.
        if (desired !== state.goal && progress === 1 && now >= state.holdUntil
            && now - state.pendingSince >= 100) {
          state.from = state.value;
          state.goal = desired;
          state.start = now;
          state.duration = desired === 1 ? 560 : 640;
          state.holdUntil = now + state.duration + 420;
        }
        return state.value;
      },
    };
  }

  return { createController };
});
