(function attachProactiveLogic(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusProactiveLogic = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createProactiveLogic() {
  "use strict";

  const CATEGORY_IDS = Object.freeze([
    "neuroscience",
    "ai_technology",
    "experiments",
    "games_scifi",
    "books_research",
    "daily_science",
  ]);

  function silenceLimit(randomValue = Math.random()) {
    return Number(randomValue) < 0.56 ? 3 : 4;
  }

  function nextState(unansweredCount, limit) {
    const normalizedLimit = Number(limit) === 4 ? 4 : 3;
    const nextCount = Math.min(normalizedLimit, Math.max(0, Number(unansweredCount) || 0) + 1);
    const willSulk = nextCount >= normalizedLimit;
    return {
      nextCount,
      willSulk,
      phase: willSulk ? "sulking" : "waiting",
      shouldScheduleVoice: !willSulk,
    };
  }

  function normalizeCategories(value, enabled = true) {
    const allowed = new Set(CATEGORY_IDS);
    const selected = Array.isArray(value)
      ? [...new Set(value.map(String).filter(item => allowed.has(item)))]
      : [...CATEGORY_IDS];
    if (selected.length || !enabled) return selected;
    return [...CATEGORY_IDS];
  }

  return Object.freeze({ CATEGORY_IDS, silenceLimit, nextState, normalizeCategories });
}));
