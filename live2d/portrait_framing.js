(function (root) {
  "use strict";
  function fitPortrait(bounds, area, preferredScale, offsetX = 0, offsetY = 0) {
    const scale = Math.max(0.0001, Math.min(preferredScale,
      area.width / Math.max(1, bounds.width), area.height / Math.max(1, bounds.height)));
    const width = bounds.width * scale, height = bounds.height * scale;
    const left = Math.min(area.x + area.width - width,
      Math.max(area.x, area.x + (area.width - width) / 2 + offsetX));
    const top = Math.min(area.y + area.height - height,
      Math.max(area.y, area.y + area.height - height + offsetY));
    return { x: left - bounds.x * scale, y: top - bounds.y * scale, scale };
  }
  root.AmadeusPortraitFraming = { fitPortrait };
  if (typeof module !== "undefined") module.exports = { fitPortrait };
})(typeof window !== "undefined" ? window : globalThis);
