"""检索层：切片、分词、倒排索引、混合打分。

移植来源
--------
* ``chunk_document`` 的逻辑来自 EchoMind ``mcp/knowledge_base.py::_chunk_text``
  （按句号/换行切分，尽量保留语义完整性）。
* 「外部能力不可用时自动降级」的思路来自 EchoMind
  ``core/intent_recognizer.py`` —— 它在第三方兼容 API 没有 embeddings 接口时，
  自动降级为字符 n-gram 哈希向量兜底。本模块把哈希向量直接作为一等检索通道，
  而不是只当兜底。

为什么不用 ChromaDB + 小模型 embedding
-------------------------------------
1. **语料规模**：审核域全部知识只有几十条文档片段。在这个量级上，
   BM25 这类词法检索的精度不低于小模型向量检索，而召回可解释性更好
   （能说清是哪个词命中的）。
2. **确定性**：CI 与单测需要可重复的结果。向量检索依赖模型下载
   （all-MiniLM-L6-v2 约 90MB），在离线环境会直接失败。
3. **本域特性**：审核上下文里天然带着**原因码字面量**（如 ``image_blur``），
   这是一个精确匹配信号，词法检索能天然利用，向量反而会把它糊掉。

因此这里做的是三路混合打分：

    最终分 = 0.65 × BM25 归一化分 + 0.35 × 哈希向量余弦 + 原因码精确命中加分

若将来语料规模上量，只需实现 ``VectorBackend`` 协议替换哈希向量后端即可。
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, Sequence

from ai_service.corpus import DOC_TYPE_ANY, KnowledgeDoc

DEFAULT_CHUNK_SIZE = 500
BM25_K1 = 1.5
BM25_B = 0.75
LEXICAL_WEIGHT = 0.65
VECTOR_WEIGHT = 0.35
REASON_CODE_BOOST = 2.5
MAX_REASON_CODE_BOOST = 5.0
HASH_VECTOR_DIM = 512
HASH_NGRAM_SIZES = (2, 3)

CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]+")
IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
NUMBER_PATTERN = re.compile(r"\d+")
TOKEN_PATTERN = re.compile(
    r"[\u4e00-\u9fff]+|[A-Za-z_][A-Za-z0-9_]*|\d+"
)

# 确定性查询改写用的同义词表。
# 这一层解决的是「用户/审核员说人话，语料里写的是原因码」这个词汇鸿沟，
# 和 EchoMind 用 LLM 做查询改写的目标一致，只是这里用规则实现。
SYNONYM_EXPANSION: dict[str, tuple[str, ...]] = {
    "模糊": ("image_blur", "清晰度", "对焦", "抖动", "拍摄规范"),
    "糊": ("image_blur", "清晰度", "对焦"),
    "清晰": ("image_blur", "清晰度", "对焦"),
    "暗": ("image_dark", "亮度", "光线"),
    "亮": ("image_bright", "亮度", "曝光", "闪光灯"),
    "曝光": ("image_bright", "亮度", "闪光灯"),
    "反光": ("glare_detected", "高光", "光线均匀"),
    "高光": ("glare_detected", "反光"),
    "卡号": ("missing_card_number", "invalid_card_number", "卡号"),
    "有效期": ("missing_valid_date", "invalid_valid_date", "有效期限"),
    "姓名": ("missing_name", "持卡人"),
    "重拍": ("拍摄规范", "重新拍摄", "处置建议"),
    "重传": ("拍摄规范", "重新拍摄", "处置建议"),
    "重新拍": ("拍摄规范", "处置建议"),
    "身份证": ("id_card", "人像面", "国徽面", "unknown_id_card_side"),
    "银行卡": ("bank_card", "卡面"),
    "人像面": ("front", "missing_id_number", "missing_address"),
    "国徽面": ("back", "missing_issue_authority", "missing_valid_period"),
    "误报": ("误报", "放行", "glare_detected"),
    "拒绝": ("reject", "invalid_card_number"),
    "放行": ("pass", "放行"),
}


def chunk_text(text: str, chunk_size: int = DEFAULT_CHUNK_SIZE) -> list[str]:
    """按语义边界切片，尽量不把一句话切断。

    移植自 EchoMind ``KnowledgeBase._chunk_text``，保持行为一致。
    """
    if len(text) <= chunk_size:
        return [text] if text.strip() else []

    chunks: list[str] = []
    current = ""
    for sentence in text.replace("\n", "。").split("。"):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(current) + len(sentence) + 1 > chunk_size:
            if current:
                chunks.append(current)
            current = sentence
        else:
            current = f"{current}。{sentence}" if current else sentence
    if current:
        chunks.append(current)
    return chunks


def tokenize(text: str) -> list[str]:
    """中文按字 + 二字组切分，英文标识符与数字整体保留。

    对中文使用二字组（bigram）是为了在无分词词典的前提下获得可接受的召回；
    这与简历里「字符 n-gram 兜底」是同一类做法，但在这里是主路径。
    """
    if not text:
        return []

    tokens: list[str] = []
    for match in TOKEN_PATTERN.finditer(text):
        piece = match.group(0)
        if CJK_PATTERN.fullmatch(piece):
            tokens.extend(piece)
            tokens.extend(piece[i : i + 2] for i in range(len(piece) - 1))
            continue

        lowered = piece.lower()
        tokens.append(lowered)
        # image_blur → 也拆出 image、blur，提升部分命中的召回
        if "_" in lowered:
            tokens.extend(part for part in lowered.split("_") if part)
    return tokens


def expand_query(query: str) -> list[str]:
    """确定性查询改写：把口语词扩写成原因码与规范术语。

    这是 ``tool_manager`` 在 LLM 不可用时使用的改写策略，
    目标是覆盖「一个查询只命中一个角度」的问题。
    """
    expansions: list[str] = []
    for keyword, synonyms in SYNONYM_EXPANSION.items():
        if keyword in query:
            expansions.extend(synonyms)
    # 去重且保序，原始 query 永远排第一
    ordered = [query]
    seen = {query}
    for item in expansions:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


class VectorBackend(Protocol):
    """向量后端协议。换 ChromaDB / 外部 embedding 只需实现这个协议。"""

    def embed(self, text: str) -> dict[int, float]:
        """把文本编码成稀疏向量（维度索引 → 权重）。"""

    @property
    def name(self) -> str:
        """后端名称，会出现在返回体的 engine 字段里。"""


@dataclass(frozen=True)
class HashingVectorBackend:
    """字符 n-gram 哈希向量后端：零依赖、确定性。

    用带签名的哈希把一个 n-gram 映射到固定维度，权重取计数平方根。
    这不是 SOTA 做法，但在这个语料规模上足够，且**完全可复现**。
    """

    dim: int = HASH_VECTOR_DIM
    ngram_sizes: tuple[int, ...] = HASH_NGRAM_SIZES

    @property
    def name(self) -> str:
        return f"hashing-ngram-{self.dim}"

    def embed(self, text: str) -> dict[int, float]:
        counts: Counter[int] = Counter()
        normalized = re.sub(r"\s+", "", text or "")
        for size in self.ngram_sizes:
            if len(normalized) < size:
                continue
            for start in range(len(normalized) - size + 1):
                gram = normalized[start : start + size]
                digest = hashlib.md5(gram.encode("utf-8")).digest()
                bucket = int.from_bytes(digest[:4], "big") % self.dim
                counts[bucket] += 1
        return {bucket: math.sqrt(count) for bucket, count in counts.items()}

    @staticmethod
    def cosine(left: dict[int, float], right: dict[int, float]) -> float:
        if not left or not right:
            return 0.0
        if len(left) > len(right):
            left, right = right, left
        dot = sum(weight * right.get(bucket, 0.0) for bucket, weight in left.items())
        if dot <= 0.0:
            return 0.0
        left_norm = math.sqrt(sum(weight * weight for weight in left.values()))
        right_norm = math.sqrt(sum(weight * weight for weight in right.values()))
        if left_norm <= 0.0 or right_norm <= 0.0:
            return 0.0
        return dot / (left_norm * right_norm)


@dataclass
class Chunk:
    """索引里的最小检索单元。"""

    chunk_id: str
    doc_id: str
    title: str
    category: str
    content: str
    reason_codes: tuple[str, ...] = ()
    doc_types: tuple[str, ...] = (DOC_TYPE_ANY,)
    tags: tuple[str, ...] = ()
    chunk_index: int = 0
    total_chunks: int = 1
    tokens: list[str] = field(default_factory=list, repr=False)
    vector: dict[int, float] = field(default_factory=dict, repr=False)

    @property
    def term_frequencies(self) -> Counter[str]:
        return Counter(self.tokens)


@dataclass
class RetrievalHit:
    """一条检索结果。``score`` 已做过归一化，可跨查询粗略比较大小。"""

    doc_id: str
    title: str
    category: str
    content: str
    score: float
    matched_reason_codes: tuple[str, ...] = ()
    retrieval_channels: tuple[str, ...] = ()
    chunk_index: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "category": self.category,
            "content": self.content,
            "score": round(self.score, 4),
            "matched_reason_codes": list(self.matched_reason_codes),
            "retrieval_channels": list(self.retrieval_channels),
            "chunk_index": self.chunk_index,
        }


class KnowledgeRetriever:
    """混合检索器：BM25 + 哈希向量 + 原因码精确命中。"""

    def __init__(
        self,
        documents: Sequence[KnowledgeDoc] = (),
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        vector_backend: VectorBackend | None = None,
    ) -> None:
        self._vector_backend: VectorBackend = vector_backend or HashingVectorBackend()
        self._chunks: list[Chunk] = []
        self._inverted: dict[str, list[tuple[int, int]]] = {}
        self._doc_freq: Counter[str] = Counter()
        self._avg_length: float = 0.0
        if documents:
            self.add_documents(documents, chunk_size=chunk_size)

    # ── 索引构建 ──────────────────────────────────────────────────────────────

    def add_documents(
        self,
        documents: Iterable[KnowledgeDoc],
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> int:
        added = 0
        for doc in documents:
            pieces = chunk_text(doc.content, chunk_size=chunk_size) or [doc.content]
            for index, piece in enumerate(pieces):
                chunk_id = hashlib.md5(
                    f"{doc.doc_id}_{index}_{piece[:50]}".encode("utf-8")
                ).hexdigest()
                if any(existing.chunk_id == chunk_id for existing in self._chunks):
                    continue
                tokens = tokenize(f"{doc.title}。{piece}")
                self._chunks.append(
                    Chunk(
                        chunk_id=chunk_id,
                        doc_id=doc.doc_id,
                        title=doc.title,
                        category=doc.category,
                        content=piece,
                        reason_codes=tuple(doc.reason_codes),
                        doc_types=tuple(doc.doc_types) or (DOC_TYPE_ANY,),
                        tags=tuple(doc.tags),
                        chunk_index=index,
                        total_chunks=len(pieces),
                        tokens=tokens,
                        vector=self._vector_backend.embed(f"{doc.title}。{piece}"),
                    )
                )
                added += 1
        self._rebuild_index()
        return added

    def _rebuild_index(self) -> None:
        self._inverted = {}
        self._doc_freq = Counter()
        for position, chunk in enumerate(self._chunks):
            for term, frequency in chunk.term_frequencies.items():
                self._inverted.setdefault(term, []).append((position, frequency))
            for term in set(chunk.tokens):
                self._doc_freq[term] += 1
        total_length = sum(len(chunk.tokens) for chunk in self._chunks)
        self._avg_length = total_length / len(self._chunks) if self._chunks else 0.0

    # ── 查询 ──────────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        reason_codes: Sequence[str] = (),
        doc_type: str | None = None,
    ) -> list[RetrievalHit]:
        """三路混合检索。

        ``reason_codes`` 传入当前审核记录的原因码，会获得精确命中加分；
        ``doc_type`` 用于过滤掉不适用的文档（如给身份证记录召回银行卡规则）。
        """
        if not self._chunks or not query.strip():
            return []

        query_tokens = tokenize(query)
        wanted_codes = {code.strip() for code in reason_codes if str(code).strip()}

        lexical = self._bm25_scores(query_tokens)
        vector = self._vector_scores(query)

        candidates: list[tuple[float, Chunk, tuple[str, ...], tuple[str, ...]]] = []
        for position, chunk in enumerate(self._chunks):
            if not self._matches_doc_type(chunk, doc_type):
                continue

            lexical_score = lexical.get(position, 0.0)
            vector_score = vector.get(position, 0.0)
            boost, matched = self._reason_code_boost(chunk, wanted_codes)
            if lexical_score <= 0.0 and vector_score <= 0.0 and boost <= 0.0:
                continue

            channels: list[str] = []
            if lexical_score > 0.0:
                channels.append("lexical")
            if vector_score > 0.0:
                channels.append("vector")
            if boost > 0.0:
                channels.append("reason_code")

            total = (
                LEXICAL_WEIGHT * lexical_score
                + VECTOR_WEIGHT * vector_score
                + boost
            )
            candidates.append((total, chunk, matched, tuple(channels)))

        if not candidates:
            return []

        candidates.sort(key=lambda item: (-item[0], item[1].doc_id))
        best = candidates[0][0] or 1.0

        hits: list[RetrievalHit] = []
        for total, chunk, matched, channels in candidates[: max(1, top_k)]:
            hits.append(
                RetrievalHit(
                    doc_id=chunk.doc_id,
                    title=chunk.title,
                    category=chunk.category,
                    content=chunk.content,
                    # 归一化到 0~1，便于前端展示与跨查询阈值判断
                    score=min(1.0, total / best) if best else 0.0,
                    matched_reason_codes=matched,
                    retrieval_channels=channels,
                    chunk_index=chunk.chunk_index,
                )
            )
        return hits

    # ── 打分细节 ──────────────────────────────────────────────────────────────

    def _bm25_scores(self, query_tokens: Sequence[str]) -> dict[int, float]:
        if not query_tokens or not self._chunks:
            return {}
        total_docs = len(self._chunks)
        scores: dict[int, float] = {}
        for term in dict.fromkeys(query_tokens):
            postings = self._inverted.get(term)
            if not postings:
                continue
            doc_freq = self._doc_freq.get(term, 0)
            # 加 0.5 平滑并钳到非负，避免高频词产生负分
            idf = max(
                0.0,
                math.log(1.0 + (total_docs - doc_freq + 0.5) / (doc_freq + 0.5)),
            )
            for position, frequency in postings:
                length = len(self._chunks[position].tokens) or 1
                denominator = frequency + BM25_K1 * (
                    1.0 - BM25_B + BM25_B * length / (self._avg_length or 1.0)
                )
                contribution = idf * frequency * (BM25_K1 + 1.0) / denominator
                scores[position] = scores.get(position, 0.0) + contribution
        if not scores:
            return {}
        # min-max 归一化，便于与向量分加权
        values = list(scores.values())
        lowest, highest = min(values), max(values)
        span = highest - lowest
        if span <= 0.0:
            return {position: 1.0 for position in scores}
        return {position: (value - lowest) / span for position, value in scores.items()}

    def _vector_scores(self, query: str) -> dict[int, float]:
        embed = getattr(self._vector_backend, "embed", None)
        cosine = getattr(self._vector_backend, "cosine", None)
        if embed is None or cosine is None:
            return {}
        query_vector = embed(query)
        if not query_vector:
            return {}
        return {
            position: max(0.0, float(cosine(query_vector, chunk.vector)))
            for position, chunk in enumerate(self._chunks)
            if chunk.vector
        }

    @staticmethod
    def _reason_code_boost(
        chunk: Chunk,
        wanted_codes: set[str],
    ) -> tuple[float, tuple[str, ...]]:
        if not wanted_codes:
            return 0.0, ()
        matched = tuple(code for code in chunk.reason_codes if code in wanted_codes)
        if not matched:
            return 0.0, ()
        return min(MAX_REASON_CODE_BOOST, REASON_CODE_BOOST * len(matched)), matched

    @staticmethod
    def _matches_doc_type(chunk: Chunk, doc_type: str | None) -> bool:
        if not doc_type:
            return True
        return DOC_TYPE_ANY in chunk.doc_types or doc_type in chunk.doc_types

    # ── 统计 ──────────────────────────────────────────────────────────────────

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)

    @property
    def doc_count(self) -> int:
        return len({chunk.doc_id for chunk in self._chunks})

    @property
    def vector_backend_name(self) -> str:
        return self._vector_backend.name
