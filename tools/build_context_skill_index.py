#!/usr/bin/env python3
"""Build the immutable Persona Skill BM25+dense index from local assets only.

This tool never downloads a model.  It writes the embedding matrix first and
the JSON index (the commit marker) last; each file is replaced atomically from
the same directory.  A process that observes a mixed pair rejects the SHA and
fails closed instead of using stale or partial data.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.skill_retrieval import (  # noqa: E402
    INDEX_SCHEMA_VERSION,
    TOKENIZER_VERSION,
    AssetValidationError,
    OnnxBgeSmallZhEmbedder,
    RetrievalPolicy,
    lexical_tokens,
    parse_skill_manifest,
    persona_index_build_id,
    sha256_file,
    skill_source_digest,
)


def _project_path(root: Path, value: str) -> Path:
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as error:
        raise AssetValidationError("output_path_outside_project") from error
    return candidate


def _atomic_json(path: Path, value: dict) -> None:
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


def build_index(root: Path = PROJECT_ROOT) -> dict[str, object]:
    root = root.resolve()
    policy_path = root / "configs" / "context_skill_policy.json"
    try:
        raw_policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AssetValidationError("policy_invalid_json") from error
    if not isinstance(raw_policy, dict):
        raise AssetValidationError("policy_invalid_shape")
    assets = raw_policy.get("assets") if isinstance(raw_policy.get("assets"), dict) else {}
    embedding_root = _project_path(
        root,
        str(assets.get("embeddingRoot") or "assets/context-embedding/bge-small-zh-v1.5"),
    )
    index_path = _project_path(
        root,
        str(assets.get("index") or "assets/context-skills/persona-index.v2.json"),
    )
    embeddings_path = _project_path(
        root,
        str(assets.get("embeddings") or "assets/context-skills/persona-embeddings.v2.npy"),
    )
    embedder = OnnxBgeSmallZhEmbedder.load(embedding_root)
    if (
        embedder.model_id != str(raw_policy.get("modelId") or "")
        or embedder.revision != str(raw_policy.get("modelRevision") or "")
    ):
        raise AssetValidationError("policy_model_binding_mismatch")
    skills_root = root / "skills" / "persona"
    manifest_paths = sorted(skills_root.glob("*/manifest.json"), key=lambda item: item.parent.name)
    if not manifest_paths:
        raise AssetValidationError("skill_sources_missing")
    metadata = [parse_skill_manifest(path) for path in manifest_paths]
    if len({item.skill_id for item in metadata}) != len(metadata):
        raise AssetValidationError("duplicate_skill_id")
    capsule_paths = [path.parent / "SKILL.txt" for path in manifest_paths]
    capsule_bodies = [path.read_text(encoding="utf-8") for path in capsule_paths]
    if any(not body.strip() for body in capsule_bodies):
        raise AssetValidationError("skill_capsule_empty")
    route_examples: list[str] = []
    embedding_rows_by_skill: list[list[int]] = []
    for meta in metadata:
        examples = tuple(item.strip() for item in meta.positive_examples if item.strip())
        if not examples:
            raise AssetValidationError(f"skill_positive_examples_missing:{meta.skill_id}")
        rows = list(range(len(route_examples), len(route_examples) + len(examples)))
        route_examples.extend(examples)
        embedding_rows_by_skill.append(rows)
    vectors = embedder.embed(tuple(route_examples))
    if len(vectors) != len(route_examples):
        raise AssetValidationError("embedding_matrix_shape_mismatch")
    try:
        import numpy as np  # type: ignore
    except ImportError as error:
        raise AssetValidationError("dependency_missing:numpy") from error
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.shape != (len(route_examples), embedder.dimension) or not bool(np.isfinite(matrix).all()):
        raise AssetValidationError("embedding_matrix_invalid")
    norms = np.linalg.norm(matrix, axis=1)
    if bool((norms <= 1e-12).any()) or not bool(np.isfinite(norms).all()):
        raise AssetValidationError("embedding_matrix_invalid")
    matrix = matrix / norms[:, None]
    embeddings_path.parent.mkdir(parents=True, exist_ok=True)
    matrix_temporary = embeddings_path.with_name(
        f".{embeddings_path.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with matrix_temporary.open("wb") as handle:
            np.save(handle, matrix, allow_pickle=False)
            handle.flush()
            os.fsync(handle.fileno())
        matrix_hash = sha256_file(matrix_temporary)
        source_digest = skill_source_digest(skills_root)
        entries = []
        for meta, capsule_path, body, embedding_rows in zip(
            metadata, capsule_paths, capsule_bodies, embedding_rows_by_skill
        ):
            entries.append({
                "id": meta.skill_id,
                "version": meta.version,
                "name": meta.name,
                "description": meta.description,
                "aliases": list(meta.aliases),
                "positiveExamples": list(meta.positive_examples),
                "negativeExamples": list(meta.negative_examples),
                "sourceRefs": list(meta.source_refs),
                "capsulePath": capsule_path.relative_to(root).as_posix(),
                "capsuleSha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "bm25Tokens": list(lexical_tokens(meta.retrieval_document)),
                "embeddingRows": embedding_rows,
            })
        routing_material = json.dumps(
            entries,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        routing_digest = hashlib.sha256(routing_material.encode("utf-8")).hexdigest()
        build_id = persona_index_build_id(
            source_digest=source_digest,
            model_id=embedder.model_id,
            model_revision=embedder.revision,
            model_artifact_digest=embedder.artifact_digest,
            embedding_sha256=matrix_hash,
            tokenizer_version=TOKENIZER_VERSION,
            routing_digest=routing_digest,
        )
        index = {
            "schemaVersion": INDEX_SCHEMA_VERSION,
            "buildId": build_id,
            "sourceDigest": source_digest,
            "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tokenizerVersion": TOKENIZER_VERSION,
            "routingDigest": routing_digest,
            "bm25": raw_policy.get("bm25") or {"k1": 1.2, "b": 0.75},
            "embedding": {
                "modelId": embedder.model_id,
                "modelRevision": embedder.revision,
                "artifactDigest": embedder.artifact_digest,
                "dimension": embedder.dimension,
                "pooling": "CLS",
                "normalized": True,
                "file": embeddings_path.relative_to(root).as_posix(),
                "sha256": matrix_hash,
            },
            "entries": entries,
        }
        # Publish data first; the hash-bound index is the atomic commit marker.
        os.replace(matrix_temporary, embeddings_path)
        _atomic_json(index_path, index)
        return {
            "buildId": build_id,
            "sourceDigest": source_digest,
            "entryCount": len(entries),
            "embeddingDimension": embedder.dimension,
            "indexPath": str(index_path),
            "embeddingsPath": str(embeddings_path),
            "policyStillRequiresCalibration": True,
        }
    finally:
        try:
            matrix_temporary.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--index-only", action="store_true", help="Build an index for manual policy recalibration")
    args = parser.parse_args()
    try:
        result = build_index(args.root) if args.index_only else build_calibrated_index(args.root)
    except AssetValidationError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


def build_calibrated_index(root: Path = PROJECT_ROOT) -> dict[str, object]:
    """Refresh assets together, retaining the existing selection thresholds."""
    from tools.calibrate_context_skill_policy import (
        CalibrationError, CalibrationGrid, _load_real_assets, calibrate_policy,
    )

    root = root.resolve()
    policy_path = root / "configs" / "context_skill_policy.json"
    raw = json.loads(policy_path.read_text(encoding="utf-8"))
    current_policy = RetrievalPolicy.from_dict(raw)
    grid = CalibrationGrid(
        bm25_scale=(current_policy.bm25_scale,), dense_floor=(current_policy.dense_floor,),
        bm25_weight=(current_policy.bm25_weight,), dense_weight=(current_policy.dense_weight,),
        min_dense=(current_policy.min_dense,), min_score=(current_policy.min_score,),
        min_margin=(current_policy.min_margin,),
    )
    built = build_index(root)
    try:
        registry, embedder = _load_real_assets(root=root, policy_path=policy_path,
            index_override=None, embeddings_override=None, model_root_override=None)
        report = calibrate_policy(policy_path=policy_path,
            cases_path=root / "configs" / "context_skill_retrieval_cases.jsonl",
            registry=registry, embedder=embedder, grid=grid)
    except CalibrationError as error:
        raise AssetValidationError(f"persona_asset_refresh_failed:{error}") from error
    if not report["write"]["policyWritten"]:
        raise AssetValidationError("persona_asset_refresh_failed:existing_policy_validation_failed")
    return {**built, "policyStillRequiresCalibration": False,
            "validation": report["metrics"]["overall"]}


if __name__ == "__main__":
    raise SystemExit(main())
