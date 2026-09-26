(function attachMessageView(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusMessageView = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createMessageViewApi() {
  "use strict";

  function formatFileSize(value) {
    const bytes = Math.max(0, Number(value) || 0);
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  // 来源地址按不可信输入处理，限制协议并排除常见本地地址；后端网关仍需独立校验。
  function safeExternalSourceUrl(value) {
    try {
      const parsed = new URL(String(value || ""));
      if (!["http:", "https:"].includes(parsed.protocol)) return "";
      const host = parsed.hostname.toLowerCase().replace(/\.$/, "");
      if (!host || host === "localhost" || host.endsWith(".local") || host.endsWith(".internal")) return "";
      if (/^(127\.|10\.|169\.254\.|192\.168\.)/.test(host)) return "";
      const private172 = host.match(/^172\.(\d+)\./);
      if (private172 && Number(private172[1]) >= 16 && Number(private172[1]) <= 31) return "";
      if (host === "::1" || host.startsWith("fc") || host.startsWith("fd") || host.startsWith("fe80:")) return "";
      return parsed.href;
    } catch {
      return "";
    }
  }

  /**
   * 消息视图只拥有聊天日志的 DOM、消息对象到气泡的弱引用和用户点击后的呈现动作。
   * 消息数组、流式分段文本、会话身份与持久化仍由页面维护；调用 render 时传入同一批
   * 模型对象，重绘后更新动作会取得新的气泡，不会另存一份消息副本。
   */
  function createMessageView({
    document,
    chatLog,
    actions,
    notify,
    isDeveloper,
    maxAttachments,
    clipboard,
    formatContent,
    formatSpeechDiagnostic,
    setTimer,
    requestAnimationFrame,
  }) {
    let bubbles = new WeakMap();

    function assistantMetaLabel(metaData = {}) {
      const bits = ["AMADEUS"];
      const source = String(metaData.source || "");
      const internalRecoverySources = new Set([
        "pipeline-error",
        "local-recovery",
        "local-action-state-recovery",
        "model-pipeline-rescue",
      ]);
      if (source && !internalRecoverySources.has(source)) bits.push(source.toUpperCase());
      if (metaData.latencyMs) bits.push(`${metaData.latencyMs}MS`);
      if (typeof metaData.memoryCount === "number") bits.push(`MEM ${metaData.memoryCount}`);
      if (isDeveloper() && metaData.dialoguePolicy?.reply_mode) {
        bits.push(`MODE ${String(metaData.dialoguePolicy.reply_mode).toUpperCase()}`);
      }
      if (isDeveloper() && Number.isFinite(Number(metaData.dialogueValidation?.sentence_count))) {
        bits.push(`${Number(metaData.dialogueValidation.sentence_count)}S`);
      }
      if (metaData.spokenTextJaRepaired) bits.push("JA_REPAIRED");
      return bits.join(" · ");
    }

    function scrollToLatest() {
      if (!chatLog) return;
      const scroll = () => { chatLog.scrollTop = chatLog.scrollHeight; };
      scroll();
      requestAnimationFrame(() => {
        scroll();
        requestAnimationFrame(scroll);
      });
      setTimer(scroll, 120);
    }

    function scrollToMessage(messageId) {
      const target = [...(chatLog?.querySelectorAll(".msg") || [])].find(
        item => String(item.dataset.messageId || "") === String(messageId || ""),
      );
      if (!target) {
        notify("被回复的消息不在当前窗口中");
        return;
      }
      target.classList.add("history-search-hit");
      target.scrollIntoView({ block: "center", behavior: "smooth" });
      setTimer(() => target.classList.remove("history-search-hit"), 1800);
    }

    async function copyMessageText(value) {
      const text = String(value || "");
      try {
        await clipboard.writeText(text);
      } catch {
        const helper = document.createElement("textarea");
        helper.value = text;
        helper.style.position = "fixed";
        helper.style.opacity = "0";
        document.body.appendChild(helper);
        helper.select();
        document.execCommand("copy");
        helper.remove();
      }
      notify("对话文字已复制");
    }

    function appendAttachmentCards(bubble, attachments) {
      if (!Array.isArray(attachments) || !attachments.length) return;
      const list = document.createElement("div");
      list.className = "message-attachments";
      attachments.slice(0, maxAttachments).forEach(item => {
        if (!item || typeof item !== "object") return;
        const card = document.createElement("div");
        card.className = "message-attachment-card";
        const icon = document.createElement("span");
        icon.className = "message-file-icon";
        icon.textContent = String(item.mimeType || "").startsWith("image/") || item.kind === "image" ? "IMG" : "FILE";
        const label = document.createElement("span");
        label.className = "message-file-label";
        const name = document.createElement("strong");
        name.textContent = String(item.name || "附件");
        const size = document.createElement("small");
        size.textContent = formatFileSize(item.size);
        label.append(name, size);
        card.append(icon, label);
        list.appendChild(card);
      });
      bubble.appendChild(list);
    }

    function appendGeneratedFileCards(bubble, files) {
      if (!Array.isArray(files) || !files.length) return;
      const list = document.createElement("div");
      list.className = "generated-files";
      files.forEach(item => {
        if (!item || typeof item !== "object" || !item.id) return;
        const card = document.createElement("button");
        card.type = "button";
        card.className = "generated-file-card";
        card.title = `保存 ${item.name || "生成文件"}`;
        const icon = document.createElement("span");
        icon.className = "message-file-icon";
        icon.textContent = "SAVE";
        const label = document.createElement("span");
        label.className = "message-file-label";
        const name = document.createElement("strong");
        name.textContent = String(item.name || "生成文件");
        const size = document.createElement("small");
        size.textContent = `${formatFileSize(item.size)} · 点击保存`;
        label.append(name, size);
        card.append(icon, label);
        card.addEventListener("click", () => actions.saveGeneratedFile(item));
        list.appendChild(card);
      });
      bubble.appendChild(list);
    }

    function append(message) {
      const role = message?.role === "assistant" ? "assistant" : "user";
      const content = formatContent(role, message?.content);
      const metaData = message?.meta || {};
      const bubble = document.createElement("article");
      bubble.className = `msg ${role === "assistant" ? "assistant" : "user"}`;
      bubble.dataset.historyAt = String(metaData.at || "");
      bubble.dataset.historyRole = role === "assistant" ? "assistant" : "user";
      bubble.dataset.messageId = String(metaData.messageId || "");
      bubble.dataset.turnId = String(metaData.turnId || "");
      const meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = role === "assistant" ? assistantMetaLabel(metaData) : "YOU";
      const body = document.createElement("span");
      body.className = "message-body";
      body.textContent = content;
      const messageActions = document.createElement("span");
      messageActions.className = "message-actions";
      const replyButton = document.createElement("button");
      replyButton.type = "button";
      replyButton.className = "reply-message-btn";
      replyButton.textContent = "回复";
      replyButton.title = "回复这条消息";
      replyButton.setAttribute("aria-label", "回复这条消息");
      replyButton.addEventListener("click", () => actions.reply({ role, content: body.textContent, meta: metaData }));
      const copyButton = document.createElement("button");
      copyButton.type = "button";
      copyButton.className = "copy-message-btn";
      copyButton.textContent = "复制";
      copyButton.title = "复制这条对话文字";
      copyButton.setAttribute("aria-label", "复制这条对话文字");
      copyButton.addEventListener("click", () => copyMessageText(body.textContent));
      messageActions.append(replyButton, copyButton);
      const replyAnchor = metaData.replyAnchor && typeof metaData.replyAnchor === "object"
        ? metaData.replyAnchor : null;
      let replySource = null;
      if (replyAnchor?.targetMessageIds?.length) {
        replySource = document.createElement("button");
        replySource.type = "button";
        replySource.className = "message-reply-source";
        replySource.textContent = `回复自：${String(replyAnchor.quotedPreview || "原消息").slice(0, 180)}`;
        replySource.title = "跳到被回复的消息";
        replySource.addEventListener("click", () => scrollToMessage(replyAnchor.targetMessageIds[0]));
      }
      bubble.append(meta, messageActions);
      if (replySource) bubble.appendChild(replySource);
      bubble.appendChild(body);
      appendAttachmentCards(bubble, metaData.attachments);
      const diagnostic = role === "assistant" && isDeveloper() ? formatSpeechDiagnostic(metaData) : "";
      if (diagnostic) {
        const ja = document.createElement("span");
        ja.className = "dev-ja-line";
        ja.textContent = diagnostic;
        bubble.appendChild(ja);
      }
      if (role === "assistant" && Array.isArray(metaData.sources) && metaData.sources.length) {
        const sourceList = document.createElement("details");
        sourceList.className = "message-sources";
        const summary = document.createElement("summary");
        summary.textContent = `参考来源 · ${Math.min(metaData.sources.length, 8)}`;
        summary.title = "展开查看来源";
        const sourceDetails = document.createElement("div");
        sourceDetails.className = "message-source-details";
        sourceList.append(summary, sourceDetails);
        metaData.sources.slice(0, 8).forEach(source => {
          const safeUrl = safeExternalSourceUrl(source.url);
          const link = document.createElement("a");
          link.href = safeUrl || "#";
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.textContent = `[${source.id || "S?"}] ${source.title || source.domain || "网页来源"}`;
          link.title = String(source.excerpt || source.url || "");
          link.addEventListener("click", event => {
            event.preventDefault();
            if (!safeUrl) {
              notify("来源 URL 未通过安全校验");
              return;
            }
            actions.openSource({
              url: safeUrl,
              actionId: `source_${String(source.sourceUid || source.id || Date.now())}`,
            });
          });
          sourceDetails.appendChild(link);
        });
        bubble.appendChild(sourceList);
      }
      appendGeneratedFileCards(bubble, metaData.files);
      chatLog?.appendChild(bubble);
      if (chatLog) chatLog.scrollTop = chatLog.scrollHeight;
      if (message && typeof message === "object") bubbles.set(message, bubble);
      return bubble;
    }

    function render(messages) {
      if (!chatLog) return;
      chatLog.innerHTML = "";
      bubbles = new WeakMap();
      (Array.isArray(messages) ? messages : []).forEach(message => append(message));
      scrollToLatest();
    }

    function updateProgressive(message) {
      const bubble = bubbles.get(message);
      if (!bubble) return;
      const body = bubble.querySelector(".message-body");
      // 页面在写入流式模型时已完成可见文本过滤；这里仅同步该模型值，
      // 避免在每个分段上重复处理内容，也不抢占页面的 persist → scroll 时序。
      if (body) body.textContent = String(message?.content || "");
      const diagnostic = message?.role === "assistant" && isDeveloper()
        ? formatSpeechDiagnostic(message?.meta || {}) : "";
      const ja = bubble.querySelector(".dev-ja-line");
      if (ja && diagnostic) ja.textContent = diagnostic;
    }

    function updateMetadata(message) {
      const bubble = bubbles.get(message);
      if (!bubble) return;
      const metaData = message?.meta || {};
      const meta = bubble.querySelector(".meta");
      if (meta && message?.role === "assistant") meta.textContent = assistantMetaLabel(metaData);
      bubble.dataset.messageId = String(metaData.messageId || "");
      bubble.dataset.turnId = String(metaData.turnId || "");
    }

    return Object.freeze({
      append,
      render,
      scrollToLatest,
      scrollToMessage,
      updateMetadata,
      updateProgressive,
    });
  }

  return Object.freeze({ createMessageView, formatFileSize, safeExternalSourceUrl });
}));
