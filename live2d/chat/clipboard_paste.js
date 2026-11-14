(function attach(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusChatClipboard = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createClipboardApi() {
  "use strict";

  function asArray(value) {
    if (!value) return [];
    try {
      return Array.from(value);
    } catch (_) {
      return [];
    }
  }

  function isImageFile(file, typeHint = "") {
    const type = String(file?.type || typeHint || "").toLowerCase();
    return type.startsWith("image/");
  }

  function fileSignature(file, typeHint = "") {
    return [
      String(file?.name || ""),
      String(file?.size ?? ""),
      String(file?.lastModified ?? ""),
      String(file?.type || typeHint || "").toLowerCase(),
    ].join("\u0000");
  }

  function collectImageFiles(clipboardData) {
    const files = [];
    const seenObjects = new Set();
    const seenSignatures = new Set();
    const add = (file, typeHint = "") => {
      if (!file || typeof file !== "object" || !isImageFile(file, typeHint)) return;
      const signature = fileSignature(file, typeHint);
      if (seenObjects.has(file) || seenSignatures.has(signature)) return;
      seenObjects.add(file);
      seenSignatures.add(signature);
      files.push(file);
    };

    for (const file of asArray(clipboardData?.files)) add(file);
    for (const item of asArray(clipboardData?.items)) {
      const kind = String(item?.kind || "").toLowerCase();
      const type = String(item?.type || "").toLowerCase();
      if (kind && kind !== "file") continue;
      if (type && !type.startsWith("image/")) continue;
      let file = null;
      try {
        file = typeof item?.getAsFile === "function" ? item.getAsFile() : null;
      } catch (_) {
        file = null;
      }
      add(file, type);
    }
    return files;
  }

  function createPasteHandler(addPendingAttachments) {
    return event => {
      if (typeof addPendingAttachments !== "function") return;
      const files = collectImageFiles(event?.clipboardData);
      if (files.length) addPendingAttachments(files);
    };
  }

  function bind(input, addPendingAttachments) {
    if (!input?.addEventListener || typeof addPendingAttachments !== "function") return () => {};
    const handler = createPasteHandler(addPendingAttachments);
    input.addEventListener("paste", handler);
    return () => input.removeEventListener?.("paste", handler);
  }

  return {asArray, collectImageFiles, createPasteHandler, bind};
}));
