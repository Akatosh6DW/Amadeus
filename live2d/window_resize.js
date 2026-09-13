(function (root) {
  "use strict";
  function bind({ getMode, onFinish, onError }) {
    let active = null;
    document.querySelectorAll("[data-window-resize]").forEach(handle => {
      handle.addEventListener("pointerdown", async event => {
        const api = root.pywebview?.api;
        if (active || event.button !== 0 || !api?.beginWindowResize) return;
        event.preventDefault(); event.stopPropagation();
        const drag = { id: event.pointerId, mode: getMode(), x: event.screenX, y: event.screenY,
          dirty: false, ending: false, pumping: false, token: null };
        active = drag;
        handle.setPointerCapture(event.pointerId);
        document.body.classList.add("window-resizing");
        document.body.style.setProperty("--window-resize-cursor", getComputedStyle(handle).cursor);
        const cleanup = () => {
          handle.removeEventListener("pointermove", move);
          handle.removeEventListener("pointerup", end);
          handle.removeEventListener("pointercancel", end);
          handle.removeEventListener("lostpointercapture", end);
          root.removeEventListener("blur", end);
          if (handle.hasPointerCapture(drag.id)) handle.releasePointerCapture(drag.id);
          if (active === drag) active = null;
          document.body.classList.remove("window-resizing");
        };
        const pump = async () => {
          if (!drag.token || drag.pumping) return;
          drag.pumping = true;
          try {
            while (drag.dirty) {
              drag.dirty = false;
              const ending = drag.ending;
              const result = await api.updateWindowResize(drag.token, drag.x, drag.y, ending);
              if (!result?.ok) throw new Error(result?.error || "窗口缩放失败");
              if (ending) {
                cleanup();
                await onFinish(result, drag.mode);
                break;
              }
            }
          } catch (error) { cleanup(); onError(error); }
          finally { drag.pumping = false; }
        };
        const move = event => {
          if (event.pointerId !== drag.id || drag.ending) return;
          drag.x = event.screenX; drag.y = event.screenY; drag.dirty = true;
          void pump();
        };
        const end = event => {
          if (drag.ending) return;
          if (event.type === "pointerup") { drag.x = event.screenX; drag.y = event.screenY; }
          drag.ending = true; drag.dirty = true; void pump();
        };
        handle.addEventListener("pointermove", move);
        handle.addEventListener("pointerup", end);
        handle.addEventListener("pointercancel", end);
        handle.addEventListener("lostpointercapture", end);
        root.addEventListener("blur", end);
        try {
          const result = await api.beginWindowResize(handle.dataset.windowResize, drag.mode, event.screenX, event.screenY);
          if (!result?.ok) throw new Error(result?.error || "窗口缩放不可用");
          drag.token = result.token;
          void pump();
        } catch (error) { cleanup(); onError(error); }
      });
    });
    const enable = () => document.body.classList.toggle("native-window-resizable", Boolean(root.pywebview?.api?.beginWindowResize));
    root.addEventListener("pywebviewready", enable);
    enable();
  }
  root.AmadeusWindowResize = { bind };
})(window);
