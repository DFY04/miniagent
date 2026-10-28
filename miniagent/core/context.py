from miniagent.core.message import Message
from miniagent.core.protocols import ChatModel      # ★ 给 llm 参数加注解用


def estimate_tokens(text: str) -> int:
    """粗略估算一段文字要多少 token（算上下文预算用，不追求和分词器完全一致）。

    规则：中文 1 个字算 1 个 token；其他字符大约 4 个算 1 个 token。
    为什么不用真分词器：那要额外依赖，而且这里只用来判断「超没超预算」，
    估多估少 10% 不影响决策。
    """
    text = text or ""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk + (len(text) - cjk) // 4


#注意事项必须保留system message 必须保留最后一条user message
#不可以让tool call后面跟多个ass 也就是说必须按照react的模式: user-> assisatant->tool->assistant
# message里面第一条一定是system message
from typing import  List
def truncate(messages: list[Message], max_tokens: int) -> list[Message]:
    new_list:List[Message]=[]#用于保存系统词
    new_list1: List[Message] = []#用于保存其他词
    a:List[str]=[]
    # estimate_tokens=count_tokens(messages[0].content)   # ← 原变量名，会遮蔽上面那个函数
    # enumerate：一边遍历元素，一边拿到它的下标
    # 这里其实用不到下标 i，所以直接 `for m in messages` 最干净；
    # 但一旦需要下标（比如判断“是不是最后一条 system”），enumerate 就是标准写法
    for i, m in enumerate(messages):
        if m.role == "system":
            new_list.append(m)
            a.append(m.content or "")
    # ★ 修正：不能假设 messages[-1] 是 user。
    #   mem_agent.build() 时上下文常常以 function 结尾（user -> assistant(tool_call) -> function），
    #   以前直接把 messages[-1] 当成“最后一条 user”：轻则 token 算错，重则把用户的问题裁掉、
    #   只留下一条孤立的工具结果。所以从后往前找【真正的】最后一条 user。
    last_user_index = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "user":
            last_user_index = i
            break

    if last_user_index is None:
        # 找不到 user（例如只有 system，或只有 assistant/function）→ 保守处理：
        # system 全留，非 system 只留最后一条，至少给模型一个“当前状态”。
        tail = [messages[-1]] if messages and messages[-1].role != "system" else []
        return new_list + tail

    # user 之后的尾巴（assistant 的 tool_call + 对应的 function 结果）必须整段保留：
    # 它们是同一轮 ReAct 的一问一答，拆开就会出现“有工具结果、没有请求”的断头上下文。
    tail = messages[last_user_index + 1:]
    must_token = estimate_tokens(messages[last_user_index].content or "")
    for m in tail:
        must_token += estimate_tokens(m.content or "")
    system_token=estimate_tokens("".join(a))
    must_token=must_token+system_token

    for i in range(last_user_index-1,0,-1):
        if messages[i].role == "system":  # 已经在 new_list 里了，不能重复保留
            continue
        if must_token+estimate_tokens(messages[i].content or "")<max_tokens:
            new_list1.append(messages[i])
            must_token=must_token+estimate_tokens(messages[i].content or "")
    new_list1.reverse()
    must=new_list+new_list1+[messages[last_user_index]]+tail
    for i in range(len(must) - 1, 0,
                   -1):  # ★ 从后往前（索引不会乱），到 1 就停（不碰 i=0）
        if must[i].role == "function" and must[i - 1].role != "assistant":
            del must[i]
    return must





# ==================== P03：摘要压缩 ===================


SUMMARY_PROMPT = """请把下面的对话压缩成一段摘要。
要求：
1. 保留关键事实（人名、数字、结论、决定），不许丢
2. 丢掉寒暄、重复的话
3. 尽量短

对话内容：
{dialogue}"""


def summarize(messages: list[Message], llm: ChatModel) -> str:
    """把一批消息交给 LLM，压成一段摘要，返回摘要字符串。

    步骤：
      1. 把 messages 拼成 "role: content" 的多行文本
      2. 用 SUMMARY_PROMPT 包一层，交给 llm.chat([...])
      3. 返回回复的 content（strip 过）

    TODO: 实现它
    """
    dialogue = '\n'.join(f'{m.role}: {m.content or ""}' for m in messages)
    prompt = SUMMARY_PROMPT.format(dialogue=dialogue)
    reply=llm.chat([Message.user(prompt)])
    return reply.content.strip()




def compress(messages: list[Message], llm: ChatModel, max_tokens: int) -> list[Message]:
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
    total = sum(estimate_tokens(m.content or '') for m in messages)
    if total <= max_tokens:
        return messages
    new_list=truncate(messages,max_tokens)
    kept_ids = {id(m) for m in new_list}  # 存 id（int，可哈希）
    cutted_list = [m for m in messages if id(m) not in kept_ids]
    #现在我要把cutted_summery插在new_List的system message后边 (insert)
    try:
        sum_ed_list=summarize(cutted_list,llm)
    except Exception:
        return new_list
    if not sum_ed_list:
        return new_list
    sum_ed_meesage=Message.system(f"之前的摘要{sum_ed_list}")
    new_list.insert(1,sum_ed_meesage)
    #再次检查一遍看看插入摘要消息之后会不会再次超过
    total_after = sum(estimate_tokens(m.content or '') for m in new_list)
    if total_after > max_tokens:
        return truncate(new_list, max_tokens)

    return new_list