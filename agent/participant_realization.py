"""Realize explicit entity tokens in model-authored speech, using Template.

The model selects referents; this module never infers them from ordinary I/you
or user text. Entity selection and first/second-person realization are separate.
"""
from __future__ import annotations

from copy import deepcopy
from string import Template
from typing import Any, Mapping


PARTICIPANT_REALIZATION_RULE = """[人物身份与台词生成]
当前消息的发送者为用户，回答者为Amadeus。人物身份始终固定；引语需按该层说话者和听话者解释，不能按最外层消息角色套用。存在引语时，cognitive按interactionAct、referenceFrames、understoodGoal、patches顺序输出：先绑定每层是谁对谁说话，再用明确名称概括所需关系。例子和历史错误不是当前事实。
speechUnits采用身份稳定的台词：中文zh和日文ja中，指向当前用户的人称写成⟦USER⟧，指向Amadeus的人称写成⟦ASSISTANT⟧。这两个标识是人物身份，不是原句的我/你；依据已经确定的动作参与者填写，不按原文代词复制。后端会把⟦ASSISTANT⟧变成我/私、⟦USER⟧变成你/あなた，你不再进行这一步视角换算。第三方直接使用姓名或明确称谓。词语其余部分自由组织；原文引用、代码、普通名词中的文字保持原样，不在引用内部插入标识。只有表示当前两人的人称才使用标识，不能把两个身份解释成同一个人。
例如确定关系“用户向Amadeus还伞”，zh写“⟦USER⟧把伞还给⟦ASSISTANT⟧。”，ja写“⟦USER⟧が⟦ASSISTANT⟧に傘を返す。”；反向关系则交换人物标识。不要将本例当本轮事实。借入者归还，交接完成后的持有人为最后接收者；开头、解释和双语必须依据同一关系。不展开额外分析，不增加模型调用，普通聊天直接自然接话。
原样展示标记代码时，将起始⟦写成⟦⟦转义，例如⟦⟦USER⟧会按字面显示⟦USER⟧。普通我/你、引语、代码和第三方姓名不自动转换。

[引语归属先于台词]
当本轮需要解析引用/转述里的动作参与者，先在cognitive.referenceFrames中记录相关说话事件，逐层写{speaker:说话者固定名称, addressee:听话者固定名称, mode:direct或indirect, text:对应原文片段}。USER和Amadeus是固定名称，第三方保留原名；未知听者写null。不把外层消息发送者直接当成内层听者。再在understoodGoal中记录最终动作方向，并据此填写台词中的身份标识。普通无引语聊天省略referenceFrames。
直接引语才切换到被引用者的说话视角；间接转述沿用转述者视角，不能把转述片段声称为被转述者的逐字原话。例如甲说“乙告诉我，他会帮助我”，两个“我”属于甲，不能解释成“乙说的我”。解释代词时引用具体片段并标清它属于哪层，勿将间接转述凭空改为直接引语。
例如用户消息为：你告诉我“王老师说：我会给你回信”。里面：Amadeus向USER转述；王老师向Amadeus说会回信。referenceFrames按这两层记录，回信对象为Amadeus；接收到转述的USER不会变成王老师的收信人。相反，用户消息为：我告诉你“王老师说：我会给你回信”，里面王老师向USER说会回信，收信人为USER。说话事件的听话者不会因为下一层转述而改变。
这些只是本轮文本的可修正理解，不是记忆或工具授权，也不需要额外模型请求。
"""


class _ParticipantTemplate(Template):
    delimiter = "⟦"
    flags = 0
    # Named references and doubled-delimiter escaping, using the standard
    # Template engine. Other bracket uses, such as mathematics, are literal.
    pattern = r"⟦(?:(?P<escaped>⟦)|(?P<named>[A-Z_]+)⟧|(?P<braced>(?!))|(?P<invalid>[A-Z_]+))"


_REFERENTS = {
    "zh": {"USER": "你", "ASSISTANT": "我"},
    "ja": {"USER": "あなた", "ASSISTANT": "私"},
}
_TEXT_FIELDS = {"zh": "zh", "textZh": "zh", "reply": "zh",
                "ja": "ja", "spokenTextJa": "ja"}


class ParticipantTemplateError(ValueError):
    def to_dict(self) -> dict[str, str]:
        return {"code": "participant_reference_invalid",
                "repair": "Use only ⟦USER⟧/⟦ASSISTANT⟧ for the current participants; "
                          "third parties use their names. Escape a literal ⟦ as ⟦⟦. "
                          "Keep the intended actions and return the complete speechUnits response."}


def strip_participant_realization_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    """Remove internal flags at fresh model input and final delivery boundaries."""
    result = deepcopy(dict(value))
    result.pop("_participantSpeechRealized", None)
    for key in ("speechUnits", "utterances", "_modelSpeechUnits"):
        for unit in result.get(key, []) if isinstance(result.get(key), list) else []:
            if isinstance(unit, dict):
                unit.pop("_participantSpeechRealized", None)
    if isinstance(result.get("payload"), dict):
        result["payload"].pop("_participantSpeechRealized", None)
    return result


def realize_participant_text(value: str, language: str) -> str:
    try:
        return _ParticipantTemplate(value).substitute(_REFERENTS[language])
    except (KeyError, ValueError) as error:
        raise ParticipantTemplateError("participant_reference_invalid") from error


def realize_participant_speech(value: Mapping[str, Any]) -> dict[str, Any]:
    """Render only speech fields once; preserve cognition and tool arguments.

    The flag prevents escaped literal tokens from being interpreted on a second
    normalization pass. It is internal metadata, not evidence of correctness.
    """
    result = deepcopy(dict(value))
    if result.get("_participantSpeechRealized") is True:
        return result

    def project(record: dict[str, Any]) -> set[str]:
        rendered: set[str] = set()
        if record.get("_participantSpeechRealized") is True:
            return rendered
        for key, language in _TEXT_FIELDS.items():
            text = record.get(key)
            if isinstance(text, str) and "⟦" in text:
                record[key] = realize_participant_text(text, language)
                rendered.add(language)
        if rendered:
            record["_participantSpeechRealized"] = True
        return rendered

    rendered = project(result)
    for units_field in ("speechUnits", "utterances", "_modelSpeechUnits"):
        units = result.get(units_field)
        if not isinstance(units, list):
            continue
        unit_languages: set[str] = set()
        for unit in units:
            if isinstance(unit, dict):
                unit_languages.update(project(unit))
        # Units own the displayed and spoken text, including duplicate tracks.
        for language, output, aliases in (
            ("zh", "reply", ("zh", "textZh", "reply")),
            ("ja", "spokenTextJa", ("ja", "spokenTextJa")),
        ):
            if language in unit_languages:
                result[output] = "\n\n".join(
                    next((unit[key].strip() for key in aliases
                          if isinstance(unit.get(key), str) and unit[key].strip()), "")
                    for unit in units if isinstance(unit, dict)
                ).strip()
        rendered.update(unit_languages)
    if rendered:
        result["_participantSpeechRealized"] = True
    return result
