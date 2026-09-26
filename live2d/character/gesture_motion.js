// 手势姿态控制器唯一持有 gestureMotion 的时序公式；表情生命周期、帧时钟和模型写入仍由各自 owner 持有。
// 页面只提交当前 expression/frame 快照和时间，返回本帧的不可变姿态目标，不复制聊天或语音状态。
(function attachGestureMotionController(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusGestureMotion = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createGestureMotionApi() {
  "use strict";

  function clamp(value, minimum, maximum) {
    return Math.max(minimum, Math.min(maximum, value));
  }

  function easeOutCubic(value) {
    return 1 - Math.pow(1 - value, 3);
  }

  function createGestureMotionController(options = {}) {
    const getExpressionSnapshot = typeof options.getExpressionSnapshot === "function"
      ? options.getExpressionSnapshot
      : () => ({ gestureEnteredAt: 0, gestureUntil: 0, lookAwayDirection: 1, furiousTurnDirection: 0 });
    const getFrameSnapshot = typeof options.getFrameSnapshot === "function"
      ? options.getFrameSnapshot
      : () => ({ tickerTime: 0 });

    function resolve(gesture, emotion, frameOptions = {}) {
      const expression = frameOptions.expression || getExpressionSnapshot();
      const frame = frameOptions.frame || getFrameSnapshot();
      const now = Number.isFinite(Number(frameOptions.now)) ? Number(frameOptions.now) : 0;
      const speaking = Boolean(frameOptions.speaking);
      const elapsed = Math.max(0, now - Number(expression.gestureEnteredAt || 0));
      const enter = easeOutCubic(clamp(elapsed / 340, 0, 1));
      const exit = clamp((Number(expression.gestureUntil || 0) - now) / 340, 0, 1);
      const weight = clamp(Math.min(enter, exit) || 0, 0, 1);
      const phase = elapsed / 1000;
      const talk = speaking ? Math.sin(Number(frame.tickerTime || 0) * 3.1) * 0.7 : 0;
      const motion = {
        headX: 0,
        headY: 0,
        headZ: 0,
        bodyX: 0,
        bodyY: 0,
        bodyZ: 0,
        stageX: 0,
        stageY: 0,
        stageTilt: 0,
        eyeX: 0,
        eyeY: 0,
        eyeClose: 0,
        eyeSmile: 0,
        mouthSmile: 0,
        breath: 0,
        hair: 0,
        arms: 0,
      };

      if (gesture === "nod") {
        motion.headY = (Math.sin(phase * Math.PI * 4.2) * 5.2 - 2.1) * weight;
        motion.bodyX = Math.sin(phase * Math.PI * 2.1) * 2.8 * weight;
        motion.stageY = Math.sin(phase * Math.PI * 4.2) * 5 * weight;
        motion.eyeY = -0.08 * weight;
        motion.arms = 0.08 * weight;
      } else if (gesture === "tilt") {
        motion.headX = -3.2 * weight;
        motion.headZ = (10.8 + Math.sin(phase * Math.PI * 1.6) * 3.8) * weight;
        motion.bodyX = -7.2 * weight;
        motion.bodyZ = -1.2 * weight;
        motion.stageTilt = 2.2 * weight;
        motion.eyeX = 0.18 * weight;
        motion.arms = 0.18 * weight;
      } else if (gesture === "look_away") {
        const direction = emotion === "furious"
          ? Number(expression.furiousTurnDirection || 0)
          : Number(expression.lookAwayDirection || 1);
        const baseTurn = emotion === "furious" ? 0 : emotion === "annoyed" ? 8.5 : 18;
        const turn = baseTurn + Math.sin(phase * Math.PI * 1.15) * (emotion === "annoyed" ? 1.2 : 2);
        motion.headX = direction * turn * weight;
        motion.headZ = -direction * 2.8 * weight;
        motion.bodyX = direction * (emotion === "annoyed" ? 2.6 : 5.2) * weight;
        motion.bodyZ = direction * 2.2 * weight;
        motion.stageX = direction * 3.2 * weight;
        motion.stageTilt = -direction * 0.7 * weight;
        motion.eyeX = direction * (emotion === "furious" ? 0 : emotion === "annoyed" ? 0.14 : 0.28) * weight;
        motion.eyeY = 0.04 * weight;
        motion.arms = 0.08 * weight;
      } else if (gesture === "lean") {
        motion.headY = -2.2 * weight + talk * 0.35 * weight;
        motion.bodyX = 8.5 * weight;
        motion.bodyY = -1.5 * weight;
        motion.stageY = -12 * weight;
        motion.stageX = 5 * weight;
        motion.eyeY = 0.08 * weight;
        motion.breath = 0.08 * weight;
        motion.arms = -0.12 * weight;
      } else if (gesture === "soften") {
        motion.headY = -4.5 * weight;
        motion.headZ = -2.1 * weight;
        motion.bodyX = -4.2 * weight;
        motion.stageY = 6 * weight;
        motion.stageTilt = -0.8 * weight;
        motion.eyeY = 0.16 * weight;
        motion.breath = -0.08 * weight;
        motion.arms = 0.22 * weight;
      } else if (gesture === "smile") {
        motion.headY = -3.8 * weight;
        motion.headZ = Math.sin(phase * Math.PI * 1.4) * 1.6 * weight;
        motion.bodyX = 3.6 * weight;
        motion.stageY = -5 * weight;
        motion.eyeClose = 0.98 * weight;
        motion.eyeSmile = 0.38 * weight;
        motion.mouthSmile = 0.08 * weight;
        motion.breath = 0.1 * weight;
        motion.arms = 0.14 * weight;
      } else if (gesture === "laugh") {
        const laughBeat = Math.sin(phase * Math.PI * 4.6);
        const laughPulse = Math.abs(laughBeat);
        motion.headY = (-2.8 + laughBeat * 2.6) * weight;
        motion.headZ = Math.sin(phase * Math.PI * 1.8) * 2.2 * weight;
        motion.bodyX = 4.8 * weight;
        motion.bodyY = laughPulse * 1.8 * weight;
        motion.stageY = (-7 + laughPulse * 3.2) * weight;
        motion.eyeClose = (0.28 + laughPulse * 0.22) * weight;
        motion.eyeSmile = 0.18 * weight;
        motion.mouthSmile = 0.12 * weight;
        motion.breath = (0.08 + laughPulse * 0.1) * weight;
        motion.hair = laughBeat * 0.12 * weight;
        motion.arms = 0.18 * weight;
      } else if (gesture === "glitch") {
        const snap = Math.sign(Math.sin(phase * 42)) * (0.7 + Math.sin(phase * 17) * 0.3);
        motion.headX = snap * 5.8 * weight;
        motion.headZ = -snap * 6.5 * weight;
        motion.bodyZ = snap * 3.4 * weight;
        motion.stageX = snap * 7 * weight;
        motion.stageTilt = snap * 1.8 * weight;
        motion.hair = snap * 0.5 * weight;
        motion.arms = snap * 0.12 * weight;
      } else if (gesture === "tap") {
        motion.headZ = Math.sin(phase * Math.PI * 5) * 3.4 * weight;
        motion.bodyX = Math.sin(phase * Math.PI * 3) * 1.7 * weight;
        motion.arms = -0.18 * weight;
      } else if (gesture === "recoil") {
        motion.headY = 4.5 * weight;
        motion.bodyX = -9.2 * weight;
        motion.bodyZ = -2.6 * weight;
        motion.stageY = 14 * weight;
        motion.stageX = -9 * weight;
        motion.eyeY = -0.14 * weight;
        motion.arms = -0.34 * weight;
      } else if (gesture === "explain") {
        motion.headX = Math.sin(phase * Math.PI * 1.7) * 3.2 * weight;
        motion.bodyX = Math.sin(phase * Math.PI * 1.2) * 5.2 * weight;
        motion.bodyZ = Math.sin(phase * Math.PI * 1.4) * 1.8 * weight;
        motion.stageX = Math.sin(phase * Math.PI * 1.2) * 4 * weight;
        motion.stageTilt = Math.sin(phase * Math.PI * 1.4) * 0.9 * weight;
        motion.eyeX = Math.sin(phase * Math.PI * 1.7) * 0.08 * weight;
        // Explain with one opening, a hold, and a return. A sine wave repeatedly
        // crossed the arm-pose threshold while the same sentence was playing.
        motion.arms = -0.26 * weight;
      } else if (gesture === "perk") {
        motion.headY = -5.2 * weight;
        motion.bodyX = 6.4 * weight;
        motion.stageY = -13 * weight;
        motion.eyeY = -0.18 * weight;
        motion.breath = 0.12 * weight;
        motion.hair = 0.28 * weight;
        motion.arms = -0.2 * weight;
      }

      if (!gesture) {
        const idleEmotion = emotion === "thinking" ? -1 : ["excited", "laughing"].includes(emotion) ? 1 : 0;
        const tickerTime = Number(frame.tickerTime || 0);
        motion.headX += Math.sin(tickerTime * 0.42) * 1.2;
        motion.bodyX += Math.sin(tickerTime * 0.34) * (2.4 + Math.abs(idleEmotion) * 0.6);
        motion.bodyZ += Math.sin(tickerTime * 0.27) * 0.7;
        motion.stageY += Math.sin(tickerTime * 0.46) * 2.2;
        motion.stageTilt += Math.sin(tickerTime * 0.25) * 0.28;
        motion.eyeX += Math.sin(tickerTime * 0.31) * 0.025;
        motion.arms += Math.sin(tickerTime * 0.33) * 0.04;
      }

      return Object.freeze(motion);
    }

    return Object.freeze({ resolve });
  }

  return Object.freeze({ createGestureMotionController });
}));
