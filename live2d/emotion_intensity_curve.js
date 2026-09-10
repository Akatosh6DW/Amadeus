(function exposeEmotionIntensityCurve(globalObject) {
  "use strict";

  const RAW_MIN = 0.15;
  const ORDINARY_PIVOT = 0.70;
  const ORDINARY_MAX = 1.25;
  const OUTBURST_RAW_MAX = 2.50;
  const OUTBURST_VISUAL_MAX = 1.55;
  const END_DENSITY_POWER = 2.20;

  function clampValue(value, minimum, maximum) {
    return Math.max(minimum, Math.min(maximum, value));
  }

  function endDenseUnit(value) {
    const normalized = clampValue(Number(value) || 0, 0, 1);
    if (normalized <= 0.5) {
      return 0.5 * Math.pow(normalized * 2, END_DENSITY_POWER);
    }
    return 1 - 0.5 * Math.pow((1 - normalized) * 2, END_DENSITY_POWER);
  }

  function map(rawIntensity) {
    const raw = clampValue(
      Number.isFinite(Number(rawIntensity)) ? Number(rawIntensity) : ORDINARY_PIVOT,
      RAW_MIN,
      OUTBURST_RAW_MAX,
    );
    if (raw <= ORDINARY_MAX) {
      const normalized = (raw - RAW_MIN) / (ORDINARY_MAX - RAW_MIN);
      return RAW_MIN + endDenseUnit(normalized) * (ORDINARY_MAX - RAW_MIN);
    }
    const outburstProgress = (
      (raw - ORDINARY_MAX) / (OUTBURST_RAW_MAX - ORDINARY_MAX)
    );
    return ORDINARY_MAX
      + outburstProgress * (OUTBURST_VISUAL_MAX - ORDINARY_MAX);
  }

  const api = Object.freeze({
    RAW_MIN,
    ORDINARY_PIVOT,
    ORDINARY_MAX,
    OUTBURST_RAW_MAX,
    OUTBURST_VISUAL_MAX,
    END_DENSITY_POWER,
    endDenseUnit,
    map,
  });

  globalObject.AmadeusEmotionIntensity = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : window);
