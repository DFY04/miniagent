import math
import os                                        # ★ 修1：不再 from encodings import utf_8（那是个模块，不是编码字符串）
from typing import List, Optional

import chromadb
import dashscope
from dashscope import TextEmbedding
from dotenv import load_dotenv

# ★ 修2：删掉 `from multipart import file_path`——它不是你想要的路径变量


class DashScopeChromaRAG:
    def __init__(self, collection_name: str, file_path: str,
                 embed_model: str = "text-embedding-v1",
                 persist_path: str = "./chroma_db"):
        load_dotenv()
        api_key = os.getenv("DASHSCOPE_API_KEY")
        if not api_key:
            raise ValueError("DASHSCOPE_API_KEY 未在 .env 文件中配置！")
        dashscope.api_key = api_key

        self.embed_model = embed_model
        self.collection_name = collection_name
        self.file_path = file_path               # ★ 修3：存成实例属性，后面统一用 self.file_path
        self.client = chromadb.PersistentClient(path=persist_path)
        self.collection = self.client.get_or_create_collection(name=self.collection_name)

    def get_text(self, file_path: str) -> str:
        with open(file_path, "r", encoding="utf-8") as f:   # ★ 修1：用字符串 "utf-8"
            return f.read()

    def get_chunk(self, text: str) -> List[str]:
        # ★ 修4：滤掉空块（Chroma 不接受空 document，DashScope 也不接受空 input）
        return [c.strip() for c in text.split("\n\n") if c.strip()]

    def get_embeddings(self, text_list: List[str]) -> List[List[float]]:
        """批量调用 DashScope 生成向量（注意：一次最多 25 条，超了要自己切片）"""
        resp = TextEmbedding.call(
            model=self.embed_model,
            input=text_list
        )
        if resp.status_code != 200:
            raise Exception(f"向量化请求失败，返回信息：{resp}")
        embeddings = [item["embedding"] for item in resp.output["embeddings"]]
        return embeddings

    def add_documents(self, metadata: Optional[List[dict]] = None) -> int:
        """
        从 self.file_path 读文件 → 分块 → 向量化 → 入库
        :param metadata: 元数据，可选，长度必须和 chunk 数一致，用于后续过滤检索
        :return: 入库的 chunk 数
        """
        chunks = self.get_chunk(self.get_text(self.file_path))   # ★ 修5：不再用裸 file_path，也只算一次
        ids = [str(i) for i in range(len(chunks))]               # ★ 修6：列表推导式，先有值再 append；且不 reverse
        emb_list = self.get_embeddings(chunks)
        self.collection.upsert(                                  # ★ 修7：upsert 让脚本可重复跑
            documents=chunks,
            embeddings=emb_list,
            ids=ids,
            metadatas=metadata
        )
        return len(chunks)

    def search(self, query: str, n_results: int = 2, where: dict = None):
        """
        向量检索
        :param query: 用户查询文本
        :param n_results: 返回 topN 结果
        :param where: 元数据过滤条件，可选
        :return: chroma 原始返回 dict
        """
        query_emb = self.get_embeddings([query])
        result = self.collection.query(
            query_embeddings=query_emb,
            n_results=n_results,
            where=where
        )
        return result

    def delete_collection(self):
        """删除整个集合，清空里面所有向量文档"""
        self.client.delete_collection(self.collection_name)


# ====================== 离线玩具版：从零实现（零依赖，测试用） ======================
# 这一段是 RAG 的"最小可运行原型"：纯 Python 手写分块 / 余弦相似度 / 内存向量库。
# 不用 chromadb、不用 dashscope，单测全离线。真模型版见上面的 DashScopeChromaRAG。


def chunk_text(text: str, size: int = 4, overlap: int = 0) -> List[str]:
    """把长文本切成固定大小的小块，overlap 控制相邻块重叠多少。

    size=4, overlap=0 → 步长 4：'abcdefghij' -> ['abcd', 'efgh', 'ij']
    size=4, overlap=2 → 步长 2：'abcdef'     -> ['abcd', 'cdef', 'ef']
    """
    if size <= 0:
        raise ValueError('size 必须 > 0')
    if overlap < 0 or overlap >= size:
        raise ValueError('overlap 必须满足 0 <= overlap < size')

    step = size - overlap
    chunks: List[str] = []
    i = 0
    while i < len(text):
        chunks.append(text[i:i + size])   # 尾部不足 size 也照收（最后一块可能短）
        i += step
    return chunks


def cosine(a: List[float], b: List[float]) -> float:
    """余弦相似度：两个向量的夹角余弦，1=同向，0=垂直。"""
    if len(a) != len(b):
        raise ValueError(f'维度不一致：{len(a)} vs {len(b)}')
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class VectorStore:
    """内存版向量库（玩具）：add_document 入库，search 按余弦相似度取 top-k。

    只依赖 embedder 的 embed(text) 接口（鸭子类型），
    所以能换 HashEmbedder / DashScopeEmbedder，本类一行不用改。
    """

    def __init__(self, embedder):
        self.embedder = embedder
        self._chunks: List[str] = []
        self._vectors: List[List[float]] = []

    def add_document(self, document: str) -> int:
        # 玩具版：整篇当一个 chunk（真模型版的分块见 DashScopeChromaRAG.get_chunk）
        self._chunks.append(document)
        self._vectors.append(self.embedder.embed(document))
        return 1

    def __len__(self) -> int:
        return len(self._chunks)

    def search(self, query: str, k: int = 1):
        """返回 [(score, chunk), ...]，按相似度从高到低，最多 k 条。"""
        qv = self.embedder.embed(query)
        scored = [(cosine(qv, v), c) for v, c in zip(self._vectors, self._chunks)]
        scored.sort(key=lambda t: t[0], reverse=True)
        return scored[:k]


# ====================== 【使用示例】======================
if __name__ == "__main__":
    # ★ 修8：用 r"" 原始字符串，或者干脆用 __file__ 拼绝对路径，避免 \a 被当成转义符
    DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "asn.txt")

    rag = DashScopeChromaRAG(collection_name="rag_knowledge", file_path=DATA)
    print("入库 chunk 数:", rag.add_documents())

    res = rag.search(query="RAG实现步骤是什么？", n_results=2)
    print("====召回结果====")
    for doc, dist in zip(res["documents"][0], res["distances"][0]):
        print(f"距离: {dist:.4f} | {doc}")

    # 如果需要删除集合，取消下面注释
    # rag.delete_collection()
