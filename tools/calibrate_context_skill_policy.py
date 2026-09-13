#!/usr/bin/env python3
"""Calibrate the Persona Skill hybrid policy against local, family-isolated cases.

The calibrator is deliberately offline and precision-first.  It searches on
the development split, evaluates the chosen point exactly once on held-out
families, and only publishes a calibrated policy when every release gate
passes.  Reports contain case IDs, candidate scores and gate diagnostics, but
never retrieval queries or capsule bodies.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass, replace
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.persona_context import build_retrieval_query  # noqa: E402
from agent.skill_retrieval import (  # noqa: E402
    INDEX_SCHEMA_VERSION,
    AssetValidationError,
    Embedder,
    HybridSkillRetriever,
    OnnxBgeSmallZhEmbedder,
    RetrievalPolicy,
    SkillRegistry,
    bm25_okapi_scores,
    cosine_similarity,
    lexical_tokens,
    load_runtime_registry,
    select_ranked_skill_ids,
)


REPORT_SCHEMA_VERSION = 2
MIN_HELD_OUT_PRECISION = 0.95
MIN_HELD_OUT_NO_SKILL_ACCURACY = 0.95
MIN_HELD_OUT_RECALL = 0.80


class CalibrationError(RuntimeError):
    """The calibration inputs or score computation are not trustworthy."""


@dataclass(frozen=True)
class CalibrationCase:
    case_id: str
    split: str
    paraphrase_family: str
    query: str
    recent: tuple[Mapping[str, Any], ...]
    resolution: Mapping[str, Any] | None
    expected_skills: tuple[str, ...]
    critical_negative: bool
    critical_negative_for: str | None

    def retrieval_query(self) -> str:
        return build_retrieval_query(
            self.query,
            messages=self.recent,
            resolution=self.resolution,
        )


@dataclass(frozen=True)
class GridPoint:
    bm25_scale: float
    dense_floor: float
    bm25_weight: float
    dense_weight: float
    min_dense: float
    min_score: float
    min_margin: float

    def policy_dict(self) -> dict[str, Any]:
        return {
            "fusion": {
                "bm25Scale": self.bm25_scale,
                "denseFloor": self.dense_floor,
                "bm25Weight": self.bm25_weight,
                "denseWeight": self.dense_weight,
            },
            "gates": {
                "minDense": self.min_dense,
                "minScore": self.min_score,
                "minMargin": self.min_margin,
            },
        }


@dataclass(frozen=True)
class CalibrationGrid:
    bm25_scale: tuple[float, ...] = (0.75, 1.0, 2.0, 3.0)
    dense_floor: tuple[float, ...] = (0.0, 0.2, 0.35, 0.5)
    bm25_weight: tuple[float, ...] = (0.1, 0.2, 0.35, 0.5)
    dense_weight: tuple[float, ...] = (0.5, 0.65, 0.8, 0.9)
    min_dense: tuple[float, ...] = (0.5, 0.52, 0.54, 0.55, 0.6, 0.7, 0.72, 0.8)
    min_score: tuple[float, ...] = (0.3, 0.35, 0.36, 0.4, 0.5, 0.6, 0.68, 0.75)
    min_margin: tuple[float, ...] = (0.03, 0.05, 0.1, 0.12, 0.2)

    def points(self) -> tuple[GridPoint, ...]:
        axes = (
            _sorted_finite(self.bm25_scale, "bm25Scale"),
            _sorted_finite(self.dense_floor, "denseFloor"),
            _sorted_finite(self.bm25_weight, "bm25Weight"),
            _sorted_finite(self.dense_weight, "denseWeight"),
            _sorted_finite(self.min_dense, "minDense"),
            _sorted_finite(self.min_score, "minScore"),
            _sorted_finite(self.min_margin, "minMargin"),
        )
        points: list[GridPoint] = []
        for values in itertools.product(*axes):
            point = GridPoint(*values)
            if not math.isclose(
                point.bm25_weight + point.dense_weight,
                1.0,
                rel_tol=1e-9,
                abs_tol=1e-9,
            ):
                continue
            if point.bm25_scale <= 0 or not -1.0 <= point.dense_floor < 1.0:
                continue
            if point.bm25_weight < 0 or point.dense_weight <= 0:
                continue
            if not -1.0 <= point.min_dense <= 1.0:
                continue
            if not 0.0 <= point.min_score <= 1.0:
                continue
            if not 0.0 <= point.min_margin <= 1.0:
                continue
            points.append(point)
        if not points:
            raise CalibrationError("calibration_grid_empty")
        return tuple(points)


@dataclass(frozen=True)
class _RawCandidate:
    skill_id: str
    bm25_score: float
    embedding_score: float


@dataclass(frozen=True)
class _PreparedCase:
    case: CalibrationCase
    retrieval_query: str
    candidates: tuple[_RawCandidate, ...]


def _sorted_finite(values: Sequence[float], name: str) -> tuple[float, ...]:
    result = tuple(sorted({float(item) for item in values}))
    if not result or not all(math.isfinite(item) for item in result):
        raise CalibrationError(f"calibration_grid_invalid:{name}")
    return result


def _load_json_object(path: Path, error_prefix: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise CalibrationError(f"{error_prefix}_missing") from error
    except (OSError, json.JSONDecodeError) as error:
        raise CalibrationError(f"{error_prefix}_invalid_json") from error
    if not isinstance(value, dict):
        raise CalibrationError(f"{error_prefix}_invalid_shape")
    return value


def load_calibration_cases(path: Path) -> tuple[CalibrationCase, ...]:
    """Load cases and reject paraphrase-family leakage across split boundaries."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as error:
        raise CalibrationError("calibration_cases_missing") from error
    except OSError as error:
        raise CalibrationError("calibration_cases_unreadable") from error
    cases: list[CalibrationCase] = []
    seen_ids: set[str] = set()
    family_split: dict[str, str] = {}
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            raise CalibrationError(
                f"calibration_case_invalid_json:{line_number}"
            ) from error
        if not isinstance(raw, dict):
            raise CalibrationError(f"calibration_case_invalid_shape:{line_number}")
        case_id = str(raw.get("id") or "").strip()
        split = str(raw.get("split") or "").strip()
        family = str(raw.get("paraphraseFamily") or "").strip()
        query = str(raw.get("query") or "").strip()
        if not case_id or not family or not query or split not in {"dev", "held_out"}:
            raise CalibrationError(f"calibration_case_required_field:{line_number}")
        if case_id in seen_ids:
            raise CalibrationError(f"calibration_case_duplicate_id:{case_id}")
        seen_ids.add(case_id)
        prior_split = family_split.setdefault(family, split)
        if prior_split != split:
            raise CalibrationError(f"paraphrase_family_split_leakage:{family}")
        expected_raw = raw.get("expectedSkills") if "expectedSkills" in raw else (
            [] if raw.get("expectedSkill") is None else [raw["expectedSkill"]]
        )
        if not isinstance(expected_raw, list) or any(
            not isinstance(value, str) or not value.strip() for value in expected_raw
        ):
            raise CalibrationError(f"calibration_case_expected_skill_invalid:{case_id}")
        expected = tuple(sorted(value.strip() for value in expected_raw))
        if len(set(expected)) != len(expected) or len(expected) > 3:
            raise CalibrationError(f"calibration_case_expected_skill_invalid:{case_id}")
        recent_raw = raw.get("recent") or []
        if not isinstance(recent_raw, list) or not all(
            isinstance(item, dict) for item in recent_raw
        ):
            raise CalibrationError(f"calibration_case_recent_invalid:{case_id}")
        resolution_raw = raw.get("resolution")
        if resolution_raw is not None and not isinstance(resolution_raw, dict):
            raise CalibrationError(f"calibration_case_resolution_invalid:{case_id}")
        critical_negative = bool(raw.get("criticalNegative", False))
        critical_for_raw = raw.get("criticalNegativeFor")
        critical_for = (
            str(critical_for_raw).strip() if critical_for_raw is not None else None
        )
        if critical_negative and expected:
            raise CalibrationError(f"critical_negative_must_expect_none:{case_id}")
        if critical_for_raw is not None and not critical_for:
            raise CalibrationError(f"critical_negative_for_invalid:{case_id}")
        cases.append(CalibrationCase(
            case_id=case_id,
            split=split,
            paraphrase_family=family,
            query=query,
            recent=tuple(dict(item) for item in recent_raw),
            resolution=dict(resolution_raw) if resolution_raw is not None else None,
            expected_skills=expected,
            critical_negative=critical_negative,
            critical_negative_for=critical_for,
        ))
    if not cases:
        raise CalibrationError("calibration_cases_empty")
    dev = tuple(item for item in cases if item.split == "dev")
    held_out = tuple(item for item in cases if item.split == "held_out")
    if not dev or not held_out:
        raise CalibrationError("calibration_split_missing")
    if not any(item.expected_skills for item in dev):
        raise CalibrationError("calibration_dev_positive_missing")
    if not any(item.expected_skills for item in held_out):
        raise CalibrationError("calibration_held_out_positive_missing")
    if not any(not item.expected_skills for item in held_out):
        raise CalibrationError("calibration_held_out_no_skill_missing")
    return tuple(cases)


def _normalize_vector(value: Sequence[float], dimension: int) -> tuple[float, ...]:
    try:
        vector = tuple(float(item) for item in value)
    except (TypeError, ValueError) as error:
        raise CalibrationError("calibration_embedding_invalid") from error
    if len(vector) != dimension or dimension <= 0:
        raise CalibrationError("calibration_embedding_dimension_mismatch")
    if not all(math.isfinite(item) for item in vector):
        raise CalibrationError("calibration_embedding_non_finite")
    magnitude = math.sqrt(sum(item * item for item in vector))
    if not math.isfinite(magnitude) or magnitude <= 1e-12:
        raise CalibrationError("calibration_embedding_zero_norm")
    return tuple(item / magnitude for item in vector)


def _prepare_cases(
    cases: Sequence[CalibrationCase],
    registry: SkillRegistry,
    embedder: Embedder,
    base_policy: RetrievalPolicy,
) -> tuple[_PreparedCase, ...]:
    if (
        embedder.model_id != registry.model_id
        or embedder.revision != registry.model_revision
        or embedder.dimension != registry.embedding_dim
    ):
        raise CalibrationError("calibration_model_binding_mismatch")
    queries = tuple(item.retrieval_query() for item in cases)
    if any(not item for item in queries):
        raise CalibrationError("calibration_query_empty")
    try:
        # Production encodes one query per turn. Quantized ONNX inference can
        # depend on batch padding, so calibrate using that same batch shape.
        batches = tuple(embedder.embed((query,)) for query in queries)
        if any(len(batch) != 1 for batch in batches):
            raise CalibrationError("calibration_embedding_batch_shape_invalid")
        raw_vectors = tuple(batch[0] for batch in batches)
    except Exception as error:
        raise CalibrationError(
            f"calibration_embedding_failed:{type(error).__name__}"
        ) from error
    if len(raw_vectors) != len(cases):
        raise CalibrationError("calibration_embedding_batch_shape_invalid")
    documents = tuple(entry.bm25_tokens for entry in registry.entries)
    dense_vectors = tuple(
        tuple(
            _normalize_vector(vector, registry.embedding_dim)
            for vector in entry.dense_vectors
        )
        for entry in registry.entries
    )
    prepared: list[_PreparedCase] = []
    for case, query, raw_vector in zip(cases, queries, raw_vectors):
        query_vector = _normalize_vector(raw_vector, registry.embedding_dim)
        bm25_values = bm25_okapi_scores(
            lexical_tokens(query),
            documents,
            k1=base_policy.bm25_k1,
            b=base_policy.bm25_b,
        )
        raw_candidates = tuple(
            _RawCandidate(
                skill_id=entry.meta.skill_id,
                bm25_score=float(bm25_score),
                embedding_score=max(
                    cosine_similarity(query_vector, exemplar)
                    for exemplar in dense_vector
                ),
            )
            for entry, dense_vector, bm25_score in zip(
                registry.entries,
                dense_vectors,
                bm25_values,
            )
        )
        prepared.append(_PreparedCase(case, query, raw_candidates))
    return tuple(prepared)


def _prediction_for_point(
    prepared: _PreparedCase,
    point: GridPoint,
    max_selected: int,
) -> tuple[str, ...]:
    ranked: list[tuple[float, str, float]] = []
    for item in prepared.candidates:
        bm25_normalized = item.bm25_score / (item.bm25_score + point.bm25_scale)
        dense_normalized = max(0.0, min(
            1.0,
            (item.embedding_score - point.dense_floor) / (1.0 - point.dense_floor),
        ))
        fused = (
            point.bm25_weight * bm25_normalized
            + point.dense_weight * dense_normalized
        )
        if not all(math.isfinite(value) for value in (
            bm25_normalized,
            dense_normalized,
            fused,
        )):
            raise CalibrationError("calibration_candidate_non_finite")
        ranked.append((fused, item.skill_id, item.embedding_score))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return select_ranked_skill_ids(
        ranked, min_dense=point.min_dense, min_score=point.min_score,
        min_margin=point.min_margin, max_selected=max_selected,
    )[0]


def _metrics(
    predictions: Sequence[tuple[CalibrationCase, tuple[str, ...]]],
) -> dict[str, Any]:
    expected_positive = sum(len(item.expected_skills) for item, _ in predictions)
    predicted_positive = sum(len(predicted) for _, predicted in predictions)
    correct_positive = sum(
        len(set(item.expected_skills).intersection(predicted))
        for item, predicted in predictions
    )
    no_skill_count = sum(not item.expected_skills for item, _ in predictions)
    no_skill_correct = sum(
        not item.expected_skills and not predicted
        for item, predicted in predictions
    )
    exact_correct = sum(set(item.expected_skills) == set(predicted) for item, predicted in predictions)
    critical_count = sum(
        item.critical_negative or item.critical_negative_for is not None
        for item, _ in predictions
    )
    critical_false_positive = 0
    for item, predicted in predictions:
        hit = item.critical_negative and bool(predicted)
        if item.critical_negative_for is not None:
            hit = hit or item.critical_negative_for in predicted
        critical_false_positive += int(hit)
    precision = (
        correct_positive / predicted_positive
        if predicted_positive
        else (1.0 if expected_positive == 0 else 0.0)
    )
    recall = correct_positive / expected_positive if expected_positive else 1.0
    no_skill_accuracy = no_skill_correct / no_skill_count if no_skill_count else 1.0
    case_count = len(predictions)
    return {
        "caseCount": case_count,
        "expectedPositiveCount": expected_positive,
        "predictedPositiveCount": predicted_positive,
        "truePositiveCount": correct_positive,
        "falsePositiveCount": predicted_positive - correct_positive,
        "falseNegativeCount": expected_positive - correct_positive,
        "noSkillCount": no_skill_count,
        "noSkillCorrectCount": no_skill_correct,
        "criticalNegativeCount": critical_count,
        "criticalFalsePositiveCount": critical_false_positive,
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "noSkillAccuracy": round(no_skill_accuracy, 6),
        "exactAccuracy": round(exact_correct / case_count, 6) if case_count else 0.0,
    }


def _meets_release_metrics(metrics: Mapping[str, Any]) -> bool:
    return bool(
        int(metrics["criticalFalsePositiveCount"]) == 0
        and float(metrics["precision"]) >= MIN_HELD_OUT_PRECISION
        and float(metrics["noSkillAccuracy"]) >= MIN_HELD_OUT_NO_SKILL_ACCURACY
        and float(metrics["recall"]) >= MIN_HELD_OUT_RECALL
    )


def _policy_for_point(
    base: RetrievalPolicy,
    registry: SkillRegistry,
    point: GridPoint,
) -> RetrievalPolicy:
    return replace(
        base,
        calibrated=True,
        model_id=registry.model_id,
        model_revision=registry.model_revision,
        model_artifact_digest=registry.model_artifact_digest,
        index_build_id=registry.build_id,
        source_digest=registry.source_digest,
        bm25_scale=point.bm25_scale,
        dense_floor=point.dense_floor,
        bm25_weight=point.bm25_weight,
        dense_weight=point.dense_weight,
        min_dense=point.min_dense,
        min_score=point.min_score,
        min_margin=point.min_margin,
    )


def _point_sort_key(
    point: GridPoint,
    metrics: Mapping[str, Any],
) -> tuple[Any, ...]:
    """Precision-first objective with deterministic strict-gate tie breaking."""

    eligible = _meets_release_metrics(metrics)
    return (
        int(eligible),
        int(int(metrics["criticalFalsePositiveCount"]) == 0),
        float(metrics["precision"]),
        float(metrics["noSkillAccuracy"]),
        float(metrics["recall"]),
        float(metrics["exactAccuracy"]),
        -int(metrics["falsePositiveCount"]),
        -int(metrics["falseNegativeCount"]),
        point.min_margin,
        point.min_score,
        point.min_dense,
        point.dense_weight,
        -point.bm25_scale,
        point.dense_floor,
        -point.bm25_weight,
    )


def _safe_case_report(
    prepared: _PreparedCase,
    predicted: tuple[str, ...],
    result: Any,
    policy: RetrievalPolicy,
) -> dict[str, Any]:
    candidates = tuple(result.candidates)
    selected_count = len(predicted)
    boundary_index = max(0, selected_count - 1)
    boundary = candidates[boundary_index] if len(candidates) > boundary_index else None
    outside_score = candidates[boundary_index + 1].fused_score if len(candidates) > boundary_index + 1 else 0.0
    margin = boundary.fused_score - outside_score if boundary is not None else None
    case = prepared.case
    expected_set, predicted_set = set(case.expected_skills), set(predicted)
    critical_hit = bool(case.critical_negative and predicted)
    if case.critical_negative_for is not None:
        critical_hit = critical_hit or case.critical_negative_for in predicted
    return {
        "caseId": case.case_id,
        "split": case.split,
        "paraphraseFamily": case.paraphrase_family,
        "expectedSkills": list(case.expected_skills),
        "predictedSkills": list(predicted),
        "correct": predicted_set == expected_set,
        "criticalNegative": case.critical_negative,
        "criticalNegativeFor": case.critical_negative_for,
        "criticalFalsePositive": critical_hit,
        "fallbackReason": result.fallback_reason,
        "metrics": {
            "truePositive": len(expected_set & predicted_set),
            "falsePositive": len(predicted_set - expected_set),
            "falseNegative": len(expected_set - predicted_set),
            "noSkillCorrect": not expected_set and not predicted_set,
            "criticalFalsePositive": critical_hit,
        },
        "candidates": [item.diagnostic_dict() for item in candidates],
        "gates": {
            "minDense": {"minimum": policy.min_dense},
            "minScore": {"minimum": policy.min_score},
            "minMargin": {
                "scope": "selected_group_vs_first_excluded",
                "value": round(margin, 6) if margin is not None else None,
                "minimum": policy.min_margin,
                "passed": bool(margin is not None and margin >= policy.min_margin),
            },
            "maxSelected": policy.max_selected,
        },
    }


def evaluate_calibration(
    *,
    raw_policy: Mapping[str, Any],
    cases: Sequence[CalibrationCase],
    cases_sha256: str,
    registry: SkillRegistry,
    embedder: Embedder,
    grid: CalibrationGrid | None = None,
) -> dict[str, Any]:
    """Return a deterministic calibration report without writing any files."""

    try:
        policy_seed = dict(raw_policy)
        policy_seed["calibrationStatus"] = "pending_recalibration"
        base_policy = RetrievalPolicy.from_dict(policy_seed)
    except (AssetValidationError, TypeError, ValueError) as error:
        raise CalibrationError(f"calibration_policy_invalid:{error}") from error
    if not registry.entries:
        raise CalibrationError("calibration_registry_empty")
    skill_ids = {entry.meta.skill_id for entry in registry.entries}
    for case in cases:
        if not set(case.expected_skills).issubset(skill_ids):
            raise CalibrationError(
                f"calibration_expected_skill_unknown:{case.case_id}"
            )
        if (
            case.critical_negative_for is not None
            and case.critical_negative_for not in skill_ids
        ):
            raise CalibrationError(
                f"calibration_critical_skill_unknown:{case.case_id}"
            )
    prepared = _prepare_cases(cases, registry, embedder, base_policy)
    active_grid = grid or CalibrationGrid()
    points = active_grid.points()
    dev_prepared = tuple(item for item in prepared if item.case.split == "dev")
    best_point: GridPoint | None = None
    best_metrics: dict[str, Any] | None = None
    best_key: tuple[Any, ...] | None = None
    for point in points:
        predictions = tuple(
            (item.case, _prediction_for_point(item, point, base_policy.max_selected))
            for item in dev_prepared
        )
        metrics = _metrics(predictions)
        key = _point_sort_key(point, metrics)
        if best_key is None or key > best_key:
            best_point = point
            best_metrics = metrics
            best_key = key
    if best_point is None or best_metrics is None:
        raise CalibrationError("calibration_search_no_result")

    selected_policy = _policy_for_point(base_policy, registry, best_point)
    try:
        selected_policy.validate()
    except AssetValidationError as error:
        raise CalibrationError(f"calibration_selected_policy_invalid:{error}") from error

    retriever = HybridSkillRetriever(embedder)
    case_reports: list[dict[str, Any]] = []
    final_predictions: list[tuple[CalibrationCase, tuple[str, ...]]] = []
    for item in prepared:
        result = retriever.retrieve(item.retrieval_query, registry, selected_policy)
        predicted = tuple(skill.skill_id for skill in result.selected)
        final_predictions.append((item.case, predicted))
        case_reports.append(
            _safe_case_report(item, predicted, result, selected_policy)
        )

    dev_predictions = tuple(
        item for item in final_predictions if item[0].split == "dev"
    )
    held_predictions = tuple(
        item for item in final_predictions if item[0].split == "held_out"
    )
    dev_metrics = _metrics(dev_predictions)
    held_metrics = _metrics(held_predictions)
    overall_metrics = _metrics(tuple(final_predictions))
    # The report path uses the production retriever.  Any drift from the
    # equivalent grid scorer invalidates the calibration instead of silently
    # publishing thresholds evaluated under different semantics.
    if dev_metrics != best_metrics:
        raise CalibrationError("calibration_retriever_score_drift")
    dev_passed = _meets_release_metrics(dev_metrics)
    held_passed = _meets_release_metrics(held_metrics)
    all_critical_passed = int(overall_metrics["criticalFalsePositiveCount"]) == 0
    release_passed = bool(dev_passed and held_passed and all_critical_passed)
    family_split: dict[str, list[str]] = {"dev": [], "held_out": []}
    for case in cases:
        family_split[case.split].append(case.paraphrase_family)
    family_split = {
        split: sorted(set(families))
        for split, families in sorted(family_split.items())
    }
    case_reports.sort(key=lambda item: (item["split"], item["caseId"]))
    return {
        "schemaVersion": REPORT_SCHEMA_VERSION,
        "calibrator": "persona-context-hybrid-multilabel-v2",
        "casesSha256": cases_sha256,
        "bindings": {
            "modelId": registry.model_id,
            "modelRevision": registry.model_revision,
            "modelArtifactDigest": registry.model_artifact_digest,
            "indexBuildId": registry.build_id,
            "sourceDigest": registry.source_digest,
            "registryGeneration": registry.generation,
        },
        "familySplit": family_split,
        "search": {
            "selectionSplit": "dev",
            "gridPointCount": len(points),
            "selectedPolicy": {**best_point.policy_dict(), "maxSelected": base_policy.max_selected, "topK": base_policy.top_k},
        },
        "metrics": {
            "dev": dev_metrics,
            "heldOut": held_metrics,
            "overall": overall_metrics,
        },
        "releaseGate": {
            "criticalFalsePositiveMaximum": 0,
            "minimumHeldOutPrecision": MIN_HELD_OUT_PRECISION,
            "minimumHeldOutNoSkillAccuracy": MIN_HELD_OUT_NO_SKILL_ACCURACY,
            "minimumHeldOutRecall": MIN_HELD_OUT_RECALL,
            "developmentPassed": dev_passed,
            "heldOutPassed": held_passed,
            "allCriticalNegativesPassed": all_critical_passed,
            "passed": release_passed,
        },
        "cases": case_reports,
    }


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _calibrated_policy(
    raw_policy: Mapping[str, Any],
    report: Mapping[str, Any],
) -> dict[str, Any]:
    result = copy.deepcopy(dict(raw_policy))
    bindings = report["bindings"]
    selected = report["search"]["selectedPolicy"]
    result["calibrationStatus"] = "calibrated"
    result["modelId"] = bindings["modelId"]
    result["modelRevision"] = bindings["modelRevision"]
    result["modelArtifactDigest"] = bindings["modelArtifactDigest"]
    result["indexBuildId"] = bindings["indexBuildId"]
    result["sourceDigest"] = bindings["sourceDigest"]
    result["maxSelected"] = selected["maxSelected"]
    result["topK"] = selected["topK"]
    fusion = result.get("fusion") if isinstance(result.get("fusion"), dict) else {}
    fusion.update(selected["fusion"])
    result["fusion"] = fusion
    gates = result.get("gates") if isinstance(result.get("gates"), dict) else {}
    gates.update(selected["gates"])
    result["gates"] = gates
    result["calibration"] = {
        "toolSchemaVersion": REPORT_SCHEMA_VERSION,
        "casesSha256": report["casesSha256"],
        "metrics": report["metrics"],
        "releaseGate": report["releaseGate"],
    }
    result["notes"] = (
        "Hybrid 阈值只用 paraphraseFamily 隔离的 dev split 选择，并由 held-out split 验证；"
        "多选时每项须通过绝对分数门槛，minMargin 比较入选组与首个未选项；"
        "按完整 Skill 集合计算误选与漏选。模型、索引、来源或选择规则变化后必须重新校准。"
    )
    try:
        RetrievalPolicy.from_dict(result)
    except (AssetValidationError, TypeError, ValueError) as error:
        raise CalibrationError(f"calibrated_policy_invalid:{error}") from error
    return result


def calibrate_policy(
    *,
    policy_path: Path,
    cases_path: Path,
    registry: SkillRegistry,
    embedder: Embedder,
    grid: CalibrationGrid | None = None,
    dry_run: bool = False,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """Evaluate and optionally publish an exactly bound calibrated policy."""

    raw_policy = _load_json_object(policy_path, "calibration_policy")
    try:
        case_bytes = cases_path.read_bytes()
    except OSError as error:
        raise CalibrationError("calibration_cases_unreadable") from error
    cases = load_calibration_cases(cases_path)
    report = evaluate_calibration(
        raw_policy=raw_policy,
        cases=cases,
        cases_sha256=hashlib.sha256(case_bytes).hexdigest(),
        registry=registry,
        embedder=embedder,
        grid=grid,
    )
    policy_written = False
    if bool(report["releaseGate"]["passed"]) and not dry_run:
        _atomic_json(policy_path, _calibrated_policy(raw_policy, report))
        policy_written = True
    final_report = {
        **report,
        "write": {
            "dryRun": bool(dry_run),
            "policyWritten": policy_written,
            "reportWritten": bool(report_path is not None and not dry_run),
        },
    }
    if report_path is not None and not dry_run:
        _atomic_json(report_path, final_report)
    return final_report


def _project_path(root: Path, value: str | Path, error: str) -> Path:
    candidate_value = Path(value)
    candidate = (
        candidate_value.resolve()
        if candidate_value.is_absolute()
        else (root / candidate_value).resolve()
    )
    try:
        candidate.relative_to(root.resolve())
    except ValueError as cause:
        raise CalibrationError(error) from cause
    return candidate


def _load_real_assets(
    *,
    root: Path,
    policy_path: Path,
    index_override: Path | None,
    embeddings_override: Path | None,
    model_root_override: Path | None,
) -> tuple[SkillRegistry, OnnxBgeSmallZhEmbedder]:
    raw_policy = _load_json_object(policy_path, "calibration_policy")
    assets = raw_policy.get("assets") if isinstance(raw_policy.get("assets"), dict) else {}
    index_path = _project_path(
        root,
        index_override or str(assets.get("index") or ""),
        "calibration_index_path_outside_root",
    )
    embeddings_path = _project_path(
        root,
        embeddings_override or str(assets.get("embeddings") or ""),
        "calibration_embeddings_path_outside_root",
    )
    model_root = _project_path(
        root,
        model_root_override or str(assets.get("embeddingRoot") or ""),
        "calibration_model_path_outside_root",
    )
    raw_index = _load_json_object(index_path, "calibration_index")
    if int(raw_index.get("schemaVersion") or 0) != INDEX_SCHEMA_VERSION:
        raise CalibrationError("calibration_index_schema_mismatch")
    embedding = (
        raw_index.get("embedding")
        if isinstance(raw_index.get("embedding"), dict)
        else {}
    )
    try:
        # A stale calibrated binding is expected immediately after rebuilding
        # an index.  Parse its thresholds as an explicitly uncalibrated seed,
        # then bind the current, independently validated assets below.
        policy_seed = dict(raw_policy)
        policy_seed["calibrationStatus"] = "pending_recalibration"
        base_policy = RetrievalPolicy.from_dict(policy_seed)
        bound_policy = replace(
            base_policy,
            calibrated=True,
            model_id=str(embedding.get("modelId") or ""),
            model_revision=str(embedding.get("modelRevision") or ""),
            model_artifact_digest=str(embedding.get("artifactDigest") or ""),
            index_build_id=str(raw_index.get("buildId") or ""),
            source_digest=str(raw_index.get("sourceDigest") or ""),
        )
        bound_policy.validate()
        embedder = OnnxBgeSmallZhEmbedder.load(model_root)
        registry = load_runtime_registry(
            skills_root=root / "skills" / "persona",
            index_path=index_path,
            embeddings_path=embeddings_path,
            policy=bound_policy,
            model_artifact_digest=embedder.artifact_digest,
        )
    except AssetValidationError as error:
        raise CalibrationError(str(error)) from error
    if (
        embedder.model_id != registry.model_id
        or embedder.revision != registry.model_revision
        or embedder.dimension != registry.embedding_dim
    ):
        raise CalibrationError("calibration_model_binding_mismatch")
    return registry, embedder


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline precision-first calibration for Persona Context Skills."
    )
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--policy", type=Path, default=Path("configs/context_skill_policy.json"))
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path("configs/context_skill_retrieval_cases.jsonl"),
    )
    parser.add_argument("--index", type=Path)
    parser.add_argument("--embeddings", type=Path)
    parser.add_argument("--model-root", type=Path)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("data/runtime/context_skill_calibration_report.json"),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        policy_path = _project_path(
            root, args.policy, "calibration_policy_path_outside_root"
        )
        cases_path = _project_path(
            root, args.cases, "calibration_cases_path_outside_root"
        )
        report_path = _project_path(
            root, args.report, "calibration_report_path_outside_root"
        )
        registry, embedder = _load_real_assets(
            root=root,
            policy_path=policy_path,
            index_override=args.index,
            embeddings_override=args.embeddings,
            model_root_override=args.model_root,
        )
        report = calibrate_policy(
            policy_path=policy_path,
            cases_path=cases_path,
            registry=registry,
            embedder=embedder,
            dry_run=args.dry_run,
            report_path=report_path,
        )
    except CalibrationError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, **report}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(report["releaseGate"]["passed"]) else 3


if __name__ == "__main__":
    raise SystemExit(main())
