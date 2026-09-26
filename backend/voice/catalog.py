"""开发者语音参考审核目录与候选映射服务。

服务负责审核记录、试听选择和候选 style map 等目录派生文件。调用方为每次
请求提供最新路径快照，因此应用设置或兼容层被 patch 后不会被导入期缓存固化。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Callable
import wave

from agent.fingerprints import runtime_fingerprint
from backend.runtime.requests import RequestValidationError


VOICE_REFERENCE_REVIEW_STATUSES = {"unreviewed", "approved", "rejected"}
VOICE_REFERENCE_MAPPING_FIT_STATUSES = {"unreviewed", "matched", "mismatched"}
VOICE_REFERENCE_REVIEW_REASONS = {
    "timbre_mismatch",
    "emotion_weak",
    "emotion_mismatch",
    "noise_or_hoarse",
    "silence_or_duration",
    "text_mismatch",
    "other",
}
VOICE_REFERENCE_SEMANTIC_REVIEW_REASONS = {"emotion_weak", "emotion_mismatch"}
VOICE_REFERENCE_HARD_REVIEW_REASONS = {
    "timbre_mismatch",
    "noise_or_hoarse",
    "silence_or_duration",
    "text_mismatch",
}


@dataclass(frozen=True)
class VoiceReferenceCatalogPaths:
    """一次目录调用会访问的全部语音参考文件位置。

    这里的路径只涵盖审核、候选和它们的运行时派生物；模型、TTS 传输和通用
    应用状态仍由各自领域持有。
    """

    project_root: Path
    data_dir: Path
    runtime_voice_root: Path
    review_path: Path
    default_review_path: Path
    baseline_review_path: Path
    candidate_override_path: Path
    default_candidate_override_path: Path
    baseline_candidate_path: Path
    candidate_style_map_path: Path
    choice_path: Path
    training_exclusion_path: Path


class VoiceReferenceCatalog:
    """读取、审核并暂存开发者语音参考候选。

    ``update_voice_reference_review`` 只写入审核数据和试听选择；
    ``effective_voice_reference_style_map`` 仅在候选有效时生成独立的运行时
    style map。两者都不会覆盖原始 style map，TTS 启动侧自行决定是否采用该
    派生路径。本对象不缓存设置或目录状态，避免开发者工作台读到旧配置。
    """

    def __init__(
        self,
        paths: VoiceReferenceCatalogPaths,
        *,
        release_mode: bool,
        settings: Callable[[], dict[str, Any]] | None = None,
        now: Callable[[], str] | None = None,
        log: Callable[..., None] | None = None,
    ) -> None:
        self.paths = paths
        self.release_mode = release_mode
        self._settings = settings
        self._clock = now
        self._logger = log

    def local_path(self, value: Any) -> Path:
        path = Path(str(value or "").strip())
        return path if path.is_absolute() else (self.paths.project_root / path).resolve()

    def now_iso(self) -> str:
        if self._clock is not None:
            return self._clock()
        return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    def settings_snapshot(self) -> dict[str, Any]:
        return self._settings() if self._settings is not None else {}

    def write_log(self, event: str, **details: Any) -> None:
        if self._logger is not None:
            self._logger(event, **details)

    def _voice_reference_text(self, value: Any, limit: int = 2000) -> str:
        return str(value or "").strip()[:limit]


    def _voice_reference_key(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            return resolved.relative_to(self.paths.project_root).as_posix().casefold()
        except ValueError:
            return str(resolved).casefold()


    def _voice_reference_id(self, path: Path) -> str:
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", path.stem).strip(".-") or "reference"
        digest = runtime_fingerprint(self._voice_reference_key(path), length=12)
        return f"{stem}-{digest}"


    def _voice_reference_duration(self, path: Path) -> float | None:
        try:
            with wave.open(str(path), "rb") as audio:
                frame_rate = audio.getframerate()
                if frame_rate <= 0:
                    return None
                return round(audio.getnframes() / frame_rate, 3)
        except (OSError, EOFError, wave.Error):
            return None


    def read_voice_reference_review_dataset(self) -> dict[str, Any]:
        """读取并规范化审核数据，必要时回退到随源码分发的基线文件。"""
        empty = {"schemaVersion": 3, "reviews": [], "updatedAt": ""}
        review_path = self.paths.review_path
        if (
            not review_path.is_file()
            and review_path == self.paths.default_review_path
        ):
            review_path = self.paths.baseline_review_path
        if not review_path.is_file():
            return empty
        try:
            raw = json.loads(review_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return empty
        raw_reviews = raw.get("reviews") if isinstance(raw, dict) else []
        if not isinstance(raw_reviews, list):
            return empty
        reviews: list[dict[str, Any]] = []
        for item in raw_reviews[:512]:
            if not isinstance(item, dict):
                continue
            reference_key = self._voice_reference_text(item.get("referenceKey"), 500).casefold()
            reference_id = re.sub(
                r"[^A-Za-z0-9_.-]+",
                "-",
                self._voice_reference_text(item.get("id"), 120),
            ).strip(".-")
            raw_reason_codes = item.get("reasonCodes")
            reason_codes = [
                code
                for code in (raw_reason_codes if isinstance(raw_reason_codes, list) else [])
                if isinstance(code, str) and code in VOICE_REFERENCE_REVIEW_REASONS
            ][:12]
            status = self._voice_reference_text(item.get("qualityStatus") or item.get("status"), 24).lower()
            if not reference_key or not reference_id or status not in VOICE_REFERENCE_REVIEW_STATUSES:
                continue
            mapping_fit_status = self._voice_reference_text(item.get("mappingFitStatus"), 24).lower()
            if mapping_fit_status not in VOICE_REFERENCE_MAPPING_FIT_STATUSES:
                mapping_fit_status = (
                    "mismatched"
                    if set(reason_codes) & VOICE_REFERENCE_SEMANTIC_REVIEW_REASONS
                    else "unreviewed"
                )
            # Schema v1/v2 used one rejected state for both bad audio and a bad
            # emotion label.  A semantic-only legacy rejection cannot prove that
            # the source audio itself is bad, so migrate that axis to unreviewed.
            if (
                not item.get("qualityStatus")
                and status == "rejected"
                and reason_codes
                and set(reason_codes) <= VOICE_REFERENCE_SEMANTIC_REVIEW_REASONS
            ):
                status = "unreviewed"
            reviews.append({
                "id": reference_id,
                "referenceKey": reference_key,
                "scopeKey": self._voice_reference_text(item.get("scopeKey"), 500),
                "mappingKeys": [
                    self._voice_reference_text(key, 120)
                    for key in (item.get("mappingKeys") if isinstance(item.get("mappingKeys"), list) else [])
                    if self._voice_reference_text(key, 120)
                ],
                "voiceId": self._voice_reference_text(item.get("voiceId"), 160),
                "audioPath": self._voice_reference_text(item.get("audioPath"), 1000),
                "status": status,
                "qualityStatus": status,
                "mappingFitStatus": mapping_fit_status,
                "emotionLabel": self._voice_reference_text(item.get("emotionLabel"), 160),
                "reasonCodes": reason_codes,
                "notes": self._voice_reference_text(item.get("notes"), 2000),
                "updatedAt": self._voice_reference_text(item.get("updatedAt"), 80),
            })
        return {
            "schemaVersion": 3,
            "reviews": reviews,
            "updatedAt": self._voice_reference_text(raw.get("updatedAt"), 80),
        }


    def write_voice_reference_review_dataset(self, reviews: list[dict[str, Any]]) -> dict[str, Any]:
        """以临时文件替换方式写入当前配置的审核状态文件。"""
        payload = {
            "schemaVersion": 3,
            "reviews": reviews[:512],
            "updatedAt": self.now_iso(),
        }
        self.paths.review_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.paths.review_path.with_suffix(".json.tmp")
        temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary_path.replace(self.paths.review_path)
        return payload


    def read_voice_reference_candidate_overrides(self) -> dict[str, Any]:
        empty = {
            "schemaVersion": 1,
            "enabled": False,
            "baseStyleMap": "",
            "replacements": [],
            "choiceSets": [],
        }
        if self.release_mode:
            return empty
        candidate_path = self.paths.candidate_override_path
        if (
            not candidate_path.is_file()
            and candidate_path == self.paths.default_candidate_override_path
        ):
            candidate_path = self.paths.baseline_candidate_path
        if not candidate_path.is_file():
            return empty
        try:
            raw = json.loads(candidate_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return empty
        if not isinstance(raw, dict) or not raw.get("enabled"):
            return empty
        replacements = raw.get("replacements")
        if not isinstance(replacements, list):
            replacements = []
        choice_sets = raw.get("choiceSets")
        if not isinstance(choice_sets, list):
            choice_sets = []
        return {
            "schemaVersion": 1,
            "enabled": True,
            "baseStyleMap": self._voice_reference_text(raw.get("baseStyleMap"), 1000),
            "replacements": [item for item in replacements[:128] if isinstance(item, dict)],
            # Choice sets are deliberately review-only.  They are exposed in the
            # developer workbench but never merged into the effective style map.
            "choiceSets": [item for item in choice_sets[:32] if isinstance(item, dict)],
            "updatedAt": self._voice_reference_text(raw.get("updatedAt"), 80),
        }


    def read_voice_reference_choice_state(self) -> dict[str, str]:
        if self.release_mode or not self.paths.choice_path.is_file():
            return {}
        try:
            raw = json.loads(self.paths.choice_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {}
        selections = raw.get("selections") if isinstance(raw, dict) else None
        if not isinstance(selections, dict):
            return {}
        return {
            self._voice_reference_text(key, 160): self._voice_reference_text(value, 160)
            for key, value in selections.items()
            if self._voice_reference_text(key, 160) and self._voice_reference_text(value, 160)
        }


    def write_voice_reference_choice_state(self, selections: dict[str, str]) -> None:
        payload = {
            "schemaVersion": 1,
            "selections": dict(sorted(selections.items())),
            "updatedAt": self.now_iso(),
            "note": "Developer review choice only; this file never mutates the runtime style map.",
        }
        self.paths.choice_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.paths.choice_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.paths.choice_path)


    def _voice_reference_candidate_file(self, value: Any) -> Path | None:
        raw = self._voice_reference_text(value, 1000)
        if not raw:
            return None
        path = self.local_path(raw).resolve()
        allowed_roots = (
            (self.paths.project_root / "data" / "voice").resolve(),
            (self.paths.data_dir / "developer").resolve(),
            self.paths.runtime_voice_root,
        )
        if not any(path == root or root in path.parents for root in allowed_roots):
            return None
        return path


    def _voice_reference_candidate_rejection_reason(self, candidate_audio: Path) -> str:
        """Reject development candidates already invalidated by audio-quality evidence."""
        resolved = candidate_audio.resolve()
        voice_id = resolved.stem.casefold()
        try:
            raw = json.loads(self.paths.training_exclusion_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if not isinstance(raw, dict):
            return ""
        for item in raw.get("exclusions") or []:
            if not isinstance(item, dict):
                continue
            excluded_voice_id = str(item.get("voiceId") or "").strip().casefold()
            excluded_audio = str(item.get("audioPath") or "").strip()
            same_audio = bool(excluded_audio) and self.local_path(excluded_audio).resolve() == resolved
            if same_audio or (excluded_voice_id and excluded_voice_id == voice_id):
                return "curated_audio_quality_exclusion"
        for item in raw.get("excludeRanges") or []:
            if not isinstance(item, dict):
                continue
            prefix = str(item.get("prefix") or "").strip().casefold()
            try:
                start = int(item.get("start"))
                end = int(item.get("end"))
            except (TypeError, ValueError):
                continue
            if not prefix or not voice_id.startswith(prefix):
                continue
            suffix = voice_id[len(prefix):]
            if suffix.isdigit() and start <= int(suffix) <= end:
                return "curated_scene_audio_quality_exclusion"
        for review in self.read_voice_reference_review_dataset().get("reviews", []):
            if not isinstance(review, dict) or str(review.get("status") or "").lower() != "rejected":
                continue
            reasons = {str(code) for code in (review.get("reasonCodes") or [])}
            if not reasons & VOICE_REFERENCE_HARD_REVIEW_REASONS:
                continue
            review_voice_id = str(review.get("voiceId") or "").strip().casefold()
            review_audio = str(review.get("audioPath") or "").strip()
            same_audio = bool(review_audio) and Path(review_audio).resolve() == resolved
            if same_audio or (review_voice_id and review_voice_id == voice_id):
                return "developer_review_audio_quality_rejected"
        return ""


    def _voice_reference_state_contract(self,
        *,
        audio_path: Path,
        is_currently_effective: bool,
        manual_review_status: str,
        risk_level: str = "",
        candidate_role: str = "none",
        review_workflow_status: str = "",
        explicit_system_reason: str = "",
    ) -> dict[str, Any]:
        """Keep runtime, system eligibility, candidate role, and user review orthogonal."""

        system_reason = explicit_system_reason or self._voice_reference_candidate_rejection_reason(audio_path)
        normalized_risk = self._voice_reference_text(risk_level, 40).casefold()
        if not audio_path.is_file():
            system_status = "missing"
            system_reason = system_reason or "audio_file_missing"
        elif system_reason:
            system_status = "disqualified"
        elif normalized_risk in {"high", "critical", "medium_high"}:
            system_status = "high_risk"
        else:
            system_status = "eligible"
        usage_status = "active" if is_currently_effective else "inactive"
        if system_status in {"disqualified", "missing"}:
            current_status = system_status
        elif usage_status == "active":
            current_status = "active"
        elif candidate_role == "replacement":
            current_status = "replacement_candidate"
        elif candidate_role == "audition":
            current_status = "audition_candidate"
        else:
            current_status = "inactive"
        return {
            "usageStatus": usage_status,
            "systemStatus": system_status,
            "systemReason": system_reason,
            "candidateRole": candidate_role,
            "currentStatus": current_status,
            "manualReviewStatus": manual_review_status,
            "reviewWorkflowStatus": review_workflow_status or "none",
            "selectionAllowed": bool(
                candidate_role == "audition"
                and system_status not in {"disqualified", "missing"}
            ),
        }


    def effective_voice_reference_style_map(self, settings: dict[str, Any]) -> tuple[Path | None, dict[str, Any]]:
        """生成候选替换 map，但绝不改写传入设置指向的原始 style map。"""
        style_map_value = self._voice_reference_text(settings.get("gptSovitsStyleMap"), 1000)
        base_path = self.local_path(style_map_value).resolve() if style_map_value else None
        empty = {
            "enabled": False,
            "replacements": [],
            "choiceSets": [],
            "appliedMappings": [],
            "blockedReplacements": [],
        }
        if self.release_mode or not base_path or not base_path.is_file():
            return base_path, empty
        candidate_data = self.read_voice_reference_candidate_overrides()
        configured_base = self._voice_reference_candidate_file(candidate_data.get("baseStyleMap"))
        if not candidate_data.get("enabled") or not configured_base or configured_base != base_path:
            return base_path, empty
        try:
            raw_map = json.loads(base_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return base_path, empty
        if not isinstance(raw_map, dict):
            return base_path, empty
        merged_map = dict(raw_map)
        applied: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        for raw_replacement in candidate_data["replacements"]:
            original = raw_replacement.get("original") if isinstance(raw_replacement.get("original"), dict) else {}
            candidate = raw_replacement.get("candidate") if isinstance(raw_replacement.get("candidate"), dict) else {}
            candidate_audio = self._voice_reference_candidate_file(candidate.get("refAudio"))
            candidate_text = self._voice_reference_candidate_file(candidate.get("refText"))
            original_audio = self._voice_reference_candidate_file(original.get("refAudio"))
            original_text = self._voice_reference_candidate_file(original.get("refText"))
            mapping_keys = [
                self._voice_reference_text(key, 120)
                for key in (raw_replacement.get("mappingKeys") or [])
                if self._voice_reference_text(key, 120)
            ]
            if not mapping_keys or not candidate_audio or not candidate_text:
                continue
            if not candidate_audio.is_file() or not candidate_text.is_file():
                continue
            rejection_reason = self._voice_reference_candidate_rejection_reason(candidate_audio)
            if rejection_reason:
                blocked.append({
                    "id": self._voice_reference_text(raw_replacement.get("id"), 160),
                    "mappingKeys": mapping_keys,
                    "original": {
                        "refAudio": str(original_audio or ""),
                        "refText": str(original_text or ""),
                    },
                    "candidate": {
                        "refAudio": str(candidate_audio),
                        "refText": str(candidate_text),
                    },
                    "candidateAudio": str(candidate_audio),
                    "systemReason": rejection_reason,
                    "reason": rejection_reason,
                    "replacementReason": self._voice_reference_text(raw_replacement.get("reason"), 1000),
                    "source": self._voice_reference_text(raw_replacement.get("source"), 500),
                })
                continue
            valid_keys: list[str] = []
            for mapping_key in mapping_keys:
                current = raw_map.get(mapping_key)
                if not isinstance(current, dict):
                    continue
                current_audio = self._voice_reference_candidate_file(current.get("refAudio"))
                current_text = self._voice_reference_candidate_file(current.get("refText"))
                if original_audio and current_audio != original_audio:
                    continue
                if original_text and current_text != original_text:
                    continue
                merged_map[mapping_key] = {
                    "refAudio": str(candidate_audio),
                    "refText": str(candidate_text),
                }
                valid_keys.append(mapping_key)
            if valid_keys:
                applied.append({
                    "id": self._voice_reference_text(raw_replacement.get("id"), 160),
                    "mappingKeys": valid_keys,
                    "original": {"refAudio": str(original_audio or ""), "refText": str(original_text or "")},
                    "candidate": {"refAudio": str(candidate_audio), "refText": str(candidate_text)},
                    "reason": self._voice_reference_text(raw_replacement.get("reason"), 1000),
                    "source": self._voice_reference_text(raw_replacement.get("source"), 500),
                    "status": "pending_recheck",
                })
        if not applied:
            return base_path, {
                **empty,
                **candidate_data,
                "enabled": bool(candidate_data.get("choiceSets")),
                "replacements": [],
                "appliedMappings": [],
                "blockedReplacements": blocked,
                "baseStyleMap": str(base_path),
            }
        self.paths.candidate_style_map_path.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(merged_map, ensure_ascii=False, indent=2) + "\n"
        try:
            existing = self.paths.candidate_style_map_path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        if existing != rendered:
            temporary = self.paths.candidate_style_map_path.with_suffix(".json.tmp")
            temporary.write_text(rendered, encoding="utf-8")
            temporary.replace(self.paths.candidate_style_map_path)
        return self.paths.candidate_style_map_path.resolve(), {
            **candidate_data,
            "enabled": True,
            "replacements": applied,
            "appliedMappings": sorted({key for item in applied for key in item["mappingKeys"]}),
            "blockedReplacements": blocked,
            "baseStyleMap": str(base_path),
            "candidateStyleMap": str(self.paths.candidate_style_map_path.resolve()),
        }


    def _settings_with_effective_voice_reference_map(self, settings: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        effective_path, candidate_data = self.effective_voice_reference_style_map(settings)
        effective = dict(settings)
        if effective_path:
            effective["gptSovitsStyleMap"] = str(effective_path)
        return effective, candidate_data


    def _voice_reference_map_entries(self, settings: dict[str, Any] | None = None) -> list[tuple[str, Path, Path]]:
        active_settings = settings or self.settings_snapshot()
        style_map_value = self._voice_reference_text(active_settings.get("gptSovitsStyleMap"), 1000)
        style_map_path = self.local_path(style_map_value) if style_map_value else None
        entries: list[tuple[str, Path, Path]] = []
        if style_map_path and style_map_path.is_file():
            try:
                raw_map = json.loads(style_map_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                raw_map = {}
            if isinstance(raw_map, dict):
                for raw_key, raw_item in raw_map.items():
                    if not isinstance(raw_item, dict):
                        continue
                    raw_audio = str(raw_item.get("refAudio") or "").strip()
                    raw_text = str(raw_item.get("refText") or "").strip()
                    if not raw_audio:
                        continue
                    audio = Path(raw_audio)
                    text = Path(raw_text) if raw_text else Path()
                    if not audio.is_absolute():
                        audio = (style_map_path.parent / audio).resolve()
                    if raw_text and not text.is_absolute():
                        text = (style_map_path.parent / text).resolve()
                    entries.append((str(raw_key), audio.resolve(), text.resolve() if raw_text else Path()))
        default_audio_value = self._voice_reference_text(active_settings.get("gptSovitsRefAudio"), 1000)
        default_text_value = self._voice_reference_text(active_settings.get("gptSovitsRefText"), 1000)
        if default_audio_value:
            entries.append((
                "default",
                self.local_path(default_audio_value).resolve(),
                self.local_path(default_text_value).resolve() if default_text_value else Path(),
            ))
        return entries


    def _voice_reference_choice_catalog(self,
        candidate_data: dict[str, Any],
        review_by_id: dict[str, dict[str, Any]],
        quality_review_by_key: dict[str, dict[str, Any]],
        effective_entries: list[tuple[str, Path, Path]],
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
        """Render developer-only A/B choices against the actual effective style map."""

        selections = self.read_voice_reference_choice_state()
        effective_by_mapping_key = {
            mapping_key: (
                audio_path.resolve(),
                text_path.resolve() if str(text_path) not in {"", "."} else Path(),
            )
            for mapping_key, audio_path, text_path in effective_entries
        }
        active_mapping_keys_by_reference: dict[str, list[str]] = {}
        for mapping_key, audio_path, _ in effective_entries:
            active_mapping_keys_by_reference.setdefault(
                self._voice_reference_key(audio_path),
                [],
            ).append(mapping_key)
        references: list[dict[str, Any]] = []
        warnings: dict[str, dict[str, Any]] = {}
        choice_sets: list[dict[str, Any]] = []
        for raw_set in candidate_data.get("choiceSets") or []:
            if not isinstance(raw_set, dict):
                continue
            choice_set_id = re.sub(
                r"[^A-Za-z0-9_.-]+",
                "-",
                self._voice_reference_text(raw_set.get("id"), 160),
            ).strip(".-")
            declared_current = raw_set.get("current") if isinstance(raw_set.get("current"), dict) else {}
            declared_current_audio = self._voice_reference_candidate_file(declared_current.get("refAudio"))
            declared_current_text = self._voice_reference_candidate_file(declared_current.get("refText"))
            mapping_keys = [
                self._voice_reference_text(key, 120)
                for key in (raw_set.get("mappingKeys") or [])
                if self._voice_reference_text(key, 120)
            ]
            effective_mappings = [
                (mapping_key, *effective_by_mapping_key[mapping_key])
                for mapping_key in mapping_keys
                if mapping_key in effective_by_mapping_key
            ]
            current_audio = effective_mappings[0][1] if effective_mappings else declared_current_audio
            current_text = effective_mappings[0][2] if effective_mappings else declared_current_text
            if not choice_set_id or not current_audio or not current_audio.is_file() or not mapping_keys:
                continue
            declared_current_matches = bool(
                effective_mappings
                and declared_current_audio
                and all(
                    declared_current_audio.resolve() == effective_audio.resolve()
                    and (
                        not declared_current_text
                        or str(effective_text) in {"", "."}
                        or declared_current_text.resolve() == effective_text.resolve()
                    )
                    for _, effective_audio, effective_text in effective_mappings
                )
            )
            current_metadata = declared_current if declared_current_matches or not effective_mappings else {}
            if not current_metadata:
                current_metadata = next((
                    candidate
                    for candidate in (raw_set.get("candidates") or [])
                    if isinstance(candidate, dict)
                    and (candidate_audio := self._voice_reference_candidate_file(candidate.get("refAudio")))
                    and candidate_audio.resolve() == current_audio.resolve()
                ), {})
            current_key = self._voice_reference_key(current_audio)
            risk_level = self._voice_reference_text(current_metadata.get("riskLevel"), 40) or "unknown"
            is_high_risk = risk_level.casefold() in {"high", "critical", "medium_high"}
            configured_label = self._voice_reference_text(
                current_metadata.get("attentionLabel")
                or (raw_set.get("attentionLabel") if declared_current_matches else ""),
                160,
            )
            if configured_label and declared_current_matches:
                warning_label = configured_label
            elif configured_label:
                warning_label = f"{'⚠ ' if is_high_risk else ''}当前生效 · {configured_label.lstrip('⚠★✓ ')}"[:160]
            else:
                warning_label = f"{'⚠ ' if is_high_risk else ''}当前生效 · {current_audio.stem}"[:160]
            warning_reason = self._voice_reference_text(
                current_metadata.get("riskNote") or raw_set.get("reason"),
                1000,
            )
            warnings[current_key] = {
                "attentionLevel": "high_risk" if is_high_risk else "current",
                "attentionLabel": warning_label,
                "riskLevel": risk_level,
                "riskNote": warning_reason,
                "choiceSetId": choice_set_id,
            }
            selected_candidate_id = selections.get(choice_set_id, "")
            rendered_candidates: list[dict[str, Any]] = []
            for rank, raw_candidate in enumerate(raw_set.get("candidates") or [], 1):
                if not isinstance(raw_candidate, dict):
                    continue
                candidate_id = re.sub(
                    r"[^A-Za-z0-9_.-]+",
                    "-",
                    self._voice_reference_text(raw_candidate.get("id"), 160),
                ).strip(".-")
                candidate_audio = self._voice_reference_candidate_file(raw_candidate.get("refAudio"))
                candidate_text = self._voice_reference_candidate_file(raw_candidate.get("refText"))
                if (
                    not candidate_id
                    or not candidate_audio
                    or not candidate_text
                    or not candidate_audio.is_file()
                    or not candidate_text.is_file()
                ):
                    continue
                candidate_rejection_reason = self._voice_reference_candidate_rejection_reason(candidate_audio)
                candidate_key = self._voice_reference_key(candidate_audio)
                active_mapping_keys = sorted(
                    set(active_mapping_keys_by_reference.get(candidate_key, [])),
                    key=lambda value: (value == "default", value),
                )
                active_elsewhere_mapping_keys = [
                    key for key in active_mapping_keys if key not in mapping_keys
                ]
                entry_id = f"choice-{choice_set_id}-{candidate_id}"[:120]
                review = review_by_id.get(entry_id, {})
                shared_quality_review = quality_review_by_key.get(candidate_key, {})
                status = str(
                    review.get("qualityStatus")
                    or review.get("status")
                    or shared_quality_review.get("qualityStatus")
                    or shared_quality_review.get("status")
                    or "unreviewed"
                )
                if status not in VOICE_REFERENCE_REVIEW_STATUSES:
                    status = "unreviewed"
                try:
                    reference_text = candidate_text.read_text(encoding="utf-8-sig").strip()
                except OSError:
                    reference_text = ""
                recommendation = self._voice_reference_text(raw_candidate.get("recommendation"), 80)
                attention_label = self._voice_reference_text(raw_candidate.get("attentionLabel"), 160)
                if not attention_label:
                    attention_label = "★ 建议优先试听" if rank == 1 else "试听候选"
                is_selected = candidate_id == selected_candidate_id
                is_currently_effective = any(
                    candidate_audio.resolve() == effective_audio.resolve()
                    and (
                        str(effective_text) in {"", "."}
                        or candidate_text.resolve() == effective_text.resolve()
                    )
                    for _, effective_audio, effective_text in effective_mappings
                )
                candidate_risk_level = self._voice_reference_text(raw_candidate.get("riskLevel"), 40) or "unknown"
                state_contract = self._voice_reference_state_contract(
                    audio_path=candidate_audio,
                    is_currently_effective=is_currently_effective,
                    manual_review_status=status,
                    risk_level=candidate_risk_level,
                    candidate_role="audition",
                    explicit_system_reason=candidate_rejection_reason,
                )
                entry = {
                    "id": entry_id,
                    "referenceKey": candidate_key,
                    "reviewScope": f"choice:{choice_set_id}:{candidate_id}"[:500],
                    "voiceId": candidate_audio.stem,
                    "audioFile": candidate_audio.name,
                    "audioPath": str(candidate_audio),
                    "audioExists": True,
                    "durationSeconds": self._voice_reference_duration(candidate_audio),
                    "textPaths": [str(candidate_text)],
                    "referenceTexts": [reference_text[:2000]] if reference_text else [],
                    "mappingKeys": mapping_keys,
                    "activeMappingKeys": active_mapping_keys,
                    "activeElsewhereMappingKeys": active_elsewhere_mapping_keys,
                    "status": status,
                    "qualityStatus": status,
                    "mappingFitStatus": str(review.get("mappingFitStatus") or "unreviewed"),
                    "emotionLabel": self._voice_reference_text(
                        review.get("emotionLabel") or raw_candidate.get("emotionLabel"),
                        160,
                    ),
                    "reasonCodes": list(review.get("reasonCodes") or []),
                    "notes": str(review.get("notes") or ""),
                    "reviewedAt": str(review.get("updatedAt") or ""),
                    "runtimeBehavior": "review_only_choice",
                    "isReplacementCandidate": False,
                    "isAuditionCandidate": True,
                    "isComparisonCandidate": True,
                    "recheckStatus": "",
                    "choiceSetId": choice_set_id,
                    "choiceCandidateId": candidate_id,
                    "isSelectedChoice": is_selected,
                    "isCurrentlyEffective": is_currently_effective,
                    "candidateRank": rank,
                    "recommendation": recommendation,
                    "attentionLevel": "selected" if is_selected else ("recommended" if rank <= 2 else "candidate"),
                    "attentionLabel": "✓ 已选为待晋升候选" if is_selected else attention_label,
                    "riskLevel": candidate_risk_level,
                    "riskNote": self._voice_reference_text(raw_candidate.get("riskNote"), 1000),
                    "evidence": raw_candidate.get("evidence") if isinstance(raw_candidate.get("evidence"), dict) else {},
                    "replacement": {
                        "id": choice_set_id,
                        "mappingKeys": mapping_keys,
                        "original": {
                            "refAudio": str(current_audio),
                            "refText": str(current_text) if str(current_text) not in {"", "."} else "",
                        },
                        "candidate": {
                            "refAudio": str(candidate_audio),
                            "refText": str(candidate_text),
                        },
                        "reason": self._voice_reference_text(raw_candidate.get("reason"), 1000),
                        "source": self._voice_reference_text(raw_candidate.get("source"), 500),
                    },
                    **state_contract,
                }
                references.append(entry)
                rendered_candidates.append({
                    "id": candidate_id,
                    "voiceId": candidate_audio.stem,
                    "selected": is_selected,
                    "currentlyEffective": is_currently_effective,
                    "usageStatus": state_contract["usageStatus"],
                    "systemStatus": state_contract["systemStatus"],
                    "selectionAllowed": state_contract["selectionAllowed"],
                    "recommendation": recommendation,
                    "riskLevel": entry["riskLevel"],
                })
            choice_sets.append({
                "id": choice_set_id,
                "mappingKeys": mapping_keys,
                "selectedCandidateId": selected_candidate_id,
                "currentVoiceId": current_audio.stem,
                "currentAudioPath": str(current_audio),
                "currentTextPath": str(current_text) if str(current_text) not in {"", "."} else "",
                "currentSource": "effective_style_map" if effective_mappings else "declared_current_fallback",
                "declaredCurrentMatchesEffective": declared_current_matches,
                "effectiveMappings": [
                    {
                        "mappingKey": mapping_key,
                        "voiceId": audio_path.stem,
                        "refAudio": str(audio_path),
                        "refText": str(text_path) if str(text_path) not in {"", "."} else "",
                    }
                    for mapping_key, audio_path, text_path in effective_mappings
                ],
                "candidateCount": len(rendered_candidates),
                "candidates": rendered_candidates,
            })
        return references, warnings, choice_sets


    def voice_reference_review_catalog(self, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        """汇总生效参考音、历史审核和候选试听状态供开发者工作台展示。"""
        configured_settings = settings or self.settings_snapshot()
        active_settings, candidate_data = self._settings_with_effective_voice_reference_map(configured_settings)
        reviews = self.read_voice_reference_review_dataset()
        review_by_id = {
            str(item.get("id") or ""): item
            for item in reviews["reviews"]
            if str(item.get("id") or "")
        }
        review_by_key = {
            str(item.get("referenceKey") or "").casefold(): item
            for item in reviews["reviews"]
            if (
                not str(item.get("scopeKey") or "")
                or str(item.get("scopeKey") or "").startswith("runtime:")
            )
            and not str(item.get("id") or "").startswith("choice-")
        }
        quality_review_by_key: dict[str, dict[str, Any]] = {}
        quality_review_by_voice_id: dict[str, dict[str, Any]] = {}
        runtime_review_by_scope: dict[str, dict[str, Any]] = {}
        review_by_voice_id: dict[str, dict[str, Any]] = {}
        for review in sorted(reviews["reviews"], key=lambda item: str(item.get("updatedAt") or "")):
            reference_key = str(review.get("referenceKey") or "").casefold()
            voice_id = str(review.get("voiceId") or "").strip().casefold()
            scope_key = str(review.get("scopeKey") or "").strip().casefold()
            quality_status = str(review.get("qualityStatus") or review.get("status") or "unreviewed")
            if reference_key and quality_status in {"approved", "rejected"}:
                # Source quality belongs to the audio itself and is therefore
                # shared across mapping scopes.  Mapping fit remains scope-local.
                quality_review_by_key[reference_key] = review
            if voice_id and quality_status in {"approved", "rejected"}:
                quality_review_by_voice_id[voice_id] = review
            if scope_key.startswith("runtime:"):
                runtime_review_by_scope[scope_key] = review
            if voice_id and not str(review.get("id") or "").startswith("choice-"):
                review_by_voice_id[voice_id] = review
        replacement_by_candidate_key: dict[str, dict[str, Any]] = {}
        for replacement in candidate_data.get("replacements") or []:
            candidate_audio = Path(str((replacement.get("candidate") or {}).get("refAudio") or ""))
            if str(candidate_audio) not in {"", "."}:
                replacement_by_candidate_key[self._voice_reference_key(candidate_audio)] = replacement
        effective_entries = self._voice_reference_map_entries(active_settings)
        choice_references, reference_warnings, choice_sets = self._voice_reference_choice_catalog(
            candidate_data,
            review_by_id,
            quality_review_by_key,
            effective_entries,
        )
        grouped: dict[str, dict[str, Any]] = {}
        for mapping_key, audio_path, text_path in effective_entries:
            reference_key = self._voice_reference_key(audio_path)
            item = grouped.setdefault(reference_key, {
                "id": self._voice_reference_id(audio_path),
                "referenceKey": reference_key,
                "voiceId": audio_path.stem,
                "audioFile": audio_path.name,
                "audioPath": str(audio_path),
                "audioExists": audio_path.is_file(),
                "durationSeconds": self._voice_reference_duration(audio_path),
                "textPaths": [],
                "referenceTexts": [],
                "mappingKeys": [],
            })
            if mapping_key not in item["mappingKeys"]:
                item["mappingKeys"].append(mapping_key)
            text_path_string = str(text_path) if str(text_path) not in {"", "."} else ""
            if text_path_string and text_path_string not in item["textPaths"]:
                item["textPaths"].append(text_path_string)
            if text_path_string and text_path.is_file():
                try:
                    reference_text = text_path.read_text(encoding="utf-8-sig").strip()
                except OSError:
                    reference_text = ""
                if reference_text and reference_text not in item["referenceTexts"]:
                    item["referenceTexts"].append(reference_text[:2000])
        references: list[dict[str, Any]] = []
        for reference_key, item in grouped.items():
            mapping_keys = sorted(item["mappingKeys"], key=lambda value: (value == "default", value))
            review = review_by_key.get(reference_key, {})
            if not review:
                review = next(
                    (
                        scoped_review
                        for mapping_key in mapping_keys
                        if (
                            scoped_review := runtime_review_by_scope.get(
                                f"runtime:{mapping_key}".casefold()
                            )
                        )
                        and str(scoped_review.get("voiceId") or "").casefold()
                        == str(item.get("voiceId") or "").casefold()
                    ),
                    review_by_voice_id.get(str(item.get("voiceId") or "").casefold(), {}),
                )
            quality_review = quality_review_by_key.get(reference_key, {}) or quality_review_by_voice_id.get(
                str(item.get("voiceId") or "").casefold(),
                {},
            )
            local_quality_status = str(review.get("qualityStatus") or review.get("status") or "unreviewed")
            status = local_quality_status if local_quality_status in {"approved", "rejected"} else str(
                quality_review.get("qualityStatus")
                or quality_review.get("status")
                or "unreviewed"
            )
            if status not in VOICE_REFERENCE_REVIEW_STATUSES:
                status = "unreviewed"
            replacement = replacement_by_candidate_key.get(reference_key)
            original_review: dict[str, Any] = {}
            if replacement:
                original_audio = Path(str((replacement.get("original") or {}).get("refAudio") or ""))
                if str(original_audio) not in {"", "."}:
                    original_review = review_by_key.get(self._voice_reference_key(original_audio), {})
            recheck_status = ""
            if replacement:
                recheck_status = {
                    "approved": "recheck_passed",
                    "rejected": "recheck_failed",
                }.get(status, "pending_recheck")
            warning_fields = reference_warnings.get(reference_key, {
                "attentionLevel": "",
                "attentionLabel": "",
                "riskLevel": "",
                "riskNote": "",
                "choiceSetId": "",
            })
            state_contract = self._voice_reference_state_contract(
                audio_path=Path(str(item.get("audioPath") or "")),
                is_currently_effective=True,
                manual_review_status=status,
                risk_level=str(warning_fields.get("riskLevel") or ""),
                candidate_role="replacement" if replacement else "none",
                review_workflow_status=recheck_status,
            )
            references.append({
                **item,
                "mappingKeys": mapping_keys,
                "reviewScope": f"runtime:{'|'.join(mapping_keys)}"[:500],
                "status": status,
                "qualityStatus": status,
                "mappingFitStatus": str(review.get("mappingFitStatus") or "unreviewed"),
                "emotionLabel": self._voice_reference_text(
                    review.get("emotionLabel") or quality_review.get("emotionLabel"),
                    160,
                ),
                "reasonCodes": list(review.get("reasonCodes") or []) + [
                    code
                    for code in (quality_review.get("reasonCodes") or [])
                    if code in VOICE_REFERENCE_HARD_REVIEW_REASONS
                    and code not in (review.get("reasonCodes") or [])
                ],
                "notes": str(review.get("notes") or ""),
                "reviewedAt": str(review.get("updatedAt") or ""),
                "runtimeBehavior": "developer_candidate" if replacement else "feedback_only",
                "isReplacementCandidate": bool(replacement),
                "recheckStatus": recheck_status,
                "replacement": {
                    **(replacement or {}),
                    "originalStatus": str(original_review.get("status") or "unreviewed"),
                    "originalReasonCodes": list(original_review.get("reasonCodes") or []),
                    "originalNotes": str(original_review.get("notes") or ""),
                    "originalReviewedAt": str(original_review.get("updatedAt") or ""),
                } if replacement else None,
                **warning_fields,
                "isAuditionCandidate": False,
                "isComparisonCandidate": bool(replacement),
                "isSelectedChoice": False,
                "isCurrentlyEffective": True,
                **state_contract,
            })
        for blocked in candidate_data.get("blockedReplacements") or []:
            if not isinstance(blocked, dict):
                continue
            candidate = blocked.get("candidate") if isinstance(blocked.get("candidate"), dict) else {}
            candidate_audio = self._voice_reference_candidate_file(candidate.get("refAudio"))
            candidate_text = self._voice_reference_candidate_file(candidate.get("refText"))
            if not candidate_audio or not candidate_audio.is_file():
                continue
            reference_key = self._voice_reference_key(candidate_audio)
            replacement_id = re.sub(
                r"[^A-Za-z0-9_.-]+",
                "-",
                self._voice_reference_text(blocked.get("id"), 100),
            ).strip(".-") or candidate_audio.stem
            blocked_reference_id = f"blocked-{replacement_id}"[:120]
            review = review_by_id.get(blocked_reference_id, {})
            status = str(review.get("status") or "unreviewed")
            if status not in VOICE_REFERENCE_REVIEW_STATUSES:
                status = "unreviewed"
            try:
                reference_text = (
                    candidate_text.read_text(encoding="utf-8-sig").strip()
                    if candidate_text and candidate_text.is_file()
                    else ""
                )
            except OSError:
                reference_text = ""
            state_contract = self._voice_reference_state_contract(
                audio_path=candidate_audio,
                is_currently_effective=False,
                manual_review_status=status,
                candidate_role="replacement",
                explicit_system_reason=self._voice_reference_text(
                    blocked.get("systemReason") or blocked.get("reason"),
                    160,
                ),
            )
            references.append({
                "id": blocked_reference_id,
                "referenceKey": reference_key,
                "reviewScope": f"replacement:{replacement_id}"[:500],
                "voiceId": candidate_audio.stem,
                "audioFile": candidate_audio.name,
                "audioPath": str(candidate_audio),
                "audioExists": True,
                "durationSeconds": self._voice_reference_duration(candidate_audio),
                "textPaths": [str(candidate_text)] if candidate_text else [],
                "referenceTexts": [reference_text[:2000]] if reference_text else [],
                "mappingKeys": list(blocked.get("mappingKeys") or []),
                "status": status,
                "qualityStatus": status,
                "mappingFitStatus": str(review.get("mappingFitStatus") or "unreviewed"),
                "emotionLabel": self._voice_reference_text(review.get("emotionLabel"), 160),
                "reasonCodes": list(review.get("reasonCodes") or []),
                "notes": str(review.get("notes") or ""),
                "reviewedAt": str(review.get("updatedAt") or ""),
                "runtimeBehavior": "blocked_replacement_candidate",
                "isReplacementCandidate": True,
                "isAuditionCandidate": False,
                "isComparisonCandidate": True,
                "isSelectedChoice": False,
                "isCurrentlyEffective": False,
                "recheckStatus": "",
                "attentionLevel": "system_disqualified",
                "attentionLabel": "系统状态：不合格 · 未启用",
                "riskLevel": "blocked",
                "riskNote": self._voice_reference_text(blocked.get("systemReason") or blocked.get("reason"), 1000),
                "choiceSetId": "",
                "replacement": {
                    "id": replacement_id,
                    "mappingKeys": list(blocked.get("mappingKeys") or []),
                    "original": dict(blocked.get("original") or {}),
                    "candidate": dict(candidate),
                    "reason": self._voice_reference_text(blocked.get("replacementReason"), 1000),
                    "source": self._voice_reference_text(blocked.get("source"), 500),
                },
                **state_contract,
            })
        references.extend(choice_references)
        known_reference_keys = {
            str(item.get("referenceKey") or "").casefold()
            for item in references
        }
        for review in reviews.get("reviews") or []:
            if not isinstance(review, dict):
                continue
            reference_key = str(review.get("referenceKey") or "").casefold()
            if not reference_key or reference_key in known_reference_keys:
                continue
            audio_path = self._voice_reference_candidate_file(review.get("audioPath"))
            if not audio_path:
                continue
            status = str(review.get("status") or "unreviewed")
            if status not in VOICE_REFERENCE_REVIEW_STATUSES:
                status = "unreviewed"
            state_contract = self._voice_reference_state_contract(
                audio_path=audio_path,
                is_currently_effective=False,
                manual_review_status=status,
            )
            references.append({
                "id": str(review.get("id") or self._voice_reference_id(audio_path)),
                "referenceKey": reference_key,
                "reviewScope": str(review.get("scopeKey") or f"historical:{review.get('id') or audio_path.stem}"),
                "voiceId": str(review.get("voiceId") or audio_path.stem),
                "audioFile": audio_path.name,
                "audioPath": str(audio_path),
                "audioExists": audio_path.is_file(),
                "durationSeconds": self._voice_reference_duration(audio_path),
                "textPaths": [],
                "referenceTexts": [],
                "mappingKeys": [],
                "status": status,
                "qualityStatus": status,
                "mappingFitStatus": str(review.get("mappingFitStatus") or "unreviewed"),
                "emotionLabel": self._voice_reference_text(review.get("emotionLabel"), 160),
                "reasonCodes": list(review.get("reasonCodes") or []),
                "notes": str(review.get("notes") or ""),
                "reviewedAt": str(review.get("updatedAt") or ""),
                "runtimeBehavior": "historical_review",
                "isReplacementCandidate": False,
                "isAuditionCandidate": False,
                "isComparisonCandidate": False,
                "isSelectedChoice": False,
                "isCurrentlyEffective": False,
                "recheckStatus": "",
                "attentionLevel": (
                    "system_disqualified"
                    if state_contract["systemStatus"] in {"disqualified", "missing"}
                    else "historical"
                ),
                "attentionLabel": "历史标注 · 当前未启用",
                "riskLevel": "",
                "riskNote": "",
                "choiceSetId": "",
                "replacement": None,
                **state_contract,
            })
            known_reference_keys.add(reference_key)
        for item in references:
            manual_status = str(item.get("manualReviewStatus") or item.get("status") or "unreviewed")
            mapping_fit_status = str(item.get("mappingFitStatus") or "unreviewed")
            if mapping_fit_status not in VOICE_REFERENCE_MAPPING_FIT_STATUSES:
                mapping_fit_status = "unreviewed"
            item["manualReviewStatus"] = manual_status
            item["qualityStatus"] = manual_status
            item["mappingFitStatus"] = mapping_fit_status
            item["emotionLabel"] = self._voice_reference_text(item.get("emotionLabel"), 160)
            item["manualReview"] = {
                "status": manual_status,
                "qualityStatus": manual_status,
                "mappingFitStatus": mapping_fit_status,
                "emotionLabel": item["emotionLabel"],
                "reasonCodes": list(item.get("reasonCodes") or []),
                "notes": str(item.get("notes") or ""),
                "updatedAt": str(item.get("reviewedAt") or ""),
            }
        references.sort(key=lambda item: (
            item["mappingKeys"][0] if item["mappingKeys"] else "",
            0 if item.get("attentionLevel") == "high_risk" else (
                1 if item.get("isSelectedChoice") else (2 if item.get("isAuditionCandidate") else 3)
            ),
            int(item.get("candidateRank") or 999),
            item["voiceId"],
        ))
        return {
            "ok": True,
            "schemaVersion": 3,
            "preset": str(configured_settings.get("gptSovitsModelPreset") or "custom"),
            "styleMap": str(configured_settings.get("gptSovitsStyleMap") or ""),
            "effectiveStyleMap": str(active_settings.get("gptSovitsStyleMap") or ""),
            "candidateMode": bool(candidate_data.get("enabled")),
            "candidateConfigEnabled": bool(candidate_data.get("enabled")),
            "runtimeReplacementActive": bool(candidate_data.get("appliedMappings")),
            "candidateMappings": list(candidate_data.get("appliedMappings") or []),
            "blockedReplacements": list(candidate_data.get("blockedReplacements") or []),
            "choiceSets": choice_sets,
            "linkage": {
                "source": "effective_style_map",
                "automatic": True,
                "mappingCount": len({mapping_key for mapping_key, _, _ in effective_entries}),
                "note": "The workbench resolves current references from the same effective style map used by TTS.",
            },
            "stateContract": {
                "version": 1,
                "axes": [
                    "usageStatus",
                    "systemStatus",
                    "candidateRole",
                    "reviewWorkflowStatus",
                    "manualReviewStatus",
                    "mappingFitStatus",
                ],
                "manualReviewIndependent": True,
            },
            "reviewPath": str(self.paths.review_path),
            "references": references,
            "summary": {
                "total": len(references),
                "approved": sum(item["status"] == "approved" for item in references),
                "rejected": sum(item["status"] == "rejected" for item in references),
                "unreviewed": sum(item["status"] == "unreviewed" for item in references),
                "pendingRecheck": sum(item.get("recheckStatus") == "pending_recheck" for item in references),
                "recheckPassed": sum(item.get("recheckStatus") == "recheck_passed" for item in references),
                "recheckFailed": sum(item.get("recheckStatus") == "recheck_failed" for item in references),
                "highRisk": sum(item.get("attentionLevel") == "high_risk" for item in references),
                "auditionCandidates": sum(bool(item.get("isAuditionCandidate")) for item in references),
                "selectedChoices": sum(bool(item.get("isSelectedChoice")) for item in references),
                "effectiveReferences": sum(bool(item.get("isCurrentlyEffective")) for item in references),
                "active": sum(item.get("usageStatus") == "active" for item in references),
                "inactive": sum(item.get("usageStatus") == "inactive" for item in references),
                "systemEligible": sum(item.get("systemStatus") == "eligible" for item in references),
                "systemHighRisk": sum(item.get("systemStatus") == "high_risk" for item in references),
                "systemDisqualified": sum(item.get("systemStatus") == "disqualified" for item in references),
                "systemMissing": sum(item.get("systemStatus") == "missing" for item in references),
                "manualApproved": sum(item.get("manualReviewStatus") == "approved" for item in references),
                "manualRejected": sum(item.get("manualReviewStatus") == "rejected" for item in references),
                "manualUnreviewed": sum(item.get("manualReviewStatus") == "unreviewed" for item in references),
            },
            "updatedAt": reviews.get("updatedAt") or "",
        }


    def update_voice_reference_review(self, body: dict[str, Any]) -> dict[str, Any]:
        """持久化一条审核或试听选择，并返回按最新状态重建的目录。"""
        action = self._voice_reference_text(body.get("action"), 40).lower() or "upsert"
        if action == "select_candidate":
            choice_set_id = self._voice_reference_text(body.get("choiceSetId"), 160)
            candidate_id = self._voice_reference_text(body.get("candidateId"), 160)
            catalog = self.voice_reference_review_catalog()
            choice_set = next(
                (item for item in catalog.get("choiceSets") or [] if item.get("id") == choice_set_id),
                None,
            )
            valid_candidates = {
                str(item.get("id") or "")
                for item in (choice_set or {}).get("candidates") or []
                if item.get("selectionAllowed") is not False
            }
            if not choice_set or candidate_id not in valid_candidates:
                raise RequestValidationError(
                    "invalid_voice_reference_choice",
                    "当前参考音选择集中找不到这个可用的试听候选，或该候选已被系统判定为不合格。",
                )
            selections = self.read_voice_reference_choice_state()
            selections[choice_set_id] = candidate_id
            self.write_voice_reference_choice_state(selections)
            self.write_log(
                "voice_reference_candidate_selected",
                choiceSetId=choice_set_id,
                candidateId=candidate_id,
                runtimeStyleMapChanged=False,
            )
            return self.voice_reference_review_catalog()
        reference_id = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "-",
            self._voice_reference_text(body.get("id"), 120),
        ).strip(".-")
        catalog = self.voice_reference_review_catalog()
        reference = next((item for item in catalog["references"] if item["id"] == reference_id), None)
        if not reference:
            raise RequestValidationError("unknown_voice_reference", "当前声音预设中找不到这条参考音。")
        dataset = self.read_voice_reference_review_dataset()
        reference_scope = self._voice_reference_text(reference.get("reviewScope"), 500)
        reviews = [
            item
            for item in dataset["reviews"]
            if not (
                str(item.get("id") or "") == reference["id"]
                or (
                    reference_scope
                    and str(item.get("referenceKey") or "") == reference["referenceKey"]
                    and str(item.get("scopeKey") or "") == reference_scope
                )
            )
        ]
        if action == "reset":
            self.write_voice_reference_review_dataset(reviews)
            return self.voice_reference_review_catalog()
        if action != "upsert":
            raise RequestValidationError("invalid_voice_reference_action", "未知的参考音标注操作。")
        status = self._voice_reference_text(body.get("qualityStatus") or body.get("status"), 24).lower()
        if status not in VOICE_REFERENCE_REVIEW_STATUSES:
            raise RequestValidationError("invalid_voice_reference_status", "参考音标注状态无效。")
        mapping_fit_status = self._voice_reference_text(body.get("mappingFitStatus"), 24).lower()
        if mapping_fit_status not in VOICE_REFERENCE_MAPPING_FIT_STATUSES:
            if body.get("mappingFitStatus") not in (None, ""):
                raise RequestValidationError("invalid_voice_reference_mapping_fit", "参考音标签匹配状态无效。")
            mapping_fit_status = "unreviewed"
        raw_reason_codes = body.get("reasonCodes")
        reason_codes = [
            code
            for code in (raw_reason_codes if isinstance(raw_reason_codes, list) else [])
            if isinstance(code, str) and code in VOICE_REFERENCE_REVIEW_REASONS
        ][:12]
        hard_reason_codes = [code for code in reason_codes if code in VOICE_REFERENCE_HARD_REVIEW_REASONS]
        semantic_reason_codes = [
            code for code in reason_codes if code in VOICE_REFERENCE_SEMANTIC_REVIEW_REASONS
        ]
        if (
            body.get("qualityStatus") in (None, "")
            and status == "rejected"
            and semantic_reason_codes
            and not hard_reason_codes
            and set(reason_codes) <= VOICE_REFERENCE_SEMANTIC_REVIEW_REASONS
        ):
            # Compatibility with the old editor, which sent rejected whenever a
            # semantic mismatch checkbox was selected.
            status = "unreviewed"
        if hard_reason_codes:
            status = "rejected"
        elif status != "rejected":
            reason_codes = [
                code
                for code in reason_codes
                if code in VOICE_REFERENCE_SEMANTIC_REVIEW_REASONS
            ]
        if semantic_reason_codes:
            mapping_fit_status = "mismatched"
        elif mapping_fit_status != "mismatched":
            reason_codes = [
                code
                for code in reason_codes
                if code not in VOICE_REFERENCE_SEMANTIC_REVIEW_REASONS
            ]
        notes = self._voice_reference_text(body.get("notes"), 2000)
        emotion_label = self._voice_reference_text(body.get("emotionLabel"), 160)
        if (
            status != "unreviewed"
            or mapping_fit_status != "unreviewed"
            or emotion_label
            or reason_codes
            or notes
        ):
            reviews.append({
                "id": reference["id"],
                "referenceKey": reference["referenceKey"],
                "scopeKey": reference_scope,
                "mappingKeys": list(reference.get("mappingKeys") or []),
                "voiceId": reference["voiceId"],
                "audioPath": reference["audioPath"],
                "status": status,
                "qualityStatus": status,
                "mappingFitStatus": mapping_fit_status,
                "emotionLabel": emotion_label,
                "reasonCodes": reason_codes,
                "notes": notes,
                "updatedAt": self.now_iso(),
            })
        reviews.sort(key=lambda item: (item["voiceId"], item["referenceKey"]))
        self.write_voice_reference_review_dataset(reviews)
        self.write_log(
            "voice_reference_review_updated",
            referenceId=reference["id"],
            voiceId=reference["voiceId"],
            qualityStatus=status,
            mappingFitStatus=mapping_fit_status,
            affectedMappings=reference["mappingKeys"],
        )
        return self.voice_reference_review_catalog()


    def voice_reference_review_audio_path(self, reference_id: Any, variant: Any = "candidate") -> Path | None:
        """返回目录中可播放的候选或对照原音路径，不暴露不存在的文件。"""
        normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", self._voice_reference_text(reference_id, 120)).strip(".-")
        if not normalized:
            return None
        catalog = self.voice_reference_review_catalog()
        item = next((entry for entry in catalog["references"] if entry["id"] == normalized), None)
        if not item:
            return None
        normalized_variant = self._voice_reference_text(variant, 24).lower()
        if normalized_variant == "original" and item.get("isComparisonCandidate"):
            path = Path(str((item.get("replacement") or {}).get("original", {}).get("refAudio") or "")).resolve()
        else:
            path = Path(str(item.get("audioPath") or "")).resolve()
        return path if path.is_file() else None
