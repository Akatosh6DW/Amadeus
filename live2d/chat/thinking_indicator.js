// 思考提示控制器唯一持有临时气泡、开发者进度轮询和请求代次；消息模型与聊天请求仍由页面/聊天控制器拥有。
// 普通页面只显示稳定的“正在思考”，开发者页面才读取 /api/developer/chat-progress 细节。
(function attachThinkingIndicatorController(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusThinkingIndicator = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createThinkingIndicatorApi() {
  "use strict";

  const PIPELINE_STAGES = Object.freeze({
    received: ["已接收请求", "消息已经进入处理队列", 0],
    attachments: ["读取输入", "正在读取消息与本轮附件", 0],
    context: ["整理上下文", "正在组合对话、记忆与人格信息", 1],
    "tool-executing": ["执行工具", "正在获取回答所需的视觉、文件或联网证据", 2],
    researching: ["补充证据", "现有证据不足，正在继续检索", 2],
    "model-generating": ["生成回答", "模型正在形成第一版回答", 3],
    "fallback-legacy": ["继续生成", "快速回复没有完整收尾，正在继续组织回答", 3],
    "model-synthesizing": ["综合证据", "模型正在结合工具结果形成回答", 3],
    validating: ["检查回答", "正在检查完整性、上下文与证据", 4],
    repairing: ["修正回答", "正在根据校验结果定向修正", 4],
    finalizing: ["整理输出", "正在选择完整回答并整理情绪参数", 4],
    postprocessing: ["准备交付", "正在校准日语发声稿与记忆更新", 4],
    "speech-segmenting": ["切分语音", "正在按自然语义安排分段播放", 5],
    "voice-first": ["合成首段", "正在优先生成可以立即播放的第一段", 5],
    "voice-prefetch": ["预合成后续", "前一段播放期间正在准备下一段", 5],
    "voice-wait": ["衔接语音", "下一段尚未准备完成，正在保持自然衔接", 5],
    "voice-playing": ["播放语音", "语音已经开始播放，后续段在后台准备", 5],
    complete: ["处理完成", "回答已经交付", 5],
    failed: ["处理失败", "处理链路发生错误", 5],
  });

  function createThinkingIndicatorController(options = {}) {
    const dom = options.dom || {};
    const document = options.document || globalThis.document;
    const window = options.window || globalThis;
    const isDeveloper = typeof options.isDeveloper === "function" ? options.isDeveloper : () => false;
    const getGeneration = typeof options.getGeneration === "function" ? options.getGeneration : () => 0;
    const request = typeof options.request === "function" ? options.request : (...args) => window.fetch(...args);
    const scrollToLatest = typeof options.scrollToLatest === "function" ? options.scrollToLatest : () => {};
    let thinkingIndicator = null;
    let progressTimer = 0;
    let progressRequestId = "";

    function stageDefinition(stage) {
      return PIPELINE_STAGES[String(stage || "").toLowerCase()] || ["处理中", "正在完成当前步骤", 1];
    }

    function hide(generation = 0) {
      if (!thinkingIndicator) return;
      const indicatorGeneration = Number(thinkingIndicator.dataset.generation || 0);
      if (generation && indicatorGeneration && generation !== indicatorGeneration) return;
      thinkingIndicator.remove();
      thinkingIndicator = null;
    }

    function updateStage(stage, detail = "", elapsedMs = null) {
      if (!thinkingIndicator) return;
      const [labelText, defaultDetail, step] = stageDefinition(stage);
      thinkingIndicator.dataset.stage = String(stage || "working");
      const label = thinkingIndicator.querySelector(".thinking-label");
      if (label) label.textContent = isDeveloper() ? labelText : "正在思考";
      const detailNode = thinkingIndicator.querySelector(".thinking-stage-detail");
      if (detailNode) detailNode.textContent = String(detail || defaultDetail);
      const elapsedNode = thinkingIndicator.querySelector(".thinking-elapsed");
      if (elapsedNode && Number.isFinite(Number(elapsedMs))) {
        elapsedNode.textContent = `${(Number(elapsedMs) / 1000).toFixed(1)}s`;
      }
      thinkingIndicator.querySelectorAll(".thinking-stage-track i").forEach((node, index) => {
        node.classList.toggle("done", index < step);
        node.classList.toggle("active", index === step);
      });
    }

    function stopProgress(requestId = "") {
      if (requestId && progressRequestId && requestId !== progressRequestId) return;
      if (progressTimer) window.clearInterval(progressTimer);
      progressTimer = 0;
      progressRequestId = "";
    }

    function startProgress(requestId, generation) {
      stopProgress();
      if (!isDeveloper() || !requestId) return;
      progressRequestId = requestId;
      const poll = async () => {
        if (requestId !== progressRequestId || generation !== getGeneration()) return;
        try {
          const response = await request(`/api/developer/chat-progress?requestId=${encodeURIComponent(requestId)}`, {
            cache: "no-store",
          });
          if (!response.ok) return;
          const progress = await response.json();
          if (!progress.ok || requestId !== progressRequestId || generation !== getGeneration()) return;
          updateStage(progress.stage, progress.detail, progress.elapsedMs);
        } catch {
          // 进度仅用于诊断，网络异常不能影响聊天请求。
        }
      };
      poll();
      progressTimer = window.setInterval(poll, 420);
    }

    function show(generation, stage = "context", detail = "") {
      if (!dom.chatLog) return;
      hide();
      const bubble = document.createElement("article");
      bubble.className = "msg assistant thinking-indicator";
      bubble.classList.toggle("developer-pipeline", isDeveloper());
      bubble.dataset.generation = String(generation || 0);
      bubble.setAttribute("role", "status");
      bubble.setAttribute("aria-live", "polite");
      bubble.setAttribute("aria-label", "Amadeus 正在思考");

      const meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = isDeveloper() ? "Amadeus · PIPELINE" : "Amadeus · THINKING";
      const body = document.createElement("span");
      body.className = "message-body thinking-body";
      const label = document.createElement("span");
      label.className = "thinking-label";
      label.textContent = "正在思考";
      const dots = document.createElement("span");
      dots.className = "thinking-dots";
      dots.setAttribute("aria-hidden", "true");
      for (let index = 0; index < 3; index += 1) {
        const dot = document.createElement("i");
        dot.style.setProperty("--thinking-dot-index", String(index));
        dots.appendChild(dot);
      }
      body.append(label, dots);
      if (isDeveloper()) {
        const stagePanel = document.createElement("span");
        stagePanel.className = "thinking-stage-panel";
        const track = document.createElement("span");
        track.className = "thinking-stage-track";
        track.setAttribute("aria-hidden", "true");
        for (let index = 0; index < 6; index += 1) track.appendChild(document.createElement("i"));
        const stageDetail = document.createElement("span");
        stageDetail.className = "thinking-stage-detail";
        const elapsed = document.createElement("span");
        elapsed.className = "thinking-elapsed";
        elapsed.textContent = "0.0s";
        stagePanel.append(track, stageDetail, elapsed);
        body.appendChild(stagePanel);
      }
      bubble.append(meta, body);
      dom.chatLog.appendChild(bubble);
      thinkingIndicator = bubble;
      updateStage(stage, detail, 0);
      scrollToLatest();
      window.requestAnimationFrame?.(() => bubble.scrollIntoView?.({ block: "end", inline: "nearest" }));
    }

    return Object.freeze({
      show,
      hide,
      updateStage,
      startProgress,
      stopProgress,
      getState: () => ({
        hasIndicator: Boolean(thinkingIndicator),
        generation: Number(thinkingIndicator?.dataset?.generation || 0),
        progressRequestId,
        progressActive: Boolean(progressTimer),
      }),
      getStageDefinition: stageDefinition,
    });
  }

  return Object.freeze({ createThinkingIndicatorController, pipelineStages: PIPELINE_STAGES });
}));
