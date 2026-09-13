(function (root) {
  "use strict";
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
  const number = (value, fallback) => Number.isFinite(Number(value)) ? Number(value) : fallback;

  function normalizeView(value = {}) {
    return {
      focusX: clamp(number(value?.focusX, 0.5), 0, 1),
      focusY: clamp(number(value?.focusY, 0.5), 0, 1),
      zoom: clamp(number(value?.zoom, 1), 1, 3),
    };
  }

  function normalizeViews(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value).filter(([, view]) => (
      view && typeof view === "object" && !Array.isArray(view)
    )).map(([key, view]) => [key, normalizeView(view)]));
  }

  function viewKey(preset, path = "") {
    return preset === "custom" ? `custom:${path}` : preset;
  }

  function layout(sourceWidth, sourceHeight, width, height, value) {
    const view = normalizeView(value);
    const scale = Math.max(width / Math.max(1, sourceWidth), height / Math.max(1, sourceHeight)) * view.zoom;
    const imageWidth = sourceWidth * scale, imageHeight = sourceHeight * scale;
    const halfX = Math.min(0.5, width / (2 * Math.max(1, imageWidth)));
    const halfY = Math.min(0.5, height / (2 * Math.max(1, imageHeight)));
    const focusX = clamp(view.focusX, halfX, 1 - halfX);
    const focusY = clamp(view.focusY, halfY, 1 - halfY);
    return {
      width: imageWidth, height: imageHeight,
      x: width / 2 - focusX * imageWidth, y: height / 2 - focusY * imageHeight,
      focusX, focusY, zoom: view.zoom,
    };
  }

  function panView(sourceWidth, sourceHeight, width, height, value, dx, dy) {
    const current = layout(sourceWidth, sourceHeight, width, height, value);
    const next = layout(sourceWidth, sourceHeight, width, height, {
      focusX: current.focusX - dx / current.width,
      focusY: current.focusY - dy / current.height,
      zoom: current.zoom,
    });
    return { focusX: next.focusX, focusY: next.focusY, zoom: next.zoom };
  }

  function cropRect(sourceWidth, sourceHeight, ratio, view) {
    const position = layout(sourceWidth, sourceHeight, ratio * 1000, 1000, view);
    return { x: -position.x / position.width, y: -position.y / position.height,
      width: ratio * 1000 / position.width, height: 1000 / position.height };
  }

  function previewGeometry(sourceWidth, sourceHeight, viewport, view) {
    const ratio = clamp(viewport.width / Math.max(1, viewport.height), viewport.minRatio, viewport.maxRatio);
    const crops = {
      narrow: cropRect(sourceWidth, sourceHeight, viewport.minRatio, view),
      wide: cropRect(sourceWidth, sourceHeight, viewport.maxRatio, view),
      current: cropRect(sourceWidth, sourceHeight, ratio, view),
    };
    // Keep the preview coordinate system anchored to the complete source image.
    // The guides are the crops; the image itself must never pan or zoom while
    // the user edits the focus point.
    return { crops, ratio, bounds: { x: 0, y: 0, width: 1, height: 1 } };
  }

  function mount({ frame, image, guides, narrowLabel, wideLabel, zoomInput, zoomValue, resetButton, status, getViewport, onChange }) {
    let sourceUrl = "", view = normalizeView(), natural = null, drag = null, generation = 0;
    let geometry = null;
    const paint = () => {
      zoomInput.value = String(Math.round(view.zoom * 100));
      zoomValue.textContent = `${Math.round(view.zoom * 100)}%`;
      zoomInput.disabled = resetButton.disabled = !natural;
      if (!natural) return;
      const rect = frame.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      geometry = previewGeometry(natural.width, natural.height, getViewport(), view);
      Object.assign(image.style, {
        width: `${rect.width}px`, height: `${rect.height}px`, left: "0px", top: "0px",
      });
      for (const [name, guide] of Object.entries(guides)) {
        const crop = geometry.crops[name];
        Object.assign(guide.style, {
          left: `${crop.x * 100}%`, top: `${crop.y * 100}%`,
          width: `${crop.width * 100}%`, height: `${crop.height * 100}%`,
        });
      }
    };
    const refresh = () => {
      const viewport = getViewport();
      const ratio = natural ? natural.width / Math.max(1, natural.height) : viewport.maxRatio;
      frame.style.aspectRatio = String(ratio);
      frame.style.width = `min(100%, ${300 * ratio}px)`;
      narrowLabel.textContent = `最窄 ${viewport.minRatio.toFixed(2)}:1`;
      wideLabel.textContent = `最宽 ${viewport.maxRatio.toFixed(2)}:1`;
      paint();
    };
    const commit = next => {
      view = normalizeView(next);
      if (natural) {
        const viewport = getViewport();
        const ratio = clamp(viewport.width / Math.max(1, viewport.height), viewport.minRatio, viewport.maxRatio);
        const fitted = layout(natural.width, natural.height, ratio * 1000, 1000, view);
        view = { focusX: fitted.focusX, focusY: fitted.focusY, zoom: fitted.zoom };
      }
      refresh();
      onChange({ ...view });
    };
    const stopDrag = () => {
      const pointerId = drag?.id;
      drag = null;
      frame.classList.remove("dragging");
      if (pointerId != null && frame.hasPointerCapture(pointerId)) frame.releasePointerCapture(pointerId);
    };
    frame.addEventListener("pointerdown", event => {
      if (!natural || event.button !== 0 || drag || event.target !== guides.current) return;
      event.preventDefault();
      const rect = frame.getBoundingClientRect();
      const crop = geometry.crops.current;
      drag = { id: event.pointerId, x: event.clientX, y: event.clientY,
        imageWidth: Math.max(1, rect.width), imageHeight: Math.max(1, rect.height),
        view: { focusX: crop.x + crop.width / 2, focusY: crop.y + crop.height / 2, zoom: view.zoom } };
      frame.setPointerCapture(event.pointerId);
      frame.classList.add("dragging");
      frame.focus({ preventScroll: true });
    });
    frame.addEventListener("pointermove", event => {
      if (!drag || event.pointerId !== drag.id) return;
      const next = layout(natural.width, natural.height, geometry.ratio * 1000, 1000, {
        focusX: drag.view.focusX + (event.clientX - drag.x) / drag.imageWidth,
        focusY: drag.view.focusY + (event.clientY - drag.y) / drag.imageHeight,
        zoom: drag.view.zoom,
      });
      commit(next);
      drag.x = event.clientX;
      drag.y = event.clientY;
      drag.view = { ...view };
    });
    for (const event of ["pointerup", "pointercancel", "lostpointercapture"]) frame.addEventListener(event, stopDrag);
    frame.addEventListener("keydown", event => {
      const direction = { ArrowLeft: [-16, 0], ArrowRight: [16, 0], ArrowUp: [0, -16], ArrowDown: [0, 16] }[event.key];
      if (!natural || !direction) return;
      event.preventDefault();
      const crop = geometry.crops.current;
      const rect = frame.getBoundingClientRect();
      commit({ focusX: crop.x + crop.width / 2 + direction[0] / Math.max(1, rect.width),
        focusY: crop.y + crop.height / 2 + direction[1] / Math.max(1, rect.height), zoom: view.zoom });
    });
    zoomInput.addEventListener("input", () => {
      stopDrag();
      const crop = geometry?.crops.current;
      commit({ focusX: crop ? crop.x + crop.width / 2 : view.focusX,
        focusY: crop ? crop.y + crop.height / 2 : view.focusY, zoom: Number(zoomInput.value) / 100 });
    });
    resetButton.addEventListener("click", () => { stopDrag(); commit(normalizeView()); });
    new ResizeObserver(paint).observe(frame);
    return {
      refresh,
      setSource(url, nextView) {
        view = normalizeView(nextView);
        if (url === sourceUrl) { refresh(); return; }
        stopDrag();
        sourceUrl = url;
        const request = ++generation;
        natural = null;
        image.hidden = true;
        status.textContent = url ? "正在加载预览…" : "选择图片后可调整取景";
        refresh();
        if (!url) return;
        const loader = new Image();
        loader.onload = () => {
          if (request !== generation) return;
          natural = { width: loader.naturalWidth, height: loader.naturalHeight };
          image.src = url;
          image.hidden = false;
          status.textContent = "拖动白色取景框调整焦点；方向键也可移动。";
          refresh();
        };
        loader.onerror = () => {
          if (request !== generation) return;
          status.textContent = "图片加载失败，请重新选择。";
        };
        loader.src = url;
      },
    };
  }

  const api = { normalizeView, normalizeViews, viewKey, layout, panView, cropRect, previewGeometry, mount };
  root.AmadeusBackgroundEditor = api;
  if (typeof module !== "undefined") module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
