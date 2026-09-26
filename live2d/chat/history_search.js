(function attachChatHistorySearch(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusChatHistorySearch = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createChatHistorySearchApi() {
  "use strict";

  /**
   * 聊天历史搜索控制器只拥有搜索会话状态和搜索面板的细节：筛选、分页、
   * 请求取消、结果渲染与命中已加载气泡后的定位。页面如何切换其他面板、
   * 数据从哪个存储作用域读取，仍由调用方负责。
   */
  function createChatHistorySearchController({
    document,
    elements,
    requestPage,
    notify,
    setTimer,
    clearTimer,
    createAbortController,
  }) {
    const state = {
      query: "",
      role: "all",
      offset: 0,
      limit: 30,
      total: 0,
      hasMore: false,
      timer: 0,
      controller: null,
      results: [],
    };
    let bound = false;

    function clearScheduledSearch() {
      clearTimer(state.timer);
      state.timer = 0;
    }

    function updateRoleButtons() {
      elements.roleButtons?.forEach(button => {
        const active = String(button.dataset.historyRole || "all") === state.role;
        button.classList.toggle("active", active);
        button.setAttribute("aria-selected", String(active));
      });
    }

    function formatTime(value) {
      const parsed = new Date(String(value || ""));
      if (!Number.isFinite(parsed.getTime())) return "时间未知";
      return new Intl.DateTimeFormat("zh-CN", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      }).format(parsed);
    }

    function appendHighlightedText(node, text, query) {
      const source = String(text || "");
      const needle = String(query || "").trim();
      if (!needle) {
        node.textContent = source;
        return;
      }
      const sourceFolded = source.normalize("NFKC").toLocaleLowerCase();
      const needleFolded = needle.normalize("NFKC").toLocaleLowerCase();
      const index = sourceFolded.indexOf(needleFolded);
      if (index < 0) {
        node.textContent = source;
        return;
      }
      node.append(
        document.createTextNode(source.slice(0, index)),
        Object.assign(document.createElement("mark"), {
          textContent: source.slice(index, index + needle.length),
        }),
        document.createTextNode(source.slice(index + needle.length)),
      );
    }

    function findLoadedBubble(result) {
      const candidates = [
        ...(result.matchRoles || []).map(role => ({
          role,
          content: role === "user" ? result.user : result.assistant,
        })),
      ];
      return [...(elements.chatLog?.querySelectorAll(".msg") || [])].find(bubble => {
        const role = bubble.classList.contains("assistant") ? "assistant" : "user";
        const body = bubble.querySelector(".message-body");
        return candidates.some(candidate =>
          candidate.role === role
          && String(body?.textContent || "").trim() === String(candidate.content || "").trim()
          && (
            !result.at
            || !bubble.dataset.historyAt
            || bubble.dataset.historyAt === String(result.at)
          )
        );
      }) || null;
    }

    function activateResult(result, card) {
      const loaded = findLoadedBubble(result);
      if (loaded) {
        close();
        loaded.classList.add("history-search-hit");
        loaded.scrollIntoView({ block: "center", behavior: "smooth" });
        setTimer(() => loaded.classList.remove("history-search-hit"), 2600);
        return;
      }
      card.classList.toggle("expanded");
      card.querySelectorAll(".chat-history-result-copy").forEach(node => {
        node.style.whiteSpace = card.classList.contains("expanded") ? "pre-wrap" : "";
      });
    }

    function renderResults() {
      if (!elements.results) return;
      elements.results.replaceChildren();
      if (!state.results.length) {
        const empty = document.createElement("div");
        empty.className = "chat-history-search-empty";
        empty.textContent = state.query
          ? "没有找到相关聊天记录"
          : "还没有可显示的聊天记录";
        elements.results.appendChild(empty);
      }
      state.results.forEach(result => {
        const card = document.createElement("button");
        card.type = "button";
        card.className = "chat-history-result";
        card.dataset.historyResultId = String(result.id || "");
        const head = document.createElement("span");
        head.className = "chat-history-result-head";
        const actor = document.createElement("strong");
        actor.textContent = (result.matchRoles || []).includes("user")
          && !(result.matchRoles || []).includes("assistant")
          ? "我"
          : (result.matchRoles || []).includes("assistant")
            && !(result.matchRoles || []).includes("user")
            ? "AMADEUS"
            : "本轮对话";
        const at = document.createElement("span");
        at.textContent = formatTime(result.at);
        head.append(actor, at);
        card.appendChild(head);
        for (const [role, content] of [
          ["user", result.user],
          ["assistant", result.assistant],
        ]) {
          if (!String(content || "").trim()) continue;
          const copy = document.createElement("span");
          copy.className = "chat-history-result-copy";
          copy.dataset.role = role;
          appendHighlightedText(
            copy,
            `${role === "user" ? "我" : "AMADEUS"}：${String(content).trim()}`,
            state.query,
          );
          card.appendChild(copy);
        }
        card.addEventListener("click", () => activateResult(result, card));
        elements.results.appendChild(card);
      });
      const page = Math.floor(state.offset / state.limit) + 1;
      if (elements.page) elements.page.textContent = `第 ${page} 页`;
      if (elements.prevButton) elements.prevButton.disabled = state.offset <= 0;
      if (elements.nextButton) elements.nextButton.disabled = !state.hasMore;
    }

    async function search() {
      if (elements.panel?.classList.contains("hidden")) return;
      clearScheduledSearch();
      state.query = String(elements.input?.value || "").trim();
      state.controller?.abort();
      const controller = createAbortController();
      state.controller = controller;
      if (elements.status) elements.status.textContent = "正在查找…";
      try {
        const data = await requestPage({
          query: state.query,
          role: state.role,
          limit: state.limit,
          offset: state.offset,
          signal: controller.signal,
        });
        // 被后发请求替换的旧响应不能把当前面板倒写回去。
        if (controller !== state.controller) return;
        state.total = Math.max(0, Number(data.total) || 0);
        state.hasMore = Boolean(data.hasMore);
        state.results = Array.isArray(data.results) ? data.results : [];
        if (elements.status) {
          elements.status.textContent = state.query
            ? `找到 ${state.total} 条相关记录`
            : `最近 ${state.total} 条记录`;
        }
        renderResults();
      } catch (error) {
        if (error?.name === "AbortError") return;
        if (elements.status) elements.status.textContent = "查找失败";
        state.results = [];
        renderResults();
        notify(`聊天记录查找失败：${error.message}`);
      }
    }

    function scheduleSearch() {
      clearScheduledSearch();
      state.query = String(elements.input?.value || "").trim();
      state.offset = 0;
      state.timer = setTimer(search, 180);
    }

    function open() {
      elements.panel?.classList.remove("hidden");
      elements.opener?.setAttribute("aria-expanded", "true");
      state.offset = 0;
      updateRoleButtons();
      search();
      setTimer(() => elements.input?.focus(), 0);
    }

    function close() {
      clearScheduledSearch();
      state.controller?.abort();
      state.controller = null;
      elements.panel?.classList.add("hidden");
      elements.opener?.setAttribute("aria-expanded", "false");
    }

    function bind() {
      if (bound) return;
      bound = true;
      elements.closeButton?.addEventListener("click", close);
      elements.input?.addEventListener("input", scheduleSearch);
      elements.input?.addEventListener("keydown", event => {
        if (event.key !== "Enter") return;
        event.preventDefault();
        state.offset = 0;
        search();
      });
      elements.roleButtons?.forEach(button => {
        button.addEventListener("click", () => {
          state.role = String(button.dataset.historyRole || "all");
          state.offset = 0;
          updateRoleButtons();
          search();
        });
      });
      elements.prevButton?.addEventListener("click", () => {
        state.offset = Math.max(0, state.offset - state.limit);
        search();
      });
      elements.nextButton?.addEventListener("click", () => {
        if (!state.hasMore) return;
        state.offset += state.limit;
        search();
      });
    }

    return Object.freeze({ bind, open, close, search });
  }

  return Object.freeze({ createChatHistorySearchController });
}));
