"""Generate a long, fixed context prefix to maximise prompt-cache hits.

The returned string is byte-identical on every call, so sending it repeatedly
as the leading part of the prompt makes request #1 populate the cache and all
following requests reuse it (cached input tokens).
"""

_BASE_SECTION = (
    "上下文缓存（Prompt Cache）是大模型推理服务降本增效的关键技术之一。"
    "其核心思想是：当多次推理请求共享相同的长前缀时，服务端将该前缀对应的KV缓存"
    "在显存或内存中保留一段时间，后续命中缓存的请求可直接复用这部分KV，从而显著降低"
    "首Token延迟与输入侧的计费开销。glm-5.2是智谱AI推出的通用大语言模型，支持长上下文"
    "理解、多轮对话、代码生成、数学推理与工具调用等能力，并内置了上下文缓存机制。"
    "在计费层面，输入侧区分缓存命中与未命中两种情况：缓存命中部分按较低单价计费，"
    "未命中部分按标准输入单价计费，输出部分按输出单价计费。因此，构造相同的长前缀"
    "并重复请求，是验证缓存命中与计费正确性的有效手段。本测试以test01身份对glm-5.2"
    "发起多次包含该长前缀的请求，并通过运营管理接口核对总量与收入公式。"
)


def build_long_context(approx_tokens=3000, tag=""):
    """Build a long fixed prefix of roughly ``approx_tokens`` tokens.

    For Chinese text ~2 characters per token, so we target that many characters
    by repeating the base section. ``tag`` is prepended once at the very start;
    it becomes part of the cache key, so different runs (different tags) each
    independently fill then hit the cache, and are distinguishable in the data.
    Within a single run the tag is fixed, so every request shares the prefix
    and request #1 fills the cache while #2..N hit it.
    """
    target_chars = approx_tokens * 2
    section = _BASE_SECTION
    repeats = max(1, (target_chars + len(section) - 1) // len(section))
    body = "\n\n".join([section] * repeats)
    if tag:
        return f"[run-tag:{tag}]\n\n{body}"
    return body
