// 关系面板控制器唯一持有 developer relationship 读取代次、当前快照和关系 DOM。
// 关系运行态由后端/页面会话拥有；面板只通过 storageScopedFetch 读取指定 scope，不修改任何关系状态。
(function attachRelationshipController(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusRelationshipController = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createRelationshipControllerApi() {
  "use strict";

  function number(value, fallback = 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function formatJson(value) {
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value ?? "-");
    }
  }

  function createRelationshipController(options = {}) {
    const elements = options.elements || {};
    const transport = options.transport || {};
    const feedback = options.feedback || {};
    const documentObject = options.document
      || (typeof document !== "undefined" ? document : null);
    const isDeveloper = typeof options.isDeveloper === "function"
      ? options.isDeveloper
      : () => false;
    const request = typeof transport.request === "function"
      ? transport.request
      : (url, init) => {
          if (typeof fetch !== "function") throw new Error("relationship transport is unavailable");
          return fetch(url, init);
        };

    let bound = false;
    let generation = 0;
    let loadInFlight = 0;
    let relationship = null;

    function isCurrent(token) {
      return token === generation;
    }

    function requestedScope() {
      const scopeValue = String(elements.scopeInput?.value || "release");
      return ["release", "normal", "developer"].includes(scopeValue)
        ? scopeValue
        : "release";
    }

    function render(nextRelationship = {}) {
      if (!isDeveloper()) return false;
      relationship = nextRelationship && typeof nextRelationship === "object"
        ? { ...nextRelationship }
        : {};
      const stageLabels = {
        close_companion: "亲密同伴：朋友以上，尚未进入承诺关系",
        trusted_intimacy: "深度信赖：亲密与信任已经稳定形成",
        committed_partner: "稳定伴侣：已进入明确、持续的恋人关系",
        life_partner: "生活伴侣：日常计划与共同习惯深度交织",
        old_married: "长期伴侣：接近老夫老妻般的稳定默契",
      };
      const stage = String(relationship.stage || "close_companion");
      const developerView = relationship.developerView || {};
      if (elements.stage) {
        elements.stage.textContent = `${stage} / ${stageLabels[stage] || "未知阶段"}`;
      }
      const metrics = [
        ["熟悉度", relationship.familiarity],
        ["信任", relationship.trust],
        ["打趣默契", relationship.teasingLevel],
        ["关心程度", relationship.careLevel],
        ["关系稳定", relationship.stability],
      ];
      if (elements.meters && documentObject?.createElement) {
        elements.meters.replaceChildren(...metrics.map(([label, rawValue]) => {
          const value = clamp(number(rawValue), 0, 1);
          const row = documentObject.createElement("div");
          row.className = "relationship-meter";
          const name = documentObject.createElement("span");
          name.textContent = label;
          const meter = documentObject.createElement("i");
          meter.style?.setProperty?.("--value", `${(value * 100).toFixed(1)}%`);
          const valueNode = documentObject.createElement("code");
          valueNode.textContent = value.toFixed(3);
          row.append?.(name, meter, valueNode);
          return row;
        }));
      }
      const radarValues = metrics.map(([, rawValue]) => clamp(number(rawValue), 0, 1));
      const radarPoints = radarValues.map((value, index) => {
        const angle = (-90 + index * 72) * Math.PI / 180;
        const radius = 60 * value;
        return [100 + Math.cos(angle) * radius, 80 + Math.sin(angle) * radius];
      });
      if (elements.radarValue) {
        elements.radarValue.setAttribute?.(
          "points",
          radarPoints.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join(" "),
        );
      }
      if (elements.radarDots && documentObject?.createElementNS) {
        const svgNamespace = "http://www.w3.org/2000/svg";
        elements.radarDots.replaceChildren(...radarPoints.map(([x, y]) => {
          const dot = documentObject.createElementNS(svgNamespace, "circle");
          dot.setAttribute("cx", x.toFixed(2));
          dot.setAttribute("cy", y.toFixed(2));
          dot.setAttribute("r", "2.8");
          return dot;
        }));
      }
      if (elements.radar) {
        elements.radar.setAttribute?.(
          "aria-label",
          `关系五维：熟悉 ${(radarValues[0] * 100).toFixed(0)}%，信任 ${(radarValues[1] * 100).toFixed(0)}%，`
          + `默契 ${(radarValues[2] * 100).toFixed(0)}%，关心 ${(radarValues[3] * 100).toFixed(0)}%，稳定 ${(radarValues[4] * 100).toFixed(0)}%`,
        );
      }
      const nextStage = developerView.nextStage || null;
      const readiness = nextStage ? clamp(number(nextStage.readiness), 0, 1) : 1;
      if (elements.nextTitle) {
        elements.nextTitle.textContent = nextStage ? `下一阶段：${nextStage.label}` : "已到最高关系阶段";
      }
      if (elements.readiness) {
        elements.readiness.style?.setProperty?.("--value", `${(readiness * 100).toFixed(1)}%`);
      }
      if (elements.readinessValue) {
        elements.readinessValue.textContent = nextStage
          ? `瓶颈进度 ${(readiness * 100).toFixed(1)}%`
          : "全部阶段已达成";
      }
      if (elements.reactionProfile) {
        elements.reactionProfile.textContent = `成人视觉反应：${developerView.visualReactionProfile || "按当前关系自然调整"}`;
      }
      if (elements.stagePath && documentObject?.createElement) {
        const stagePath = Array.isArray(developerView.stagePath) ? developerView.stagePath : [];
        elements.stagePath.replaceChildren(...stagePath.map(item => {
          const node = documentObject.createElement("span");
          node.className = `relationship-stage-node ${item.state || "locked"}`;
          node.textContent = item.label || item.id || "-";
          node.title = item.state === "current" ? "当前阶段" : item.state === "reached" ? "已达成" : "未解锁";
          return node;
        }));
      }
      if (elements.requirements && documentObject?.createElement) {
        const requirements = nextStage && Array.isArray(nextStage.requirements)
          ? nextStage.requirements
          : [];
        elements.requirements.replaceChildren(...requirements.map(item => {
          const row = documentObject.createElement("div");
          row.className = `relationship-requirement ${item.met ? "met" : "pending"}`;
          const label = documentObject.createElement("span");
          label.textContent = `${item.met ? "✓" : "○"} ${item.label}`;
          const meter = documentObject.createElement("i");
          meter.style?.setProperty?.("--value", `${(clamp(number(item.progress), 0, 1) * 100).toFixed(1)}%`);
          const valueNode = documentObject.createElement("code");
          valueNode.textContent = `${item.current} / ${item.required}`;
          valueNode.title = item.met ? "已满足" : `还差 ${item.remaining}`;
          row.append?.(label, meter, valueNode);
          return row;
        }));
      }
      if (elements.explanation) {
        elements.explanation.textContent = developerView.metricExplanation
          || "关系阶段采用多条件门槛制，不由单个数值决定。";
      }
      if (elements.detail) {
        elements.detail.textContent = formatJson({
          interactionCount: relationship.interactionCount || 0,
          positiveEvents: relationship.positiveEvents || 0,
          sharedRoutineEvents: relationship.sharedRoutineEvents || 0,
          repairEvents: relationship.repairEvents || 0,
          lastEmotionalEvent: relationship.lastEmotionalEvent || "",
          milestones: relationship.milestones || [],
          nextStage: developerView.nextStage || null,
          visualReactionProfile: developerView.visualReactionProfile || "",
          updatedAt: relationship.updatedAt || "",
        });
      }
      return relationship;
    }

    async function load() {
      if (!isDeveloper()) return null;
      const token = ++generation;
      loadInFlight += 1;
      const scope = requestedScope();
      try {
        const response = await request(
          `/api/developer/relationship?scope=${encodeURIComponent(scope)}`,
        );
        const data = await response.json();
        if (!isCurrent(token)) return { stale: true };
        if (!response.ok || data.ok === false) {
          throw new Error(data.error || `HTTP ${response.status}`);
        }
        const snapshot = render(data.relationship || {});
        if (elements.scopeStatus) {
          elements.scopeStatus.textContent = data.scope === "release"
            ? "正在查看正式 release 用户数据（只读）；不会写入 release，也不会切换开发者运行态。"
            : data.scope === "normal"
              ? "正在查看源码普通模式数据（只读）；开发者对话、记忆和关系运行态仍保持隔离。"
              : "正在查看开发者隔离数据（只读）；此页面不会修改关系运行态。";
        }
        return snapshot;
      } catch (error) {
        if (!isCurrent(token)) return { stale: true };
        if (elements.stage) elements.stage.textContent = `加载失败：${error.message}`;
        if (elements.scopeStatus) elements.scopeStatus.textContent = "关系数据加载失败，未切换任何运行状态。";
        feedback.warn?.("relationship load failed:", error);
        return null;
      } finally {
        loadInFlight = Math.max(0, loadInFlight - 1);
      }
    }

    function bind() {
      if (bound) return false;
      bound = true;
      elements.refresh?.addEventListener?.("click", () => { void load(); });
      elements.scopeInput?.addEventListener?.("change", () => { void load(); });
      return true;
    }

    return Object.freeze({
      bind,
      getState: () => ({
        bound,
        generation,
        loadInFlight,
        relationship: relationship ? { ...relationship } : null,
      }),
      load,
      render,
    });
  }

  return Object.freeze({ createRelationshipController });
}));
