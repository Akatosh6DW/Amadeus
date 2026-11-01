"""Storage ownership declared by the model, never inferred from keywords.

Old records without a purpose remain compatible. Non-personal declarations
can live in the conversation ledger, but are not personal memory projections.
The configuration tools and adaptation ledger own their respective writes.
"""
from collections.abc import Mapping
from typing import Any


ENGINEERING_PREFERENCE_PURPOSE = "engineering_preference"
EXPLICIT_USER_AUTHORIZATION = "explicit_user_request"


MEMORY_PURPOSE_PROMPT = (
    "个人长期记忆只记录人的稳定事实、生活喜好、经历和真实关系。工程偏好（怎样执行任务、用工具、汇报）"
    "用 PROPOSE_USER_INTERACTION_PREFERENCE/PROPOSE_USER_EXPERIENCE；用户明确要求跨会话长期保存并以后遵守时，"
    "本轮紧接 PROMOTE_USER_ADAPTATION（targetId=真实候选ID，authorization=\"explicit_user_request\"，sourceSpan=授权原话）生效，已授权无需再次确认；"
    "只是推断或观察时保留候选，不晋升，"
    "保存在独立偏好库，不再复制进个人记忆。QQ 开关、语音频率、账号、对象回复要求等运行配置由配置工具保存；"
    "改配置成功不等于新增个人事实，人物目录用对应 QQ 工具管理。临时称谓和关系扮演也不自动成为真实关系记忆。"
    "同一句话混合多种信息时逐项处理，个人事实仍可记住；不要因为同时改设置而丢弃整轮记忆。"
    "belief.value 或旧 memoryUpdates 中用 memoryPurpose 标注 personal/engineering_preference/"
    "runtime_configuration/conversation_role；非 personal 不投影到个人长期记忆，仍正常完成当前任务。"
)


def memory_purpose(value: Mapping[str, Any]) -> str:
    return str(value.get('memoryPurpose') or 'personal').strip().lower()


def is_personal_memory(value: Mapping[str, Any]) -> bool:
    return memory_purpose(value) == 'personal'


def is_engineering_preference(value: Mapping[str, Any]) -> bool:
    """Return the model-declared engineering preference storage owner."""

    return memory_purpose(value) == ENGINEERING_PREFERENCE_PURPOSE


def is_user_scoped(value: Mapping[str, Any]) -> bool:
    """Return whether the model explicitly requested the user scope."""

    return str(value.get("scope") or "").strip().lower() == "user"
