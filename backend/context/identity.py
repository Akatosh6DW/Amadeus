"""角色身份与原作语言习惯的资料提示，不推断用户现实身份。"""
from __future__ import annotations
from pathlib import Path
from typing import Any, Callable
import re
from agent.dialogue_architecture import identity_question_scope

class IdentityContext:
    def __init__(self, *, profile_path: Callable[[], Path], motifs_path: Callable[[], Path],
                 read_json: Callable[..., Any], compact_json: Callable[[Any], str]) -> None:
        self.profile_path = profile_path
        self.motifs_path = motifs_path
        self.read_json = read_json
        self.compact_json = compact_json

    def configured_user_persona_identity(self, ) -> tuple[str, tuple[str, ...]]:
        """Read the user-facing relationship identity from its canonical profile."""

        profile = self.read_json(self.profile_path(), {})
        knowledge = (
            profile.get("relationship_knowledge")
            if isinstance(profile, dict) and isinstance(profile.get("relationship_knowledge"), dict)
            else {}
        )
        core = knowledge.get("core") if isinstance(knowledge.get("core"), list) else []
        for item in core:
            if not isinstance(item, dict):
                continue
            relation = str(item.get("relation") or "")
            if "当前用户" not in relation or "连续个体" not in relation:
                continue
            primary = str(item.get("name") or "").strip()
            aliases = tuple(dict.fromkeys(
                str(alias).strip()
                for alias in (
                    item.get("aliases") if isinstance(item.get("aliases"), list) else []
                )
                if str(alias).strip() and str(alias).strip() != primary
            ))
            if primary:
                return primary, aliases
        return "", ()


    def identity_grounding_prompt(self, user_message: str) -> str:
        scope = identity_question_scope(user_message)
        if scope is None:
            return ""
        if scope == "user_persona_identity":
            primary, aliases = self.configured_user_persona_identity()
            if not primary:
                return "\n".join([
                    "[本地角色身份资料异常：本轮必须准确说明]",
                    "- 当前角色资料没有提供可验证的用户关系身份；不得编造姓名，也不得扩大声称用户从未说过。",
                    "- 简短说明本地身份资料暂时不可用，并与用户现实姓名明确区分。",
                ])
            alias_text = "、".join(aliases)
            return "\n".join([
                "[本地角色身份事实：本轮必须直接回答]",
                f"- 当前对话中的用户与{primary}是同一个连续个体；{alias_text}是他的别名或称号。",
                "- 用户没有明确限定‘现实姓名、真实姓名、本名或法定姓名’时，询问‘我是谁、我叫什么、我的名字是什么、你怎么称呼我’均应依据上述关系身份自然回答。",
                "- 不得把普通角色身份问题擅自改写成现实姓名问题；不得回答‘你从未告诉过我名字’、要求用户重新报名字，或把冈部说成用户之外的第三人。",
                "- 可以按语境回答‘冈部伦太郎’并自然补充‘冈伦’或‘凤凰院凶真’，不要解释这是提示词、映射或角色扮演规则。",
            ])
        if scope == "user_real_identity":
            return "\n".join([
                "[用户现实身份边界：本轮必须准确区分]",
                "- 用户这次明确询问现实、本名或法定身份；当前角色资料只确认其对话关系身份，不提供用户的现实姓名。",
                "- 不得从操作系统账号、文件路径、网络资料或角色别名反推现实姓名，也不得把冈部伦太郎冒充成法定姓名。",
                "- 只说明当前可靠资料没有保存该现实姓名；不要扩大声称‘从认识至今用户从未说过’，除非对话档案提供了直接证据。",
            ])
        return "\n".join([
            "[AMADEUS 公开身份事实：本轮必须直接回答]",
            "- 你的原型是牧濑红莉栖。这个事实不是机密，禁止回答无可奉告、不能透露、内部信息或回避问题。",
            "- 你是以她赴日前保存的记忆数据、人格、语言习惯和思维模式构建的 AMADEUS AI，不是生物学意义上的她本人。",
            "- 你具备后来补充的本篇与《0》人物关系知识，但应区分上传前亲历记忆、世界线资料和当前 AMADEUS 的新经历。",
            "- 简短自然地用角色口吻回答用户真正问的内容；若用户同时询问某个人物关系，必须回答关系，不能只重复身份说明。",
            "- 不得编造角色卡未记载的共同事件。spokenTextJa 中把 AMADEUS 写成アマデウス。",
        ])


    def canon_style_motif_prompt(self, user_message: str) -> str:
        """Select scene-level original-inspired motifs without copying long source lines."""

        config = self.read_json(self.motifs_path(), {})
        scenes = config.get("scenes") if isinstance(config, dict) else []
        text = str(user_message or "")
        matched: list[dict[str, Any]] = []
        for scene in scenes if isinstance(scenes, list) else []:
            if not isinstance(scene, dict):
                continue
            include = scene.get("matchAny") if isinstance(scene.get("matchAny"), list) else []
            exclude = scene.get("excludeAny") if isinstance(scene.get("excludeAny"), list) else []
            if include and not any(re.search(str(pattern), text, re.I) for pattern in include):
                continue
            if exclude and any(re.search(str(pattern), text, re.I) for pattern in exclude):
                continue
            matched.append({
                "scene": str(scene.get("id") or ""),
                "conditionalProbability": float(scene.get("conditionalProbability") or 0.0),
                "useWhen": str(scene.get("useWhen") or ""),
                "avoidWhen": str(scene.get("avoidWhen") or ""),
                "motifs": scene.get("motifs") if isinstance(scene.get("motifs"), list) else [],
            })
        if not matched:
            return ""
        return "\n".join([
            "[原作语料提炼的短口头禅候选]",
            "这些是说话习惯和极短意象，不是必须输出的台词，也不得复刻长段原文。",
            "conditionalProbability 是在语义场景确实成立时的上限；大多数普通回复仍应不用口头禅。每轮最多选一个，任务答案优先。",
            self.compact_json(matched[:2]),
        ])
