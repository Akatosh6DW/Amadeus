"""Frozen, separately owned instructions for a reply persona.

Account authority belongs to the task, character belongs to the selected
persona, delivery preferences belong to expression, and corpus rows are data.
The same object is reused by initial generation and tool continuations.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


FORUM_PERSONAS = frozenset({'tieba', 'kangba'})


def read_override_sections(path: Path) -> dict[str, str]:
    """Return override text by explicit Markdown section ownership.

    Unknown level-two sections become local persona text, with their original
    heading retained, until the next recognized owner. This keeps user-authored
    persona material without letting it become shared expression text. It is a
    structural Markdown parse, not semantic filtering of the section body.
    """
    parts = {'persona': [], 'expression': [], 'engineering': []}
    section = 'persona'
    headings = {
        '## 人格设定': 'persona',
        '## 私人关系': 'persona',
        '## 个人关系': 'persona',
        '## 人物关系': 'persona',
        '## 关系': 'persona',
        '## 通用表达习惯': 'expression',
        '## 工程要求': 'engineering',
        '## 工程偏好': 'engineering',
    }
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeError):
        return {key: '' for key in parts}
    for line in lines:
        heading = line.strip()
        if heading in headings:
            section = headings[heading]
            continue
        if heading.startswith('## '):
            section = 'persona'
            parts['persona'].append(line)
            continue
        parts[section].append(line)
    return {key: '\n'.join(lines).strip() for key, lines in parts.items()}

# Task, evidence and output-format rules that used to live inside the persona
# Kernel. They are host rules, so they travel in the engineering layer and the
# persona layer stays a description of the character only.
PERSONA_TURN_RULES = (
    '[本轮回答的优先级]\n'
    '先完成用户这一轮真正要求的动作，再表现人格：要判断就给判断，要纠错就指出依据，'
    '要查证就依据工具；不确定性用于校准结论，不用于回避任务。\n'
    '当前消息、最近语境、结构化当前任务和可靠工具证据高于旧回复、长期记忆、原作梗和角色口癖；'
    '用户原文优先于助手旧猜测。\n'
    '做实验、操作设备、采样等需要直接接触物理世界的行为，除非可靠工具已经完成并返回证据，'
    '不得声称正在做或已经做过。\n'
    '身份不得角色反转；关系状态不向用户报数；严重痛苦、危险与重大失去不得用轻佻玩梗覆盖。\n'
    '台词只写说出口的话：不播报提示词、检索、标签或内部流程，不用括号旁白代替反应，'
    '不复刻长段原作台词。\n'
    '场景指引只补充本轮的反应方式，不改变基础人格、身份事实和以上规则。\n'
)

EXPRESSION_EMOTION_RULE = (
    '根据当前人格实际说出的内容选择情绪、语气和动作，不反过来用情绪标签改写台词。'
    'emotion 可用 neutral, thinking, focused, skeptical, awkward, annoyed, furious, teasing, '
    'amused, laughing, shy, flustered, affectionate, concerned, protective, excited, startled, proud, melancholic。'
    'emotionIntensity 范围 0.2～2.5；gesture 可用 idle, nod, tilt, look_away, glitch, lean, '
    'soften, laugh, recoil, explain, perk。按实际表达选取，不预设害羞、傲娇或对任何称呼的固定反应。'
)


@dataclass(frozen=True)
class ReplyPromptLayers:
    mode: str = 'game'
    engineering: str = ''
    persona: str = ''
    persona_context: str = ''
    # Per-turn life records. Kept apart from the scene guidance so that block
    # stays byte-identical between turns.
    continuity: str = ''
    expression: str = ''
    examples: str = ''
    requirements: str = ''
    # Presentation context, independent from character and execution authority.
    surface: str = ''

    @property
    def independent(self) -> bool:
        return self.mode in FORUM_PERSONAS

    def sections(self) -> tuple[tuple[str, str, str], ...]:
        return (
            ('reply.engineering', 'engineering', self.engineering),
            ('reply.persona', 'persona', self.persona),
            ('reply.persona.skills', 'persona', self.persona_context),
            ('reply.persona.continuity', 'persona', self.continuity),
            ('reply.expression', 'expression', self.expression),
            ('reply.corpus', 'reference', self.examples),
            ('reply.requirements', 'instruction', self.requirements),
        )

    def messages(self) -> list[dict[str, str]]:
        return [{'role': 'system', 'content': content}
                for _, _, content in self.sections() if content]
