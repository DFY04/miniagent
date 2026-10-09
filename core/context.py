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



