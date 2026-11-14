(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.AmadeusStickerPreferences = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const clone = value => value == null ? null : {...value};
  function create({element, onChange = () => {}, defaultLabel = '智能选择（不限定系列和比例）'}) {
    const doc = element.ownerDocument;
    let value = null, series = [], disabled = false, signature = '';
    if (element.classList?.add) element.classList.add('sticker-settings');
    else element.className = `${element.className || ''} sticker-settings`.trim();
    element.dataset ||= {};
    const modeLabel = doc.createElement('label'); modeLabel.className = 'sticker-mode';
    const mode = doc.createElement('input'); mode.type = 'checkbox';
    mode.setAttribute('aria-label', '自定义表情系列与比例');
    const modeCopy = doc.createElement('span'); modeCopy.className = 'sticker-mode-copy'; modeCopy.textContent = '自定义表情系列与比例';
    modeLabel.append(mode, modeCopy);
    const defaults = doc.createElement('p'); defaults.className = 'qq-note sticker-default-note'; defaults.textContent = defaultLabel;
    const rows = doc.createElement('div'); rows.className = 'sticker-series-list';
    const summary = doc.createElement('p'); summary.className = 'qq-note sticker-summary'; summary.setAttribute('aria-live', 'polite');
    element.append(modeLabel, defaults, rows, summary);
    let controls = [];
    function total() { return Object.values(value || {}).reduce((sum, n) => sum + n, 0); }
    function changed() { sync(); onChange(clone(value)); }
    mode.addEventListener('change', () => { value = mode.checked ? {} : null; changed(); });
    function sync() {
      mode.checked = value !== null; mode.disabled = disabled;
      element.dataset.customized = value !== null ? 'true' : 'false';
      element.dataset.disabled = disabled ? 'true' : 'false';
      defaults.hidden = value !== null;
      rows.hidden = summary.hidden = value === null;
      for (const control of controls) {
        const checked = value != null && Object.hasOwn(value, control.name);
        const percent = checked ? value[control.name] : 0;
        control.check.checked = checked; control.check.disabled = disabled;
        control.range.disabled = disabled || !checked;
        control.range.value = percent; control.output.textContent = `${percent}%`;
      }
      summary.textContent = `各系列占全部回复的比例：合计 ${total()}%，约 ${100-total()}% 不主动配图。无贴切表情时可跳过；明确索要图片时仍会响应。`;
    }
    function rebuild() {
      const names = [...new Set([...series, ...Object.keys(value || {})])].sort();
      const next = JSON.stringify(names);
      if (next === signature) return;
      signature = next; controls = []; rows.replaceChildren();
      if (!names.length) {
        const note = doc.createElement('p'); note.className = 'qq-note'; note.textContent = '当前图库还没有表情系列。导入带系列名称或系列标签的表情后会列在这里。'; rows.append(note);
      }
      names.forEach(name => {
        const row = doc.createElement('div'); row.className = 'sticker-series-row';
        const label = doc.createElement('label'); label.className = 'sticker-series-label';
        const check = doc.createElement('input'); check.type = 'checkbox'; check.setAttribute('aria-label', `使用${name}系列`);
        const title = doc.createElement('span'); title.className = 'sticker-series-name'; title.textContent = name + (series.includes(name) ? '' : '（当前图库未找到）'); label.append(check, title);
        const range = doc.createElement('input'); range.className = 'sticker-series-slider'; range.type = 'range'; range.min = 0; range.max = 100; range.step = 1;
        range.setAttribute('aria-label', `${name}使用比例`);
        const output = doc.createElement('output'); output.className = 'sticker-series-percent';
        check.addEventListener('change', () => {
          if (check.checked) value[name] = Math.min(20, Math.max(0, 100-total()));
          else delete value[name];
          changed();
        });
        range.addEventListener('input', () => {
          value[name] = Math.max(0, Math.min(Number(range.value), 100-total()+(value[name] || 0)));
          changed();
        });
        row.append(label, range, output); rows.append(row); controls.push({name, check, range, output});
      });
    }
    return {
      render(state = {}) {
        if (Object.hasOwn(state, 'value')) value = clone(state.value);
        if (Object.hasOwn(state, 'series')) series = state.series || [];
        if (Object.hasOwn(state, 'disabled')) disabled = Boolean(state.disabled);
        rebuild(); sync();
      },
      getValue() { return clone(value); },
    };
  }
  return {create};
}));
