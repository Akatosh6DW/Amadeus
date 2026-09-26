// 开发者语音参考控制器唯一持有参考音目录、筛选/审阅草稿、音频对象 URL 和请求代次。
// speech playback 只负责实际播放；参数实验台只负责表情训练，页面通过窄 port 协调两个工作区。
(function attachDeveloperVoiceReferenceController(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusVoiceReferenceController = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createDeveloperVoiceReferenceControllerApi() {
  "use strict";

  const REVIEW_LABELS = Object.freeze({
    unreviewed: "未审阅",
    approved: "合格",
    rejected: "不合格",
  });
  const MAPPING_FIT_LABELS = Object.freeze({
    unreviewed: "未判断",
    matched: "匹配",
    mismatched: "不匹配（原音可用）",
  });
  const RECHECK_LABELS = Object.freeze({
    pending_recheck: "待复检",
    recheck_passed: "复检通过",
    recheck_failed: "复检仍不合格",
  });
  const USAGE_LABELS = Object.freeze({ active: "启用中", inactive: "未启用" });
  const SYSTEM_LABELS = Object.freeze({
    eligible: "可合成",
    high_risk: "高风险",
    disqualified: "禁用",
    missing: "文件缺失",
  });
  const CANDIDATE_ROLE_LABELS = Object.freeze({ audition: "试听候选", replacement: "替换候选" });

  function createDeveloperVoiceReferenceController(options = {}) {
    const dom = options.dom || {};
    const document = options.document || globalThis.document;
    const window = options.window || globalThis;
    const urlApi = options.urlApi || window.URL || globalThis.URL;
    const isDeveloper = typeof options.isDeveloper === "function" ? options.isDeveloper : () => false;
    const storageScopedFetch = typeof options.storageScopedFetch === "function"
      ? options.storageScopedFetch
      : (...args) => window.fetch(...args);
    const notify = typeof options.notify === "function" ? options.notify : () => {};
    const onExpressionTrainingSelected = typeof options.onExpressionTrainingSelected === "function"
      ? options.onExpressionTrainingSelected
      : () => {};
    const onExpressionTrainingLoaded = typeof options.onExpressionTrainingLoaded === "function"
      ? options.onExpressionTrainingLoaded
      : () => {};
    const confirmAction = typeof options.confirm === "function"
      ? options.confirm
      : message => window.confirm(message);
    let bound = false;
    let trainingKind = "expression";
    let references = [];
    let catalog = null;
    let loaded = false;
    let selectedId = "";
    let draft = null;
    let dirty = false;
    let candidateAudioUrl = "";
    let originalAudioUrl = "";
    const audioLoadTokens = { candidate: 0, original: 0 };
    let loadGeneration = 0;
    let mutationGeneration = 0;
    let mutationBusy = false;

    function displayStatus(item) {
      const manualStatus = item?.manualReviewStatus || item?.status || "unreviewed";
      return REVIEW_LABELS[manualStatus] || "未审阅";
    }

    function displayKeys(item) {
      return (item?.mappingKeys || []).filter(key => key !== "default");
    }

    function cloneReference(item) {
      return {
        ...item,
        mappingKeys: Array.isArray(item?.mappingKeys) ? [...item.mappingKeys] : [],
        referenceTexts: Array.isArray(item?.referenceTexts) ? [...item.referenceTexts] : [],
        reasonCodes: Array.isArray(item?.reasonCodes) ? [...item.reasonCodes] : [],
      };
    }

    function replaceCatalog(data) {
      catalog = data && typeof data === "object" ? { ...data } : null;
      references = Array.isArray(data?.references) ? data.references.map(cloneReference) : [];
      loaded = true;
      if (selectedId && !references.some(item => item.id === selectedId)) selectedId = "";
      renderGallery();
    }

    function setTrainingKind(kind) {
      if (!isDeveloper()) return;
      trainingKind = kind === "voice-reference" ? "voice-reference" : "expression";
      const voiceActive = trainingKind === "voice-reference";
      dom.devTrainingExpressionTab?.classList.toggle("active", !voiceActive);
      dom.devTrainingExpressionTab?.setAttribute("aria-selected", String(!voiceActive));
      dom.devTrainingVoiceReferenceTab?.classList.toggle("active", voiceActive);
      dom.devTrainingVoiceReferenceTab?.setAttribute("aria-selected", String(voiceActive));
      dom.devExpressionTrainingBrowserContent?.classList.toggle("hidden", voiceActive);
      dom.devVoiceReferenceBrowserContent?.classList.toggle("hidden", !voiceActive);
      if (dom.devExpressionTrainingBrowserContent) {
        dom.devExpressionTrainingBrowserContent.hidden = voiceActive;
        dom.devExpressionTrainingBrowserContent.setAttribute("aria-hidden", String(voiceActive));
      }
      if (dom.devVoiceReferenceBrowserContent) {
        dom.devVoiceReferenceBrowserContent.hidden = !voiceActive;
        dom.devVoiceReferenceBrowserContent.setAttribute("aria-hidden", String(!voiceActive));
      }
      if (voiceActive) loadReferences(true);
      else onExpressionTrainingSelected();
    }

    async function loadReferences(force = false) {
      if (!isDeveloper() || (loaded && !force)) {
        renderGallery();
        return;
      }
      const generation = ++loadGeneration;
      if (dom.devVoiceReferenceStatus) dom.devVoiceReferenceStatus.textContent = "载入参考音…";
      if (dom.devVoiceReferenceRefreshBtn) dom.devVoiceReferenceRefreshBtn.disabled = true;
      try {
        const response = await storageScopedFetch("/api/developer/voice-reference-review", { cache: "no-store" });
        const data = await response.json();
        if (!response.ok || data.ok === false) throw new Error(data.message || data.error || `HTTP ${response.status}`);
        if (generation !== loadGeneration) return;
        replaceCatalog(data);
      } catch (error) {
        if (generation === loadGeneration) {
          if (dom.devVoiceReferenceStatus) dom.devVoiceReferenceStatus.textContent = `载入失败：${error.message}`;
          notify(`参考音载入失败：${error.message}`);
        }
      } finally {
        if (generation === loadGeneration && dom.devVoiceReferenceRefreshBtn) {
          dom.devVoiceReferenceRefreshBtn.disabled = false;
        }
      }
    }

    function renderGallery() {
      if (!isDeveloper() || !dom.devVoiceReferenceGallery) return;
      const query = String(dom.devVoiceReferenceFilterInput?.value || "").trim().toLowerCase();
      const statusFilter = String(dom.devVoiceReferenceStatusFilter?.value || "");
      const runtimeFilter = String(dom.devVoiceReferenceRuntimeFilter?.value || "");
      const systemFilter = String(dom.devVoiceReferenceSystemFilter?.value || "");
      const filtered = references.filter(item => {
        const manualStatus = item.manualReviewStatus || item.status || "unreviewed";
        if (statusFilter && manualStatus !== statusFilter) return false;
        if (runtimeFilter === "active" && item.usageStatus !== "active") return false;
        if (runtimeFilter === "inactive" && item.usageStatus !== "inactive") return false;
        if (runtimeFilter === "audition" && item.candidateRole !== "audition") return false;
        if (runtimeFilter === "replacement" && item.candidateRole !== "replacement") return false;
        if (systemFilter && item.systemStatus !== systemFilter) return false;
        if (!query) return true;
        const haystack = [
          item.voiceId,
          item.audioFile,
          ...(item.mappingKeys || []),
          ...(item.activeMappingKeys || []),
          ...(item.activeElsewhereMappingKeys || []),
          ...(item.referenceTexts || []),
          item.notes,
          item.emotionLabel,
          item.mappingFitStatus,
          item.attentionLabel,
          item.riskNote,
          item.recommendation,
          item.usageStatus,
          item.systemStatus,
          item.systemReason,
          item.candidateRole,
        ].join("\n").toLowerCase();
        return haystack.includes(query);
      });
      if (!filtered.length) {
        const empty = document.createElement("div");
        empty.className = "dev-training-empty";
        empty.textContent = loaded ? "没有符合筛选条件的参考音。" : "尚未载入参考音。";
        dom.devVoiceReferenceGallery.replaceChildren(empty);
      } else {
        dom.devVoiceReferenceGallery.replaceChildren(...filtered.map(item => {
          const card = document.createElement("button");
          card.type = "button";
          card.dataset.referenceId = item.id;
          const manualStatus = item.manualReviewStatus || item.status || "unreviewed";
          const usageStatus = item.usageStatus || (item.isCurrentlyEffective ? "active" : "inactive");
          const systemStatus = item.systemStatus || "eligible";
          card.className = "dev-training-card dev-voice-reference-card";
          card.classList.add(`usage-${usageStatus}`, `system-${systemStatus}`, `manual-${manualStatus}`);
          if (item.attentionLevel) card.classList.add(`attention-${item.attentionLevel}`);
          if (item.candidateRole && item.candidateRole !== "none") card.classList.add(`role-${item.candidateRole}`);
          card.classList.toggle("selected", item.id === selectedId);

          const icon = document.createElement("div");
          icon.className = "dev-voice-reference-icon";
          icon.textContent = "♫";
          const badge = document.createElement("span");
          badge.className = `dev-voice-reference-status-badge ${manualStatus}`;
          badge.textContent = `原音质量：${displayStatus(item)}`;
          icon.append(badge);

          const copy = document.createElement("div");
          copy.className = "dev-training-card-copy";
          const stateRow = document.createElement("div");
          stateRow.className = "dev-voice-reference-state-row";
          const usageBadge = document.createElement("span");
          usageBadge.className = `dev-voice-reference-active-badge ${usageStatus}`;
          usageBadge.textContent = `映射：${USAGE_LABELS[usageStatus] || usageStatus}`;
          const systemBadge = document.createElement("span");
          systemBadge.className = `dev-voice-reference-system-badge ${systemStatus}`;
          systemBadge.textContent = `合成风险：${SYSTEM_LABELS[systemStatus] || systemStatus}`;
          stateRow.append(usageBadge, systemBadge);
          const mappingFitStatus = item.mappingFitStatus || "unreviewed";
          const fitBadge = document.createElement("span");
          fitBadge.className = `dev-voice-reference-fit-badge ${mappingFitStatus}`;
          fitBadge.textContent = `标签匹配：${MAPPING_FIT_LABELS[mappingFitStatus] || mappingFitStatus}`;
          stateRow.append(fitBadge);
          if (item.emotionLabel) {
            const emotionBadge = document.createElement("span");
            emotionBadge.className = "dev-voice-reference-emotion-badge";
            emotionBadge.textContent = `情绪：${item.emotionLabel}`;
            stateRow.append(emotionBadge);
          }
          if (item.candidateRole && item.candidateRole !== "none") {
            const roleBadge = document.createElement("span");
            roleBadge.className = `dev-voice-reference-role-badge ${item.candidateRole}`;
            roleBadge.textContent = CANDIDATE_ROLE_LABELS[item.candidateRole] || item.candidateRole;
            stateRow.append(roleBadge);
          }
          const activeElsewhereMappingKeys = Array.isArray(item.activeElsewhereMappingKeys)
            ? item.activeElsewhereMappingKeys.filter(Boolean)
            : [];
          if (activeElsewhereMappingKeys.length) {
            const elsewhereBadge = document.createElement("span");
            elsewhereBadge.className = "dev-voice-reference-active-badge active";
            elsewhereBadge.textContent = `其他用途启用：${activeElsewhereMappingKeys.join(" / ")}`;
            stateRow.append(elsewhereBadge);
          }
          if (item.reviewWorkflowStatus && item.reviewWorkflowStatus !== "none") {
            const workflowBadge = document.createElement("span");
            workflowBadge.className = `dev-voice-reference-workflow-badge ${item.reviewWorkflowStatus}`;
            workflowBadge.textContent = `复检：${RECHECK_LABELS[item.reviewWorkflowStatus] || item.reviewWorkflowStatus}`;
            stateRow.append(workflowBadge);
          }
          copy.append(stateRow);
          if (item.attentionLabel) {
            const attention = document.createElement("span");
            attention.className = `dev-voice-reference-attention ${item.attentionLevel || "candidate"}`;
            attention.textContent = item.attentionLabel;
            copy.append(attention);
          }
          const title = document.createElement("strong");
          title.textContent = displayKeys(item).join(" / ") || item.voiceId;
          const meta = document.createElement("span");
          const duration = Number.isFinite(Number(item.durationSeconds))
            ? `${Number(item.durationSeconds).toFixed(2)} 秒`
            : "时长未知";
          meta.textContent = `${item.audioFile || item.voiceId} · ${duration}`;
          const transcript = document.createElement("span");
          transcript.textContent = (item.referenceTexts || []).join(" / ") || "未找到参考文本";
          copy.append(title, meta, transcript);
          card.append(icon, copy);
          return card;
        }));
      }
      const approved = references.filter(item => (item.manualReviewStatus || item.status) === "approved").length;
      const rejected = references.filter(item => (item.manualReviewStatus || item.status) === "rejected").length;
      const unreviewed = references.length - approved - rejected;
      const pendingRecheck = references.filter(item => item.reviewWorkflowStatus === "pending_recheck").length;
      const highRisk = references.filter(item => item.systemStatus === "high_risk");
      const systemDisqualified = references.filter(item => ["disqualified", "missing"].includes(item.systemStatus));
      const auditionCandidates = references.filter(item => item.candidateRole === "audition");
      const selectedChoices = auditionCandidates.filter(item => item.isSelectedChoice);
      const effectiveReferences = references.filter(item => item.usageStatus === "active");
      const inactiveReferences = references.filter(item => item.usageStatus === "inactive");
      const eligibleReferences = references.filter(item => item.systemStatus === "eligible");
      const activeDisqualified = systemDisqualified.filter(item => item.usageStatus === "active");
      const linkageActive = catalog?.linkage?.source === "effective_style_map";
      if (dom.devVoiceReferenceAttentionBanner) {
        const hasSystemWarning = Boolean(systemDisqualified.length || highRisk.length);
        dom.devVoiceReferenceAttentionBanner.hidden = !hasSystemWarning && !linkageActive;
        dom.devVoiceReferenceAttentionBanner.classList.toggle("hidden", !hasSystemWarning && !linkageActive);
        dom.devVoiceReferenceAttentionBanner.classList.toggle("synced", !hasSystemWarning && linkageActive);
        dom.devVoiceReferenceAttentionBanner.textContent = systemDisqualified.length
          ? `⚠ 合成风险中有 ${systemDisqualified.length} 条禁用或文件缺失参考${activeDisqualified.length ? `，其中 ${activeDisqualified.length} 条仍在启用` : ""}。这是系统判定，不等于“我的标注”。`
          : (highRisk.length
            ? `⚠ 系统检测到 ${highRisk.length} 条高风险参考。已提供 ${auditionCandidates.length} 个试听候选；${selectedChoices.length ? `已记录 ${selectedChoices.length} 个待晋升选择` : "尚未记录待晋升选择"}。`
            : (linkageActive ? "✓ 已与实际生效的 style map 联动；映射、合成风险、原音质量和标签匹配会分别显示。" : ""));
      }
      if (dom.devVoiceReferenceStatus) {
        dom.devVoiceReferenceStatus.textContent = `${references.length} 条 · 映射：启用 ${effectiveReferences.length} / 未启用 ${inactiveReferences.length} · 合成风险：可合成 ${eligibleReferences.length} / 高风险 ${highRisk.length} / 禁用或缺失 ${systemDisqualified.length} · 原音质量：合格 ${approved} / 不合格 ${rejected} / 未审阅 ${unreviewed} · 待复检 ${pendingRecheck}（各维度互不覆盖）`;
      }
    }

    function handleGalleryClick(event) {
      const card = event.target.closest("[data-reference-id]");
      if (card) openEditor(card.dataset.referenceId);
    }

    function syncReasonAvailability() {
      dom.devVoiceReferenceReasons?.querySelectorAll('input[type="checkbox"]').forEach(input => {
        input.disabled = false;
      });
    }

    function markDirty() {
      dirty = true;
      syncReasonAvailability();
    }

    function handleReasonChange() {
      const hasQualityReason = Boolean(
        dom.devVoiceReferenceReasons?.querySelector('input[data-review-axis="quality"]:checked'),
      );
      const hasMappingReason = Boolean(
        dom.devVoiceReferenceReasons?.querySelector('input[data-review-axis="mapping"]:checked'),
      );
      if (hasQualityReason && dom.devVoiceReferenceReviewStatusInput) {
        dom.devVoiceReferenceReviewStatusInput.value = "rejected";
      }
      if (hasMappingReason && dom.devVoiceReferenceMappingFitInput) {
        dom.devVoiceReferenceMappingFitInput.value = "mismatched";
      }
      markDirty();
    }

    function handleStatusChange() {
      if (dom.devVoiceReferenceReviewStatusInput?.value !== "rejected") {
        dom.devVoiceReferenceReasons?.querySelectorAll('input[data-review-axis="quality"]').forEach(input => {
          input.checked = false;
        });
      }
      markDirty();
    }

    function handleMappingFitChange() {
      if (dom.devVoiceReferenceMappingFitInput?.value !== "mismatched") {
        dom.devVoiceReferenceReasons?.querySelectorAll('input[data-review-axis="mapping"]').forEach(input => {
          input.checked = false;
        });
      }
      markDirty();
    }

    async function loadAudio(referenceId, variant = "candidate") {
      const originalVariant = variant === "original";
      const tokenKey = originalVariant ? "original" : "candidate";
      const loadToken = ++audioLoadTokens[tokenKey];
      const audioElement = originalVariant ? dom.devVoiceReferenceOriginalAudio : dom.devVoiceReferenceAudio;
      const previousUrl = originalVariant ? originalAudioUrl : candidateAudioUrl;
      if (previousUrl) urlApi?.revokeObjectURL?.(previousUrl);
      if (originalVariant) originalAudioUrl = "";
      else candidateAudioUrl = "";
      audioElement?.removeAttribute?.("src");
      try {
        const response = await storageScopedFetch(`/api/developer/voice-reference-review/audio?id=${encodeURIComponent(referenceId)}&variant=${encodeURIComponent(variant)}`, { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const objectUrl = urlApi.createObjectURL(await response.blob());
        if (loadToken !== audioLoadTokens[tokenKey] || selectedId !== referenceId) {
          urlApi.revokeObjectURL(objectUrl);
          return;
        }
        if (originalVariant) originalAudioUrl = objectUrl;
        else candidateAudioUrl = objectUrl;
        if (audioElement) {
          audioElement.src = objectUrl;
          audioElement.load?.();
        }
      } catch (error) {
        notify(`参考音读取失败：${error.message}`);
      }
    }

    function openEditor(referenceId) {
      const item = references.find(entry => entry.id === referenceId);
      if (!item) return;
      selectedId = item.id;
      draft = {
        id: item.id,
        status: item.manualReviewStatus || item.status || "unreviewed",
        mappingFitStatus: item.mappingFitStatus || "unreviewed",
        emotionLabel: item.emotionLabel || "",
        notes: item.notes || "",
        reasonCodes: [...(item.reasonCodes || [])],
      };
      dirty = false;
      onExpressionTrainingSelected("close-editor");
      dom.devTrainingBrowser?.classList.add("hidden");
      dom.devTrainingEditor?.classList.add("hidden");
      dom.devVoiceReferenceEditor?.classList.remove("hidden");
      const activeElsewhereMappingKeys = Array.isArray(item.activeElsewhereMappingKeys)
        ? item.activeElsewhereMappingKeys.filter(Boolean)
        : [];
      if (dom.devVoiceReferenceEditorHint) {
        dom.devVoiceReferenceEditorHint.textContent = `${item.audioFile || item.voiceId} · ${Number(item.durationSeconds || 0).toFixed(2)} 秒 · 原音质量：${displayStatus(item)}`;
      }
      if (dom.devVoiceReferenceSummary) {
        const warning = item.attentionLabel ? ` ${item.attentionLabel}。` : "";
        const usageStatus = item.usageStatus || (item.isCurrentlyEffective ? "active" : "inactive");
        const systemStatus = item.systemStatus || "eligible";
        const roleLabel = item.runtimeBehavior === "historical_review"
          ? "历史审阅记录"
          : (CANDIDATE_ROLE_LABELS[item.candidateRole] || "正式参考");
        const workflowLabel = item.reviewWorkflowStatus && item.reviewWorkflowStatus !== "none"
          ? `；复检流程：${RECHECK_LABELS[item.reviewWorkflowStatus] || item.reviewWorkflowStatus}`
          : "";
        const fitStatus = item.mappingFitStatus || "unreviewed";
        const emotionLabel = item.emotionLabel ? `；你标的情绪：${item.emotionLabel}` : "";
        const activeElsewhere = activeElsewhereMappingKeys.length
          ? `；其他已启用用途：${activeElsewhereMappingKeys.join("、")}`
          : "";
        dom.devVoiceReferenceSummary.textContent = `参考音 ${item.voiceId}；当前用途映射状态：${USAGE_LABELS[usageStatus] || usageStatus}${activeElsewhere}；合成风险：${SYSTEM_LABELS[systemStatus] || systemStatus}；用途：${roleLabel}；原音质量：${displayStatus(item)}；标签匹配：${MAPPING_FIT_LABELS[fitStatus] || fitStatus}${emotionLabel}${workflowLabel}；当前卡片对应 ${displayKeys(item).join("、") || "默认声音"}。${warning}`;
      }
      if (dom.devVoiceReferenceMappings) dom.devVoiceReferenceMappings.value = (item.mappingKeys || []).join("\n");
      if (dom.devVoiceReferenceTranscript) dom.devVoiceReferenceTranscript.value = (item.referenceTexts || []).join("\n\n");
      const replacement = item.replacement || null;
      const comparisonCandidate = Boolean(item.isReplacementCandidate || item.isAuditionCandidate || item.isComparisonCandidate);
      if (dom.devVoiceReferenceOriginalPlayer) dom.devVoiceReferenceOriginalPlayer.hidden = !comparisonCandidate;
      if (dom.devVoiceReferenceCandidateLabel) {
        dom.devVoiceReferenceCandidateLabel.textContent = item.candidateRole === "audition"
          ? (item.usageStatus === "active"
            ? "试听候选（当前用途同时启用中）"
            : (activeElsewhereMappingKeys.length ? "试听候选（当前用途未启用；其他用途已启用）" : "试听候选（未启用）"))
          : (item.candidateRole === "replacement" ? "替换候选" : "当前参考音");
      }
      if (dom.devVoiceReferenceChangeInfo) {
        const originalFile = String(replacement?.original?.refAudio || "").split(/[\\/]/).pop() || "";
        dom.devVoiceReferenceChangeInfo.textContent = comparisonCandidate
          ? `原音 ${originalFile} → 候选 ${item.audioFile}\n${item.candidateRole === "audition" ? "试听依据" : "替换依据"}：${replacement?.reason || "根据人工标注重新筛选"}\n候选来源：${replacement?.source || "本地原作语音库"}${item.systemReason ? `\n系统状态依据：${item.systemReason}` : ""}${item.riskNote ? `\n风险说明：${item.riskNote}` : ""}`
          : (`${item.systemReason ? `系统状态依据：${item.systemReason}` : "当前参考音尚未被替换。"}${item.riskNote ? `\n风险说明：${item.riskNote}` : ""}`);
      }
      if (dom.devVoiceReferenceReviewStatusInput) dom.devVoiceReferenceReviewStatusInput.value = draft.status;
      if (dom.devVoiceReferenceMappingFitInput) dom.devVoiceReferenceMappingFitInput.value = draft.mappingFitStatus;
      if (dom.devVoiceReferenceEmotionLabelInput) dom.devVoiceReferenceEmotionLabelInput.value = draft.emotionLabel;
      if (dom.devVoiceReferenceNotesInput) dom.devVoiceReferenceNotesInput.value = draft.notes;
      dom.devVoiceReferenceReasons?.querySelectorAll('input[type="checkbox"]').forEach(input => {
        input.checked = draft.reasonCodes.includes(input.value);
      });
      syncReasonAvailability();
      if (dom.devVoiceReferenceSelectCandidateBtn) {
        const auditionCandidate = item.candidateRole === "audition" || item.isAuditionCandidate;
        dom.devVoiceReferenceSelectCandidateBtn.hidden = !auditionCandidate;
        dom.devVoiceReferenceSelectCandidateBtn.classList.toggle("hidden", !auditionCandidate);
        dom.devVoiceReferenceSelectCandidateBtn.disabled = Boolean(item.isSelectedChoice || item.selectionAllowed === false);
        dom.devVoiceReferenceSelectCandidateBtn.textContent = item.selectionAllowed === false
          ? "合成风险判定为禁用或文件缺失，不能选为待晋升候选"
          : (item.isSelectedChoice ? "✓ 已选为待晋升候选" : "选择为待晋升候选（不会立即应用）");
      }
      if (comparisonCandidate) {
        loadAudio(item.id, "original");
        window.setTimeout?.(() => loadAudio(item.id, "candidate"), 0);
      } else {
        loadAudio(item.id, "candidate");
      }
      window.requestAnimationFrame?.(() => dom.devVoiceReferenceEditor?.scrollIntoView?.({ block: "start", inline: "nearest" }));
    }

    function closeEditor() {
      if (dirty && !confirmAction("当前参考音标注尚未保存，确定返回吗？")) return false;
      dom.devVoiceReferenceAudio?.pause?.();
      dom.devVoiceReferenceOriginalAudio?.pause?.();
      if (candidateAudioUrl) urlApi?.revokeObjectURL?.(candidateAudioUrl);
      candidateAudioUrl = "";
      if (originalAudioUrl) urlApi?.revokeObjectURL?.(originalAudioUrl);
      originalAudioUrl = "";
      audioLoadTokens.candidate += 1;
      audioLoadTokens.original += 1;
      draft = null;
      dirty = false;
      dom.devVoiceReferenceEditor?.classList.add("hidden");
      dom.devTrainingBrowser?.classList.remove("hidden");
      setTrainingKind("voice-reference");
      return true;
    }

    async function persistReview(payload) {
      if (mutationBusy) return;
      const generation = ++mutationGeneration;
      mutationBusy = true;
      if (dom.devVoiceReferenceSaveBtn) dom.devVoiceReferenceSaveBtn.disabled = true;
      if (dom.devVoiceReferenceResetBtn) dom.devVoiceReferenceResetBtn.disabled = true;
      try {
        const response = await storageScopedFetch("/api/developer/voice-reference-review", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (!response.ok || data.ok === false) throw new Error(data.message || data.error || `HTTP ${response.status}`);
        if (generation !== mutationGeneration) return;
        replaceCatalog(data);
        dirty = false;
        closeEditor();
        notify(payload.action === "reset" ? "已重置为未审阅" : "参考音标注已保存");
      } catch (error) {
        if (generation === mutationGeneration) notify(`参考音标注保存失败：${error.message}`);
      } finally {
        if (generation === mutationGeneration) {
          mutationBusy = false;
          if (dom.devVoiceReferenceSaveBtn) dom.devVoiceReferenceSaveBtn.disabled = false;
          if (dom.devVoiceReferenceResetBtn) dom.devVoiceReferenceResetBtn.disabled = false;
        }
      }
    }

    async function selectCandidate() {
      const item = references.find(entry => entry.id === draft?.id);
      if (!item?.isAuditionCandidate || item.selectionAllowed === false || !item.choiceSetId || !item.choiceCandidateId) return;
      if (mutationBusy) return;
      const generation = ++mutationGeneration;
      mutationBusy = true;
      if (dom.devVoiceReferenceSelectCandidateBtn) dom.devVoiceReferenceSelectCandidateBtn.disabled = true;
      try {
        const response = await storageScopedFetch("/api/developer/voice-reference-review", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            action: "select_candidate",
            choiceSetId: item.choiceSetId,
            candidateId: item.choiceCandidateId,
          }),
        });
        const data = await response.json();
        if (!response.ok || data.ok === false) throw new Error(data.message || data.error || `HTTP ${response.status}`);
        if (generation !== mutationGeneration) return;
        replaceCatalog(data);
        openEditor(item.id);
        notify(`已记录 ${item.voiceId} 为待晋升候选；运行时参考音未改变`);
      } catch (error) {
        if (generation === mutationGeneration) notify(`候选选择保存失败：${error.message}`);
      } finally {
        if (generation === mutationGeneration) {
          mutationBusy = false;
          if (dom.devVoiceReferenceSelectCandidateBtn) dom.devVoiceReferenceSelectCandidateBtn.disabled = false;
        }
      }
    }

    async function saveReview() {
      if (!draft) return;
      const selectedReasons = [
        ...(dom.devVoiceReferenceReasons?.querySelectorAll('input[type="checkbox"]:checked') || []),
      ].map(input => input.value);
      const qualityReasons = selectedReasons.filter(code => !["emotion_weak", "emotion_mismatch"].includes(code));
      const mappingReasons = selectedReasons.filter(code => ["emotion_weak", "emotion_mismatch"].includes(code));
      const status = qualityReasons.length
        ? "rejected"
        : String(dom.devVoiceReferenceReviewStatusInput?.value || "unreviewed");
      const mappingFitStatus = mappingReasons.length
        ? "mismatched"
        : String(dom.devVoiceReferenceMappingFitInput?.value || "unreviewed");
      await persistReview({
        action: "upsert",
        id: draft.id,
        status,
        qualityStatus: status,
        mappingFitStatus,
        emotionLabel: String(dom.devVoiceReferenceEmotionLabelInput?.value || "").trim(),
        reasonCodes: selectedReasons,
        notes: String(dom.devVoiceReferenceNotesInput?.value || "").trim(),
      });
    }

    async function resetReview() {
      if (!draft) return;
      await persistReview({ action: "reset", id: draft.id });
    }

    function loadTrainingData() {
      loadReferences();
      onExpressionTrainingLoaded();
    }

    function bind() {
      if (bound) return false;
      bound = true;
      if (dom.devTrainingExpressionTab) dom.devTrainingExpressionTab.addEventListener("click", () => setTrainingKind("expression"));
      if (dom.devTrainingVoiceReferenceTab) dom.devTrainingVoiceReferenceTab.addEventListener("click", () => setTrainingKind("voice-reference"));
      dom.devVoiceReferenceFilterInput?.addEventListener("input", renderGallery);
      dom.devVoiceReferenceStatusFilter?.addEventListener("change", renderGallery);
      dom.devVoiceReferenceRuntimeFilter?.addEventListener("change", renderGallery);
      dom.devVoiceReferenceSystemFilter?.addEventListener("change", renderGallery);
      dom.devVoiceReferenceRefreshBtn?.addEventListener("click", () => loadReferences(true));
      dom.devVoiceReferenceGallery?.addEventListener("click", handleGalleryClick);
      dom.devVoiceReferenceCloseEditorBtn?.addEventListener("click", closeEditor);
      dom.devVoiceReferenceSaveBtn?.addEventListener("click", saveReview);
      dom.devVoiceReferenceResetBtn?.addEventListener("click", resetReview);
      dom.devVoiceReferenceSelectCandidateBtn?.addEventListener("click", selectCandidate);
      dom.devVoiceReferenceReviewStatusInput?.addEventListener("change", handleStatusChange);
      dom.devVoiceReferenceEmotionLabelInput?.addEventListener("input", markDirty);
      dom.devVoiceReferenceMappingFitInput?.addEventListener("change", handleMappingFitChange);
      dom.devVoiceReferenceNotesInput?.addEventListener("input", markDirty);
      dom.devVoiceReferenceReasons?.addEventListener("change", handleReasonChange);
    }

    return Object.freeze({
      bind,
      setTrainingKind,
      loadTrainingData,
      loadReferences,
      renderGallery,
      openEditor,
      closeEditor,
      saveReview,
      resetReview,
      selectCandidate,
      getState: () => ({
        bound,
        trainingKind,
        references: references.map(cloneReference),
        catalog: catalog ? { ...catalog } : null,
        loaded,
        selectedId,
        draft: draft ? { ...draft, reasonCodes: [...draft.reasonCodes] } : null,
        dirty,
        candidateAudioUrl,
        originalAudioUrl,
        loadGeneration,
        mutationGeneration,
        mutationBusy,
      }),
    });
  }

  return Object.freeze({ createDeveloperVoiceReferenceController });
}));
