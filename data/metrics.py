"""Shared metric helpers used by both conftest.py (for the baseline snapshot)
and tests/test_glm52_cache_billing.py (for the after-snapshot + delta check).

Kept free of allure/pytest imports so it can be reused in any context.
"""


def find_today_entry(cost, user_id, today):
    """Return today's per-user row from a daily-cost response, or None."""
    if not isinstance(cost, dict):
        return None
    rows = cost.get("data", [])
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("user_id")) == str(user_id) and row.get("date") == today:
            return row
    return None


def find_model_entry(entry, model_name):
    """Return the per-model sub-entry for ``model_name`` from a daily-cost row."""
    if not isinstance(entry, dict):
        return None
    for m in entry.get("models", []) or []:
        if isinstance(m, dict) and m.get("model_name") == model_name:
            return m
    return None


def cached_of(usage):
    """Cached input tokens for one model request, tolerating several field names.

    ⚠️ releases/xb01 口径: 客户响应里的 ``cached_input_tokens`` /
    ``prompt_tokens_details.cached_tokens`` 是 **计费口径 (billed)** 的命中量，
    由 ``relay/adaptor/openai/usage_filter.go:38-83`` 用
    ``model.ComputeBilledCachedTokens`` 算出后**改写**进响应 (commit
    f08d2f02)。缓存命中率区间优化开启时 billed ≠ 真实 KV cache 命中 (±5% 抖动,
    ``model/cache_optimize.go``)；未配置/冷启动/Redis 异常时 billed == 真实值。
    而 daily-cost 接口 (``controller/log.go:564``) 用的是 logs 表的 **真实**
    ``cached_tokens``。因此 ``sum_request_tokens`` 里的 cached 是 **billed**，
    不能与 daily-cost delta 的 cached 直接强相等——见 daily-cost 测试的口径说明。
    """
    if not isinstance(usage, dict):
        return 0
    v = usage.get("cached_input_tokens")
    if isinstance(v, (int, float)):
        return int(v)
    details = usage.get("prompt_tokens_details") or usage.get("prompt_cache_hit_tokens")
    if isinstance(details, dict):
        v = details.get("cached_tokens") or details.get("cached_input_tokens")
        if isinstance(v, (int, float)):
            return int(v)
    if isinstance(details, (int, float)):
        return int(details)
    return 0


def tpm_bucket_size(window_sec):
    """Mirror nexus ``model/tpm_capacity.go:tpmBucketSize`` bucket-width rule.

    The operation ``tpm-capacity`` report adaptively buckets ``logs`` rows by:
      window ≤ 6h   → 60s   (per-minute)
      window ≤ 2d   → 300s  (5-minute)
      else          → 3600s (1-hour)
    ``series[].user_tpm`` is a per-minute rate, so a bucket's token total =
    ``(bucket_sec/60) * user_tpm``. Tests must derive the bucket width from the
    *query* window (not a hardcoded 5 min), else wide windows (hourly buckets)
    are miscomputed.
    """
    if window_sec <= 6 * 3600:
        return 60
    if window_sec <= 2 * 24 * 3600:
        return 300
    return 3600


def sum_request_tokens(usages):
    """Aggregate the *actual* tokens consumed by our model requests.

    Grounded in nexus ``relay/controller/helper.go`` (releases/xb01):
      - ``promptTokens := usage.PromptTokens`` (helper.go:126) — the FULL prompt,
        cached input included (cached is NOT subtracted). This is what gets
        stored as ``logs.prompt_tokens`` (``model/log.go:25``).
      - ``cachedTokens = usage.PromptTokensDetails.CachedTokens`` clamped to
        prompt (helper.go:193-198) — the **real** KV cache hit, stored as
        ``logs.cached_tokens`` (``model/log.go:27``).
      - ``billedCachedTokens`` (helper.go:204-210) — cache-hit-rate optimization
        adjusted value, stored as ``logs.cached_tokens_billed``
        (``model/log.go:30``); used only for **quota** deduction (helper.go:218-220).
      - ``totalTokens := promptTokens + completionTokens`` (helper.go:237) — what
        ``common.RecordTPMUsage`` records (helper.go:319/372) and what
        ``tpm-capacity`` sums from logs (``tpm_capacity.go:244``).

    Returns a dict with total_input, cached_input, uncached_input, completion,
    total_tokens, request_count.

    ⚠️ ``cached_input`` here is the **billed** value from the customer-facing
    ``usage`` (see ``cached_of``), which may differ from the **real** cached
    reported by daily-cost when cache-hit-rate optimization is active.
    ``total_input`` (= full prompt) and ``completion`` have no billed/real split
    and are the safe fields for cross-checking against daily-cost deltas.
    """
    total_input = sum(int(u.get("prompt_tokens", 0) or 0) for u in usages)
    cached_input = sum(cached_of(u) for u in usages)
    completion = sum(int(u.get("completion_tokens", 0) or 0) for u in usages)
    uncached_input = max(total_input - cached_input, 0)
    return {
        "total_input": total_input,
        "cached_input": cached_input,
        "uncached_input": uncached_input,
        "completion": completion,
        "total_tokens": total_input + completion,  # nexus helper.go:237
        "request_count": len(usages),
    }


def expected_amount(
    uncached, cached, completion, input_price, cached_price, output_price
):
    """Revenue = input_price * uncached + cached_price * cached + output_price * completion.

    Prices are CNY per million tokens. This mirrors nexus
    ``controller/log.go:computeLogCost`` (releases/xb01), which the daily-cost
    interface uses for both per-model and aggregate ``amount``
    (``controller/log.go:582``). ``computeLogCost`` uses the **real** cached
    (``r.CachedTokens``, ``log.go:582``), so the daily-cost ``amount`` is computed
    on real cached — NOT the billed cached the customer sees in ``usage``.

    Note: this is the **operation-platform pricing layer** (per-Mtok CNY), distinct
    from nexus's internal quota billing (``helper.go:215-230`` uses
    ``billedCachedTokens`` + ``PriceQuotaPerToken`` × group ratio). The daily-cost
    interface asserts the operation platform's displayed revenue, not quota.
    """
    return (
        input_price * uncached / 1e6
        + cached_price * cached / 1e6
        + output_price * completion / 1e6
    )


def extract_snapshot(entry):
    """Pull the comparable token/amount fields out of a daily-cost row (or None)."""
    if entry is None:
        return {
            "uncached_input_tokens": 0,
            "cached_input_tokens": 0,
            "completion_tokens": 0,
            "amount": 0.0,
        }
    return {
        "uncached_input_tokens": int(entry.get("uncached_input_tokens", 0) or 0),
        "cached_input_tokens": int(entry.get("cached_input_tokens", 0) or 0),
        "completion_tokens": int(entry.get("completion_tokens", 0) or 0),
        "amount": float(entry.get("amount", 0.0) or 0.0),
    }


def delta_snapshots(after, before):
    """after - before for each snapshot field (after values clamp at 0)."""
    return {
        "uncached_input_tokens": max(
            after["uncached_input_tokens"] - before["uncached_input_tokens"], 0
        ),
        "cached_input_tokens": max(
            after["cached_input_tokens"] - before["cached_input_tokens"], 0
        ),
        "completion_tokens": max(
            after["completion_tokens"] - before["completion_tokens"], 0
        ),
        "amount": round(max(after["amount"] - before["amount"], 0.0), 6),
    }


# ---------------------------------------------------------------------------
# cache-optimize overview helpers
#
# The overview endpoint (controller/cache_optimize.go:GetCacheOptimizeOverview)
# returns per-user rows with month & hour aggregates of prompt/real-cached/
# billed-cached tokens, derived rates, and Redis period counters (ctr_*).
# These helpers extract a comparable snapshot and compute deltas, mirroring the
# handler's buildCacheOptRow so the test can cross-check against actual data.
# ---------------------------------------------------------------------------


def find_overview_user(raw, user_id):
    """Return the row for ``user_id`` from a cache-optimize overview response.

    The handler always emits a row for any user with month traffic OR any user
    configured in the optimization config (``controller/cache_optimize.go:76-88``),
    so test01 appears once it has any month traffic (or is configured).
    """
    if not isinstance(raw, dict):
        return None
    rows = raw.get("data", [])
    for row in rows or []:
        if isinstance(row, dict) and str(row.get("user_id")) == str(user_id):
            return row
    return None


def extract_overview_snapshot(row):
    """Pull the comparable fields out of a cache-optimize overview row (or None).

    Mirrors ``controller/cache_optimize.go:cacheOptUserRow`` (line 18-49) and the
    rates computed in ``buildCacheOptRow`` (line 94-131):
      - ``real_rate`` = ``month_real / month_prompt`` (when prompt>0, else 0)
      - ``billed_rate`` = ``month_billed / month_prompt``
      - ``hour_real_rate`` = ``hour_real / hour_prompt``; ``hour_billed_rate`` likewise
      - ``ctr_*`` are Redis period counters (``cache_opt:{userId}:{YYYYMM}``):
        ``ctr_granted`` = Σ ``max(billed-real, 0)`` per request (cache_optimize.go:232-235)
    """
    if row is None:
        return {
            "has_target": False,
            "target_rate": 0.0,
            "delta": 0.0,
            "jitter": 0.0,
            "budget_tokens": 0,
            "month_prompt": 0,
            "month_real": 0,
            "month_billed": 0,
            "real_rate": 0.0,
            "billed_rate": 0.0,
            "hour_prompt": 0,
            "hour_real": 0,
            "hour_billed": 0,
            "hour_real_rate": 0.0,
            "hour_billed_rate": 0.0,
            "ctr_prompt": 0,
            "ctr_billed": 0,
            "ctr_granted": 0,
        }
    return {
        "has_target": bool(row.get("has_target", False)),
        "target_rate": float(row.get("target_rate", 0.0) or 0.0),
        "delta": float(row.get("delta", 0.0) or 0.0),
        "jitter": float(row.get("jitter", 0.0) or 0.0),
        "budget_tokens": int(row.get("budget_tokens", 0) or 0),
        "month_prompt": int(row.get("month_prompt", 0) or 0),
        "month_real": int(row.get("month_real", 0) or 0),
        "month_billed": int(row.get("month_billed", 0) or 0),
        "real_rate": float(row.get("real_rate", 0.0) or 0.0),
        "billed_rate": float(row.get("billed_rate", 0.0) or 0.0),
        "hour_prompt": int(row.get("hour_prompt", 0) or 0),
        "hour_real": int(row.get("hour_real", 0) or 0),
        "hour_billed": int(row.get("hour_billed", 0) or 0),
        "hour_real_rate": float(row.get("hour_real_rate", 0.0) or 0.0),
        "hour_billed_rate": float(row.get("hour_billed_rate", 0.0) or 0.0),
        "ctr_prompt": int(row.get("ctr_prompt", 0) or 0),
        "ctr_billed": int(row.get("ctr_billed", 0) or 0),
        "ctr_granted": int(row.get("ctr_granted", 0) or 0),
    }


# Cumulative counters whose after-before diff is meaningful. hour_* reset every
# natural hour, so they are only diff'd when baseline & after share the same
# Beijing-natural-hour bucket (see beijing_hour_bucket); month_* and ctr_* are
# monotonic within a month.
_OVERVIEW_CUMULATIVE_KEYS = (
    "month_prompt",
    "month_real",
    "month_billed",
    "hour_prompt",
    "hour_real",
    "hour_billed",
    "ctr_prompt",
    "ctr_billed",
    "ctr_granted",
)


def delta_overview_snapshots(after, before):
    """after - before for the overview cumulative counters (raw, no clamp).

    month_* and ctr_* are monotonic within a period so never go negative.
    hour_* reset each natural hour, so a negative hour diff signals a bucket
    rollover — callers must gate hour-delta assertions on
    ``beijing_hour_bucket(baseline_ts) == beijing_hour_bucket(after_ts)``.
    """
    return {k: after[k] - before[k] for k in _OVERVIEW_CUMULATIVE_KEYS}


def beijing_hour_bucket(ts):
    """Unix start of the Beijing-natural-hour containing ``ts``.

    The overview ``hour_*`` aggregate covers ``[hourStart, now]`` in Beijing time
    (``controller/cache_optimize.go:65``, ``time.FixedZone("CST", 8*3600)``).
    Used to decide whether the baseline and after-overview snapshots were taken
    in the same natural hour (only then is the hour delta attributable to our
    run; otherwise the hour window rolled over and the delta is meaningless).
    """
    return (ts + 8 * 3600) // 3600 * 3600 - 8 * 3600


# ---------------------------------------------------------------------------
# log list + log stat helpers
#
# The log list (controller/log.go:GetAllLogs) and stat (GetLogsStat) both query
# consume logs (type=2). The list paginates (ItemsPerPage=10, order by id desc)
# and fills each row's ``cost`` via ``fillLogCosts`` using the REAL
# ``cached_tokens`` (computeLogCost, log.go:225-228). The stat aggregates
# (COUNT/SUM) plus ``cost_amount`` = ``sumModelCosts`` which uses the BILLED
# ``cached_tokens_billed`` (log.go:235-244). For test01 (no optimization,
# billed==real) the two cost paths agree numerically.
# ---------------------------------------------------------------------------


def filter_run_logs(entries, user_id, model_name, win_start, win_end, pad_sec=60):
    """Filter log-list rows to those attributable to our run.

    Matches rows that are: consume logs (type=2), belong to ``user_id``, use
    ``model_name``, and have ``created_at`` within ``[win_start - pad, win_end +
    pad]``. The server list is pre-filtered by username/model/window, so this is
    a defensive client-side re-filter (guards against type=0 returning non-
    consume rows, and against timestamp edge cases at the padded boundary).
    """
    out = []
    lo = win_start - pad_sec
    hi = win_end + pad_sec
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        if str(e.get("user_id")) != str(user_id):
            continue
        if e.get("model_name") != model_name:
            continue
        if int(e.get("type", 0) or 0) != 2:  # LogTypeConsume = 2
            continue
        ca = int(e.get("created_at", 0) or 0)
        if ca < lo or ca > hi:
            continue
        out.append(e)
    return out


def log_entry_cost(entry, input_price, cached_price, output_price):
    """Mirror nexus ``computeLogCost`` (controller/log.go:211-222) for a log row.

    Uses the row's REAL ``cached_tokens`` (``fillLogCosts`` at log.go:227 passes
    ``lg.CachedTokens``), clamped to ``prompt_tokens``. Returns:
        ((prompt - cached) * input_price + cached * cached_price
         + completion * output_price) / 1e6
    """
    prompt = int(entry.get("prompt_tokens", 0) or 0)
    cached = int(entry.get("cached_tokens", 0) or 0)
    if cached > prompt:
        cached = prompt
    completion = int(entry.get("completion_tokens", 0) or 0)
    return expected_amount(
        prompt - cached, cached, completion, input_price, cached_price, output_price
    )


def extract_log_stat(raw):
    """Pull the comparable fields out of a /api/log/stat response (or None).

    Mirrors ``model.LogsSummary`` (model/log.go:300-310): ``request_count`` /
    ``prompt_tokens`` / ``completion_tokens`` / ``quota`` are COUNT/SUM (all
    rows); ``avg_elapsed_ms`` / ``avg_ttft_ms`` are AVG over rows where the field
    > 0, truncated to int (log.go:365-366); ``cost_amount`` is filled by
    ``sumModelCosts`` (BILLED cached).
    """
    if not isinstance(raw, dict):
        return None
    data = raw.get("data")
    if not isinstance(data, dict):
        return None
    return {
        "request_count": int(data.get("request_count", 0) or 0),
        "prompt_tokens": int(data.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(data.get("completion_tokens", 0) or 0),
        "quota": int(data.get("quota", 0) or 0),
        "avg_elapsed_ms": int(data.get("avg_elapsed_ms", 0) or 0),
        "avg_ttft_ms": int(data.get("avg_ttft_ms", 0) or 0),
        "cost_amount": float(data.get("cost_amount", 0.0) or 0.0),
    }


def delta_log_stat(after, before):
    """Delta (after - before) for the cumulative stat fields + reconstructed avg.

    ``request_count`` / ``prompt_tokens`` / ``completion_tokens`` / ``quota`` /
    ``cost_amount`` are cumulative over a fixed window → the after-before diff
    equals this run's contribution (assuming no concurrent traffic by the same
    user in the window; the diff of pre-existing traffic cancels since the window
    is fixed and those rows are present in both snapshots).

    ``avg_elapsed_ms`` / ``avg_ttft_ms`` are averages, so we reconstruct the
    approximate per-row total via ``avg * request_count`` and diff that:
        delta_avg = (after.avg * after.count - before.avg * before.count) / delta.count
    Caveat: nexus averages only rows where the field > 0, but ``request_count``
    counts ALL rows; the reconstruction is exact only when every row has a
    measured value. For test01's non-stream requests ``elapsed_time > 0`` so the
    delta_avg is reliable; the baseline contribution may carry a small error if
    pre-existing baseline rows had ``elapsed_time = 0``. Callers should treat
    delta_avg with a relative tolerance.
    """
    rc = after["request_count"] - before["request_count"]
    delta = {
        "request_count": rc,
        "prompt_tokens": after["prompt_tokens"] - before["prompt_tokens"],
        "completion_tokens": after["completion_tokens"] - before["completion_tokens"],
        "quota": after["quota"] - before["quota"],
        "cost_amount": round(after["cost_amount"] - before["cost_amount"], 6),
        "delta_avg_elapsed_ms": 0.0,
        "delta_avg_ttft_ms": 0.0,
    }
    if rc != 0:
        delta["delta_avg_elapsed_ms"] = (
            after["avg_elapsed_ms"] * after["request_count"]
            - before["avg_elapsed_ms"] * before["request_count"]
        ) / rc
        delta["delta_avg_ttft_ms"] = (
            after["avg_ttft_ms"] * after["request_count"]
            - before["avg_ttft_ms"] * before["request_count"]
        ) / rc
    return delta


# ---------------------------------------------------------------------------
# stress-metrics helpers
#
# The stress-metrics dashboard (controller/usage.go:GetStressMetrics →
# model/stress_metrics.go:GetStressMetrics, releases/xb01) loads all consume
# rows in [start, end] into Go memory and computes summary + per-minute series
# + input/output token histograms in a single pass. The histograms bucket each
# request's prompt_tokens / completion_tokens into fixed ranges. The bucket
# boundaries and the input/output bucket functions below are direct ports of
# nexus ``inputBucket`` / ``outputBucket`` (stress_metrics.go:283-310), verified
# against the Go test cases in ``stress_metrics_test.go:6-27``.
#
# Because the endpoint has no user_id / username filter (only model_name /
# token_name), the test uses a fixed-window baseline-delta approach (same as
# log-stat): the baseline is captured before the requests, the after-snapshot
# after, and the delta isolates this run's contribution (pre-existing traffic
# is in both snapshots and cancels).
# ---------------------------------------------------------------------------

# Input histogram: 9 buckets.  Bounds are upper-inclusive per ``inputBucket``.
#   v==0 → 0; v<=bounds[i] → i+1; else → len(bounds)+1 (>100k).
IN_HIST_LABELS = [
    "0",
    "1-100",
    "100-1k",
    "1k-5k",
    "5k-10k",
    "10k-20k",
    "20k-50k",
    "50k-100k",
    ">100k",
]
IN_HIST_BOUNDS = [100, 1000, 5000, 10000, 20000, 50000, 100000]

# Output histogram: 9 buckets.  v>=10000 → last bucket (10000(满)).
#   v==0 → 0; v<=bounds[i] → i+1; else → len(labels)-2 (5k-9999).
OUT_HIST_LABELS = [
    "0",
    "1-32",
    "32-100",
    "100-500",
    "500-1k",
    "1k-2k",
    "2k-5k",
    "5k-9999",
    "10000(满)",
]
OUT_HIST_BOUNDS = [32, 100, 500, 1000, 2000, 5000]


def input_bucket(v):
    """Mirror nexus ``model/stress_metrics.go:inputBucket`` (releases/xb01).

    ``v==0`` → 0 (the "0" bucket); ``v <= bounds[i]`` → ``i+1``; no match →
    ``len(bounds)+1`` (the ">100k" bucket).  Verified against Go test cases in
    ``stress_metrics_test.go:6-14``.

    >>> [(v, input_bucket(v)) for v in
    ...  [0, 1, 100, 101, 1000, 1001, 5000, 10000, 20000, 50000,
    ...   100000, 100001, 476994]]
    [(0, 0), (1, 1), (100, 1), (101, 2), (1000, 2), (1001, 3), (5000, 3),
     (10000, 4), (20000, 5), (50000, 6), (100000, 7), (100001, 8), (476994, 8)]
    """
    v = int(v)
    if v == 0:
        return 0
    for i, b in enumerate(IN_HIST_BOUNDS):
        if v <= b:
            return i + 1
    return len(IN_HIST_BOUNDS) + 1


def output_bucket(v):
    """Mirror nexus ``model/stress_metrics.go:outputBucket`` (releases/xb01).

    ``v==0`` → 0; ``v>=10000`` → ``len(OUT_HIST_LABELS)-1`` (the "10000(满)"
    bucket, hits max_tokens cap); ``v <= bounds[i]`` → ``i+1``; no match →
    ``len(OUT_HIST_LABELS)-2`` (the "5k-9999" bucket).  Verified against Go
    test cases in ``stress_metrics_test.go:17-27``.

    >>> [(v, output_bucket(v)) for v in
    ...  [0, 32, 100, 500, 1000, 2000, 5000, 5001, 9999, 10000, 20000]]
    [(0, 0), (32, 1), (100, 2), (500, 3), (1000, 4), (2000, 5), (5000, 6),
     (5001, 7), (9999, 7), (10000, 8), (20000, 8)]
    """
    v = int(v)
    if v == 0:
        return 0
    if v >= 10000:
        return len(OUT_HIST_LABELS) - 1
    for i, b in enumerate(OUT_HIST_BOUNDS):
        if v <= b:
            return i + 1
    return len(OUT_HIST_LABELS) - 2


def expected_hist_counts(per_request_values, bucket_fn, num_buckets):
    """Compute the expected histogram counts from per-request token values.

    Replicates the Go loop at ``stress_metrics.go:169-170``
    (``m.InHist.Counts[inputBucket(r.PromptTokens)]++``).  Given a list of
    per-request token counts and a bucket function, returns a list of
    ``num_buckets`` ints with the count in each bucket.
    """
    counts = [0] * num_buckets
    for v in per_request_values:
        counts[bucket_fn(v)] += 1
    return counts


def extract_stress_snapshot(raw):
    """Pull the comparable fields from a stress-metrics response (or None).

    Mirrors the ``StressMetrics`` / ``StressSummary`` / ``StressHistogram``
    structs (``model/stress_metrics.go:35-73``).  Returns a dict with:
      - ``n``, ``intok_total``, ``outtok_total``, ``intok_max``, ``outtok_max``,
        ``peak_conc``, ``lat_avg``, ``lat_max``  (summary card)
      - ``start``, ``end``  (min/max created_at, Beijing-time formatted)
      - ``in_hist_counts`` / ``out_hist_counts``  (list[int], 9 elements each)
      - ``in_hist_labels`` / ``out_hist_labels``  (list[str], from the response)
    """
    if not isinstance(raw, dict):
        return None
    data = raw.get("data")
    if not isinstance(data, dict):
        return None
    summary = data.get("summary") or {}
    in_hist = data.get("in_hist") or {}
    out_hist = data.get("out_hist") or {}
    return {
        "n": int(summary.get("n", 0) or 0),
        "start": summary.get("start", ""),
        "end": summary.get("end", ""),
        "intok_total": int(summary.get("intok_total", 0) or 0),
        "outtok_total": int(summary.get("outtok_total", 0) or 0),
        "intok_max": int(summary.get("intok_max", 0) or 0),
        "outtok_max": int(summary.get("outtok_max", 0) or 0),
        "peak_conc": int(summary.get("peak_conc", 0) or 0),
        "lat_avg": float(summary.get("lat_avg", 0.0) or 0.0),
        "lat_max": float(summary.get("lat_max", 0.0) or 0.0),
        "in_hist_counts": [int(c) for c in (in_hist.get("counts") or [])],
        "out_hist_counts": [int(c) for c in (out_hist.get("counts") or [])],
        "in_hist_labels": list(in_hist.get("labels") or []),
        "out_hist_labels": list(out_hist.get("labels") or []),
    }


def delta_hist_counts(after_counts, before_counts):
    """Element-wise delta for histogram counts (after - before).

    The baseline and after snapshots use the same fixed window, so
    pre-existing traffic cancels and the delta equals this run's per-bucket
    request counts.  Returns a list aligned with the histogram labels.
    """
    return [a - b for a, b in zip(after_counts, before_counts)]
