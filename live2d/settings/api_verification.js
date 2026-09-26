(function attachApiVerification(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusApiVerification = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createApiVerificationApi() {
  "use strict";

  /**
   * API 设置页只拥有本次页面会话的验证结论与配置代次。
   * 设置对象、通用保存事务和运行时连接文案仍由页面维护；保存成功或连接
   * 事件通过明确方法通知本控制器，旧的测试响应不能覆盖较新的 API 配置。
   */
  function createApiVerificationController({
    elements,
    getSettings,
    saveSettings,
    request,
    summarizeError,
    notify,
    savedApiKeyMask,
    onTestSettled,
  }) {
    let apiVerification = "unverified";
    let apiConfigurationRevision = 0;
    let bound = false;

    function label() {
      const settings = getSettings();
      if (!settings.modelConfigured) return "API 未配置";
      return {
        unverified: "API 已填写，未验证",
        verified: "API 连接已验证",
        invalid: "API Key 验证失败",
        failed: "API 连接失败",
      }[apiVerification];
    }

    function render() {
      if (elements.settingsStatus) {
        elements.settingsStatus.textContent = `${getSettings().apiModel || ""} / ${label()}`;
      }
    }

    function setVerification(value) {
      apiVerification = value;
      render();
    }

    function apiSettingsChanged(update, previousSettings) {
      const previous = previousSettings || {};
      return ["apiEndpoint", "apiModel", "apiNetworkMode", "apiProxyUrl", "useResponseFormat"]
        .some(key => Object.hasOwn(update, key) && update[key] !== previous[key])
        || (Object.hasOwn(update, "apiKey") && update.apiKey !== "__KEEP__");
    }

    function onSettingsSaved(update, previousSettings) {
      if (!apiSettingsChanged(update, previousSettings)) return false;
      apiConfigurationRevision += 1;
      setVerification("unverified");
      if (elements.apiTestStatus) {
        elements.apiTestStatus.textContent = "API 设置已更改，请保存后测试连接。";
        elements.apiTestStatus.classList.remove("error");
      }
      return true;
    }

    function observeConnection(data = {}) {
      const source = String(data.source || "").toLowerCase();
      if (data.connectionState === "failed") {
        setVerification(data.connectionCode === "upstream_http_401" ? "invalid" : "failed");
      } else if (source === "api" && !data.connectionState) {
        setVerification("verified");
      }
    }

    function secretInputValue(input) {
      const value = input?.value.trim() || "";
      return !value || value === savedApiKeyMask ? "__KEEP__" : value;
    }

    async function saveFromForm() {
      try {
        await saveSettings({
          apiEndpoint: elements.apiEndpointInput.value.trim(),
          apiModel: elements.apiModelInput.value.trim(),
          cognitiveRepairModel: elements.cognitiveRepairModelInput.value,
          apiKey: secretInputValue(elements.apiKeyInput),
          visionEnabled: elements.visionEnabledInput?.checked ?? true,
          visionApiKey: secretInputValue(elements.visionApiKeyInput),
          apiNetworkMode: elements.apiNetworkModeInput?.value || "auto",
          apiProxyUrl: elements.apiProxyUrlInput?.value.trim() || "",
          useResponseFormat: elements.responseFormatInput.checked,
        }, "API 设置已保存");
      } catch (error) {
        notify(`API 设置保存失败：${error.message}`);
      }
    }

    async function testConnection() {
      const revision = apiConfigurationRevision;
      elements.apiTestStatus.textContent = "正在连接 DeepSeek...";
      try {
        const response = await request("/api/test-api", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        const data = await response.json();
        if (revision !== apiConfigurationRevision) return;
        const verified = response.ok && data.ok === true;
        const transport = data.transport && typeof data.transport === "object" ? data.transport : null;
        const routeSummary = transport
          ? `；路线 ${transport.networkMode || "auto"} / ${transport.proxyConfigured ? `代理(${transport.proxySource || "configured"})` : "直连"}`
          : "";
        elements.apiTestStatus.textContent = `${data.message || (data.ok ? "API 连接正常" : "API 连接失败")}${routeSummary}`;
        elements.apiTestStatus.classList.toggle("error", !verified);
        setVerification(verified ? "verified" : data.code === "http_401" ? "invalid" : "failed");
        onTestSettled?.(label());
      } catch (error) {
        if (revision !== apiConfigurationRevision) return;
        elements.apiTestStatus.textContent = `测试失败：${summarizeError(error)}`;
        elements.apiTestStatus.classList.add("error");
        setVerification("failed");
        onTestSettled?.(label());
      }
    }

    function bindMaskedInput(input) {
      input?.addEventListener("focus", () => {
        if (input.value === savedApiKeyMask) input.select();
      });
      input?.addEventListener("beforeinput", () => {
        if (input.value === savedApiKeyMask) input.value = "";
      });
    }

    function bind() {
      if (bound) return;
      bound = true;
      bindMaskedInput(elements.apiKeyInput);
      bindMaskedInput(elements.visionApiKeyInput);
      elements.saveApiButton?.addEventListener("click", saveFromForm);
      elements.testApiButton?.addEventListener("click", testConnection);
    }

    return Object.freeze({
      bind,
      label,
      render,
      onSettingsSaved,
      observeConnection,
      saveFromForm,
      testConnection,
    });
  }

  return Object.freeze({ createApiVerificationController });
}));
