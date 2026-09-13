"""Independent rollout switches for semantic recovery and TTS delivery.

Launchers choose rollout modes explicitly.  A process started without a launcher
stays on the established behavior, so it never pays for shadow-only model work by
accident.  Frozen releases enable the calibrated Persona Skill by default; every
switch still has its own environment override so one risky layer can be rolled
back without disabling the others.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return bool(default)
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


def _env_choice(name: str, default: str, allowed: set[str]) -> str:
    value = str(os.environ.get(name) or default).strip().lower()
    return value if value in allowed else default


@dataclass(frozen=True)
class ArchitectureFeatures:
    persona_context_mode: str
    semantic_recovery_v2_shadow: bool
    semantic_recovery_v2_enabled: bool
    plain_text_salvage_enabled: bool
    field_level_repair_enabled: bool
    backend_single_turn_recovery_enabled: bool
    model_pipeline_rescue_enabled: bool
    frontend_full_retry_disabled: bool
    conditional_semantic_audit_enabled: bool
    backend_ndjson_envelope_enabled: bool
    tts_resegment_on_failure_enabled: bool
    tts_adaptive_retry_shadow_enabled: bool
    tts_bounded_retry_enabled: bool
    tts_high_confidence_resegment_enabled: bool
    tts_visible_segment_pipeline_enabled: bool

    def to_public_dict(self) -> dict[str, bool | str]:
        return {
            "".join(
                word if index == 0 else word[:1].upper() + word[1:]
                for index, word in enumerate(key.split("_"))
            ): value if isinstance(value, str) else bool(value)
            for key, value in asdict(self).items()
        }


def architecture_features(*, release_mode: bool = False) -> ArchitectureFeatures:
    """Resolve rollout flags.

    A frozen release uses the calibrated Persona Skill by default.  Normal and
    developer launchers choose independently tested migration stages explicitly;
    a direct process must not initialize a shadow observer whose result is
    discarded.
    """

    # The ordinary source launcher and the packaged release share the same
    # user-facing recovery and TTS behavior.  Each flag remains overridable for
    # rollback and diagnostics without enabling developer-only UI paths.
    ordinary_interaction_default = True
    default_persona_context_mode = "on" if release_mode else "off"
    semantic_enabled = _env_flag(
        "AMADEUS_SEMANTIC_RECOVERY_V2_ENABLED",
        ordinary_interaction_default,
    )
    return ArchitectureFeatures(
        persona_context_mode=_env_choice(
            "AMADEUS_PERSONA_CONTEXT_MODE",
            default_persona_context_mode,
            {"off", "shadow", "on"},
        ),
        semantic_recovery_v2_shadow=_env_flag(
            "AMADEUS_SEMANTIC_RECOVERY_V2_SHADOW",
            True,
        ),
        semantic_recovery_v2_enabled=semantic_enabled,
        plain_text_salvage_enabled=_env_flag(
            "AMADEUS_PLAIN_TEXT_SALVAGE_ENABLED",
            ordinary_interaction_default,
        ),
        field_level_repair_enabled=_env_flag(
            "AMADEUS_FIELD_LEVEL_REPAIR_ENABLED",
            ordinary_interaction_default,
        ),
        backend_single_turn_recovery_enabled=_env_flag(
            "AMADEUS_BACKEND_SINGLE_TURN_RECOVERY_ENABLED",
            ordinary_interaction_default,
        ),
        model_pipeline_rescue_enabled=_env_flag(
            "AMADEUS_MODEL_PIPELINE_RESCUE_ENABLED",
            ordinary_interaction_default,
        ),
        frontend_full_retry_disabled=_env_flag(
            "AMADEUS_FRONTEND_FULL_RETRY_DISABLED",
            ordinary_interaction_default,
        ),
        conditional_semantic_audit_enabled=_env_flag(
            "AMADEUS_CONDITIONAL_SEMANTIC_AUDIT_ENABLED",
            ordinary_interaction_default,
        ),
        backend_ndjson_envelope_enabled=_env_flag(
            "AMADEUS_BACKEND_NDJSON_ENVELOPE_ENABLED",
            ordinary_interaction_default,
        ),
        tts_resegment_on_failure_enabled=_env_flag(
            "AMADEUS_TTS_RESEGMENT_ON_FAILURE_ENABLED",
            ordinary_interaction_default,
        ),
        tts_adaptive_retry_shadow_enabled=_env_flag(
            "AMADEUS_TTS_ADAPTIVE_RETRY_SHADOW_ENABLED",
            ordinary_interaction_default,
        ),
        tts_bounded_retry_enabled=_env_flag(
            "AMADEUS_TTS_BOUNDED_RETRY_ENABLED",
            ordinary_interaction_default,
        ),
        tts_high_confidence_resegment_enabled=_env_flag(
            "AMADEUS_TTS_HIGH_CONFIDENCE_RESEGMENT_ENABLED",
            ordinary_interaction_default,
        ),
        tts_visible_segment_pipeline_enabled=_env_flag(
            "AMADEUS_TTS_VISIBLE_SEGMENT_PIPELINE_ENABLED",
            ordinary_interaction_default,
        ),
    )
