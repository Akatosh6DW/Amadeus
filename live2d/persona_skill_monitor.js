"use strict";

window.AmadeusPersonaSkills = (() => {
  function mount(root, request) {
    const scope = root.querySelector("[data-persona-scope]");
    const history = root.querySelector("[data-persona-history]");
    const grid = root.querySelector("[data-persona-grid]");
    const status = root.querySelector("[data-persona-status]");
    const question = root.querySelector("[data-persona-question]");
    const summary = root.querySelector("[data-persona-summary]");
    const kernel = root.querySelector("[data-persona-kernel]");
    let data = { skills: [], records: [] };
    let selected = "latest";
    let pendingScope = null;
    let version = 0;
    let error = "";

    function option(value, label) {
      const item = document.createElement("option");
      item.value = value;
      item.textContent = label;
      return item;
    }

    function render(next) {
      if (next) { data = next; error = ""; }
      const records = data.records || [];
      if (selected !== "latest" && !records.some(item => item.requestId === selected)) selected = "latest";
      const record = selected === "latest" ? records[0] : records.find(item => item.requestId === selected);
      history.replaceChildren(option("latest", "自动跟随最新回复"), ...records.map(item =>
        option(item.requestId, `${new Date(item.at).toLocaleTimeString("zh-CN", { hour12: false })} · ${item.userMessage || "对话请求"}`)));
      history.value = selected;
      const active = new Set(record?.activeSkillIds || []);
      const compared = new Set(record?.mode === "shadow" ? record.retrievedSkillIds : []);
      grid.replaceChildren(...(data.skills || []).map(skill => {
        const used = active.has(skill.id);
        const candidate = !used && compared.has(skill.id);
        const card = document.createElement("div");
        card.className = "persona-skill-card";
        card.dataset.skillId = skill.id;
        card.dataset.state = used ? "active" : candidate ? "candidate" : "idle";
        card.title = skill.description || skill.name;
        const name = document.createElement("strong");
        name.textContent = skill.name;
        const badge = document.createElement("span");
        badge.className = "persona-skill-state";
        badge.textContent = used ? "● 本轮已使用" : candidate ? "◐ 旁路命中" : record ? "○ 本轮未使用" : "○ 等待记录";
        const id = document.createElement("small");
        id.textContent = skill.id;
        card.append(name, badge, id);
        return card;
      }));
      summary.textContent = `${active.size} / ${(data.skills || []).length} 个场景 skill 已使用`;
      question.textContent = record ? `本轮用户：${record.userMessage || "（无文字）"}` : "发送一条消息后，这里会显示本轮的人格选择。";
      kernel.dataset.active = record?.kernelActive ? "true" : "false";
      kernel.textContent = record?.kernelActive ? "● 基础人格已注入" : record && record.mode !== "on" ? "旧人格模式" : "基础人格：等待记录";
      if (record?.mode === "on" && !record.kernelActive) kernel.textContent = "基础人格未注入";
      if (record?.mode === "none") kernel.textContent = "尚无人格输入记录";
      let note = "等待本次后端启动后的调用记录";
      if (record) {
        const time = new Date(record.at).toLocaleTimeString("zh-CN", { hour12: false });
        note = `${time} · `;
        if (record.mode === "none") note += "本轮尚无人格输入记录：尚未调用模型，或为本地回复。";
        else if (record.mode === "shadow") note += "旁路比较：橙色仅为检索候选，实际回复使用旧人格。";
        else if (record.mode === "off") note += "当前使用旧人格，场景 skill 检索未启用。";
        else if (active.size) note += "绿色 skill 已包含在本轮模型输入中。";
        else if (record.fallbackReason && !/below|margin|no_candidate|empty_query/.test(record.fallbackReason))
          note += `人格检索未就绪：${record.fallbackReason}`;
        else note += record.promptSent ? "本轮未选中场景 skill。" : "本轮模型输入没有注入场景 skill。";
      }
      status.textContent = error || note;
      root.dataset.connection = error ? "error" : "ready";
    }

    async function refresh() {
      const requestedScope = scope.value;
      if (pendingScope === requestedScope) return;
      pendingScope = requestedScope;
      const requestVersion = ++version;
      try {
        const response = await request(`/api/developer/persona-skills?scope=${encodeURIComponent(requestedScope)}`, { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        if (requestVersion === version && scope.value === requestedScope) render(payload);
      } catch (failure) {
        if (requestVersion !== version) return;
        error = `读取失败：${failure.message}`;
        data = { ...data, records: [] };
        render();
      } finally {
        if (requestVersion === version) pendingScope = null;
      }
    }

    scope.addEventListener("change", () => {
      selected = "latest";
      data = { ...data, records: [] };
      render();
      refresh();
    });
    history.addEventListener("change", () => { selected = history.value; render(); });
    root.querySelector("[data-persona-refresh]").addEventListener("click", refresh);
    render();
    return { refresh, render };
  }
  return { mount };
})();
