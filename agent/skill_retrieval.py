"""Fail-closed local retrieval for Amadeus context skills.

The module intentionally has no network client and imports optional numerical
dependencies only inside the ONNX/index loaders.  A production selection is
valid only when the calibrated policy, immutable registry and dense model all
agree on their build identifiers.  Lexical BM25 is therefore never promoted
to a production decision by itself.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import threading
import unicodedata
from typing import Any, Iterable, Protocol, Sequence


INDEX_SCHEMA_VERSION = 2
POLICY_SCHEMA_VERSION = 1
TOKENIZER_VERSION = "nfkc-cjk-kana-unigram-bigram-latin-word-v1"


class SkillRetrievalError(RuntimeError):
    """Base class for deterministic, fail-closed retrieval failures."""


class AssetValidationError(SkillRetrievalError):
    """A required local dependency or immutable asset failed validation."""


class Embedder(Protocol):
    """Small local sentence encoder contract used by the hybrid retriever."""

    @property
    def model_id(self) -> str: ...

    @property
    def revision(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


@dataclass(frozen=True)
class ContextSkillMeta:
    skill_id: str
    name: str
    description: str
    domain: str
    version: int
    aliases: tuple[str, ...]
    positive_examples: tuple[str, ...]
    negative_examples: tuple[str, ...]
    source_refs: tuple[str, ...] = ()

    @property
    def retrieval_document(self) -> str:
        return "\n".join((
            self.name,
            self.description,
            *self.aliases,
            *self.positive_examples,
        ))


@dataclass(frozen=True)
class SkillCandidate:
    skill_id: str
    bm25_score: float
    bm25_normalized: float
    embedding_score: float
    dense_normalized: float
    fused_score: float

    def diagnostic_dict(self) -> dict[str, Any]:
        return {
            "skillId": self.skill_id,
            "bm25Score": round(self.bm25_score, 6),
            "bm25Normalized": round(self.bm25_normalized, 6),
            "embeddingScore": round(self.embedding_score, 6),
            "denseNormalized": round(self.dense_normalized, 6),
            "fusedScore": round(self.fused_score, 6),
        }


@dataclass(frozen=True)
class LoadedSkill:
    skill_id: str
    version: int
    body: str
    capsule_sha256: str
    source_refs: tuple[str, ...]

    def identity_dict(self) -> dict[str, Any]:
        """Return safe metadata only; never include the capsule body in logs."""

        return {
            "skillId": self.skill_id,
            "version": self.version,
            "capsuleSha256": self.capsule_sha256,
            "sourceRefs": list(self.source_refs),
        }


@dataclass(frozen=True)
class SkillRegistryEntry:
    meta: ContextSkillMeta
    capsule_body: str
    capsule_sha256: str
    bm25_tokens: tuple[str, ...]
    dense_vectors: tuple[tuple[float, ...], ...]

    def load(self) -> LoadedSkill:
        return LoadedSkill(
            skill_id=self.meta.skill_id,
            version=self.meta.version,
            body=self.capsule_body,
            capsule_sha256=self.capsule_sha256,
            source_refs=self.meta.source_refs,
        )


@dataclass(frozen=True)
class SkillRegistry:
    schema_version: int
    generation: int
    build_id: str
    source_digest: str
    model_id: str
    model_revision: str
    model_artifact_digest: str
    embedding_dim: int
    entries: tuple[SkillRegistryEntry, ...]

    def entry(self, skill_id: str) -> SkillRegistryEntry | None:
        return next(
            (item for item in self.entries if item.meta.skill_id == skill_id),
            None,
        )


class RegistryStore:
    """Atomically publishes immutable registry generations to concurrent turns."""

    def __init__(self, initial: SkillRegistry | None = None) -> None:
        self._lock = threading.RLock()
        self._registry = initial
        self._generation = int(initial.generation) if initial is not None else 0

    def snapshot(self) -> SkillRegistry | None:
        with self._lock:
            return self._registry

    def replace(self, registry: SkillRegistry) -> SkillRegistry:
        with self._lock:
            self._generation += 1
            published = replace(registry, generation=self._generation)
            self._registry = published
            return published


@dataclass(frozen=True)
class RetrievalPolicy:
    schema_version: int
    calibrated: bool
    model_id: str
    model_revision: str
    model_artifact_digest: str
    index_build_id: str
    source_digest: str
    bm25_k1: float
    bm25_b: float
    bm25_scale: float
    dense_floor: float
    bm25_weight: float
    dense_weight: float
    min_dense: float
    min_score: float
    min_margin: float
    top_k: int
    max_selected: int

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RetrievalPolicy":
        fusion = value.get("fusion") if isinstance(value.get("fusion"), dict) else {}
        gates = value.get("gates") if isinstance(value.get("gates"), dict) else {}
        bm25 = value.get("bm25") if isinstance(value.get("bm25"), dict) else {}
        policy = cls(
            schema_version=int(value.get("schemaVersion") or 0),
            calibrated=str(value.get("calibrationStatus") or "").lower() == "calibrated",
            model_id=str(value.get("modelId") or ""),
            model_revision=str(value.get("modelRevision") or ""),
            model_artifact_digest=str(value.get("modelArtifactDigest") or ""),
            index_build_id=str(value.get("indexBuildId") or ""),
            source_digest=str(value.get("sourceDigest") or ""),
            bm25_k1=float(bm25.get("k1") or 1.2),
            bm25_b=float(bm25.get("b") or 0.75),
            bm25_scale=float(fusion.get("bm25Scale") or 1.0),
            dense_floor=float(fusion.get("denseFloor") or 0.0),
            bm25_weight=float(fusion.get("bm25Weight") or 0.0),
            dense_weight=float(fusion.get("denseWeight") or 0.0),
            min_dense=float(gates.get("minDense") or 0.0),
            min_score=float(gates.get("minScore") or 0.0),
            min_margin=float(gates.get("minMargin") or 0.0),
            top_k=int(value.get("topK") or 3),
            max_selected=int(value.get("maxSelected") or 1),
        )
        policy.validate()
        return policy

    @classmethod
    def load(cls, path: Path) -> "RetrievalPolicy":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise AssetValidationError("policy_missing") from error
        except (OSError, json.JSONDecodeError) as error:
            raise AssetValidationError("policy_invalid_json") from error
        if not isinstance(raw, dict):
            raise AssetValidationError("policy_invalid_shape")
        return cls.from_dict(raw)

    def validate(self) -> None:
        if self.schema_version != POLICY_SCHEMA_VERSION:
            raise AssetValidationError("policy_schema_mismatch")
        if self.calibrated and not self.model_artifact_digest:
            raise AssetValidationError("policy_model_artifact_digest_missing")
        if not 1 <= self.max_selected <= 3:
            raise AssetValidationError("policy_max_selected_out_of_range")
        if not 1 <= self.top_k <= 8:
            raise AssetValidationError("policy_top_k_out_of_range")
        if self.top_k < self.max_selected:
            raise AssetValidationError("policy_top_k_below_max_selected")
        if self.bm25_k1 <= 0 or not 0 <= self.bm25_b <= 1:
            raise AssetValidationError("policy_bm25_parameters_invalid")
        if self.bm25_scale <= 0 or not -1 <= self.dense_floor < 1:
            raise AssetValidationError("policy_normalization_invalid")
        if self.bm25_weight < 0 or self.dense_weight <= 0:
            raise AssetValidationError("policy_weights_invalid")
        if not math.isclose(
            self.bm25_weight + self.dense_weight,
            1.0,
            rel_tol=1e-6,
            abs_tol=1e-6,
        ):
            raise AssetValidationError("policy_weights_do_not_sum_to_one")
        numeric = (
            self.min_dense,
            self.min_score,
            self.min_margin,
            self.bm25_k1,
            self.bm25_b,
            self.bm25_scale,
            self.dense_floor,
            self.bm25_weight,
            self.dense_weight,
        )
        if not all(math.isfinite(item) for item in numeric):
            raise AssetValidationError("policy_non_finite_value")


@dataclass(frozen=True)
class RetrievalResult:
    selected: tuple[LoadedSkill, ...]
    candidates: tuple[SkillCandidate, ...]
    fallback_reason: str | None


def select_ranked_skill_ids(
    ranked: Sequence[tuple[float, str, float]],
    *, min_dense: float, min_score: float, min_margin: float, max_selected: int,
) -> tuple[tuple[str, ...], str | None]:
    """Choose the smallest relevant group separated from the remaining candidates.

    Rows are (fused score, skill ID, dense score), sorted by descending score.
    The margin separates the selected group from the first excluded skill;
    related skills inside that group do not have to defeat one another.
    Inspect the full ranking so the selection cap cannot hide an ambiguous tie.
    """
    if not ranked:
        return (), "no_candidates"
    if ranked[0][2] < min_dense:
        return (), "dense_below_minimum"
    if ranked[0][0] < min_score:
        return (), "fused_below_minimum"
    count = 0
    for index, (score, _, dense) in enumerate(ranked[:max_selected]):
        if dense < min_dense or score < min_score:
            break
        outside_score = ranked[index + 1][0] if index + 1 < len(ranked) else 0.0
        if score - outside_score >= min_margin:
            count = index + 1
            break
    if not count:
        return (), "margin_below_minimum"
    return tuple(row[1] for row in ranked[:count]), None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise AssetValidationError(f"asset_unreadable:{path.name}") from error
    return digest.hexdigest()


def persona_index_build_id(
    *,
    source_digest: str,
    model_id: str,
    model_revision: str,
    model_artifact_digest: str,
    embedding_sha256: str,
    tokenizer_version: str,
    routing_digest: str,
) -> str:
    """Bind model bundle, vectors and Skill-to-row routing into one build ID."""

    material = "\n".join((
        str(source_digest),
        str(model_id),
        str(model_revision),
        str(model_artifact_digest),
        str(embedding_sha256),
        str(tokenizer_version),
        str(routing_digest),
    ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _is_cjk_or_kana(character: str) -> bool:
    code = ord(character)
    return bool(
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x3040 <= code <= 0x309F
        or 0x30A0 <= code <= 0x30FF
        or 0x31F0 <= code <= 0x31FF
    )


def lexical_tokens(text: str) -> tuple[str, ...]:
    """NFKC tokenizer for BM25, not a persona classifier.

    Han and kana receive character unigrams and adjacent bigrams.  Latin
    letters and numbers are accumulated into case-folded word tokens.
    """

    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    tokens: list[str] = []
    latin_buffer: list[str] = []
    previous_cjk = ""

    def flush_latin() -> None:
        if latin_buffer:
            tokens.append("w:" + "".join(latin_buffer))
            latin_buffer.clear()

    for character in normalized:
        if _is_cjk_or_kana(character):
            flush_latin()
            tokens.append("c:" + character)
            if previous_cjk:
                tokens.append("b:" + previous_cjk + character)
            previous_cjk = character
            continue
        previous_cjk = ""
        category = unicodedata.category(character)
        if character.isascii() and (character.isalnum() or character == "_"):
            latin_buffer.append(character)
        elif category.startswith("L") or category.startswith("N"):
            latin_buffer.append(character)
        else:
            flush_latin()
    flush_latin()
    return tuple(tokens)


def bm25_okapi_scores(
    query_tokens: Sequence[str],
    documents: Sequence[Sequence[str]],
    *,
    k1: float = 1.2,
    b: float = 0.75,
) -> tuple[float, ...]:
    """Return standard Okapi BM25 scores for an in-memory immutable corpus."""

    if not documents:
        return ()
    document_count = len(documents)
    average_length = sum(len(item) for item in documents) / document_count
    average_length = average_length or 1.0
    document_frequency: dict[str, int] = {}
    for document in documents:
        for token in set(document):
            document_frequency[token] = document_frequency.get(token, 0) + 1
    unique_query = tuple(dict.fromkeys(query_tokens))
    scores: list[float] = []
    for document in documents:
        frequencies: dict[str, int] = {}
        for token in document:
            frequencies[token] = frequencies.get(token, 0) + 1
        length_norm = k1 * (1 - b + b * len(document) / average_length)
        score = 0.0
        for token in unique_query:
            frequency = frequencies.get(token, 0)
            if not frequency:
                continue
            frequency_docs = document_frequency.get(token, 0)
            inverse_frequency = math.log(
                1 + (document_count - frequency_docs + 0.5) / (frequency_docs + 0.5)
            )
            score += inverse_frequency * (
                frequency * (k1 + 1) / (frequency + length_norm)
            )
        scores.append(score)
    return tuple(scores)


def _finite_vector(value: Sequence[float], expected_dim: int) -> tuple[float, ...]:
    vector = tuple(float(item) for item in value)
    if len(vector) != expected_dim or expected_dim <= 0:
        raise AssetValidationError("embedding_dimension_mismatch")
    if not all(math.isfinite(item) for item in vector):
        raise AssetValidationError("embedding_non_finite")
    magnitude = math.sqrt(sum(item * item for item in vector))
    if not math.isfinite(magnitude) or magnitude <= 1e-12:
        raise AssetValidationError("embedding_zero_norm")
    return tuple(item / magnitude for item in vector)


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise AssetValidationError("embedding_dimension_mismatch")
    value = sum(float(a) * float(b) for a, b in zip(left, right))
    if not math.isfinite(value):
        raise AssetValidationError("embedding_similarity_non_finite")
    return max(-1.0, min(1.0, value))


class HybridSkillRetriever:
    """Precision-first BM25+dense ranker with three mandatory gates."""

    def __init__(self, embedder: Embedder | None) -> None:
        self._embedder = embedder

    def retrieve(
        self,
        query: str,
        registry: SkillRegistry | None,
        policy: RetrievalPolicy | None,
    ) -> RetrievalResult:
        try:
            return self._retrieve(query, registry, policy)
        except AssetValidationError as error:
            return RetrievalResult((), (), str(error))
        except Exception as error:  # runtime inference failures must fail closed
            return RetrievalResult((), (), f"dense_inference_failed:{type(error).__name__}")

    def _retrieve(
        self,
        query: str,
        registry: SkillRegistry | None,
        policy: RetrievalPolicy | None,
    ) -> RetrievalResult:
        if policy is None:
            raise AssetValidationError("policy_unavailable")
        policy.validate()
        if not policy.calibrated:
            raise AssetValidationError("policy_not_calibrated")
        if registry is None:
            raise AssetValidationError("index_unavailable")
        if registry.schema_version != INDEX_SCHEMA_VERSION:
            raise AssetValidationError("index_schema_mismatch")
        if not registry.entries:
            raise AssetValidationError("index_empty")
        if self._embedder is None:
            raise AssetValidationError("embedder_unavailable")
        if (
            registry.model_id != policy.model_id
            or registry.model_revision != policy.model_revision
            or self._embedder.model_id != policy.model_id
            or self._embedder.revision != policy.model_revision
        ):
            raise AssetValidationError("model_binding_mismatch")
        if registry.build_id != policy.index_build_id:
            raise AssetValidationError("index_build_binding_mismatch")
        if registry.source_digest != policy.source_digest:
            raise AssetValidationError("source_digest_binding_mismatch")
        if self._embedder.dimension != registry.embedding_dim:
            raise AssetValidationError("embedding_dimension_mismatch")
        query_text = unicodedata.normalize("NFKC", str(query or "")).strip()
        if not query_text:
            return RetrievalResult((), (), "query_empty")
        raw_vectors = self._embedder.embed((query_text,))
        if len(raw_vectors) != 1:
            raise AssetValidationError("embedder_batch_shape_invalid")
        query_vector = _finite_vector(raw_vectors[0], registry.embedding_dim)
        documents = tuple(item.bm25_tokens for item in registry.entries)
        bm25_values = bm25_okapi_scores(
            lexical_tokens(query_text),
            documents,
            k1=policy.bm25_k1,
            b=policy.bm25_b,
        )
        candidates: list[SkillCandidate] = []
        for entry, lexical_score in zip(registry.entries, bm25_values):
            if not entry.dense_vectors:
                raise AssetValidationError("skill_dense_exemplars_missing")
            dense_score = max(
                cosine_similarity(
                    query_vector,
                    _finite_vector(vector, registry.embedding_dim),
                )
                for vector in entry.dense_vectors
            )
            bm25_normalized = lexical_score / (lexical_score + policy.bm25_scale)
            dense_normalized = max(0.0, min(
                1.0,
                (dense_score - policy.dense_floor) / (1.0 - policy.dense_floor),
            ))
            fused = (
                policy.bm25_weight * bm25_normalized
                + policy.dense_weight * dense_normalized
            )
            if not all(math.isfinite(item) for item in (
                lexical_score,
                bm25_normalized,
                dense_score,
                dense_normalized,
                fused,
            )):
                raise AssetValidationError("candidate_non_finite")
            candidates.append(SkillCandidate(
                skill_id=entry.meta.skill_id,
                bm25_score=lexical_score,
                bm25_normalized=bm25_normalized,
                embedding_score=dense_score,
                dense_normalized=dense_normalized,
                fused_score=fused,
            ))
        candidates.sort(key=lambda item: (-item.fused_score, item.skill_id))
        top_candidates = tuple(candidates[:policy.top_k])
        selected_ids, reason = select_ranked_skill_ids(
            tuple((item.fused_score, item.skill_id, item.embedding_score) for item in candidates),
            min_dense=policy.min_dense, min_score=policy.min_score,
            min_margin=policy.min_margin, max_selected=policy.max_selected,
        )
        loaded = []
        for skill_id in selected_ids:
            entry = registry.entry(skill_id)
            if entry is None:
                raise AssetValidationError("selected_skill_missing")
            loaded.append(entry.load())
        return RetrievalResult(tuple(loaded), top_candidates, reason)


def skill_source_digest(skills_root: Path) -> str:
    """Hash authoritative manifests and capsule bytes in stable path order."""

    digest = hashlib.sha256()
    paths = sorted(
        (
            path for path in skills_root.rglob("*")
            if path.is_file() and path.name in {"manifest.json", "SKILL.txt"}
        ),
        key=lambda item: item.relative_to(skills_root).as_posix(),
    )
    if not paths:
        raise AssetValidationError("skill_sources_missing")
    for path in paths:
        relative = path.relative_to(skills_root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def parse_skill_manifest(path: Path) -> ContextSkillMeta:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AssetValidationError(f"skill_manifest_invalid:{path.parent.name}") from error
    if not isinstance(raw, dict):
        raise AssetValidationError(f"skill_manifest_invalid:{path.parent.name}")
    skill_id = str(raw.get("id") or "").strip()
    if not skill_id or skill_id != path.parent.name:
        raise AssetValidationError(f"skill_id_path_mismatch:{path.parent.name}")
    domain = str(raw.get("domain") or "").strip()
    if domain != "persona":
        raise AssetValidationError(f"skill_domain_invalid:{skill_id}")

    def strings(name: str) -> tuple[str, ...]:
        value = raw.get(name)
        if not isinstance(value, list):
            raise AssetValidationError(f"skill_{name}_missing:{skill_id}")
        return tuple(str(item).strip() for item in value if str(item).strip())

    positives = strings("positiveExamples")
    negatives = strings("negativeExamples")
    if not positives or not negatives:
        raise AssetValidationError(f"skill_examples_missing:{skill_id}")
    return ContextSkillMeta(
        skill_id=skill_id,
        name=str(raw.get("name") or skill_id).strip(),
        description=str(raw.get("description") or "").strip(),
        domain=domain,
        version=int(raw.get("version") or 0),
        aliases=strings("aliases"),
        positive_examples=positives,
        negative_examples=negatives,
        source_refs=strings("sourceRefs"),
    )


def load_runtime_registry(
    *,
    skills_root: Path,
    index_path: Path,
    embeddings_path: Path,
    policy: RetrievalPolicy,
    model_artifact_digest: str,
    generation: int = 0,
) -> SkillRegistry:
    """Load and cross-check a production index without rebuilding it."""

    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise AssetValidationError("index_missing") from error
    except (OSError, json.JSONDecodeError) as error:
        raise AssetValidationError("index_invalid_json") from error
    if not isinstance(index, dict):
        raise AssetValidationError("index_invalid_shape")
    if int(index.get("schemaVersion") or 0) != INDEX_SCHEMA_VERSION:
        raise AssetValidationError("index_schema_mismatch")
    source_digest = skill_source_digest(skills_root)
    if str(index.get("sourceDigest") or "") != source_digest:
        raise AssetValidationError("index_source_digest_mismatch")
    if str(index.get("sourceDigest") or "") != policy.source_digest:
        raise AssetValidationError("policy_source_digest_mismatch")
    embedding = index.get("embedding") if isinstance(index.get("embedding"), dict) else {}
    if (
        str(embedding.get("modelId") or "") != policy.model_id
        or str(embedding.get("modelRevision") or "") != policy.model_revision
    ):
        raise AssetValidationError("index_model_binding_mismatch")
    indexed_artifact_digest = str(embedding.get("artifactDigest") or "")
    if not indexed_artifact_digest or indexed_artifact_digest != str(model_artifact_digest):
        raise AssetValidationError("index_model_artifact_binding_mismatch")
    if indexed_artifact_digest != policy.model_artifact_digest:
        raise AssetValidationError("policy_model_artifact_binding_mismatch")
    if str(index.get("tokenizerVersion") or "") != TOKENIZER_VERSION:
        raise AssetValidationError("index_tokenizer_version_mismatch")
    expected_embedding_hash = str(embedding.get("sha256") or "")
    if not expected_embedding_hash or sha256_file(embeddings_path) != expected_embedding_hash:
        raise AssetValidationError("embedding_file_hash_mismatch")
    try:
        import numpy as np  # type: ignore
    except ImportError as error:
        raise AssetValidationError("dependency_missing:numpy") from error
    try:
        matrix = np.load(str(embeddings_path), allow_pickle=False)
    except Exception as error:
        raise AssetValidationError("embedding_matrix_invalid") from error
    if getattr(matrix, "ndim", 0) != 2 or not bool(np.isfinite(matrix).all()):
        raise AssetValidationError("embedding_matrix_invalid")
    dimension = int(embedding.get("dimension") or 0)
    raw_entries = index.get("entries") if isinstance(index.get("entries"), list) else []
    routing_material = json.dumps(
        raw_entries,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    routing_digest = hashlib.sha256(routing_material.encode("utf-8")).hexdigest()
    if str(index.get("routingDigest") or "") != routing_digest:
        raise AssetValidationError("index_routing_digest_mismatch")
    computed_build_id = persona_index_build_id(
        source_digest=source_digest,
        model_id=str(embedding.get("modelId") or ""),
        model_revision=str(embedding.get("modelRevision") or ""),
        model_artifact_digest=indexed_artifact_digest,
        embedding_sha256=expected_embedding_hash,
        tokenizer_version=TOKENIZER_VERSION,
        routing_digest=routing_digest,
    )
    if str(index.get("buildId") or "") != computed_build_id:
        raise AssetValidationError("index_build_digest_mismatch")
    if computed_build_id != policy.index_build_id:
        raise AssetValidationError("policy_index_build_mismatch")
    if matrix.shape[1:] != (dimension,) or matrix.shape[0] < len(raw_entries):
        raise AssetValidationError("embedding_matrix_shape_mismatch")
    entries: list[SkillRegistryEntry] = []
    resolved_root = skills_root.resolve()
    claimed_rows: set[int] = set()
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise AssetValidationError("index_entry_invalid")
        capsule_relative = Path(str(raw_entry.get("capsulePath") or ""))
        capsule_path = (skills_root.parent.parent / capsule_relative).resolve()
        try:
            capsule_path.relative_to(resolved_root)
        except ValueError as error:
            raise AssetValidationError("capsule_path_outside_registry") from error
        manifest_path = capsule_path.parent / "manifest.json"
        meta = parse_skill_manifest(manifest_path)
        if meta.skill_id != str(raw_entry.get("id") or ""):
            raise AssetValidationError("index_skill_id_mismatch")
        body = capsule_path.read_text(encoding="utf-8")
        capsule_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if capsule_hash != str(raw_entry.get("capsuleSha256") or ""):
            raise AssetValidationError(f"capsule_hash_mismatch:{meta.skill_id}")
        indexed_tokens = tuple(str(item) for item in raw_entry.get("bm25Tokens", []))
        expected_tokens = lexical_tokens(meta.retrieval_document)
        if indexed_tokens != expected_tokens:
            raise AssetValidationError(f"bm25_tokens_mismatch:{meta.skill_id}")
        raw_rows = raw_entry.get("embeddingRows")
        if not isinstance(raw_rows, list) or not raw_rows:
            raise AssetValidationError(f"embedding_rows_missing:{meta.skill_id}")
        embedding_rows: list[int] = []
        for raw_row in raw_rows:
            if not isinstance(raw_row, int) or isinstance(raw_row, bool):
                raise AssetValidationError(f"embedding_row_invalid:{meta.skill_id}")
            if raw_row < 0 or raw_row >= matrix.shape[0] or raw_row in claimed_rows:
                raise AssetValidationError(f"embedding_row_invalid:{meta.skill_id}")
            claimed_rows.add(raw_row)
            embedding_rows.append(raw_row)
        entries.append(SkillRegistryEntry(
            meta=meta,
            capsule_body=body,
            capsule_sha256=capsule_hash,
            bm25_tokens=indexed_tokens,
            dense_vectors=tuple(
                tuple(float(item) for item in matrix[row].tolist())
                for row in embedding_rows
            ),
        ))
    if claimed_rows != set(range(matrix.shape[0])):
        raise AssetValidationError("embedding_rows_not_closed")
    if not entries:
        raise AssetValidationError("index_empty")
    return SkillRegistry(
        schema_version=INDEX_SCHEMA_VERSION,
        generation=int(generation),
        build_id=str(index.get("buildId") or ""),
        source_digest=source_digest,
        model_id=str(embedding.get("modelId") or ""),
        model_revision=str(embedding.get("modelRevision") or ""),
        model_artifact_digest=indexed_artifact_digest,
        embedding_dim=dimension,
        entries=tuple(entries),
    )


class OnnxBgeSmallZhEmbedder:
    """Offline-only BGE-small-zh-v1.5 ONNX encoder (CLS + L2 normalize)."""

    def __init__(
        self,
        *,
        model_id: str,
        revision: str,
        artifact_digest: str,
        dimension: int,
        tokenizer: Any,
        session: Any,
        max_length: int,
    ) -> None:
        self._model_id = model_id
        self._revision = revision
        self._artifact_digest = artifact_digest
        self._dimension = int(dimension)
        self._tokenizer = tokenizer
        self._session = session
        self._max_length = int(max_length)

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def revision(self) -> str:
        return self._revision

    @property
    def artifact_digest(self) -> str:
        return self._artifact_digest

    @property
    def dimension(self) -> int:
        return self._dimension

    @classmethod
    def load(cls, asset_root: Path) -> "OnnxBgeSmallZhEmbedder":
        manifest_path = asset_root / "manifest.json"
        manifest_digest = sha256_file(manifest_path)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise AssetValidationError("embedding_manifest_missing") from error
        except (OSError, json.JSONDecodeError) as error:
            raise AssetValidationError("embedding_manifest_invalid") from error
        if not isinstance(manifest, dict) or int(manifest.get("schemaVersion") or 0) != 1:
            raise AssetValidationError("embedding_manifest_schema_mismatch")
        if str(manifest.get("modelId") or "") != "BAAI/bge-small-zh-v1.5":
            raise AssetValidationError("embedding_model_id_mismatch")
        if str(manifest.get("pooling") or "").upper() != "CLS" or manifest.get("normalized") is not True:
            raise AssetValidationError("embedding_pooling_contract_mismatch")
        model_path = asset_root / str(manifest.get("modelFile") or "model.onnx")
        tokenizer_path = asset_root / str(manifest.get("tokenizerFile") or "tokenizer.json")
        if sha256_file(model_path) != str(manifest.get("modelSha256") or "").lower():
            raise AssetValidationError("embedding_model_hash_mismatch")
        if sha256_file(tokenizer_path) != str(manifest.get("tokenizerSha256") or "").lower():
            raise AssetValidationError("embedding_tokenizer_hash_mismatch")
        external_data = manifest.get("externalData")
        if external_data is None and manifest.get("externalDataFile"):
            external_data = [{
                "file": manifest.get("externalDataFile"),
                "sha256": manifest.get("externalDataSha256"),
            }]
        if external_data is not None:
            if not isinstance(external_data, list) or not external_data:
                raise AssetValidationError("embedding_external_data_manifest_invalid")
            resolved_asset_root = asset_root.resolve()
            for item in external_data:
                if not isinstance(item, dict):
                    raise AssetValidationError("embedding_external_data_manifest_invalid")
                external_path = (asset_root / str(item.get("file") or "")).resolve()
                try:
                    external_path.relative_to(resolved_asset_root)
                except ValueError as error:
                    raise AssetValidationError("embedding_external_data_path_invalid") from error
                expected_hash = str(item.get("sha256") or "").lower()
                if not expected_hash or sha256_file(external_path) != expected_hash:
                    raise AssetValidationError("embedding_external_data_hash_mismatch")
        try:
            import numpy  # noqa: F401  # type: ignore
        except ImportError as error:
            raise AssetValidationError("dependency_missing:numpy") from error
        try:
            import onnxruntime as ort  # type: ignore
        except ImportError as error:
            raise AssetValidationError("dependency_missing:onnxruntime") from error
        try:
            from tokenizers import Tokenizer  # type: ignore
        except ImportError as error:
            raise AssetValidationError("dependency_missing:tokenizers") from error
        try:
            tokenizer = Tokenizer.from_file(str(tokenizer_path))
            session = ort.InferenceSession(
                str(model_path),
                providers=["CPUExecutionProvider"],
            )
        except Exception as error:
            raise AssetValidationError("embedding_runtime_initialization_failed") from error
        return cls(
            model_id=str(manifest.get("modelId")),
            revision=str(manifest.get("modelRevision") or ""),
            artifact_digest=manifest_digest,
            dimension=int(manifest.get("dimension") or 0),
            tokenizer=tokenizer,
            session=session,
            max_length=int(manifest.get("maxLength") or 512),
        )

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        try:
            import numpy as np  # type: ignore
            encodings = self._tokenizer.encode_batch([str(item) for item in texts])
            if not encodings:
                return ()
            width = min(
                self._max_length,
                max(1, max(len(item.ids) for item in encodings)),
            )
            input_ids: list[list[int]] = []
            attention: list[list[int]] = []
            token_types: list[list[int]] = []
            for encoding in encodings:
                ids = list(encoding.ids[:width])
                mask = [1] * len(ids)
                types = list(encoding.type_ids[:width]) or [0] * len(ids)
                padding = width - len(ids)
                input_ids.append(ids + [0] * padding)
                attention.append(mask + [0] * padding)
                token_types.append(types + [0] * padding)
            available_inputs = {item.name for item in self._session.get_inputs()}
            feed: dict[str, Any] = {}
            if "input_ids" in available_inputs:
                feed["input_ids"] = np.asarray(input_ids, dtype=np.int64)
            if "attention_mask" in available_inputs:
                feed["attention_mask"] = np.asarray(attention, dtype=np.int64)
            if "token_type_ids" in available_inputs:
                feed["token_type_ids"] = np.asarray(token_types, dtype=np.int64)
            if "input_ids" not in feed or "attention_mask" not in feed:
                raise AssetValidationError("embedding_onnx_inputs_invalid")
            outputs = self._session.run(None, feed)
            hidden = np.asarray(outputs[0])
            if hidden.ndim != 3 or hidden.shape[0] != len(texts):
                raise AssetValidationError("embedding_onnx_output_invalid")
            pooled = hidden[:, 0, :]
            norms = np.linalg.norm(pooled, axis=1, keepdims=True)
            if not bool(np.isfinite(pooled).all()) or bool((norms <= 1e-12).any()):
                raise AssetValidationError("embedding_non_finite")
            normalized = pooled / norms
            if normalized.shape[1] != self._dimension:
                raise AssetValidationError("embedding_dimension_mismatch")
            return tuple(tuple(float(item) for item in row) for row in normalized.tolist())
        except AssetValidationError:
            raise
        except Exception as error:
            raise AssetValidationError("embedding_inference_failed") from error


_SHARED_EMBEDDER_LOCK = threading.RLock()
_SHARED_EMBEDDERS: dict[str, OnnxBgeSmallZhEmbedder] = {}


def load_shared_bge_embedder(asset_root: Path) -> OnnxBgeSmallZhEmbedder:
    """Load one validated ONNX encoder per asset directory and process.

    Persona skill selection and chatbot-memory recall use the same local model.
    Sharing the session avoids loading the roughly 24 MB model twice while
    keeping either caller independent of the other's feature switch.
    """

    key = str(Path(asset_root).resolve())
    with _SHARED_EMBEDDER_LOCK:
        cached = _SHARED_EMBEDDERS.get(key)
        if cached is not None:
            return cached
        loaded = OnnxBgeSmallZhEmbedder.load(Path(key))
        _SHARED_EMBEDDERS[key] = loaded
        return loaded
