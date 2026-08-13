"""Generate a long, fixed context prefix plus a variable-length uncached tail.

``build_long_context`` returns a byte-identical string on every call, so
sending it repeatedly as the ``system`` message makes request #1 populate the
prompt cache and all following requests reuse it (cached input tokens).

``build_variable_tail`` returns a string of roughly ``approx_tokens`` tokens;
it is appended to the ``user`` message (after the cached system prefix) so it
is always uncached, making the total prompt length vary per request without
breaking cache-hit semantics.
"""

import random

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


def build_variable_tail(approx_tokens=0, seed=""):
    """Build a variable-length uncached tail of roughly ``approx_tokens`` tokens.

    The tail is meant to be appended to the ``user`` message, AFTER the fixed
    ``system`` prefix. Because prompt caching caches the leading prefix (the
    system message), the tail — which lives in the user message — is never
    cached regardless of its content. Making ``approx_tokens`` vary per request
    therefore varies the total ``prompt_tokens`` (prefix + user) without
    affecting the cached portion, preserving cache-hit semantics.

    For Chinese text ~2 characters per token, so we target that many characters
    by repeating the base section (granularity ~150 tokens per section).
    ``approx_tokens <= 0`` returns an empty string (no tail). ``seed`` is
    prepended as a short unique marker so each request's tail is byte-distinct
    (prevents any accidental cross-request caching of the user message and makes
    individual requests distinguishable in logs).
    """
    if approx_tokens <= 0:
        return ""
    target_chars = approx_tokens * 2
    section = _BASE_SECTION
    repeats = max(1, (target_chars + len(section) - 1) // len(section))
    body = "\n\n".join([section] * repeats)
    if seed:
        return f"[tail:{seed}]\n\n{body}"
    return body


def random_tail_tokens(min_tokens=0, max_tokens=2000):
    """Return a random target tail length in [min_tokens, max_tokens].

    Uses ``min``/``max`` to clamp so the result is always a valid int in range
    even if the caller passes a reversed or equal pair.
    """
    lo = min(min_tokens, max_tokens)
    hi = max(min_tokens, max_tokens)
    return random.randint(lo, hi)
