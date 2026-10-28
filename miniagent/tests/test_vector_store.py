"""test_vector_store.py —— 从零 RAG 的验收测试（全离线、零依赖）。

跑法：
    .venv\\Scripts\\python.exe -m pytest miniagent/tests/test_vector_store.py -v
    .venv\\Scripts\\python.exe -m miniagent.tests.test_vector_store
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from miniagent.memory.embedders import HashEmbedder  # noqa: E402
from miniagent.memory.vector_store import VectorStore, chunk_text, cosine  # noqa: E402


# ==================== chunk_text ====================

def test_chunk_no_overlap():
    """不重叠：每 size 个字符一刀，尾巴不足 size 也留着。"""
    assert chunk_text('abcdefghij', size=4) == ['abcd', 'efgh', 'ij']


def test_chunk_with_overlap():
    """重叠：步长 = size - overlap。size=4, overlap=2 → 步长 2，起点 0/2/4。"""
    assert chunk_text('abcdef', size=4, overlap=2) == ['abcd', 'cdef', 'ef']


def test_chunk_rejects_zero_size():
    import pytest
    with pytest.raises(ValueError):
        chunk_text('abc', size=0)


def test_chunk_rejects_overlap_ge_size():
    """overlap >= size 会让步长为 0 或负 → 必须报错，否则死循环。"""
    import pytest
    with pytest.raises(ValueError):
        chunk_text('abc', size=4, overlap=4)


# ==================== cosine ====================

def test_cosine_same_direction_is_one():
    assert abs(cosine([1, 0], [2, 0]) - 1.0) < 1e-9


def test_cosine_orthogonal_is_zero():
    assert abs(cosine([1, 0], [0, 1])) < 1e-9


def test_cosine_zero_vector_is_zero():
    assert cosine([0, 0], [1, 1]) == 0.0


def test_cosine_dim_mismatch_raises():
    import pytest
    with pytest.raises(ValueError):
        cosine([1, 0], [1, 0, 0])


# ==================== VectorStore ====================

DOCS = [
    '公司2023年营收3650万元，主要来自杭州和上海。',
    '员工年假政策：入职满一年享受5天带薪年假。',
    '产品退货流程：7天内可无理由退货。',
]


def _store():
    s = VectorStore(embedder=HashEmbedder())
    for d in DOCS:
        s.add_document(d)
    return s


def test_add_document_returns_chunk_count():
    s = VectorStore(embedder=HashEmbedder())
    n = s.add_document('公司2023年营收3650万元。')
    assert n >= 1
    assert len(s) == n


def test_search_retrieves_relevant_chunk():
    """★ 最重要的一条：问"营收"，要捞回营收那块。"""
    out = _store().search('去年营收多少', k=1)
    assert out, '应该有结果'
    assert '营收' in out[0][1]


def test_search_retrieves_second_topic():
    """换个话题再验一次，防止"永远返回第一块"的假通过。"""
    out = _store().search('年假几天', k=1)
    assert out and '年假' in out[0][1]


def test_search_respects_k():
    s = _store()
    assert len(s.search('公司', k=2)) <= 2
    assert len(s.search('公司', k=1)) <= 1


def test_search_empty_store_returns_empty():
    assert VectorStore(embedder=HashEmbedder()).search('随便问点什么', k=3) == []


def test_search_sorted_desc():
    out = _store().search('营收', k=3)
    scores = [score for score, _ in out]
    assert scores == sorted(scores, reverse=True)


# ==================== 脚本模式入口 ====================

def main() -> None:
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f'  ✅ {name}')
            passed += 1
        except Exception as e:
            print(f'  ❌ {name}  ->  {type(e).__name__}: {e}')
            failed += 1
    print(f'\n{passed} passed, {failed} failed')
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
