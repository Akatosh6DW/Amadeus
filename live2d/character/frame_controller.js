// 逐帧视线控制器唯一持有指针、眨眼、呼吸时钟和眼睛/头部目标的平滑状态。
// 页面只提交帧增量与表情语义，再读取不可变快照写入模型参数；口型、背景和开发者覆盖仍由各自 owner 负责。
(function attachFrameController(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusFrameController = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createFrameControllerApi() {
  "use strict";

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function number(value, fallback) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function copyVector(value) {
    return {
      x: number(value?.x, 0),
      y: number(value?.y, 0),
    };
  }

  function createFrameController(options = {}) {
    const clock = options.clock || {};
    const now = typeof clock.now === "function"
      ? clock.now
      : () => (typeof performance !== "undefined" && typeof performance.now === "function"
        ? performance.now()
        : Date.now());
    const random = typeof clock.random === "function" ? clock.random : () => Math.random();
    const initialNow = number(options.initialNow, number(now(), 0));
    const state = {
      tickerTime: 0,
      blinkUntil: 0,
      nextBlink: initialNow + 1400,
      blink: 0,
      eyeOpen: 1,
      breath: 0.5,
      idleSway: 0,
      pointer: { x: 0, y: 0 },
      headLook: { x: 0, y: 0 },
      eyeLook: { x: 0, y: 0 },
      eyeDrift: { x: 0, y: 0, nextAt: initialNow + 900 },
      shyGazeAwayDirection: random() < 0.5 ? -1 : 1,
    };

    function getState() {
      return {
        tickerTime: state.tickerTime,
        blinkUntil: state.blinkUntil,
        nextBlink: state.nextBlink,
        blink: state.blink,
        eyeOpen: state.eyeOpen,
        breath: state.breath,
        idleSway: state.idleSway,
        pointer: copyVector(state.pointer),
        headLook: copyVector(state.headLook),
        eyeLook: copyVector(state.eyeLook),
        eyeDrift: {
          x: state.eyeDrift.x,
          y: state.eyeDrift.y,
          nextAt: state.eyeDrift.nextAt,
        },
        shyGazeAwayDirection: state.shyGazeAwayDirection,
      };
    }

    function setPointer(pointer) {
      state.pointer.x = clamp(number(pointer?.x, 0), -1, 1);
      state.pointer.y = clamp(number(pointer?.y, 0), -1, 1);
      return getState();
    }

    function advance(delta, optionsForFrame = {}) {
      const frameNow = number(optionsForFrame.now, number(now(), 0));
      state.tickerTime += number(delta, 0) / 60;
      if (frameNow > state.nextBlink) {
        state.blinkUntil = frameNow + 105;
        state.nextBlink = frameNow + 1800 + random() * 3400;
      }
      state.blink = frameNow < state.blinkUntil
        ? Math.max(0, (state.blinkUntil - frameNow) / 105)
        : 0;
      state.eyeOpen = 1 - Math.sin(state.blink * Math.PI);
      state.breath = 0.5 + Math.sin(state.tickerTime * 2.2) * 0.42;
      state.idleSway = Math.sin(state.tickerTime * 0.9);
      return getState();
    }

    function updateLookTargets(optionsForFrame = {}) {
      const frameNow = number(optionsForFrame.now, number(now(), 0));
      const expression = optionsForFrame.expression && typeof optionsForFrame.expression === "object"
        ? optionsForFrame.expression
        : {};
      const visualEmotion = String(optionsForFrame.visualEmotion || "neutral");
      const avoidsDirectGaze = ["shy", "flustered"].includes(visualEmotion);
      // 眼睛避开指针时保留上一次方向，只有明显越过脸部中线才翻转，避免每帧抖动。
      if (avoidsDirectGaze) {
        if (state.pointer.x > 0.14) state.shyGazeAwayDirection = -1;
        else if (state.pointer.x < -0.14) state.shyGazeAwayDirection = 1;
      }
      if (frameNow > state.eyeDrift.nextAt) {
        const shyBias = avoidsDirectGaze ? state.shyGazeAwayDirection * 0.08 : 0;
        state.eyeDrift = {
          x: clamp((random() - 0.5) * 0.26 + shyBias, -0.32, 0.32),
          y: clamp(
            (random() - 0.5) * 0.16
              + (expression.currentEmotion === "thinking" ? -0.08 : 0),
            -0.22,
            0.22,
          ),
          nextAt: frameNow + 850 + random() * 1700,
        };
      }
      // 眼睛先转向，指针靠近边缘后才让头部跟随，保留原来的响应比例。
      const overflowX = Math.sign(state.pointer.x)
        * clamp((Math.abs(state.pointer.x) - 0.56) / 0.44, 0, 1);
      const overflowY = Math.sign(state.pointer.y)
        * clamp((Math.abs(state.pointer.y) - 0.48) / 0.52, 0, 1);
      const headTarget = visualEmotion === "furious"
        ? { x: 0, y: 0 }
        : {
          x: clamp(state.pointer.x * 0.18 + overflowX * 0.38, -0.54, 0.54),
          y: clamp(state.pointer.y * 0.16 + overflowY * 0.4, -0.54, 0.54),
        };
      const eyeTarget = visualEmotion === "furious"
        ? { x: 0, y: 0 }
        : avoidsDirectGaze
          ? {
            x: state.shyGazeAwayDirection
              * (0.48 + clamp(Math.abs(state.pointer.x), 0, 1) * 0.14),
            y: clamp(state.pointer.y * 0.38 + state.eyeDrift.y * 0.22, -0.46, 0.46),
          }
          : {
            x: clamp(state.pointer.x * 0.9 + state.eyeDrift.x * 0.55, -0.92, 0.92),
            y: clamp(state.pointer.y * 0.98 + state.eyeDrift.y * 0.45, -0.96, 0.96),
          };
      state.headLook.x += (headTarget.x - state.headLook.x) * 0.055;
      state.headLook.y += (headTarget.y - state.headLook.y) * 0.055;
      state.eyeLook.x += (eyeTarget.x - state.eyeLook.x) * 0.2;
      state.eyeLook.y += (eyeTarget.y - state.eyeLook.y) * 0.2;
      return getState();
    }

    return Object.freeze({
      advance,
      getState,
      setPointer,
      updateLookTargets,
    });
  }

  return Object.freeze({ createFrameController });
}));
