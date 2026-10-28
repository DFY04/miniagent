"""RAG 一键跑：切块 → 向量化 → 存库 → 检索。

跑法：
    .venv\\Scripts\\python.exe -m miniagent.examples.rag_quickstart

要联网 + 消耗 DASHSCOPE_API_KEY（很便宜，几厘钱）。
"""
from __future__ import annotations

from miniagent.memory.embedders import DashScopeEmbedder
from miniagent.memory.vector_store import VectorStore

DOC = ('公司2023年营收3650万元，主要来自杭州和上海两个城市。'
       '杭州是公司总部所在地，上海设有研发中心。'
       '员工年假政策：入职满一年享受5天带薪年假。'
       '产品退货流程：7天内可无理由退货，需保留发票。')


def main() -> None:
    store = VectorStore(DashScopeEmbedder())          # ← 真向量模型

    n = store.add_document(DOC, size=25)               # 切块 + 向量化
    print(f'切成了 {n} 块：')
    for i, (chunk, _) in enumerate(store._items):
        print(f'  [{i}] {chunk}')

    query = '去年营收多少'
    print(f'\n查询：{query!r}')
    for score, chunk in store.search(query, k=3):
        print(f'  {score:.3f}  {chunk}')


if __name__ == '__main__':
    main()
