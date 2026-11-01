"""Owner-requested desktop speech settings; QQ policy has a separate owner."""
from __future__ import annotations

from typing import Any, Callable


VOICE_FIELDS = ("speechLanguage", "voiceLocale", "gptSovitsTargetLanguage", "voiceEnabled")


def configure_desktop_voice(
    settings: dict[str, Any], args: dict[str, Any], *,
    save: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    # Never accept QQ destinations or arbitrary application configuration here.
    if args.get("scope") != "desktop" or args.get("commitIntent") is not True:
        return {"ok": False, "error": "desktop_voice_owner_intent_required", "attempted": False}
    if set(args) - {"scope", "commitIntent", "speechLanguage", "voiceEnabled"}:
        return {"ok": False, "error": "unsupported_voice_setting", "attempted": False}
    update = {key: args[key] for key in ("speechLanguage", "voiceEnabled") if key in args}
    if not update or ("speechLanguage" in update and update["speechLanguage"] not in ("zh-CN", "ja-JP")):
        return {"ok": False, "error": "invalid_speech_language", "attempted": False}
    if "voiceEnabled" in update and type(update["voiceEnabled"]) is not bool:
        return {"ok": False, "error": "invalid_voice_enabled", "attempted": False}
    saved = save(update)
    public = {key: saved[key] for key in VOICE_FIELDS if key in saved}
    # Subsequent rendering/synthesis in this turn must use the new language too.
    settings.update(public)
    return {"ok": True, "scope": "desktop", "settings": public,
            "message": "已保存主界面语音设置；QQ 单独指定的语言和语音频率不变，选择跟随主界面的 QQ 会话会跟随语言变化"}
