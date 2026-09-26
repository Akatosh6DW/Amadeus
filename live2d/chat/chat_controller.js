// 普通聊天控制器独占请求代次、Abort、流事件、待交付助手消息和当前请求。
// 页面只通过明确的传输、播放、会话和反馈端口协作；上下文切换先取消旧代次，再交给新回合。
(function attachChatController(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusChatController = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createChatControllerApi() {
  "use strict";

  const STREAM_CANCELLATION_TYPES = new Set([
    "cancel", "canceled", "cancelled", "client_canceled", "client_cancelled",
    "request_canceled", "request_cancelled", "request_superseded",
    "user_canceled", "user_cancelled",
  ]);

  function createChatController(options = {}) {
    const transport = options.transport || {};
    const feedback = options.feedback || {};
    const playbackPort = options.playback || {};
    const clock = options.clock || {};
    const request = typeof transport.request === "function"
      ? transport.request
      : (url, init) => {
          if (typeof fetch !== "function") throw new Error("chat request transport is unavailable");
          return fetch(url, init);
        };
    const now = typeof clock.now === "function"
      ? clock.now
      : () => (typeof performance !== "undefined" && typeof performance.now === "function"
        ? performance.now() : Date.now());
    const delay = typeof clock.delay === "function"
      ? clock.delay
      : milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
    const createAbortController = typeof clock.createAbortController === "function"
      ? clock.createAbortController
      : () => new AbortController();

    let generation = 0;
    let abortController = null;
    let requestInFlight = false;
    let activeRequestId = "";
    let activeAnswerRequestId = "";
    let supersededRequestId = "";
    let pendingAssistant = null;
    let currentRequest = null;

    function getGeneration() { return generation; }
    function isCurrent(value) { return Number(value) === generation; }
    function currentRequestId() {
      return String(activeRequestId || activeAnswerRequestId || "").trim();
    }
    function createRequestId(group, inputCount) {
      const safeGroup = String(group || "turn").replace(/[^A-Za-z0-9._:-]/g, "").slice(0, 80);
      return `${safeGroup}:g${generation + 1}:n${Math.max(0, Number(inputCount) || 0)}`;
    }
    function getState() {
      return {
        generation,
        requestId: currentRequestId(),
        activeRequestId,
        activeAnswerRequestId,
        inFlight: requestInFlight,
        hasPendingAssistant: Boolean(pendingAssistant),
        currentRequest: currentRequest ? {
          generation: currentRequest.generation,
          requestId: currentRequest.requestId,
          startedAt: currentRequest.startedAt,
        } : null,
      };
    }
    function syncSubmitButton() { feedback.syncSubmitButton?.(); }
    function stopProgress(requestId, stoppedGeneration) {
      feedback.stopThinkingProgress?.(requestId, stoppedGeneration);
      feedback.hideThinking?.(stoppedGeneration);
    }
    function clearRequestState({ keepSuperseded = false } = {}) {
      requestInFlight = false;
      abortController = null;
      activeRequestId = "";
      activeAnswerRequestId = "";
      currentRequest = null;
      pendingAssistant = null;
      if (!keepSuperseded) supersededRequestId = "";
    }
    function abortActive({ reason = "cancelled", notifyBackend = false, interruptSpeech = false,
      keepSuperseded = false, clearPending = true } = {}) {
      const requestId = currentRequestId();
      const stoppedGeneration = generation;
      abortController?.abort(reason);
      abortController = null;
      generation += 1;
      requestInFlight = false;
      activeRequestId = "";
      activeAnswerRequestId = "";
      currentRequest = null;
      if (clearPending) pendingAssistant = null;
      if (!keepSuperseded) supersededRequestId = "";
      stopProgress(requestId, stoppedGeneration);
      if (notifyBackend && requestId) feedback.notifyBackendCancellation?.(requestId);
      if (interruptSpeech) feedback.interruptSpeech?.();
      feedback.onCancelled?.({ requestId, generation: stoppedGeneration, reason });
      syncSubmitButton();
      return requestId;
    }
    function cancelCurrent(options = {}) {
      return abortActive({
        reason: options.reason || "user_cancelled",
        notifyBackend: options.notifyBackend !== false,
        interruptSpeech: options.interruptSpeech !== false,
        clearPending: options.clearPending !== false,
      });
    }
    function invalidateForContextChange(options = {}) {
      return abortActive({
        reason: options.reason || "context_changed",
        notifyBackend: options.notifyBackend !== false,
        interruptSpeech: options.interruptSpeech === true,
        clearPending: true,
      });
    }
    function supersedeCurrent() {
      if (!requestInFlight) return "";
      const requestId = currentRequestId();
      supersededRequestId = activeRequestId || requestId;
      abortActive({
        reason: "request_superseded",
        notifyBackend: false,
        interruptSpeech: false,
        keepSuperseded: true,
        clearPending: false,
      });
      return supersededRequestId;
    }
    function consumeSupersededRequestId() {
      const value = supersededRequestId;
      supersededRequestId = "";
      return value;
    }
    function bindAnswerRequest(requestId) {
      activeAnswerRequestId = String(requestId || activeAnswerRequestId || "");
      return activeAnswerRequestId;
    }
    function clearAnswerRequest(requestId) {
      if (!requestId || activeAnswerRequestId === String(requestId)) activeAnswerRequestId = "";
    }
    function setPendingAssistant(message) {
      pendingAssistant = message || null;
      return pendingAssistant;
    }
    function getPendingAssistant() { return pendingAssistant; }
    function clearPendingAssistant() { pendingAssistant = null; }
    function isSpeechCancellation(error) {
      return Boolean(error?.streamCancelled || error?.cancelled)
        || String(error?.name || "").toLowerCase() === "aborterror";
    }
    function isStreamCancellation(error) {
      if (isSpeechCancellation(error)) return true;
      const type = String(error?.streamErrorType || error?.reason || "").trim().toLowerCase();
      return STREAM_CANCELLATION_TYPES.has(type);
    }
    function validateFinalTurnEnvelope(result, requestId) {
      const envelope = result?.finalTurnEnvelope;
      const expectedTurnId = `${requestId}:turn`;
      const required = Array.isArray(envelope?.required_acts) ? envelope.required_acts.map(String) : [];
      const completed = new Set(Array.isArray(envelope?.completed_acts)
        ? envelope.completed_acts.map(String) : []);
      const valid = Boolean(
        envelope && typeof envelope === "object"
        && String(envelope.turn_id || "") === expectedTurnId
        && envelope.turn_complete === true
        && envelope.standalone === true
        && envelope.depends_on_undelivered_prefix === false
        && required.every(item => completed.has(item)),
      );
      if (!valid) {
        const error = new Error("最终回答信封与当前请求或必答合同不一致");
        error.streamFallbackAllowed = false;
        error.streamErrorType = "invalid_final_turn_envelope";
        error.streamErrorStage = "client_delivery_validation";
        throw error;
      }
      return envelope;
    }
    function retryableStatus(status) {
      return [408, 425, 429, 500, 502, 503, 504].includes(Number(status));
    }
    async function fetchStream(payload, signal, playback, requestId, requestGeneration) {
      const response = await request("/api/chat-stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal,
        body: JSON.stringify(payload),
      });
      if (!response.ok || !response.body) {
        const error = new Error(await response.text() || `HTTP ${response.status}`);
        error.retryable = true;
        throw error;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";
      let finalResult = null;
      let finalEnvelopeValidated = false;
      let route = "";
      let hadUtterance = false;
      let hadAnswerUtterance = false;
      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        const lines = buffer.split(/\r?\n/);
        buffer = done ? "" : lines.pop() || "";
        for (const rawLine of lines) {
          const line = rawLine.trim();
          if (!line) continue;
          let event;
          try {
            event = JSON.parse(line);
          } catch (parseError) {
            const error = new Error("流式接口返回了无法解析的 NDJSON 事件");
            error.cause = parseError;
            error.streamFallbackAllowed = !hadUtterance;
            error.streamErrorType = "stream_event_parse_failed";
            error.streamErrorStage = "client_ndjson_parse";
            throw error;
          }
          const eventType = String(event.type || "");
          const eventPayload = event.payload && typeof event.payload === "object" ? event.payload : {};
          if (eventType === "turn_header") {
            route = String(eventPayload.route || route);
            feedback.updateThinkingStage?.(
              route === "fast_chat" ? "model-generating" : "context",
              route === "fast_chat" ? "快路径已建立，等待第一条完整语句" : "已完成请求分流",
            );
          } else if (eventType === "utterance") {
            hadUtterance = true;
            hadAnswerUtterance = true;
            playback.enqueue(eventPayload);
          } else if (eventType === "progress_utterance") {
            hadUtterance = true;
            playback.enqueue({ ...eventPayload, progressOnly: true });
          } else if (eventType === "tool_status") {
            feedback.updateThinkingStage?.(
              String(eventPayload.stage || "tool-executing"),
              String(eventPayload.displayText || "正在执行工具"),
            );
          } else if (eventType === "turn_trailer") {
            finalResult = eventPayload.finalResult || finalResult;
            route = String(eventPayload.route || route);
            if (finalResult) {
              validateFinalTurnEnvelope(finalResult, requestId);
              finalEnvelopeValidated = true;
              if (!hadAnswerUtterance) playback.enqueueFinal(finalResult);
            }
          } else if (eventType === "turn_abort") {
            const error = new Error(String(eventPayload.message || eventPayload.reason || "流式回答中断"));
            error.streamFallbackAllowed = Boolean(eventPayload.fallbackAllowed) && !hadUtterance;
            error.streamErrorType = String(eventPayload.errorType || eventPayload.reason || "stream_failed");
            error.code = String(eventPayload.code || "");
            error.streamErrorStage = String(eventPayload.stage || "stream");
            error.connectionFailure = Boolean(eventPayload.connectionFailure);
            error.completedTools = Array.isArray(eventPayload.completedTools) ? eventPayload.completedTools : [];
            error.streamCancelled = Boolean(eventPayload.cancelled) || isStreamCancellation(error);
            throw error;
          }
        }
        if (done) break;
      }
      if (!finalResult) {
        const error = new Error("流式接口没有返回完整收尾");
        error.streamFallbackAllowed = !hadUtterance;
        throw error;
      }
      if (!finalEnvelopeValidated) validateFinalTurnEnvelope(finalResult, requestId);
      playback.setFinalResult(finalResult);
      playback.close();
      return { data: finalResult, route, hadUtterance };
    }
    async function fetchLegacy(payload, signal) {
      const attempts = 3;
      const retryDelays = [650, 1800];
      let lastError = null;
      for (let attempt = 1; attempt <= attempts; attempt += 1) {
        if (signal.aborted) throw new DOMException("Request aborted", "AbortError");
        try {
          const response = await request("/api/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            signal,
            body: JSON.stringify(payload),
          });
          const responseText = await response.text();
          let data = {};
          try {
            data = responseText ? JSON.parse(responseText) : {};
          } catch (parseError) {
            const error = new Error(`本地服务返回了无法解析的响应（HTTP ${response.status}）`);
            error.cause = parseError;
            error.retryable = response.ok || retryableStatus(response.status);
            throw error;
          }
          if (!response.ok || data.ok === false) {
            const error = new Error(data.error || `HTTP ${response.status}`);
            error.code = String(data.code || "");
            error.retryable = typeof data.retryable === "boolean"
              ? data.retryable : retryableStatus(response.status);
            throw error;
          }
          if (attempt > 1) feedback.toast?.("连接已恢复，刚才的问题没有丢失");
          return data;
        } catch (error) {
          if (signal.aborted || error?.name === "AbortError") throw error;
          lastError = error;
          if (error?.retryable === false || attempt >= attempts) break;
          const localFailure = !error?.code
            && (error instanceof TypeError || /fetch|网络|解析/.test(String(error?.message || "")));
          feedback.updateConnectionStatus?.({
            connectionState: localFailure ? "backend-reconnecting" : "reconnecting",
            attempt,
            attempts,
          });
          if (localFailure) await feedback.recoverBackend?.();
          await delay(retryDelays[attempt - 1] || 1200);
        }
      }
      throw lastError || new Error("聊天连接未返回结果");
    }

    async function startTurn({
      payload,
      requestId,
      requestStartedAt = now(),
      inputs = [],
      proactiveContext = "",
      execute,
      createPlayback,
      onCommit,
      onLegacy,
      onError,
      onCancelled,
      onCommitted,
      onSettled,
      allowLegacyFallback = true,
      frontendFullRetryDisabled = false,
    } = {}, owner = null) {
      if (!payload || !requestId || requestInFlight) return null;
      const turnGeneration = ++generation;
      const controller = createAbortController();
      const inputSnapshot = Array.isArray(inputs) ? inputs.slice() : [];
      if (owner) {
        owner.accepted = true;
        owner.generation = turnGeneration;
        owner.requestId = String(requestId);
        owner.requestStartedAt = requestStartedAt;
        owner.inputs = inputSnapshot;
        owner.proactiveContext = proactiveContext;
        owner.controller = controller;
        owner.playback = null;
      }
      abortController = controller;
      activeRequestId = String(requestId);
      activeAnswerRequestId = String(requestId);
      requestInFlight = true;
      pendingAssistant = null;
      currentRequest = {
        generation: turnGeneration,
        requestId: String(requestId),
        startedAt: requestStartedAt,
        inputs: inputSnapshot,
        payload,
        proactiveContext,
        playback: null,
      };
      feedback.onStart?.({ generation: turnGeneration, requestId, requestStartedAt, inputs });
      let playback = typeof createPlayback === "function"
        ? createPlayback(turnGeneration, requestStartedAt, requestId)
        : null;
      currentRequest.playback = playback;
      if (owner) owner.playback = playback;
      let streamed = null;
      try {
        streamed = typeof execute === "function"
          ? { data: await execute({ generation: turnGeneration, requestId, signal: controller.signal }), legacy: true }
          : await fetchStream(payload, controller.signal, playback, String(requestId), turnGeneration);
      } catch (streamError) {
        if (isStreamCancellation(streamError)) {
          playback?.abort?.("stream_cancelled");
          if (turnGeneration === generation) onCancelled?.(streamError, getState());
          return null;
        }
        if (streamError?.connectionFailure && streamError?.streamFallbackAllowed) {
          playback?.abort?.();
          feedback.updateConnectionStatus?.({ connectionState: "reconnecting", attempt: 1, attempts: 1 });
          feedback.updateThinkingStage?.("backend-reconnecting", "上游暂时不可达，问题已保留，正在自动恢复一次");
          await delay(2500);
          if (controller.signal.aborted) throw new DOMException("Request aborted", "AbortError");
          playback = typeof createPlayback === "function"
            ? createPlayback(turnGeneration, requestStartedAt, requestId)
            : null;
          currentRequest.playback = playback;
          if (owner) owner.playback = playback;
          try {
            streamed = await fetchStream(payload, controller.signal, playback, String(requestId), turnGeneration);
            feedback.toast?.("连接已恢复，刚才的问题没有丢失");
          } catch (recoveryError) {
            streamError = recoveryError;
          }
        }
        if (streamed) {
          // 同一个 requestId 可以安全重放，后端会复用已完成的结果。
        } else if (!streamError?.streamFallbackAllowed) {
          playback?.abort?.();
          throw streamError;
        } else if (frontendFullRetryDisabled || !allowLegacyFallback) {
          playback?.abort?.();
          streamError.streamFallbackAllowed = false;
          streamError.streamErrorType = String(
            streamError.streamErrorType || "backend_single_turn_recovery_failed",
          );
          throw streamError;
        } else {
          playback?.abort?.();
          feedback.updateThinkingStage?.("fallback-legacy", "流式协议不可用，正在切回完整回答路径");
          const legacyData = await fetchLegacy(payload, controller.signal);
          streamed = { data: legacyData, route: "fallback_legacy", hadUtterance: false, legacy: true };
        }
      }
      if (turnGeneration !== generation) return null;
      const data = streamed.data;
      const assistantData = {
        ...data,
        source: data.source || "api",
        clientRequestStartedAt: requestStartedAt,
        clientChatGeneration: turnGeneration,
      };
      const applied = streamed.legacy
        ? await onLegacy?.(assistantData, inputs.join("\n"), {
            generation: turnGeneration, requestId, proactiveContext, playback,
          })
        : await onCommit?.(assistantData, playback, inputs.join("\n"), {
            generation: turnGeneration, requestId, proactiveContext,
          });
      if (applied && turnGeneration === generation) onCommitted?.({
        generation: turnGeneration, requestId, proactiveContext, data: assistantData,
      });
      return applied;
    }

    async function runTurn(args = {}) {
      // 每次调用保存自己的代次与取消信号。旧请求迟到失败时不能读取
      // currentRequest，否则它可能已经指向新请求并误清理新输入/播放状态。
      const owner = { accepted: false };
      try {
        return await startTurn(args, owner);
      } catch (error) {
        if (!owner.accepted
          || owner.controller?.signal?.aborted
          || error?.name === "AbortError"
          || !isCurrent(owner.generation)) {
          return null;
        }
        await args.onError?.(error, {
          generation: owner.generation,
          requestId: owner.requestId,
          inputs: owner.inputs,
          proactiveContext: owner.proactiveContext,
          playback: owner.playback,
        });
        return null;
      } finally {
        if (owner.accepted
          && generation === owner.generation
          && currentRequest?.generation === owner.generation
          && currentRequest?.requestId === owner.requestId) {
          const settled = getState();
          feedback.onSettled?.(settled);
          requestInFlight = false;
          abortController = null;
          activeRequestId = "";
          if (!playbackPort.pipelineActive?.() && activeAnswerRequestId === owner.requestId) activeAnswerRequestId = "";
          currentRequest = null;
          syncSubmitButton();
          args.onSettled?.(settled);
        }
      }
    }

    return {
      createRequestId,
      startTurn: runTurn,
      cancelCurrent,
      invalidateForContextChange,
      supersedeCurrent,
      consumeSupersededRequestId,
      bindAnswerRequest,
      clearAnswerRequest,
      setPendingAssistant,
      getPendingAssistant,
      clearPendingAssistant,
      getGeneration,
      getState,
      isCurrent,
      isStreamCancellation,
      validateFinalTurnEnvelope,
      isRequestInFlight: () => requestInFlight,
      currentRequestId,
    };
  }

  return { createChatController };
}));
