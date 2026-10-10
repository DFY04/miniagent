from miniagent.core.message import Message
from qwen_agent.utils.tokenization_qwen import count_tokens, tokenizer



#count_tokens
#精确计算 摒弃粗略计算
# def exact_tokens(text: str) -> int:        # ← 原名（测试要的是 estimate_tokens）
def estimate_tokens(text: str) -> int:
    return count_tokens(text)


#注意事项必须保留system message 必须保留最后一条user message
#不可以让tool call后面跟多个ass 也就是说必须按照react的模式: user-> assisatant->tool->assistant
# message里面第一条一定是system message
from typing import  List
def truncate(messages: list[Message], max_tokens: int) -> list[Message]:
    new_list:List[Message]=[]#用于保存系统词
    new_list1: List[Message] = []#用于保存其他词
    a:List[str]=[]
    # estimate_tokens=count_tokens(messages[0].content)   # ← 原变量名，会遮蔽上面那个函数
    for i in range(len(messages)):
        if messages[i].role=="system":
            new_list.append(messages[i])
            a.append(messages[i].content)
    last_user_token=estimate_tokens(messages[-1].content)
    system_token=estimate_tokens("".join(a))
    must_token=last_user_token+system_token

    for i in range(len(messages)-2,0,-1):

        if must_token+estimate_tokens(messages[i].content)<max_tokens:
            new_list1.append(messages[i])
            must_token=must_token+estimate_tokens(messages[i].content)
    new_list1.reverse()
    new_list1.append(messages[-1])
    must=new_list+new_list1
    for i in range(len(must) - 1, 0,
                   -1):  # ★ 从后往前（索引不会乱），到 1 就停（不碰 i=0）
        if must[i].role == "function" and must[i - 1].role != "assistant":
            del must[i]
    return must





# ==================== P03：摘要压缩 ====================

SUMMARY_PROMPT = """请把下面的对话压缩成一段摘要。
要求：
1. 保留关键事实（人名、数字、结论、决定），不许丢
2. 丢掉寒暄、重复的话
3. 尽量短

对话内容：
{dialogue}"""


def summarize(messages: list[Message], llm) -> str:
    """把一批消息交给 LLM，压成一段摘要，返回摘要字符串。

    步骤：
      1. 把 messages 拼成 "role: content" 的多行文本
      2. 用 SUMMARY_PROMPT 包一层，交给 llm.chat([...])
      3. 返回回复的 content（strip 过）

    TODO: 实现它
    """
    raise NotImplementedError('TODO: summarize')


def compress(messages: list[Message], llm, max_tokens: int) -> list[Message]:
    """超预算时先用摘要压缩，而不是直接丢。

    规则：
      1. 总量没超 max_tokens → 原样返回（一次 LLM 都不调）
      2. 超了 → 用 truncate 算出"哪些留下"，被丢掉的那部分交给 summarize
      3. 摘要做成一条消息，插在【所有 system 之后】
      4. 再检查一遍：还超 → 再 truncate 一次兜底
      5. summarize 抛异常 / 返回空 → 直接返回 truncate 的结果（降级，绝不能崩）

    提示：truncate 返回的是同一批 Message 对象，可以用 id() 比对出"谁被丢了"。

    TODO: 实现它
    """
    raise NotImplementedError('TODO: compress')
