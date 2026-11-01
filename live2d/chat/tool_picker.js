(function attach(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusToolPicker = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const TOOLS = [
    {id: "qq", label: "QQ 代管", icon: "Q", permission: "permissionQqControl"},
    {id: "web", label: "联网搜索", icon: "◎", permission: "permissionWebSearch"},
    {id: "screen", label: "读取屏幕", icon: "▣", permission: "permissionReadScreen"},
    {id: "camera", label: "读取摄像头", icon: "◉", permission: "permissionReadCamera"},
    {id: "files", label: "工作区文件", icon: "▤", permission: "permissionFileRead"},
    {id: "schedule", label: "定时任务", icon: "◷"},
    {id: "bookmarks", label: "收藏信息", icon: "☆"},
  ];
  function normalizeSelection(value) {
    return TOOLS.filter(tool => Array.isArray(value) && value.includes(tool.id)).map(tool => tool.id);
  }
  function createController({button, menu, chips, input, fileInput, getSettings = () => ({}), onChanged, openSettings}) {
    const doc = button.ownerDocument;
    const win = doc.defaultView;
    let selected = [];
    // Keep the popup outside the chat panel's clipped/dragged coordinate system.
    doc.body.append(menu);
    function close({focus = false} = {}) {
      menu.hidden = true;
      button.setAttribute("aria-expanded", "false");
      if (focus) button.focus();
    }
    function node(tag, text, className) {
      const item = doc.createElement(tag);
      if (text) item.textContent = text;
      if (className) item.className = className;
      return item;
    }
    function renderChips() {
      chips.replaceChildren();
      chips.hidden = !selected.length;
      for (const tool of TOOLS.filter(item => selected.includes(item.id))) {
        const chip = node("span", "", "composer-tool-chip");
        chip.append(node("span", tool.label));
        if (tool.id === "qq") {
          chip.title = "选择 QQ 工具仅用于对话；开始或暂停代管请打开「管理」";
          const settings = node("button", "管理"); settings.type = "button";
          settings.setAttribute("aria-label", "管理 QQ 连接、回复对象和回复设置");
          settings.addEventListener("click", () => openSettings?.("qq"));
          chip.append(settings);
        }
        const remove = node("button", "×"); remove.type = "button";
        const removeLabel = tool.id === "qq" ? "从对话移除 QQ 工具（不会暂停代管）" : `取消选择${tool.label}`;
        remove.setAttribute("aria-label", removeLabel);
        remove.title = removeLabel;
        remove.addEventListener("click", () => toggle(tool.id));
        chip.append(remove); chips.append(chip);
      }
    }
    function toggle(id) {
      selected = normalizeSelection(selected.includes(id) ? selected.filter(value => value !== id) : [...selected, id]);
      renderChips(); close(); onChanged?.([...selected]); input.focus();
    }
    function attachFile(images) {
      close(); fileInput.accept = images ? "image/*" : ""; fileInput.click();
    }
    function open() {
      menu.replaceChildren();
      for (const [label, icon, images] of [["读取图片", "▧", true], ["添加文件", "＋", false]]) {
        const action = node("button", "", "tool-picker-item"); action.type = "button"; action.setAttribute("role", "menuitem");
        action.append(node("span", icon, "tool-picker-icon"), node("span", label));
        action.addEventListener("click", () => attachFile(images)); menu.append(action);
      }
      const divider = node("div", "工具与插件", "tool-picker-heading"); divider.setAttribute("role", "presentation"); menu.append(divider);
      const permissions = getSettings();
      for (const tool of TOOLS) {
        const action = node("button", "", "tool-picker-item"); action.type = "button";
        action.setAttribute("role", "menuitemcheckbox"); action.setAttribute("aria-checked", String(selected.includes(tool.id)));
        action.dataset.tool = tool.id;
        action.append(node("span", tool.icon, "tool-picker-icon"), node("span", tool.label));
        if (tool.permission && !permissions[tool.permission]) action.append(node("small", "未授权"));
        action.append(node("span", selected.includes(tool.id) ? "✓" : "", "tool-picker-check"));
        action.addEventListener("click", () => toggle(tool.id)); menu.append(action);
      }
      menu.hidden = false;
      menu.style.maxHeight = `${Math.max(160, win.innerHeight - 24)}px`;
      const rect = button.getBoundingClientRect();
      menu.style.left = `${Math.max(12, Math.min(rect.left, win.innerWidth - menu.offsetWidth - 12))}px`;
      menu.style.top = `${Math.max(12, rect.top - menu.offsetHeight - 8)}px`;
      button.setAttribute("aria-expanded", "true");
      menu.querySelector("button")?.focus();
    }
    button.addEventListener("click", () => menu.hidden ? open() : close());
    doc.addEventListener("pointerdown", event => { if (!menu.contains(event.target) && !button.contains(event.target)) close(); });
    menu.addEventListener("keydown", event => {
      if (event.key === "Escape") { event.preventDefault(); close({focus: true}); }
      if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        const actions = [...menu.querySelectorAll("button")];
        let index = actions.indexOf(doc.activeElement);
        index = event.key === "Home" ? 0 : event.key === "End" ? actions.length - 1 : (index + (event.key === "ArrowDown" ? 1 : -1) + actions.length) % actions.length;
        actions[index]?.focus();
      }
    });
    menu.addEventListener("focusout", event => { if (!menu.contains(event.relatedTarget) && event.relatedTarget !== button) close(); });
    win.addEventListener("resize", () => close());
    renderChips();
    return {selected: () => [...selected], has: id => selected.includes(id), close,
      clear() { selected = []; renderChips(); close(); onChanged?.([]); }};
  }
  return {TOOLS, normalizeSelection, createController};
}));
