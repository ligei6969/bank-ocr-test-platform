"""Tests for the retrieval layer: tokenising, query expansion, hybrid scoring.

Everything here is deterministic by design — no embeddings are downloaded and
no network is touched, so the suite stays reproducible in CI.
"""

from __future__ import annotations

import pytest

from ai_service.corpus import (
    DOC_TYPE_ANY,
    DOC_TYPE_BANK_CARD,
    DOC_TYPE_ID_CARD,
    KnowledgeDoc,
    load_documents,
)
from ai_service.retrieval import (
    HashingVectorBackend,
    KnowledgeRetriever,
    chunk_text,
    expand_query,
    tokenize,
)


@pytest.fixture(scope="module")
def retriever() -> KnowledgeRetriever:
    return KnowledgeRetriever(load_documents())


@pytest.fixture(scope="module")
def docs_by_id() -> dict[str, KnowledgeDoc]:
    return {doc.doc_id: doc for doc in load_documents()}


# ── 分词 ──────────────────────────────────────────────────────────────────────

def test_tokenize_emits_characters_and_bigrams_for_chinese() -> None:
    tokens = tokenize("图片模糊")

    assert "图" in tokens
    assert "模糊" in tokens  # 二字组提升召回


def test_tokenize_keeps_identifiers_whole_and_splits_their_parts() -> None:
    tokens = tokenize("检测 glare_detected 原因码")

    assert "glare_detected" in tokens
    assert "glare" in tokens
    assert "detected" in tokens


def test_tokenize_lowercases_identifiers_and_keeps_numbers() -> None:
    tokens = tokenize("OCR_MODE=245")

    assert "ocr_mode" in tokens
    assert "245" in tokens


def test_tokenize_of_empty_text_returns_nothing() -> None:
    assert tokenize("") == []


# ── 切片 ──────────────────────────────────────────────────────────────────────

def test_short_text_is_a_single_chunk() -> None:
    assert chunk_text("很短的一段说明。") == ["很短的一段说明。"]


def test_long_text_is_split_on_sentence_boundaries() -> None:
    chunks = chunk_text("第一句。第二句。第三句。", chunk_size=6)

    assert len(chunks) > 1
    assert all(chunk.strip() for chunk in chunks)


def test_blank_text_produces_no_chunks() -> None:
    assert chunk_text("   ") == []


# ── 查询改写（规则降级路径）──────────────────────────────────────────────────

def test_expand_query_keeps_the_original_first_and_adds_reason_codes() -> None:
    expanded = expand_query("图片模糊")

    assert expanded[0] == "图片模糊"
    assert "image_blur" in expanded


def test_expand_query_maps_colloquial_terms_to_reason_codes() -> None:
    assert "glare_detected" in expand_query("反光了怎么办")
    assert "missing_valid_date" in expand_query("有效期没识别出来")
    assert "image_dark" in expand_query("拍得太暗")


def test_expand_query_is_deduplicated_and_stable() -> None:
    first = expand_query("图片模糊重拍")
    second = expand_query("图片模糊重拍")

    assert first == second
    assert len(first) == len(set(first))


def test_expand_query_leaves_unknown_terms_untouched() -> None:
    assert expand_query("完全无关的词汇") == ["完全无关的词汇"]


# ── 哈希向量后端 ──────────────────────────────────────────────────────────────

def test_hashing_backend_is_deterministic() -> None:
    backend = HashingVectorBackend()

    assert backend.embed("图片模糊") == backend.embed("图片模糊")


def test_hashing_backend_cosine_is_one_for_identical_text() -> None:
    backend = HashingVectorBackend()
    vector = backend.embed("原因码 image_blur")

    assert backend.cosine(vector, vector) == pytest.approx(1.0)


def test_hashing_backend_cosine_is_zero_for_empty_input() -> None:
    backend = HashingVectorBackend()

    assert backend.cosine({}, backend.embed("abc")) == 0.0


# ── 索引 ──────────────────────────────────────────────────────────────────────

def test_index_builds_from_the_shipped_corpus(retriever: KnowledgeRetriever) -> None:
    assert retriever.doc_count == len(load_documents())
    assert retriever.chunk_count >= retriever.doc_count


def test_adding_the_same_document_twice_does_not_duplicate_chunks() -> None:
    retriever = KnowledgeRetriever()
    doc = KnowledgeDoc(
        doc_id="tmp.doc",
        title="临时文档",
        category="reason_code",
        content="这是一段用来验证去重的说明文字。",
    )

    retriever.add_documents([doc])
    first_count = retriever.chunk_count
    retriever.add_documents([doc])

    assert retriever.chunk_count == first_count


# ── 检索 ──────────────────────────────────────────────────────────────────────

def test_empty_query_returns_nothing(retriever: KnowledgeRetriever) -> None:
    assert retriever.search("") == []
    assert retriever.search("   ") == []


def test_reason_code_context_pulls_the_matching_document_to_the_top(
    retriever: KnowledgeRetriever,
) -> None:
    hits = retriever.search(
        "卡片上有反光怎么办",
        reason_codes=["glare_detected"],
        doc_type=DOC_TYPE_BANK_CARD,
    )

    assert hits
    assert hits[0].doc_id == "rc.glare_detected"
    assert "glare_detected" in hits[0].matched_reason_codes
    assert "reason_code" in hits[0].retrieval_channels


def test_hits_are_scored_and_ordered_by_descending_score(
    retriever: KnowledgeRetriever,
) -> None:
    hits = retriever.search("图片模糊导致有效期缺失", reason_codes=["image_blur"])

    scores = [hit.score for hit in hits]
    assert scores == sorted(scores, reverse=True)
    assert all(0.0 <= score <= 1.0 for score in scores)


def test_doc_type_filter_excludes_other_document_families(
    retriever: KnowledgeRetriever,
    docs_by_id: dict[str, KnowledgeDoc],
) -> None:
    hits = retriever.search("卡号 有效期 姓名 缺失", doc_type=DOC_TYPE_ID_CARD, top_k=20)

    assert hits
    for hit in hits:
        scopes = docs_by_id[hit.doc_id].doc_types
        # 只允许「通用」或「身份证」，银行卡专属文档必须被过滤掉
        assert DOC_TYPE_ANY in scopes or DOC_TYPE_ID_CARD in scopes


def test_bank_card_only_documents_are_filtered_out_for_id_card_queries(
    retriever: KnowledgeRetriever,
    docs_by_id: dict[str, KnowledgeDoc],
) -> None:
    hits = retriever.search("银行卡号 卡号 有效期", doc_type=DOC_TYPE_ID_CARD, top_k=20)

    bank_card_only = {
        hit.doc_id
        for hit in hits
        if docs_by_id[hit.doc_id].doc_types == (DOC_TYPE_BANK_CARD,)
    }
    assert bank_card_only == set()


def test_any_scoped_documents_are_visible_to_both_document_types(
    retriever: KnowledgeRetriever,
    docs_by_id: dict[str, KnowledgeDoc],
) -> None:
    hits = retriever.search("拍摄规范 光线", doc_type=DOC_TYPE_ID_CARD, top_k=20)

    assert any(DOC_TYPE_ANY in docs_by_id[hit.doc_id].doc_types for hit in hits)


def test_top_k_limits_the_number_of_hits(retriever: KnowledgeRetriever) -> None:
    assert len(retriever.search("拍摄规范", top_k=2)) <= 2


def test_retrieval_channel_labels_are_reported(retriever: KnowledgeRetriever) -> None:
    hits = retriever.search("模糊 反光", reason_codes=["image_blur"])
    channels = {channel for hit in hits for channel in hit.retrieval_channels}

    assert "lexical" in channels
    assert "vector" in channels
    assert "reason_code" in channels


def test_hits_serialise_to_json_safe_dicts(retriever: KnowledgeRetriever) -> None:
    hit = retriever.search("模糊", reason_codes=["image_blur"])[0]
    payload = hit.to_dict()

    assert set(payload) >= {
        "doc_id",
        "title",
        "category",
        "content",
        "score",
        "matched_reason_codes",
        "retrieval_channels",
    }
    assert isinstance(payload["matched_reason_codes"], list)
    assert isinstance(payload["retrieval_channels"], list)


def test_unrelated_query_still_returns_something_or_nothing_gracefully(
    retriever: KnowledgeRetriever,
) -> None:
    """不该抛异常；有结果就得分合法，没结果就是空列表。"""
    hits = retriever.search("zzzzzzz 987654321", top_k=3)

    assert isinstance(hits, list)
    assert all(0.0 <= hit.score <= 1.0 for hit in hits)


def test_vector_backend_name_is_reported(retriever: KnowledgeRetriever) -> None:
    assert retriever.vector_backend_name == "hashing-ngram-512"
