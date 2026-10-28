"""rag_probe.py —— 用 asn.txt 当"探针语料"，验证 RAG 整条链路是否通。

探针语料的目的**不是**刷高分，而是回答三个问题：
1. 文件读进来了吗？切成几块？（读文件 + 分块）
2. 向量化成功了吗？维度多少？（外部 API + 网络/密钥）
3. 入库能被检索到吗？同一个话题能不能优先召回？（入库 + 检索）

跑法（在项目根目录）：
    .venv\\Scripts\\python.exe -X utf8 -m miniagent.examples.rag_probe

注意：会真的调用 DashScope（消耗额度），并且会清空同名集合后重建。
"""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import chromadb  # noqa: E402

from miniagent.memory.vector_store import DashScopeChromaRAG  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ASN_TXT = os.path.join(HERE, '..', 'memory', 'asn.txt')
DB_PATH = os.path.join(HERE, '..', 'memory', 'chroma_probe')   # 探针用独立库，不污染正式库
COLLECTION = 'asn_probe'

# (问题, 期望命中的块号)  块号从 1 开始，和 asn.txt 里的段序一致
CASES: list[tuple[str, int]] = [
    ('ChromaDB需要单独部署服务吗', 1),
    ('RAG实现步骤是什么', 3),
    ('怎么判断两段文本像不像', 4),
    ('文档该怎么切分', 5),
    ('召回效果差是什么原因', 6),
]


def main() -> None:
    # ---------- 每次重跑都从干净状态开始，否则旧数据会混进来 ----------
    client = chromadb.PersistentClient(path=DB_PATH)
    try:
        client.delete_collection(COLLECTION)
    except Exception:
        pass

    rag = DashScopeChromaRAG(
        collection_name=COLLECTION,
        file_path=ASN_TXT,
        persist_path=DB_PATH,
    )

    # ---------- 1. 读 + 切 ----------
    text = rag.get_text(rag.file_path)
    chunks = rag.get_chunk(text)
    print(f'[1] 读文件: {len(text)} 字符 → 切成 {len(chunks)} 块')
    if len(chunks) < 2:
        print('    !! 只切出 1 块：检查 asn.txt 里段与段之间有没有真空行')

    # ---------- 2. 向量化 ----------
    emb = rag.get_embeddings(chunks[:1])[0]
    norm = math.sqrt(sum(x * x for x in emb))
    print(f'[2] 向量化成功: 维度 {len(emb)}，向量模长 {norm:.2f}')
    if abs(norm - 1.0) > 1e-6:
        print('    !! 向量未归一化：Chroma 默认度量是 l2，距离数值会被"长度"带偏')

    # ---------- 3. 入库 ----------
    n = rag.add_documents()
    print(f'[3] 入库 {n} 块，集合现有 {rag.collection.count()} 条\n')

    # ---------- 4. 检索 ----------
    print('%-28s %-5s %-11s %-5s %s' % ('问题', 'Top1', '距离', '判定', '召回内容(前 24 字)'))
    print('-' * 96)
    hit1 = hit3 = 0
    for q, expect in CASES:
        res = rag.search(query=q, n_results=3)
        ids = [int(i) for i in res['ids'][0]]          # id 是 0 基，块号 = id + 1
        blocks = [i + 1 for i in ids]
        top1, dist1 = blocks[0], res['distances'][0][0]
        doc1 = res['documents'][0][0].replace('\n', ' ')[:24]
        hit1 += top1 == expect
        hit3 += expect in blocks
        mark = '★' if top1 == expect else ('△' if expect in blocks else '×')
        print('%-28s %-5s %-11.4f %-5s %s' % (q, f'块{top1}', dist1, mark, doc1))
    print('-' * 96)
    print(f'Top1 命中 {hit1}/{len(CASES)}    Top3 命中 {hit3}/{len(CASES)}')
    print('\n图例：★ Top1 就对  △ Top3 里有（排序偏了）  × Top3 都没有（语料缺答案或表述差太远）')


if __name__ == '__main__':
    main()
