"""embedders.py —— "文本 → 向量"这一步的零件箱（可替换）。

【为什么单独一个文件】
    这是整条 RAG 链路里**唯一需要外部模型 / 网络**的一环。把它隔离出来，就能自由替换：

        HashEmbedder      → 零依赖玩具版（离线测试用，不花钱、确定性）
        DashScopeEmbedder → 真模型（真语义，需要 DASHSCOPE_API_KEY）

    两者接口完全一致：

        embed(text, text_type='document') -> List[float]

    `VectorStore` 只认这个接口（鸭子类型），根本不关心背后是哪个模型。
    **这就是依赖注入的回报：换模型，VectorStore 一行都不用改。**
"""
from __future__ import annotations

import hashlib
import math
import os
from typing import List


# ============================================================
#  玩具版：零依赖、离线、确定性
# ============================================================
class HashEmbedder:
    """零依赖的"玩具 embedding"：把文本变成定长向量。

    【原理】用"字符重叠"模拟"语义相近"：
        1. 文本切成 bigram（两字一组）
        2. 每个 token 稳定哈希到固定维度，该位 +1
        3. 归一化成"长度 1"

    【定位】离线测试 / 演示：不花钱、不联网、结果完全确定。
    【局限】它只认字面重叠，**不懂语义** ——
            "营收"和"收入"在它眼里毫无关系。真语义要模型才有。
    """

    def __init__(self, dim: int = 64):
        self.dim = dim

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        t = ''.join(ch for ch in text if not ch.isspace())
        return [t[i:i + 2] for i in range(len(t) - 1)]

    def embed(self, text: str, text_type: str = 'document') -> List[float]:
        """text_type 在玩具版里用不上（它不区分 query/document），
        但**必须留着这个参数** —— 否则接口就和真模型不一致了。"""
        vec = [0.0] * self.dim
        for tok in self._tokenize(text):
            # 用 md5 而不是内置 hash()：内置 hash 每进程种子不同，结果不稳定
            h = int(hashlib.md5(tok.encode('utf-8')).hexdigest(), 16)
            vec[h % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0:
            return vec
        return [x / norm for x in vec]


# ============================================================
#  真模型：语义的来源
# ============================================================
class DashScopeEmbedder:
    """真 embedding：调阿里 DashScope 的 text-embedding 模型。

    这就是"语义"的来源 —— 模型在海量语料上训练过，知道"营收"和"收入"是一回事。
    **这种东西手写不出来，只能调。**

    【关键参数 text_type】官方自建 RAG 指南要求：
        - 建库（文档）时： text_type='document'
        - 查询  时：       text_type='query'
        同一个模型对两种输入用**不同的编码方式**，
        这样"问题"和"文档"才落在同一个向量空间里、能对齐比较。

    Args:
        model: 模型名，默认 'text-embedding-v3'；老账号可退到 'text-embedding-v1'。
        api_key: 不传就读环境变量 DASHSCOPE_API_KEY（本项目在 .env 里）。
        dimension: 输出维度，仅 v3/v4 支持；不传就让模型用默认值。

    用法：
        emb = DashScopeEmbedder()
        v = emb.embed('公司去年营收多少', text_type='query')   # -> [0.02, -0.13, ...]
    """

    def __init__(self,
                 model: str = 'text-embedding-v3',
                 api_key: str | None = None,
                 dimension: int | None = None):
        # 从 .env 读 DASHSCOPE_API_KEY（生产环境一般在应用启动时统一加载）
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        self.model = model
        self.dimension = dimension
        self.api_key = api_key or os.getenv('DASHSCOPE_API_KEY')
        if not self.api_key:
            raise ValueError(
                '缺少 DASHSCOPE_API_KEY —— 请写进 .env，或 DashScopeEmbedder(api_key=...)'
            )

    def embed(self, text: str, text_type: str = 'document') -> List[float]:
        from dashscope import TextEmbedding          # 延迟导入：离线测试时不需要它

        kwargs = {'text_type': text_type}
        if self.dimension is not None:
            kwargs['dimension'] = self.dimension

        resp = TextEmbedding.call(
            model=self.model,
            input=text,
            api_key=self.api_key,
            **kwargs,
        )
        if resp.status_code != 200:
            raise RuntimeError(f'embedding 调用失败 [{resp.status_code}] {resp.message}')

        # 响应结构：output['embeddings'] = [{'text_index': 0, 'embedding': [...]}]
        return resp.output['embeddings'][0]['embedding']
