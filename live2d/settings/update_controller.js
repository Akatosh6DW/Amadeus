// Release-only update check and progress controller.  Network details stay in
// the local update routes; this controller only renders public release status.
(function attachUpdateController(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusUpdateController = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createUpdateControllerApi() {
  "use strict";

  function formatBytes(value) {
    const bytes = Math.max(0, Number(value) || 0);
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
    if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  }

  function createUpdateController({
    elements,
    request,
    confirm = () => true,
    notify = () => {},
    intervalMs = 1000,
  }) {
    if (!elements || typeof request !== "function") throw new TypeError("update controller ports are required");
    let bound = false;
    let polling = null;
    let latest = null;

    function setText(element, value) {
      if (element) element.textContent = value == null ? "" : String(value);
    }

    function render(payload = {}) {
      latest = payload;
      const phase = String(payload.phase || "idle");
      const available = payload.available === true;
      const releaseOnly = payload.code === "release_only";
      const current = payload.currentVersion ? `当前版本：v${payload.currentVersion}` : "当前版本：未知";
      setText(elements.current, current);
      if (releaseOnly) {
        setText(elements.status, payload.message || "开发模式不提供更新。 ");
        if (elements.check) elements.check.disabled = true;
        if (elements.start) elements.start.disabled = true;
        return payload;
      }
      if (payload.latestVersion) {
        setText(elements.latest, `最新版本：v${payload.latestVersion}`);
      }
      const total = Number(payload.totalBytes) || 0;
      const downloaded = Math.min(total, Number(payload.downloadedBytes) || 0);
      if (elements.progress) {
        elements.progress.max = Math.max(1, total);
        elements.progress.value = downloaded;
        elements.progress.hidden = !["downloading", "verifying", "staging", "restarting"].includes(phase);
      }
      if (elements.check) elements.check.disabled = ["checking", "downloading", "verifying", "staging", "restarting"].includes(phase);
      if (elements.start) elements.start.disabled = !available || ["downloading", "verifying", "staging", "restarting"].includes(phase);
      if (phase === "checking") setText(elements.status, "正在检查更新…");
      else if (phase === "downloading") setText(elements.status, `正在下载更新… ${formatBytes(downloaded)} / ${formatBytes(total)}`);
      else if (phase === "verifying") setText(elements.status, "正在校验更新文件…");
      else if (phase === "staging") setText(elements.status, "正在准备重启更新…");
      else if (phase === "restarting") setText(elements.status, "下载与校验完成，正在重启安装更新…");
      else if (phase === "failed") setText(elements.status, `更新失败：${payload.error || "未知错误"}`);
      else if (available) setText(elements.status, "发现新版本，可以立即更新。");
      else if (payload.latestVersion) setText(elements.status, "当前已是最新版本。");
      else setText(elements.status, "点击“检查更新”获取发行版信息。");
      return payload;
    }

    async function read(url, init) {
      const response = await request(url, init);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok && !payload.code) throw new Error(`HTTP ${response.status}`);
      return payload;
    }

    async function check() {
      render({ ...latest, phase: "checking" });
      try {
        return render(await read("/api/update/check", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        }));
      } catch (error) {
        const failed = { ...latest, phase: "failed", error: error.message };
        render(failed);
        return failed;
      }
    }

    async function refresh() {
      try {
        return render(await read("/api/update/status"));
      } catch (error) {
        const failed = { ...latest, phase: "failed", error: error.message };
        render(failed);
        return failed;
      }
    }

    function stopPolling() {
      if (polling !== null) {
        clearInterval(polling);
        polling = null;
      }
    }

    function startPolling() {
      stopPolling();
      polling = setInterval(async () => {
        try {
          const payload = await read("/api/update/status");
          render(payload);
          if (["failed", "restarting", "idle"].includes(String(payload.phase || ""))) stopPolling();
        } catch (error) {
          render({ ...latest, phase: "failed", error: error.message });
          stopPolling();
        }
      }, intervalMs);
    }

    async function start() {
      if (!latest || latest.available !== true) return { ok: false, code: "update_not_available" };
      if (!confirm("发现新版本。现在下载并重启更新吗？")) return { ok: false, cancelled: true };
      render({ ...latest, phase: "downloading" });
      try {
        const result = await read("/api/update/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ confirmed: true }),
        });
        render({ ...latest, ...result });
        if (result.ok) {
          notify("更新已开始，完成后应用会自动重启。");
          startPolling();
        }
        return result;
      } catch (error) {
        const failed = { ...latest, phase: "failed", error: error.message };
        render(failed);
        return failed;
      }
    }

    function bind() {
      if (bound) return false;
      bound = true;
      elements.check?.addEventListener("click", () => { check(); });
      elements.start?.addEventListener("click", () => { start(); });
      return true;
    }

    return Object.freeze({ bind, check, refresh, start, render, stop: stopPolling, getLatest: () => latest });
  }

  return Object.freeze({ createUpdateController, formatBytes });
}));
