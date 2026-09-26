"""从已解析的直接称呼选择稳定反应；关系状态仍由关系领域持有。

提示、最终核验和原始音频使用同一请求键取样。含业务问题的称呼不抢占任务答案；
本服务不调用模型或保存关系，也不使用主题关键词代替人称/称呼解析。
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
import json
import re
from agent.action_grounding import interpret_user_input
from agent.fingerprints import runtime_fingerprint
from backend.common.values import clamp_float
from .nickname_catalog import (
    NICKNAME_REACTION_VARIANTS, NICKNAME_DERE_REACTION_VARIANTS,
    NICKNAME_ONLY_REACTION_PROBABILITY, NICKNAME_REQUEST_REACTION_PROBABILITY,
)

@dataclass(frozen=True)
class NicknameReactionPorts:
    read_relationship: Callable[[], dict[str, Any]]
    normalize_relationship: Callable[[Any], dict[str, Any]]
    action_config: Callable[[], dict[str, Any]]

class NicknameReactions:
    def __init__(self, ports: NicknameReactionPorts) -> None:
        self.ports = ports

    def nickname_affection_score(self, relationship_state: dict[str, Any] | None = None) -> float:
        """Return one smooth warmth score without inventing a second relationship store."""

        state = self.ports.normalize_relationship(
            relationship_state
            if isinstance(relationship_state, dict)
            else self.ports.read_relationship()
        )
        score = (
            0.30 * float(state.get("familiarity") or 0.0)
            + 0.30 * float(state.get("trust") or 0.0)
            + 0.25 * float(state.get("careLevel") or 0.0)
            + 0.15 * float(state.get("stability") or 0.0)
        )
        stage_floor = {
            "close_companion": 0.0,
            "trusted_intimacy": 0.68,
            "committed_partner": 0.82,
            "life_partner": 0.90,
            "old_married": 0.96,
        }.get(str(state.get("stage") or "close_companion"), 0.0)
        if str(state.get("commitmentStatus") or "") == "confirmed":
            stage_floor = max(stage_floor, 0.82)
        return clamp_float(max(score, stage_floor), 0, 1, 0.5)


    def nickname_original_reaction_probability(self, affection_score: float) -> float:
        """Fade assertive original clips out as the relationship becomes warmer."""

        # At early/default familiarity the original sharp reactions are certain;
        # between established trust and partnership they blend smoothly; after a
        # stable partnership only the softer tsundere complaint remains.
        return clamp_float((0.82 - float(affection_score)) / 0.20, 0, 1, 0.5)


    def stable_nickname_reaction_sample(
        self,
        user_message: str,
        reaction_key: str,
        purpose: str,
    ) -> float:
        """Return a stable per-turn sample so prompt, validation and audio agree."""

        material = "\x1f".join((
            str(reaction_key or "message-stable"),
            str(user_message or ""),
            str(purpose or "selection"),
        ))
        value = int(runtime_fingerprint(material, length=16), 16)
        return value / float(0xFFFFFFFFFFFFFFFF)


    def nickname_reaction_context(
        self,
        user_message: str,
        reaction_key: str = "",
        relationship_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resolve only a parsed direct address; the surrounding topic is not keyword-scored."""

        text = re.sub(r"\s+", " ", str(user_message or "")).strip()
        interpreted = interpret_user_input(text, self.ports.action_config())
        vocative = interpreted.vocatives[-1] if interpreted.vocatives else None
        if vocative is None:
            return {
                "addressed": False,
                "kind": "none",
                "mode": "none",
                "reactionProbability": 0.0,
                "remainder": text,
            }
        kind = str(vocative.vocative_type or "")
        assertive_variants = NICKNAME_REACTION_VARIANTS.get(kind, ())
        soft_variants = NICKNAME_DERE_REACTION_VARIANTS.get(kind, ())
        if not assertive_variants and not soft_variants:
            return {
                "addressed": True,
                "kind": kind or "other",
                "nickname": vocative.exact_text,
                "mode": "none",
                "reactionSelected": False,
                "remainder": interpreted.action_parse_text,
            }
        remainder = str(interpreted.action_parse_text or "").strip(" \t\r\n，,。！？!?：:")
        nickname_only = not remainder
        probability = (
            NICKNAME_ONLY_REACTION_PROBABILITY
            if nickname_only else NICKNAME_REQUEST_REACTION_PROBABILITY
        )
        selected = self.stable_nickname_reaction_sample(
            text,
            reaction_key,
            "gate",
        ) < probability
        affection_score = self.nickname_affection_score(relationship_state)
        original_probability = self.nickname_original_reaction_probability(affection_score)
        assertive_selected = (
            bool(assertive_variants)
            and (
                not soft_variants
                or self.stable_nickname_reaction_sample(text, reaction_key, "relationship-style")
                < original_probability
            )
        )
        variants = assertive_variants if assertive_selected else soft_variants
        reaction_family = "original_assertive" if assertive_selected else "soft_tsundere"
        variant_index = min(
            len(variants) - 1,
            int(self.stable_nickname_reaction_sample(
                text,
                reaction_key,
                "variant",
            ) * len(variants)),
        )
        reaction = dict(variants[variant_index])
        return {
            "addressed": True,
            "kind": kind,
            "nickname": vocative.exact_text,
            "mode": (
                "required_original" if reaction.get("audio") else "required_canon_style"
            ) if selected else "probabilistic_skipped",
            "reactionSelected": selected,
            "reactionProbability": probability,
            "nicknameOnly": nickname_only,
            "remainder": remainder,
            "reaction": reaction,
            "reactionVariantCount": len(variants),
            "reactionFamily": reaction_family,
            "affectionScore": affection_score,
            "originalReactionProbability": original_probability,
        }


    def nickname_reaction_prompt(self, user_message: str, reaction_key: str = "") -> str:
        context = self.nickname_reaction_context(user_message, reaction_key)
        if not context["addressed"] or not context.get("reactionSelected"):
            return ""
        reaction = context["reaction"]
        tone_rule = (
            "- 当前关系仍偏早期：这一句保留原作式的短促、偏傲羞恼，但不升级成真正生气、拒绝执行或长篇训斥。"
            if context.get("reactionFamily") == "original_assertive"
            else "- 当前关系已经足够亲近：仍然嘴硬，但只做撒娇般的小声抱怨；禁止凶狠训斥、真正发火或拒绝正题。"
        )
        delivery_rule = (
            "- 这一个短单元会由后端绑定已经核验的原作音频，禁止改字、扩写、与后面的任务答案合并，也不要在后文重复同一句。"
            if reaction.get("audio")
            else "- 这一个短单元没有可核验的同句原作音频，由当前语音模型合成；禁止改字、扩写、与后面的任务答案合并，也不要在后文重复同一句。"
        )
        return "\n".join([
            "[直接外号的原作反应：本轮概率抽中，必须执行]",
            f"- 用户把“{context['nickname']}”当作呼语直接叫你；这不是在讨论这个词。",
            f"- 第一项 speechUnits 必须逐字使用中文：{json.dumps(reaction['reply'], ensure_ascii=False)}",
            f"- 同一项的日语必须逐字使用：{json.dumps(reaction['spokenTextJa'], ensure_ascii=False)}",
            delivery_rule,
            "- 若用户同时要求分析、修改、打开、查找或做其他事情，从第二个语义单元开始正常完成正题；外号反应不能替代任务或动作回执。",
            "- 同一段后续语义单元保持第一句的情绪、表情方向、voiceStyle 与 voiceTone；内容可以回到正题，但不能突然切成平静或另一种情绪。",
            tone_rule,
        ])


    def christina_nickname_prompt(self, user_message: str, reaction_key: str = "") -> str:
        """Compatibility wrapper retained for persona/UI tests and older callers."""

        context = self.nickname_reaction_context(user_message, reaction_key)
        if context.get("kind") != "christina":
            return ""
        return self.nickname_reaction_prompt(user_message, reaction_key)
