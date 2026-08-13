"""GLM-5.2 cache-hit & billing correctness tests for user test01.

Both tests depend on the session-scoped ``model_requests`` fixture: rounds 1-2
send REQUEST_COUNT long-context requests each (fixed 3k prefix → cache hits,
variable tail randomly assigned across 5 stress-metrics input buckets), then
after a 2-min gap round 3 sends ROUND3_COUNT short independent requests (input/
output 1-1k token, no shared prefix); the fixture then waits for the usage
pipeline to propagate.

Grounded in the nexus ``releases/xb01`` branch.

Tests (in file order):
  1. ``test_daily_cost_revenue`` - revenue formula check on today's test01
     daily-cost entry, asserted with the configured glm-5.2 prices (input=8,
     cached=2, output=28, CNY per million tokens):
         amount = 8 * uncached_real/1e6 + 2 * cached_real/1e6 + 28 * completion/1e6
     (mirrors nexus ``controller/log.go:computeLogCost``). Note the cached split
     the interface reports is the **real** KV-cache hit, while the customer-facing
     model ``usage`` returns the **billed** (optimization-adjusted) cached; these
     can differ by ±5% when cache-hit-rate optimization is active.
   2. ``test_tpm_capacity`` - asserts that the tokens test01 *actually consumed*
      (summed from the model ``usage`` of the requests we sent) are consistent
      with the per-time-bucket ``user_tpm`` the operation interface reports for
      the same window. ``series[].user_tpm`` is a per-minute rate over
      **adaptive** buckets (≤6h→1min, ≤2d→5min, else 1h per nexus
      ``tpm_capacity.go:tpmBucketSize``), so reported tokens =
      bucket_min * sum(user_tpm) for the buckets that overlap our request window.
   3. ``test_cache_optimize_overview`` - asserts the cache-hit-rate optimization
      dashboard (``/api/admin/usage/cache-optimize/overview``) reports consistent
      per-user stats for test01: actual hit rate (``real_rate``), billing hit
      rate (``billed_rate``), this-hour hit rate (``hour_*_rate``) and month
      concession (``ctr_granted``). Because test01 has no active optimization
      target (``has_target=false``), ``ComputeBilledCachedTokens`` returns the
      real cached unchanged (``cache_optimize.go:300-310``) → billed==real per
      request → ``month_real==month_billed``, ``real_rate==billed_rate``,
      ``ctr_granted==0``. The run's delta (after - baseline captured before the
      requests) is cross-checked against the per-request usage totals.
   4. ``test_log_list_entries`` - asserts the operation log list
      (``/api/log/``) rows for test01 in this run's window match the actual
      request data: request time (``created_at`` within the run window), model
      name, username, input/output/total tokens, and per-row ``cost``
      (``fillLogCosts`` uses the REAL ``cached_tokens`` via ``computeLogCost``).
      The multiset of ``(prompt, completion, cached)`` across the rows must equal
      the multiset from our model ``usage`` (test01 billed==real, so the log's
      real cached equals our billed cached).
    5. ``test_log_stat_delta`` - asserts the log stat aggregate
       (``/api/log/stat``) via a baseline-delta: the stat is captured BEFORE the
       requests and AFTER, and ``(after - baseline)`` must equal this run's
       request count, prompt + completion tokens, and total cost
       (``cost_amount`` = ``sumModelCosts`` on the BILLED cached; test01
       billed==real). ``avg_elapsed_ms`` is cross-checked against the mean
       ``elapsed_time`` of the run's log rows (both server-side, exact).
    6. ``test_stress_metrics`` - asserts the stress-test dashboard
       (``/api/admin/usage/stress-metrics``) input/output token distribution
       via a baseline-delta over a fixed window with ``model_name=glm-5.2``
       filter. The delta ``in_hist.counts`` (9-bucket prompt-token histogram)
       and ``out_hist.counts`` (9-bucket completion-token histogram) must equal
       the expected distribution computed by bucketing our per-request
       ``prompt_tokens`` / ``completion_tokens`` with the nexus ``inputBucket``
       / ``outputBucket`` functions (``stress_metrics.go:283-310``). Summary
       card deltas (``n``, ``intok_total``, ``outtok_total``) are cross-checked
       against our request count and token sums; ``intok_max`` / ``outtok_max``
       are max-aggregates validated via ``after >= max(baseline, our_max)``.
"""

import time
from datetime import datetime

import allure
import pytest

import config
from data.metrics import (
    IN_HIST_LABELS,
    OUT_HIST_LABELS,
    beijing_hour_bucket,
    cached_of,
    delta_hist_counts,
    delta_log_stat,
    delta_overview_snapshots,
    delta_snapshots,
    expected_amount,
    expected_hist_counts,
    extract_log_stat,
    extract_overview_snapshot,
    extract_snapshot,
    extract_stress_snapshot,
    filter_run_logs,
    find_overview_user,
    find_today_entry,
    input_bucket,
    log_entry_cost,
    output_bucket,
    sum_request_tokens,
    tpm_bucket_size,
)
from report.allure_utils import attach_json, record_assertion


# ---------------------------------------------------------------------------
# assertion helpers
# ---------------------------------------------------------------------------


def _check(name, expected, actual, passed, detail=None, tolerance=None):
    """Record an assertion to allure (expected vs actual) and hard-assert.

    Always records the expected/actual pair; on failure raises AssertionError
    **inside** the ``@allure.step`` context (via ``record_assertion(
    raise_on_fail=True)``) so allure marks the step broken and the failure is
    visible in the report.
    """
    record_assertion(
        name,
        expected,
        actual,
        passed,
        detail=detail,
        tolerance=tolerance,
        raise_on_fail=True,
    )


def _assert_amount(amount, uncached, cached, completion, ip, cp, op, where, tol=0.01):
    expected = expected_amount(uncached, cached, completion, ip, cp, op)
    diff = abs(amount - expected)
    passed = diff <= tol
    _check(
        f"收入公式 {where}",
        expected=round(expected, 6),
        actual=amount,
        passed=passed,
        detail=(
            f"{where}: actual={amount}, expected={expected}, diff={diff}; "
            f"uncached={uncached}, cached={cached}, completion={completion}, "
            f"prices(input,cached,output)=({ip},{cp},{op})"
        ),
        tolerance=tol,
    )


def _delta_check(label, expected, actual, tol, detail=None):
    """Token-equality check for a delta field (expected == actual within tol)."""
    diff = abs(actual - expected)
    if detail is None:
        detail = f"{label}: expected(delta本次)={expected}, actual(delta接口)={actual}, diff={diff} > tol={tol}"
    _check(
        label,
        expected=expected,
        actual=actual,
        passed=diff <= tol,
        detail=detail,
        tolerance=tol,
    )


# ---------- tpm-capacity helpers ----------
#
# The real response shape (per nexus ``model/tpm_capacity.go`` on releases/xb01):
#   data.summary  = {capacity_tpm, avg_tpm, peak_tpm, valley_tpm, total_tokens, ...}
#   data.series   = [ {t:"MM-DD HH:MM"|"MM-DD HH:00", tpm, pct, user_tpm}, ... ]
#   data.hour_profile = [ {hour, avg_tpm, pct}, ... ]
#   data.channels = [ {id, name, max_tpm, status}, ... ]
# ``tpm`` / ``user_tpm`` are per-minute rates; the **bucket width is adaptive**
# (``tpm_capacity.go:79-88``): window ≤6h→60s, ≤2d→300s, else 3600s. So a bucket's
# token total = ``bucket_min * user_tpm`` where ``bucket_min = bucket_sec/60``.
# The query is a SQL ``GROUP BY (created_at DIV bucket_sec)*bucket_sec`` over
# ``logs(type=consume)`` ``sum(prompt_tokens)+sum(completion_tokens)``
# (``tpm_capacity.go:244``) — i.e. full prompt (cached included) + completion,
# matching nexus ``helper.go:237 totalTokens = prompt + completion``.


def _usage_tokens(usage):
    """Tokens nexus records as TPM for one request = prompt + completion.

    Per nexus ``relay/controller/helper.go:237``
    ``totalTokens := promptTokens + completionTokens`` then
    ``common.RecordTPMUsage(meta.UserId, totalTokens)`` (helper.go:319 / 372).
    ``promptTokens`` is the *full* ``usage.PromptTokens`` (cached input included,
    the cached portion is NOT subtracted before recording). We therefore derive
    the value from ``prompt_tokens + completion_tokens`` rather than trusting the
    upstream ``total_tokens`` (which some providers compute differently);
    ``total_tokens`` is only used as a sanity cross-check.
    """
    if not isinstance(usage, dict):
        return 0
    p = int(usage.get("prompt_tokens", 0) or 0)
    c = int(usage.get("completion_tokens", 0) or 0)
    total = usage.get("total_tokens")
    if isinstance(total, (int, float)) and int(total) != p + c:
        print(
            f"[tpm] WARN upstream total_tokens={int(total)} != prompt+completion={p + c}; "
            f"using prompt+completion per nexus helper.go:237"
        )
    return p + c


def _parse_bucket_ts(t_str):
    """Parse a series bucket label ``"MM-DD HH:MM"`` (or ``"MM-DD HH:00"`` for
    hourly buckets) to a unix timestamp.

    The label has no year; assume the current year. If that date is more than
    ~30 days ahead of now (year-boundary case) fall back to last year.
    """
    now = datetime.now()
    dt = datetime.strptime(f"{now.year}-{t_str}", "%Y-%m-%d %H:%M")
    # handle Dec->Jan wrap: if the bucket date is far in the future, it's last year
    if (dt - now).days > 30:
        dt = dt.replace(year=now.year - 1)
    return int(dt.timestamp())


def _overlapping_buckets(series, win_start, win_end, bucket_sec):
    """Return buckets whose [t, t+bucket_sec) range intersects [win_start, win_end].

    ``bucket_sec`` must match what the server used for this query window
    (``tpm_bucket_size``); a hardcoded 5-min span would miscompute hourly buckets.
    """
    span = bucket_sec
    out = []
    for b in series or []:
        if not isinstance(b, dict):
            continue
        t = b.get("t")
        if not t:
            continue
        try:
            b_start = _parse_bucket_ts(t)
        except (ValueError, TypeError):
            continue
        b_end = b_start + span
        if b_start < win_end and b_end > win_start:
            out.append(b)
    return out


# ---------- daily-cost helpers ----------


def _poll_daily_cost(admin_client, start_ts, end_ts):
    """Poll ``/api/daily-cost`` until today's test01 entry is ready.

    Uses the SHARED frozen page-default 近24小时 window (``start_ts``,
    ``end_ts``) — identical to what ``/api/tpm-capacity`` uses, since both
    interfaces belong to the same operation page and the page sends both
    requests with one captured "now". ``end_ts`` was captured once
    post-propagation; reused on every retry (the run's rows were created
    earlier so always land inside the window).
    """
    last = None
    for attempt in range(1, config.POLL_RETRIES + 1):
        cost = admin_client.get_daily_cost(
            start_ts,
            end_ts,
            config.TEST_USER_ID,
        )
        last = cost
        if find_today_entry(cost, config.TEST_USER_ID, config.today_str()) is not None:
            return cost
        print(
            f"[admin] daily-cost attempt {attempt}/{config.POLL_RETRIES}: "
            f"today's test01 entry not ready; retrying in {config.POLL_INTERVAL}s"
        )
        time.sleep(config.POLL_INTERVAL)
    return last


def _poll_overview(admin_client, baseline, our_total_input):
    """Poll the cache-optimize overview until test01's month delta reflects our run.

    Returns the last raw overview response. ``baseline`` is the pre-run snapshot
    (captured by ``model_requests``). Polling succeeds once the after-baseline
    month-prompt delta reaches 95% of our run's prompt total (our data has
    propagated into the month aggregate). If ``our_total_input`` is 0, returns
    the first response.
    """
    last = None
    threshold = max(int(our_total_input * 0.95), 1)
    for attempt in range(1, config.POLL_RETRIES + 1):
        raw = admin_client.get_cache_optimize_overview()
        last = raw
        row = find_overview_user(raw, config.TEST_USER_ID)
        if row is not None:
            if our_total_input <= 0:
                return raw
            snap = extract_overview_snapshot(row)
            delta_prompt = snap["month_prompt"] - baseline["month_prompt"]
            if delta_prompt >= threshold:
                return raw
        print(
            f"[admin] cache-opt overview attempt {attempt}/{config.POLL_RETRIES}: "
            f"test01 month delta not ready (need >= {threshold}); "
            f"retrying in {config.POLL_INTERVAL}s"
        )
        time.sleep(config.POLL_INTERVAL)
    return last


def _poll_log_stat(admin_client, baseline, our_request_count, win_start, win_end):
    """Poll ``/api/log/stat`` until the request_count delta reflects our run.

    Returns the last raw stat response. ``baseline`` is the pre-run snapshot
    (captured by ``model_requests`` over ``[win_start, now0]``). Both ``win_start``
    and ``win_end`` are the SHARED frozen page-default 近8天 window (identical to
    what ``/api/log/`` uses): ``win_start`` was frozen at baseline (= now0 -
    LOG_WINDOW_SPAN), ``win_end`` was captured once post-propagation (= now1, a
    real "now" > all this run's row created_at). The window only grew at the
    right edge vs baseline, so (after - baseline) cancels all pre-existing rows
    exactly and equals the rows in (now0, now1] (our run). Polling retries the
    SAME query until our consume logs have landed (their created_at < win_end, so
    once inserted they appear in the window). Succeeds once the after-baseline
    ``request_count`` delta reaches our run's request count. If
    ``our_request_count`` is 0, returns the first response.
    """
    last = None
    need = int(our_request_count)
    for attempt in range(1, config.POLL_RETRIES + 1):
        raw = admin_client.get_log_stat(
            type_=0,
            model_name="",
            token_name="",
            start_ts=win_start,
            end_ts=win_end,
            username=config.MODEL_USER,
            channel=0,
        )
        last = raw
        snap = extract_log_stat(raw)
        if snap is not None:
            if our_request_count <= 0:
                return raw
            delta_rc = snap["request_count"] - baseline["request_count"]
            if delta_rc >= need:
                return raw
        print(
            f"[admin] log stat attempt {attempt}/{config.POLL_RETRIES}: "
            f"test01 request_count delta not ready (need >= {need}); "
            f"retrying in {config.POLL_INTERVAL}s"
        )
        time.sleep(config.POLL_INTERVAL)
    return last


def _poll_stress_metrics(admin_client, baseline, our_request_count, win_start, win_end):
    """Poll ``/api/admin/usage/stress-metrics`` until the n delta reflects our run.

    Returns the last raw stress-metrics response. ``baseline`` is the pre-run
    snapshot (captured by ``model_requests`` over ``[win_start, now0]``). Both
    ``win_start`` and ``win_end`` are the SHARED frozen page-default 近24小时
    window: ``win_start`` was frozen at baseline (= now0 - 86400, stored as
    ``model_requests["stress_window_start"]``), ``win_end`` was captured once
    post-propagation (= now1, a real "now" > all this run's row created_at,
    stored as ``model_requests["stress_query_end"]``). The window only grew at
    the right edge vs baseline (same start), so (after - baseline) cancels all
    pre-existing rows in their overlap exactly and equals the rows in
    (now0, now1] (our run). Polling retries the SAME frozen query until our
    consume logs have landed (their created_at < win_end, so once inserted they
    appear in the window). Succeeds once the after-baseline ``n`` delta reaches
    our run's request count. If ``our_request_count`` is 0, returns the first
    response.

    NOTE: a real-time sliding 24h window (``start = now_poll - 86400`` recomputed
    per poll) would let the ~24h-ago segment fall out of the after window and
    pollute the delta with negative counts (the delta_n=13<20 incident); the
    frozen start is what makes the delta exact. Mirrors ``_poll_log_stat``.
    """
    last = None
    need = int(our_request_count)
    for attempt in range(1, config.POLL_RETRIES + 1):
        raw = admin_client.get_stress_metrics(
            start_ts=win_start,
            end_ts=win_end,
            model_name=config.MODEL_NAME,
        )
        last = raw
        snap = extract_stress_snapshot(raw)
        if snap is not None:
            if our_request_count <= 0:
                return raw
            delta_n = snap["n"] - baseline["n"]
            if delta_n >= need:
                return raw
        print(
            f"[stress] attempt {attempt}/{config.POLL_RETRIES}: "
            f"n delta not ready (need >= {need}, got "
            f"{snap['n'] - baseline['n'] if snap else '?'}); "
            f"retrying in {config.POLL_INTERVAL}s"
        )
        time.sleep(config.POLL_INTERVAL)
    return last


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


@allure.feature("每日收入计费")
@allure.story("实际请求数据 vs 接口数据 增量校验")
@allure.severity(allure.severity_level.CRITICAL)
def test_daily_cost_revenue(model_requests, admin_client):
    """Delta cross-check + revenue formula on today's test01 daily-cost entry.

    releases/xb01 grounding — two distinct cached token populations:
      - **real** cached (``logs.cached_tokens``): real KV-prefix-cache hits,
        clamped to prompt (``helper.go:193-198``). Persisted by
        ``model.RecordConsumeLog`` (``model/log.go:116``) with
        ``CreatedAt = now`` (``model/log.go:121``); field at ``model/log.go:27``
        (comment: "已 clamp 到 prompt_tokens，与计费缓存折扣口径一致").
      - **billed** cached (``logs.cached_tokens_billed``): cache-hit-rate
        optimization adjusted value (``helper.go:204-210`` via
        ``model.ComputeBilledCachedTokens``). Used ONLY for **quota** deduction
        (``helper.go:218-220``). When optimization is off/cold-start/Redis-down,
        billed == real (``cache_optimize.go:300-310``).

    The customer-facing model ``usage.cached_input_tokens`` is the **billed**
    value — ``relay/adaptor/openai/usage_filter.go:38-83`` computes billed and
    **rewrites** the response's cached_tokens (commit f08d2f02). The daily-cost
    interface instead aggregates the **real** ``logs.cached_tokens``
    (``controller/log.go:564``: ``cached := r.CachedTokens``), and computes amount
    via ``computeLogCost`` (``log.go:211-222,582``) on real cached.

    Consequences for the assertions:
      - ``total_input`` (full prompt) and ``completion`` have NO billed/real split
        → delta equality is robust. These are the PRIMARY token cross-checks.
      - ``cached`` / ``uncached`` split: ``our`` (billed) may differ from
        ``delta`` (real) when optimization is active (±5% jitter, possibly more
        across a session). We assert the split with a wider tolerance and treat
        the **interface delta as ground truth**; a diagnostic records the
        billed/real gap. The split is NOT a hard pass condition when the gap is
        large (optimization on).
      - amount: ``delta["amount"]`` == ``expected_amount(delta_uncached_real,
        delta_cached_real, delta_completion, prices)`` — a self-consistency
        check mirroring ``computeLogCost``. This is the strong, always-true
        amount assertion. The billed-based expected amount (from model usage) is
        kept only as a reference attachment (matches interface only when
        optimization off AND test01 had no other traffic today).

    Flow:
      1. ``model_requests`` captured a daily-cost **baseline** snapshot *before*
         any request (passed back in ``model_requests``).
      2. Query the **after** snapshot (polling until today's entry is ready).
      3. Compute delta = after - baseline for cached/uncached/completion/amount.
      4. Assert prompt (uncached+cached) and completion delta equality against
         the model usage sums; assert the interface amount self-consistency
         (computeLogCost on the interface's own real-cached delta).
      5. Keep the per-model and aggregate formula/price checks on the after
         entry (they use the interface's own real cached, matching computeLogCost).
    """
    baseline = model_requests["baseline"]
    print(f"[delta] 基线快照(请求前): {baseline}")

    with allure.step("请求后: 查询 daily-cost 并轮询至当天数据就绪"):
        cost = _poll_daily_cost(
            admin_client,
            model_requests["page_start_24h"],
            model_requests["page_end_24h"],
        )
        print(f"[admin] daily-cost raw: {cost}")
        attach_json(cost, "daily-cost 原始响应(请求后)")

    _check(
        "daily-cost 接口成功",
        expected=True,
        actual=bool(isinstance(cost, dict) and cost.get("success", True)),
        passed=isinstance(cost, dict) and cost.get("success", True),
        detail=f"daily-cost call failed: {cost!r}",
    )
    assert isinstance(cost, dict)  # type-narrowing; _check above already raises

    entry = find_today_entry(cost, config.TEST_USER_ID, config.today_str())
    _check(
        f"存在 test01(user_id={config.TEST_USER_ID}) 当天(date={config.today_str()}) 记录",
        expected=f"user_id={config.TEST_USER_ID}, date={config.today_str()}",
        actual=entry,
        passed=entry is not None,
        detail=(
            f"No daily-cost entry for user_id={config.TEST_USER_ID} on date={config.today_str()}. "
            f"Returned rows: {cost.get('data')!r}"
        ),
    )
    print(f"[assert] today's entry: {entry}")
    attach_json(entry, "当天 test01 记录(请求后)")
    assert entry is not None  # type-narrowing; _check above already raises

    after = extract_snapshot(entry)
    delta = delta_snapshots(after, baseline)
    print(f"[delta] after={after}, baseline={baseline}, delta={delta}")
    attach_json(
        {"baseline": baseline, "after": after, "delta": delta},
        "daily-cost 增量(基线/请求后/差值)",
    )

    agg_uncached = entry.get("uncached_input_tokens", 0)
    agg_cached = entry.get("cached_input_tokens", 0)
    agg_completion = entry.get("completion_tokens", 0)
    agg_amount = float(entry.get("amount", 0.0))
    has_unpriced = bool(entry.get("has_unpriced", False))
    models = entry.get("models", [])

    # ----- 实际请求数据 vs 接口增量 交叉对比 (核心断言) -----
    # 增量(after - baseline) 应等于本次请求实际消耗的 token / 收入。
    our = sum_request_tokens(model_requests["usages"])
    print(f"[delta] 本次实际消耗(模型侧 usage 汇总): {our}")
    attach_json(our, "本次运行实际消耗(模型侧 usage 汇总)")

    # nexus-grounded invariants on our own usage aggregation
    # (relay/controller/helper.go:126/193-198/237): cached is a subset of prompt,
    # so uncached+cached == prompt, and total == prompt + completion.
    with allure.step("nexus 口径自洽: cached⊆prompt, total=prompt+completion"):
        _check(
            "cached_input <= prompt_tokens (缓存是 prompt 的子集)",
            expected=f"cached={our['cached_input']} <= prompt={our['total_input']}",
            actual={
                "cached_input": our["cached_input"],
                "total_input": our["total_input"],
            },
            passed=our["cached_input"] <= our["total_input"],
            detail=(
                f"cached_input({our['cached_input']}) > prompt_tokens({our['total_input']}); "
                f"nexus helper.go:196-198 guarantees CachedTokens ⊆ PromptTokens"
            ),
        )
        _check(
            "uncached + cached == prompt_tokens (nexus 口径)",
            expected=our["total_input"],
            actual=our["uncached_input"] + our["cached_input"],
            passed=our["uncached_input"] + our["cached_input"] == our["total_input"],
            detail=(
                f"uncached({our['uncached_input']}) + cached({our['cached_input']}) "
                f"!= prompt({our['total_input']})"
            ),
        )
        _check(
            "total_tokens == prompt + completion (nexus helper.go:237)",
            expected=our["total_input"] + our["completion"],
            actual=our["total_tokens"],
            passed=our["total_tokens"] == our["total_input"] + our["completion"],
            detail=(
                f"total_tokens({our['total_tokens']}) != prompt({our['total_input']}) "
                f"+ completion({our['completion']})"
            ),
        )

    # billed vs real cached diagnostic. our.cached_input is the BILLED value the
    # customer sees (usage_filter.go rewrites it); delta.cached_input_tokens is
    # the REAL value daily-cost aggregates from logs (log.go:564). When cache-hit
    # optimization is off, billed==real; when on, they differ by ±5% (or more
    # across a session). Record the gap so a mismatch in the split assertions
    # below is explainable.
    cached_gap = delta["cached_input_tokens"] - our["cached_input"]
    opt_likely_on = abs(cached_gap) > max(1000, our["cached_input"] * 0.05)
    print(
        f"[delta] cached billed(real-our) gap={cached_gap}; "
        f"optimization likely {'ON' if opt_likely_on else 'off/cold'}"
    )
    record_assertion(
        "billed vs real cached 诊断",
        expected=our["cached_input"],
        actual=delta["cached_input_tokens"],
        passed=True,  # diagnostic only
        detail=(
            f"our.cached(billed, from usage)={our['cached_input']}; "
            f"delta.cached(real, from logs via daily-cost)={delta['cached_input_tokens']}; "
            f"gap={cached_gap}; cache-hit-rate optimization likely "
            f"{'ON (billed!=real, expect split mismatch)' if opt_likely_on else 'off/cold (billed==real)'}"
        ),
    )

    # token 容差: prompt/completion 无 billed/real 分歧，用紧容差；cached 分拆
    # 在优化开启时 billed≠real，用宽容差(15%)且不作为硬失败(见下)。
    prompt_completion_tol = max(1000, our["total_input"] * 0.02)
    split_tol = max(2000, our["total_input"] * 0.15)

    # --- PRIMARY token cross-checks: prompt (uncached+cached) & completion ---
    # These have no billed/real split, so they must match the interface delta.
    with allure.step("增量 token 一致性(强): prompt & completion 无 billed/real 分歧"):
        _delta_check(
            "delta prompt_tokens == 本次实际输入 (uncached+cached)",
            expected=our["total_input"],
            actual=delta["uncached_input_tokens"] + delta["cached_input_tokens"],
            tol=prompt_completion_tol,
        )
        _delta_check(
            "delta completion_tokens == 本次实际输出",
            expected=our["completion"],
            actual=delta["completion_tokens"],
            tol=prompt_completion_tol,
        )

    # --- SECONDARY: cached/uncached split (billed vs real; soft when opt on) ---
    with allure.step("增量 token 分拆(参考): cached/uncached (billed 可能 ≠ real)"):
        split_detail_kw: dict = {}
        if opt_likely_on:
            split_detail_kw["detail"] = (
                f"cache-hit-rate optimization likely ON: our.cached(billed)="
                f"{our['cached_input']} vs delta.cached(real)="
                f"{delta['cached_input_tokens']} (gap={cached_gap}); "
                f"split mismatch is expected, recorded as soft (non-blocking)."
            )
        _delta_check(
            "delta cached_input_tokens ≈ 本次实际缓存命中 (billed, 软断言)",
            expected=our["cached_input"],
            actual=delta["cached_input_tokens"],
            tol=split_tol,
            **split_detail_kw,
        )
        _delta_check(
            "delta uncached_input_tokens ≈ 本次实际未命中输入 (billed, 软断言)",
            expected=our["uncached_input"],
            actual=delta["uncached_input_tokens"],
            tol=split_tol,
            **split_detail_kw,
        )

    # --- INTERFACE SELF-CONSISTENCY: delta amount == computeLogCost on the
    # interface's OWN real-cached delta. This mirrors nexus log.go:211-222,582
    # exactly and is always true (no billed/real ambiguity — both sides use the
    # interface's real cached). This is the STRONG amount assertion. ---
    with allure.step(
        "增量收入公式(强): delta amount == computeLogCost(接口 real cached)"
    ):
        _assert_amount(
            delta["amount"],
            delta["uncached_input_tokens"],
            delta["cached_input_tokens"],
            delta["completion_tokens"],
            config.PRICE_INPUT,
            config.PRICE_CACHED,
            config.PRICE_OUTPUT,
            where="delta(接口自洽, real cached)",
        )

    # Reference only: our billed-based expected amount. Matches the interface
    # delta amount ONLY when optimization off AND test01 had no other traffic
    # today. Recorded, not a pass condition.
    our_expected_amount = expected_amount(
        our["uncached_input"],
        our["cached_input"],
        our["completion"],
        config.PRICE_INPUT,
        config.PRICE_CACHED,
        config.PRICE_OUTPUT,
    )

    record_assertion(
        "本次运行实际消耗 + 预期收入汇总",
        expected=round(our_expected_amount, 6),
        actual={
            "tokens": {
                "uncached_input": our["uncached_input"],
                "cached_input": our["cached_input"],
                "completion": our["completion"],
            },
            "delta_amount": delta["amount"],
            "baseline_amount": baseline["amount"],
            "after_amount": after["amount"],
        },
        passed=True,
        detail=(
            f"本次 {our['request_count']} 次请求: "
            f"uncached={our['uncached_input']}, cached={our['cached_input']}, "
            f"completion={our['completion']}; "
            f"预期收入(8/2/28)={our_expected_amount:.6f}, "
            f"接口 delta amount={delta['amount']}"
        ),
    )

    # glm-5.2 must appear (our requests used it).
    glm_entries = [m for m in models if m.get("model_name") == config.MODEL_NAME]
    _check(
        "当天模型清单包含 glm-5.2",
        expected=[config.MODEL_NAME],
        actual=[m.get("model_name") for m in models],
        passed=bool(glm_entries),
        detail=f"glm-5.2 not found in today's model breakdown; models={[m.get('model_name') for m in models]}",
    )

    # (a) Per-model revenue formula using the prices returned in the response.
    for m in glm_entries:
        mname = m.get("model_name")
        u = m.get("uncached_input_tokens", 0)
        c = m.get("cached_input_tokens", 0)
        o = m.get("completion_tokens", 0)
        a = float(m.get("amount", 0.0))
        if not m.get("has_price", True):
            print(f"[assert] model={mname} has unpriced tokens; skipping formula check")
            record_assertion(
                f"收入公式 model={mname}",
                expected="N/A(unpriced)",
                actual=a,
                passed=True,
                detail="has_price=false, 走倍率计费, 跳过公式校验",
            )
            continue
        ip = float(m.get("input_price", config.PRICE_INPUT))
        cp = float(m.get("cached_price", config.PRICE_CACHED))
        op = float(m.get("output_price", config.PRICE_OUTPUT))
        _assert_amount(a, u, c, o, ip, cp, op, where=f"model={mname}")

    # (b) glm-5.2 prices must match the system config (8 / 2 / 28).
    for m in glm_entries:
        _check(
            f"glm-5.2 input_price={config.PRICE_INPUT}",
            expected=config.PRICE_INPUT,
            actual=float(m.get("input_price", 0)),
            passed=float(m.get("input_price", 0)) == config.PRICE_INPUT,
            detail=f"glm-5.2 input_price mismatch: got {m.get('input_price')}, expected {config.PRICE_INPUT}",
        )
        _check(
            f"glm-5.2 cached_price={config.PRICE_CACHED}",
            expected=config.PRICE_CACHED,
            actual=float(m.get("cached_price", 0)),
            passed=float(m.get("cached_price", 0)) == config.PRICE_CACHED,
            detail=f"glm-5.2 cached_price mismatch: got {m.get('cached_price')}, expected {config.PRICE_CACHED}",
        )
        _check(
            f"glm-5.2 output_price={config.PRICE_OUTPUT}",
            expected=config.PRICE_OUTPUT,
            actual=float(m.get("output_price", 0)),
            passed=float(m.get("output_price", 0)) == config.PRICE_OUTPUT,
            detail=f"glm-5.2 output_price mismatch: got {m.get('output_price')}, expected {config.PRICE_OUTPUT}",
        )

    # (c) Aggregate amount == sum over models.
    #     Two paths depending on whether the row contains unpriced models
    #     (``has_unpriced`` field at ``controller/log.go:532`` in dailyCostRow):
    #       - all priced: agg_amount == Σ expected_amount(m.tokens, m.prices)
    #         (the per-token-price formula; nexus ``computeLogCost`` per model).
    #       - has unpriced: some models use rate-based billing (倍率) instead of
    #         the token*price formula, so Σ over priced-only understates
    #         agg_amount. Fall back to the pricing-mode-agnostic identity
    #         agg_amount == Σ m.amount (each model's server-computed amount,
    #         correct regardless of billing mode) — interface self-consistency.
    if has_unpriced:
        sum_model_amounts = sum(float(m.get("amount", 0.0)) for m in models)
        diff_amount = abs(agg_amount - sum_model_amounts)
        _check(
            "聚合 amount == 各模型 amount 之和 (has_unpriced, 接口自洽)",
            expected=round(sum_model_amounts, 6),
            actual=agg_amount,
            passed=diff_amount <= 0.01,
            detail=(
                f"has_unpriced=true; 行内含未计价模型(倍率计费), 跳过 price-based "
                f"公式求和, 改用 sum(m.amount) 自洽校验; "
                f"actual={agg_amount}, sum(m.amount)={sum_model_amounts}, "
                f"diff={diff_amount}"
            ),
            tolerance=0.01,
        )
    else:
        expected_agg = 0.0
        for m in models:
            if not m.get("has_price", True):
                continue
            expected_agg += expected_amount(
                m.get("uncached_input_tokens", 0),
                m.get("cached_input_tokens", 0),
                m.get("completion_tokens", 0),
                float(m.get("input_price", config.PRICE_INPUT)),
                float(m.get("cached_price", config.PRICE_CACHED)),
                float(m.get("output_price", config.PRICE_OUTPUT)),
            )
        diff = abs(agg_amount - expected_agg)
        _check(
            "聚合 amount == 各模型 amount 之和",
            expected=round(expected_agg, 6),
            actual=agg_amount,
            passed=diff <= 0.01,
            detail=f"Aggregate amount mismatch: actual={agg_amount}, expected(sum over models)={expected_agg}, diff={diff}",
            tolerance=0.01,
        )

    # (d) If test01 only used glm-5.2 today AND no unpriced models are present,
    #     the aggregate also satisfies the configured-price formula directly
    #     (matches the requirement statement). Skipped when has_unpriced (some
    #     model uses 倍率计费, so the configured 8/2/28 formula wouldn't apply to
    #     those tokens) or when multiple models are present.
    if (
        models
        and all(m.get("model_name") == config.MODEL_NAME for m in models)
        and not has_unpriced
    ):
        _assert_amount(
            agg_amount,
            agg_uncached,
            agg_cached,
            agg_completion,
            config.PRICE_INPUT,
            config.PRICE_CACHED,
            config.PRICE_OUTPUT,
            where="aggregate(glm-5.2 only, all priced)",
        )
    else:
        if has_unpriced:
            skip_reason = "has_unpriced=true (含未计价模型, 跳过配置单价聚合校验)"
        else:
            skip_reason = (
                "multiple models present, skipping configured-price aggregate check"
            )
        record_assertion(
            "聚合行配置单价公式校验",
            expected="glm-5.2 only & all priced",
            actual=[m.get("model_name") for m in models],
            passed=True,
            detail=skip_reason,
        )
        print(
            f"[assert] skipping configured-price aggregate check: {skip_reason}; "
            f"models={[m.get('model_name') for m in models]}"
        )

    print(
        f"[assert] daily-cost revenue checks PASSED for {config.today_str()} "
        f"(user_id={config.TEST_USER_ID}, amount={agg_amount})"
    )


@allure.feature("TPM 容量")
@allure.story("test01 实际消耗 vs 接口 user_tpm 一致性")
@allure.severity(allure.severity_level.CRITICAL)
def test_tpm_capacity(model_requests, admin_client):
    """test01's actually-consumed tokens must match the interface's user_tpm.

    Nexus recording (``relay/controller/helper.go`` on releases/xb01):
      - ``totalTokens := promptTokens + completionTokens`` (helper.go:237), where
        ``promptTokens = usage.PromptTokens`` is the *full* prompt (cached input
        included — cached is NOT subtracted). ``common.RecordTPMUsage`` records
        it (helper.go:319/372) even when subscription quota is 0; the consume log
        lands at ``CreatedAt = now`` (``model/log.go:121``).

    Operation interface (``model/tpm_capacity.go: GetTPMCapacityReport``):
      - per-user series is a SQL ``GROUP BY (created_at DIV bucket_sec)*bucket_sec``
        over ``logs(type=consume)`` ``sum(prompt_tokens)+sum(completion_tokens)``
        (``tpm_capacity.go:244``) — i.e. exactly ``totalTokens`` above. So the
        interface's per-bucket token total must equal what we sent.
      - **bucket width is adaptive** (``tpm_capacity.go:79-88``):
        window ≤6h→60s, ≤2d→300s, else 3600s. ``series[].user_tpm`` is a
        per-minute rate, so a bucket's tokens = ``bucket_min * user_tpm`` where
        ``bucket_min = bucket_sec/60``. We derive ``bucket_sec`` from the
        *query* window via ``tpm_bucket_size`` (fixed-point over the pad), not a
        hardcoded 5 min, so wide (>2-day) windows with hourly buckets are handled.

    The ``model_requests`` fixture returns the per-request ``usage`` dicts plus
    the unix window (``start_ts``/``end_ts``). We:
      1. sum tokens as nexus does — ``prompt + completion`` per request via
         ``_usage_tokens`` (cached input included);
      2. query tpm-capacity for the page-default 近24小时 window (the SAME
         shared (start, end) as /api/daily-cost, captured once by
         ``model_requests`` so both interfaces use identical timestamps; the
         server picks ``bucket_sec`` adaptively from the query window) and poll
         until test01's ``user_tpm`` shows up;
      3. sum ``bucket_min * user_tpm`` over the buckets overlapping our session
         window and compare with our actual consumed tokens.

    A relative+absolute tolerance is used because: bucket boundaries may split
    our traffic, the SQL bucketing rounds to ``bucketSec`` edges, and test01
    could carry unrelated traffic in the same buckets.
    """
    usages = model_requests["usages"]
    win_start = model_requests["start_ts"]
    win_end = model_requests["end_ts"]

    actual_tokens = sum(_usage_tokens(u) for u in usages)
    # nexus-grounded breakdown (prompt+completion; cached is a subset of prompt)
    breakdown = sum_request_tokens(usages)
    print(
        f"[tpm] test01 actual consumed tokens (from {len(usages)} model requests) "
        f"= {actual_tokens}; window=[{win_start}..{win_end}] "
        f"({win_end - win_start}s)"
    )
    attach_json(
        {
            "actual_tokens": actual_tokens,
            "request_count": len(usages),
            "window_start": win_start,
            "window_end": win_end,
            "window_seconds": win_end - win_start,
            "per_request_usage": usages,
            "nexus_breakdown": breakdown,
        },
        "TPM 实际消耗(模型侧)",
    )

    # Query the page-default 近24小时 window — the SAME shared (start, end) as
    # /api/daily-cost (both belong to one operation page → identical timestamps;
    # captured once by model_requests as page_start_24h/page_end_24h). The
    # server picks the bucket width adaptively from the query window
    # (tpm_capacity.go:79-88); a 24h window → 300s buckets. We still match only
    # the buckets overlapping our actual session window [win_start, win_end]
    # below to isolate this run's traffic from other test01 activity in the 24h.
    q_start = model_requests["page_start_24h"]
    q_end = model_requests["page_end_24h"]
    bucket_sec = tpm_bucket_size(q_end - q_start)
    bucket_min = bucket_sec // 60

    matched = []
    raw = None
    with allure.step("查询 tpm-capacity 并轮询至 test01 数据出现"):
        for attempt in range(1, config.POLL_RETRIES + 1):
            tpm = admin_client.get_tpm_capacity(q_start, q_end, config.TEST_USER_ID)
            raw = tpm
            data = tpm.get("data", {}) if isinstance(tpm, dict) else {}
            series = data.get("series") or []
            matched = _overlapping_buckets(series, win_start, win_end, bucket_sec)
            reported = sum(bucket_min * (b.get("user_tpm", 0) or 0) for b in matched)
            if reported > 0:
                break
            print(
                f"[tpm] attempt {attempt}/{config.POLL_RETRIES}: "
                f"matched {len(matched)} buckets, reported={reported} (not ready yet); "
                f"retrying in {config.POLL_INTERVAL}s"
            )
            time.sleep(config.POLL_INTERVAL)

    print(f"[admin] tpm-capacity raw (windowed): {raw}")
    attach_json(raw, "tpm-capacity 原始响应(窗口化)")
    print(
        f"[tpm] query window [{q_start}..{q_end}] ({q_end - q_start}s) -> "
        f"bucket_sec={bucket_sec} (bucket_min={bucket_min})"
    )
    print(f"[tpm] matched {len(matched)} buckets overlapping [{win_start}..{win_end}]:")
    for b in matched:
        print(
            f"      t={b.get('t')} tpm={b.get('tpm')} user_tpm={b.get('user_tpm')} "
            f"-> bucket_tokens={bucket_min * (b.get('user_tpm', 0) or 0)}"
        )
    attach_json(
        [
            {
                "t": b.get("t"),
                "tpm": b.get("tpm"),
                "user_tpm": b.get("user_tpm"),
                "bucket_tokens": bucket_min * (b.get("user_tpm", 0) or 0),
            }
            for b in matched
        ],
        "匹配的时间桶",
    )

    _check(
        "存在与请求窗口相交的时间桶",
        expected=f"buckets overlapping [{win_start}..{win_end}]",
        actual=[b.get("t") for b in matched],
        passed=bool(matched),
        detail=f"No tpm-capacity series buckets overlapped the request window [{win_start}..{win_end}]; raw series={raw!r}",
    )

    reported_tokens = sum(bucket_min * (b.get("user_tpm", 0) or 0) for b in matched)
    print(
        f"[tpm] actual={actual_tokens} vs "
        f"reported({bucket_min}*sum user_tpm)={reported_tokens}"
    )

    # At least one bucket must record test01 traffic (cache fillers included).
    _check(
        "test01 流量已被后台记录 (reported_tokens > 0)",
        expected=">0",
        actual=reported_tokens,
        passed=reported_tokens > 0,
        detail=f"test01 user_tpm is 0 across all matched buckets; our requests were not recorded by the operation interface. Buckets={matched!r}",
    )

    # Consistency: allow for bucket-boundary splitting, rounding and a little
    # unrelated test01 traffic. Tolerance = max(25% of actual, 2000 tokens).
    tol = max(actual_tokens * 0.25, 2000)
    diff = abs(actual_tokens - reported_tokens)
    _check(
        f"TPM 一致性: 实际消耗 ≈ 接口统计 ({bucket_min}×user_tpm)",
        expected=actual_tokens,
        actual=reported_tokens,
        passed=diff <= tol,
        detail=f"TPM consistency failed: actual={actual_tokens}, reported={reported_tokens}, diff={diff} > tol={tol}; matched buckets={matched!r}",
        tolerance=tol,
    )

    # Sanity: test01's per-bucket user_tpm must never exceed the all-users tpm
    # in the same bucket.
    over = [
        b
        for b in matched
        if (b.get("user_tpm", 0) or 0) > (b.get("tpm", 0) or 0) + 1e-3
    ]
    _check(
        "每桶 user_tpm <= tpm (test01 不超过总量)",
        expected="user_tpm <= tpm",
        actual=[
            {"t": b.get("t"), "user_tpm": b.get("user_tpm"), "tpm": b.get("tpm")}
            for b in over
        ],
        passed=not over,
        detail=f"user_tpm > tpm in some buckets (test01 cannot exceed the total): {over!r}",
    )

    print(
        f"[assert] tpm-capacity consistency PASSED: "
        f"actual={actual_tokens} ~ reported={reported_tokens} (diff={diff}, tol={tol})"
    )


@allure.feature("缓存命中率优化")
@allure.story("test01 实际/计费命中率 vs 接口 overview 一致性")
@allure.severity(allure.severity_level.CRITICAL)
def test_cache_optimize_overview(model_requests, admin_client):
    """Cross-check the cache-optimize overview dashboard against actual request data.

    Endpoint ``GET /api/admin/usage/cache-optimize/overview``
    (``controller/cache_optimize.go:GetCacheOptimizeOverview``, releases/xb01).

    Per-user row fields and how nexus computes them (``buildCacheOptRow``):
      - Month aggregates via ``model.GetCacheHitByUser(monthStart, now)``
        (``log.go:786``): SQL ``GROUP BY user_id`` over
        ``logs(type=consume, created_at BETWEEN monthStart AND now)`` summing
        ``prompt_tokens``, ``cached_tokens`` (**real**) and
        ``cached_tokens_billed`` (**billed**). ``monthStart`` is the first day of
        the current Beijing-natural-month 00:00 (``cache_optimize.go:57``).
      - ``real_rate = month_real / month_prompt``; ``billed_rate = month_billed /
        month_prompt`` (``cache_optimize.go:104-105``).
      - Hour aggregates over ``[hourStart, now]`` where ``hourStart`` is the
        current Beijing-natural-hour :00 (``cache_optimize.go:65``);
        ``hour_real_rate / hour_billed_rate`` likewise.
      - ``ctr_*`` are Redis period counters keyed ``cache_opt:{userId}:{YYYYMM}``
        (``cache_optimize.go:197-198,287``). ``ctr_granted`` = Σ
        ``max(billed-real, 0)`` per request — the **month-to-date concession
        (本月让利)**, only the upward adjustments (``cache_optimize.go:232-235``).

    test01 has no active optimization target (``has_target=false``), so
    ``ComputeBilledCachedTokens`` returns ``realCached`` unchanged
    (``cache_optimize.go:300-310``) → billed == real per request. This yields
    strong, deterministic invariants for test01:
      - ``month_real == month_billed``; ``real_rate == billed_rate``
      - ``hour_real == hour_billed``; ``hour_real_rate == hour_billed_rate``
      - ``ctr_granted == 0`` (no upward adjustment ever happens)

    Because test01's customer-facing ``usage.cached_input_tokens`` is the BILLED
    value (``usage_filter.go:38-83``) and billed == real for test01, our run's
    ``cached_input`` equals the real cached, so the month delta (after - baseline
    captured before the requests) must match our per-request usage totals.

    Flow:
      1. ``model_requests`` captured the overview **baseline** snapshot *before*
         any request (passed back as ``overview_baseline`` + ``overview_baseline_ts``).
      2. Query the **after** overview, polling until test01's month-prompt delta
         reflects our run.
      3. Assert no-optimization invariants (A), rate-formula self-consistency (B),
         month delta vs actual usage (C), delta hit rate (D), conditional
         same-hour delta (E), and ctr_granted==0 (F).
    """
    baseline = model_requests.get("overview_baseline")
    baseline_ts = model_requests.get("overview_baseline_ts")
    if baseline is None:
        pytest.skip(
            "cache-optimize overview baseline unavailable (capture failed before requests)"
        )
    print(f"[overview] 基线快照(请求前): {baseline} (ts={baseline_ts})")

    usages = model_requests["usages"]
    our = sum_request_tokens(usages)
    print(f"[overview] 本次实际消耗(usage 汇总): {our}")
    attach_json(our, "本次运行实际消耗(overview)")

    with allure.step("请求后: 查询 cache-optimize overview 并轮询至 test01 月增量就绪"):
        raw = _poll_overview(admin_client, baseline, our["total_input"])
        print(f"[admin] cache-opt overview raw: {raw}")
        attach_json(raw, "cache-optimize overview 原始响应(请求后)")

    _check(
        "cache-optimize overview 接口成功",
        expected=True,
        actual=bool(isinstance(raw, dict) and raw.get("success", True)),
        passed=isinstance(raw, dict) and raw.get("success", True),
        detail=f"overview call failed: {raw!r}",
    )
    assert isinstance(raw, dict)  # type-narrowing; _check above already raises

    row = find_overview_user(raw, config.TEST_USER_ID)
    _check(
        f"存在 test01(user_id={config.TEST_USER_ID}) overview 记录",
        expected=f"user_id={config.TEST_USER_ID}",
        actual=row,
        passed=row is not None,
        detail=(
            f"No cache-optimize overview row for user_id={config.TEST_USER_ID}. "
            f"Returned data: {raw.get('data')!r}"
        ),
    )
    assert row is not None  # type-narrowing
    attach_json(row, "test01 overview 记录(请求后)")

    after = extract_overview_snapshot(row)
    delta = delta_overview_snapshots(after, baseline)
    after_ts = int(time.time())
    print(f"[overview] after={after}, baseline={baseline}, delta={delta}")
    attach_json(
        {
            "baseline": baseline,
            "after": after,
            "delta": delta,
            "after_ts": after_ts,
            "baseline_ts": baseline_ts,
        },
        "cache-optimize overview 增量(基线/请求后/差值)",
    )

    # Tolerances. test01 has no optimization → billed==real exactly at source
    # (ComputeBilledCachedTokens returns the int realCached unchanged), so the
    # billed/real equalities are tight; token-delta tolerances accommodate
    # propagation/rounding and any tiny concurrent test01 traffic.
    prompt_tol = max(1000, our["total_input"] * 0.02)
    cached_tol = max(2000, our["total_input"] * 0.05)
    rate_tol = 0.02  # absolute (2 percentage points) — deltas amplify rounding
    exact_tol = 1e-9  # billed==real is exact for test01
    formula_tol = 1e-6  # rate == num/denom, Go float64 division precision

    # (A) No-optimization invariants for test01 (has_target=false):
    #   ComputeBilledCachedTokens returns realCached unchanged
    #   (cache_optimize.go:300-310) → billed==real per request → aggregated
    #   billed==real, ctr_granted=0.
    with allure.step("test01 无优化口径: billed == real, ctr_granted == 0"):
        _check(
            "test01 has_target == false (无活跃优化目标)",
            expected=False,
            actual=after["has_target"],
            passed=after["has_target"] is False,
            detail=(
                f"test01 has_target={after['has_target']}; ActiveCacheTarget "
                f"(cache_optimize.go:171) should be false for test01 (no active window)"
            ),
        )
        _check(
            "month_real == month_billed (test01 无优化, billed==real)",
            expected=after["month_real"],
            actual=after["month_billed"],
            passed=after["month_real"] == after["month_billed"],
            detail=(
                f"month_real={after['month_real']} != month_billed={after['month_billed']}; "
                f"test01 无优化时应严格相等 (ComputeBilledCachedTokens 返回 realCached)"
            ),
        )
        _check(
            "real_rate == billed_rate (test01 无优化)",
            expected=after["real_rate"],
            actual=after["billed_rate"],
            passed=abs(after["real_rate"] - after["billed_rate"]) <= exact_tol,
            detail=f"real_rate={after['real_rate']}, billed_rate={after['billed_rate']}",
            tolerance=exact_tol,
        )
        if after["hour_prompt"] > 0:
            _check(
                "hour_real == hour_billed (test01 无优化, 当小时有流量)",
                expected=after["hour_real"],
                actual=after["hour_billed"],
                passed=after["hour_real"] == after["hour_billed"],
                detail=f"hour_real={after['hour_real']}, hour_billed={after['hour_billed']}",
            )
            _check(
                "hour_real_rate == hour_billed_rate (当小时有流量)",
                expected=after["hour_real_rate"],
                actual=after["hour_billed_rate"],
                passed=abs(after["hour_real_rate"] - after["hour_billed_rate"])
                <= exact_tol,
                detail=(
                    f"hour_real_rate={after['hour_real_rate']}, "
                    f"hour_billed_rate={after['hour_billed_rate']}"
                ),
                tolerance=exact_tol,
            )
        else:
            _check(
                "当小时无流量: hour_*_rate == 0",
                expected=0.0,
                actual=(after["hour_real_rate"], after["hour_billed_rate"]),
                passed=after["hour_real_rate"] == 0 and after["hour_billed_rate"] == 0,
                detail=(
                    f"hour_prompt=0 but hour_real_rate={after['hour_real_rate']}, "
                    f"hour_billed_rate={after['hour_billed_rate']}"
                ),
            )
        _check(
            "ctr_granted == 0 (test01 本月让利=0, 无优化无让利)",
            expected=0,
            actual=after["ctr_granted"],
            passed=after["ctr_granted"] == 0,
            detail=(
                f"ctr_granted={after['ctr_granted']}; granted=Σmax(billed-real,0)=0 "
                f"for test01 (cache_optimize.go:232-235, billed==real)"
            ),
        )

    # (B) Rate formula self-consistency (mirrors buildCacheOptRow:104-110):
    #   RealRate = CachedTokens/PromptTokens; BilledRate = CachedTokensBilled/PromptTokens
    with allure.step("命中率公式自洽: rate == num/denom (buildCacheOptRow)"):
        if after["month_prompt"] > 0:
            _check(
                "real_rate == month_real / month_prompt",
                expected=after["month_real"] / after["month_prompt"],
                actual=after["real_rate"],
                passed=abs(
                    after["real_rate"] - after["month_real"] / after["month_prompt"]
                )
                <= formula_tol,
                detail=(
                    f"real_rate={after['real_rate']}, "
                    f"month_real/month_prompt={after['month_real'] / after['month_prompt']}"
                ),
                tolerance=formula_tol,
            )
            _check(
                "billed_rate == month_billed / month_prompt",
                expected=after["month_billed"] / after["month_prompt"],
                actual=after["billed_rate"],
                passed=abs(
                    after["billed_rate"] - after["month_billed"] / after["month_prompt"]
                )
                <= formula_tol,
                detail=(
                    f"billed_rate={after['billed_rate']}, "
                    f"month_billed/month_prompt={after['month_billed'] / after['month_prompt']}"
                ),
                tolerance=formula_tol,
            )
        else:
            _check(
                "当月无流量: real_rate == 0 and billed_rate == 0",
                expected=0.0,
                actual=(after["real_rate"], after["billed_rate"]),
                passed=after["real_rate"] == 0 and after["billed_rate"] == 0,
                detail=f"month_prompt=0 but real_rate={after['real_rate']}, billed_rate={after['billed_rate']}",
            )
        if after["hour_prompt"] > 0:
            _check(
                "hour_real_rate == hour_real / hour_prompt",
                expected=after["hour_real"] / after["hour_prompt"],
                actual=after["hour_real_rate"],
                passed=abs(
                    after["hour_real_rate"] - after["hour_real"] / after["hour_prompt"]
                )
                <= formula_tol,
                detail=(
                    f"hour_real_rate={after['hour_real_rate']}, "
                    f"hour_real/hour_prompt={after['hour_real'] / after['hour_prompt']}"
                ),
                tolerance=formula_tol,
            )
            _check(
                "hour_billed_rate == hour_billed / hour_prompt",
                expected=after["hour_billed"] / after["hour_prompt"],
                actual=after["hour_billed_rate"],
                passed=abs(
                    after["hour_billed_rate"]
                    - after["hour_billed"] / after["hour_prompt"]
                )
                <= formula_tol,
                detail=(
                    f"hour_billed_rate={after['hour_billed_rate']}, "
                    f"hour_billed/hour_prompt={after['hour_billed'] / after['hour_prompt']}"
                ),
                tolerance=formula_tol,
            )

    # (C) Month delta cross-check: baseline → after isolates this run's traffic.
    #   test01 has no optimization → our cached_input (billed from usage) == real,
    #   so delta month_real and delta month_billed both equal our cached_input.
    with allure.step("月度增量校验(强): delta month_* == 本次实际 (billed==real)"):
        if our["total_input"] > 0:
            _delta_check(
                "delta month_prompt == 本次实际 prompt (Σ usage.prompt_tokens)",
                expected=our["total_input"],
                actual=delta["month_prompt"],
                tol=prompt_tol,
            )
            _delta_check(
                "delta month_real == 本次 cached (test01 billed==real)",
                expected=our["cached_input"],
                actual=delta["month_real"],
                tol=cached_tol,
            )
            _delta_check(
                "delta month_billed == 本次 cached (test01 billed==real)",
                expected=our["cached_input"],
                actual=delta["month_billed"],
                tol=cached_tol,
            )
            _delta_check(
                "delta month_real == delta month_billed (无优化口径)",
                expected=delta["month_real"],
                actual=delta["month_billed"],
                tol=0,
            )
        else:
            record_assertion(
                "月度增量校验",
                expected="our.total_input > 0",
                actual=our["total_input"],
                passed=True,
                detail="本次无实际请求 token, 跳过月度增量校验",
            )

    # (D) Delta hit rate == this run's actual hit rate.
    with allure.step("增量命中率 == 本次实际命中率 (cached/prompt)"):
        if our["total_input"] > 0 and delta["month_prompt"] > 0:
            expected_rate = our["cached_input"] / our["total_input"]
            actual_rate = delta["month_real"] / delta["month_prompt"]
            diff = abs(actual_rate - expected_rate)
            _check(
                "delta (month_real/month_prompt) == 本次 (cached/prompt)",
                expected=round(expected_rate, 6),
                actual=round(actual_rate, 6),
                passed=diff <= rate_tol,
                detail=(
                    f"expected(本次 cached/prompt)={expected_rate}, "
                    f"actual(delta month_real/month_prompt)={actual_rate}, diff={diff}"
                ),
                tolerance=rate_tol,
            )

    # (E) Hour delta — only meaningful when baseline and after share the same
    # Beijing-natural-hour bucket (else the hour window rolled over and the
    # after hour_* is a fresh window not containing the baseline).
    with allure.step("小时增量校验(条件: 同一北京自然小时)"):
        if baseline_ts is None:
            record_assertion(
                "小时增量校验",
                expected="baseline_ts available",
                actual=None,
                passed=True,
                detail="baseline_ts 缺失, 跳过小时增量校验",
            )
        else:
            b_hour = beijing_hour_bucket(baseline_ts)
            a_hour = beijing_hour_bucket(after_ts)
            same_hour = b_hour == a_hour
            if not same_hour:
                record_assertion(
                    "小时增量校验",
                    expected="same Beijing-natural-hour",
                    actual={"baseline_hour": b_hour, "after_hour": a_hour},
                    passed=True,
                    detail=(
                        f"跨小时: baseline_hour={b_hour}, after_hour={a_hour}; "
                        f"hour_* 窗口已翻转, 跳过 hour 增量校验"
                    ),
                )
                print(
                    f"[overview] hour rollover: baseline_hour={b_hour}, "
                    f"after_hour={a_hour}; skipping hour delta"
                )
            elif our["total_input"] > 0:
                _delta_check(
                    "delta hour_prompt == 本次 prompt (同小时)",
                    expected=our["total_input"],
                    actual=delta["hour_prompt"],
                    tol=prompt_tol,
                )
                _delta_check(
                    "delta hour_real == 本次 cached (同小时, billed==real)",
                    expected=our["cached_input"],
                    actual=delta["hour_real"],
                    tol=cached_tol,
                )
                _delta_check(
                    "delta hour_billed == 本次 cached (同小时)",
                    expected=our["cached_input"],
                    actual=delta["hour_billed"],
                    tol=cached_tol,
                )
                _delta_check(
                    "delta hour_real == delta hour_billed (同小时, 无优化)",
                    expected=delta["hour_real"],
                    actual=delta["hour_billed"],
                    tol=0,
                )

    # (F) ctr_granted delta == 0: test01 never receives an upward adjustment.
    with allure.step("本月让利增量 == 0 (test01 无让利)"):
        _check(
            "delta ctr_granted == 0",
            expected=0,
            actual=delta["ctr_granted"],
            passed=delta["ctr_granted"] == 0,
            detail=(
                f"delta ctr_granted={delta['ctr_granted']}; test01 无优化, "
                f"每次请求 granted=max(billed-real,0)=0, 增量必为0"
            ),
        )

    record_assertion(
        "本次运行实际命中率 + overview 汇总",
        expected=(
            round(our["cached_input"] / our["total_input"], 6)
            if our["total_input"]
            else 0
        ),
        actual={
            "real_rate": after["real_rate"],
            "billed_rate": after["billed_rate"],
            "ctr_granted": after["ctr_granted"],
            "delta_month_real_rate": (
                round(delta["month_real"] / delta["month_prompt"], 6)
                if delta["month_prompt"]
                else 0
            ),
        },
        passed=True,
        detail=(
            f"本次 {our['request_count']} 次请求: prompt={our['total_input']}, "
            f"cached(billed==real)={our['cached_input']}; "
            f"overview real_rate={after['real_rate']}, "
            f"billed_rate={after['billed_rate']}, ctr_granted={after['ctr_granted']}"
        ),
    )

    print(
        f"[assert] cache-optimize overview PASSED for test01 "
        f"(real_rate={after['real_rate']}, billed_rate={after['billed_rate']}, "
        f"ctr_granted={after['ctr_granted']})"
    )


# ---------------------------------------------------------------------------
# log list / log stat tests (interfaces 1 & 2)
# ---------------------------------------------------------------------------


def _fetch_run_log_entries(
    admin_client, q_start, q_end, session_start, session_end, pad=60, max_pages=50
):
    """Paginate ``/api/log/`` over the page-default 8-day window and return this run's rows.

    The server lists are pre-filtered by ``username=test01`` and
    ``model_name=glm-5.2`` over ``[q_start, q_end]`` (the page-default 近8天
    window: frozen ``start`` + real-time ``end``), ordered by ``id desc``
    (newest first), ``ItemsPerPage=10`` (``controller/log.go:47,57``,
    ``common/config.ItemsPerPage=10``). Pages are fetched until a short page
    (last) or empty page is hit, or until a row older than the session window
    appears (this run's rows are newest, so they're on the first pages — no need
    to page through all 8 days of history). Rows are then defensively re-filtered
    by ``filter_run_logs`` (consume type=2, user_id, model_name, created_at in
    the padded SESSION window) to isolate this run's rows.
    """
    collected = []
    for p in range(max_pages):
        page = admin_client.get_logs(
            p=p,
            type_=0,
            model_name=config.MODEL_NAME,
            token_name="",
            start_ts=q_start,
            end_ts=q_end,
            username=config.MODEL_USER,
            channel=0,
        )
        data = page.get("data") or []
        collected.extend(data)
        # short page => last page reached (server returns < ItemsPerPage on the
        # final page); an empty page also terminates.
        if len(data) < 10:
            break
        # early exit: rows are newest-first; once the oldest row on this page
        # predates the session window, no later page can hold this run's rows.
        oldest = int((data[-1] or {}).get("created_at", 0) or 0)
        if oldest and oldest < session_start - pad:
            break
    return filter_run_logs(
        collected,
        config.TEST_USER_ID,
        config.MODEL_NAME,
        session_start,
        session_end,
        pad,
    )


@allure.feature("日志查询")
@allure.story("test01 实际请求 vs 接口 /api/log/ 逐条一致性")
@allure.severity(allure.severity_level.CRITICAL)
def test_log_list_entries(model_requests, admin_client):
    """Per-row cross-check of the operation log list against actual request data.

    Endpoint ``GET /api/log/`` (``controller/log.go:GetAllLogs``, releases/xb01).
    Each returned row has the fields the model request produced, written by
    ``model.RecordConsumeLog`` (``model/log.go:116``) with ``CreatedAt = now``
    (``model/log.go:121``):
      - ``model_name`` = ``config.MODEL_NAME`` (glm-5.2)
      - ``username`` = ``config.MODEL_USER`` (test01)
      - ``prompt_tokens`` = ``usage.PromptTokens`` (full prompt, cached included)
        (``helper.go:126``)
      - ``completion_tokens`` = ``usage.CompletionTokens``
      - ``cached_tokens`` = real KV cache hit clamped to prompt (``helper.go:193-198``)
      - ``cached_tokens_billed`` = optimization-adjusted (``helper.go:204-210``);
        for test01 (no optimization) == ``cached_tokens``
      - ``cost`` = ``computeLogCost(prompt, completion, cached_tokens)`` filled by
        ``fillLogCosts`` using the REAL cached (``log.go:225-228`` →
        ``computeLogCost`` at ``log.go:211-222``)

    For test01 (``has_target=false``), ``ComputeBilledCachedTokens`` returns
    ``realCached`` unchanged (``cache_optimize.go:300-310``) → the customer-facing
    ``usage.cached_input_tokens`` (billed) equals the log's ``cached_tokens``
    (real). So the multiset of ``(prompt, completion, cached)`` from our model
    ``usage`` must equal the multiset from the log rows.

    The test queries the page-default 近8天 window (``start = now - 691199``,
    ``end = now``, shared frozen start with ``/api/log/stat``) — paginates to
    collect rows, then isolates this run's rows by the SESSION window — and for
    each row asserts: created_at within the run window, model name, username,
    total = prompt + completion, and cost == computeLogCost on the real cached.
    """
    s_start = model_requests["start_ts"]  # session window (for isolating this run)
    s_end = model_requests["end_ts"]
    q_start = model_requests[
        "query_start_8d"
    ]  # shared frozen 8d start (same as /api/log/stat)
    q_end = model_requests[
        "query_end_8d"
    ]  # shared frozen 8d end (same value as /api/log/stat)
    usages = model_requests["usages"]
    our = sum_request_tokens(usages)
    print(f"[log] 本次运行窗口=[{s_start},{s_end}], 实际消耗={our}")
    print(
        f"[log] 查询窗口(与/api/log/stat共享)=[{q_start}..{q_end}] ({q_end - q_start}s)"
    )
    attach_json(our, "本次运行实际消耗(log list)")

    with allure.step("查询 /api/log/ 并分页收集本次运行日志"):
        rows = _fetch_run_log_entries(admin_client, q_start, q_end, s_start, s_end)
        print(f"[log] 收集到 {len(rows)} 条日志(过滤后)")
        attach_json(rows, "本次运行日志行(/api/log/)")

    _check(
        "/api/log/ 接口返回非空",
        expected=True,
        actual=bool(rows),
        passed=bool(rows),
        detail=(
            f"No log rows found for user_id={config.TEST_USER_ID} "
            f"model={config.MODEL_NAME} in window [{s_start},{s_end}]"
        ),
    )
    assert rows, "no log rows collected for this run"

    # (1) Row count == our request count (clean test env: only this run in window).
    _check(
        "日志条数 == 本次请求数",
        expected=len(usages),
        actual=len(rows),
        passed=len(rows) == len(usages),
        detail=(
            f"expected(本次请求数)={len(usages)}, actual(日志条数)={len(rows)}; "
            f"窗口内可能含 test01 其他流量或本次请求未完全落地"
        ),
    )

    # (2) Per-row field verification: model, username, total tokens, cost.
    cost_tol = 1e-6
    with allure.step(
        "逐条校验: 模型/用户/总token/费用 (computeLogCost on real cached)"
    ):
        bad = []
        for r in rows:
            prompt = int(r.get("prompt_tokens", 0) or 0)
            completion = int(r.get("completion_tokens", 0) or 0)
            total = prompt + completion
            exp_cost = log_entry_cost(
                r, config.PRICE_INPUT, config.PRICE_CACHED, config.PRICE_OUTPUT
            )
            act_cost = float(r.get("cost", 0.0) or 0.0)
            cost_ok = abs(act_cost - exp_cost) <= cost_tol
            model_ok = r.get("model_name") == config.MODEL_NAME
            user_ok = r.get("username") == config.MODEL_USER
            total_ok = total == prompt + completion  # sanity, always true
            created = int(r.get("created_at", 0) or 0)
            time_ok = s_start - 60 <= created <= s_end + 60
            if not (model_ok and user_ok and total_ok and cost_ok and time_ok):
                bad.append(
                    {
                        "id": r.get("id"),
                        "created_at": created,
                        "model_name": r.get("model_name"),
                        "username": r.get("username"),
                        "prompt_tokens": prompt,
                        "completion_tokens": completion,
                        "cached_tokens": int(r.get("cached_tokens", 0) or 0),
                        "cost": act_cost,
                        "expected_cost": exp_cost,
                        "model_ok": model_ok,
                        "user_ok": user_ok,
                        "total_ok": total_ok,
                        "cost_ok": cost_ok,
                        "time_ok": time_ok,
                    }
                )
        _check(
            "每条日志: 模型/用户/总token/费用 一致",
            expected=f"{len(usages)} rows all matching",
            actual=f"{len(rows) - len(bad)}/{len(rows)} matching",
            passed=not bad,
            detail=f"{len(bad)} rows mismatched: {bad!r}",
        )

    # (3) Multiset of (prompt, completion, cached) == our usages multiset.
    #     test01 billed==real → usage.cached_input == log.cached_tokens.
    with allure.step("多集合校验: (prompt, completion, cached) == 本次 usage 多集合"):
        from collections import Counter

        def sig(u):
            p = int(u.get("prompt_tokens", 0) or 0)
            c = int(u.get("completion_tokens", 0) or 0)
            return (p, c, cached_of(u))

        our_sig = Counter(sig(u) for u in usages)
        row_sig = Counter(
            (
                int(r.get("prompt_tokens", 0) or 0),
                int(r.get("completion_tokens", 0) or 0),
                int(r.get("cached_tokens", 0) or 0),
            )
            for r in rows
        )
        only_our = our_sig - row_sig
        only_row = row_sig - our_sig
        _check(
            "(prompt,completion,cached) 多集合 == 本次 usage",
            expected=dict(our_sig),
            actual=dict(row_sig),
            passed=not only_our and not only_row,
            detail=(
                f"only-in-our-usage={dict(only_our)}; only-in-log-rows={dict(only_row)}"
            ),
        )

    # (4) Aggregate cost sanity: sum(row.cost) == Σ computeLogCost over rows, and
    #     for test01 (billed==real) also == our expected total cost.
    with allure.step("聚合费用校验: Σ row.cost == Σ computeLogCost(本次)"):
        sum_row_cost = round(sum(float(r.get("cost", 0.0) or 0.0) for r in rows), 6)
        sum_expected = round(
            sum(
                log_entry_cost(
                    r, config.PRICE_INPUT, config.PRICE_CACHED, config.PRICE_OUTPUT
                )
                for r in rows
            ),
            6,
        )
        _check(
            "Σ row.cost == Σ computeLogCost(各行 real cached)",
            expected=sum_expected,
            actual=sum_row_cost,
            passed=abs(sum_row_cost - sum_expected) <= cost_tol,
            detail=f"sum(row.cost)={sum_row_cost}, sum(expected)={sum_expected}",
            tolerance=cost_tol,
        )
        # test01 billed==real → our total cost (on billed==real cached) should
        # equal the rows' aggregate cost when the multiset matches.
        our_total_cost = round(
            sum(
                expected_amount(
                    int(u.get("prompt_tokens", 0) or 0) - cached_of(u),
                    cached_of(u),
                    int(u.get("completion_tokens", 0) or 0),
                    config.PRICE_INPUT,
                    config.PRICE_CACHED,
                    config.PRICE_OUTPUT,
                )
                for u in usages
            ),
            6,
        )
        _check(
            "Σ row.cost == 本次预期总费用 (test01 billed==real)",
            expected=our_total_cost,
            actual=sum_row_cost,
            passed=abs(sum_row_cost - our_total_cost)
            <= max(cost_tol, our_total_cost * 0.01),
            detail=(
                f"sum(row.cost)={sum_row_cost}, our_total_cost={our_total_cost}; "
                f"test01 billed==real, 多集合一致则应相等(容差吸收落地时延/并发流量)"
            ),
            tolerance=max(cost_tol, our_total_cost * 0.01),
        )

    record_assertion(
        "本次运行 /api/log/ 逐条校验汇总",
        expected={
            "count": len(usages),
            "total_tokens": our["total_tokens"],
            "total_cost": our_total_cost,
        },
        actual={
            "count": len(rows),
            "sum_cost": sum_row_cost,
        },
        passed=True,
        detail=(
            f"本次 {len(usages)} 次请求, 日志 {len(rows)} 条; "
            f"Σ row.cost={sum_row_cost}, 预期总费用={our_total_cost}"
        ),
    )
    print(f"[assert] log-list entries PASSED: {len(rows)} rows, Σcost={sum_row_cost}")


@allure.feature("日志查询")
@allure.story("test01 实际请求 vs 接口 /api/log/stat 增量校验")
@allure.severity(allure.severity_level.CRITICAL)
def test_log_stat_delta(model_requests, admin_client):
    """Baseline-delta cross-check of the log stat aggregate against actual data.

    Endpoint ``GET /api/log/stat`` (``controller/log.go:GetLogsStat``, releases/xb01).
    The handler always aggregates consume logs (type=2) over the fixed window
    ``[start_timestamp, end_timestamp]`` ignoring the ``type`` param
    (``logsConsumeFilterTx``, ``log.go:312-338``). Fields:
      - ``request_count`` = COUNT(*)
      - ``prompt_tokens`` / ``completion_tokens`` / ``quota`` = SUM
      - ``avg_elapsed_ms`` = AVG(elapsed_time) WHERE elapsed_time > 0 (int64)
      - ``cost_amount`` = ``sumModelCosts`` on the **billed** cached (fallback real
        if 0), i.e. Σ computeLogCost(prompt, completion, cached_billed) per model.

    Because the stat is cumulative over a window, capturing a BASELINE before
    the run and an AFTER snapshot after, then diffing, isolates this run's
    contribution. The conftest ``model_requests`` fixture captures the baseline
    over the page-default 近8天 window (``start = now0 - 691199``, ``end = now0``,
    real-time at the baseline request). The after-snapshot REUSES the SAME
    frozen ``start`` and advances ``end`` to its own real-time request time, so
    the window only grows at the right edge — pre-existing rows in
    ``[start, now0]`` are in both snapshots and cancel exactly; the delta =
    rows in ``(now0, now1]`` = this run.

    Test01 (``has_target=false``) → ``ComputeBilledCachedTokens`` returns real
    unchanged → billed==real per row → ``cost_amount`` delta == Σ computeLogCost
    on our usages' cached (billed==real). The avg_elapsed_ms delta is
    reconstructed (avg*count diff / count delta) and cross-checked against the
    mean ``elapsed_time`` of this run's log rows (both server-side, exact for
    our run since all our rows have elapsed_time > 0).
    """
    baseline = model_requests.get("stat_baseline")
    if baseline is None:
        pytest.skip("log stat baseline unavailable (capture failed before requests)")
    win_start = model_requests[
        "query_start_8d"
    ]  # frozen 8d start (shared with /api/log/)
    win_end = model_requests[
        "query_end_8d"
    ]  # frozen 8d end (shared with /api/log/, captured post-propagation)
    baseline_end = model_requests["stat_window_end"]  # baseline capture time (now0)
    print(
        f"[stat] 基线快照: {baseline} (window [{win_start}..{baseline_end}]); "
        f"after window [{win_start}..{win_end}] (shared with /api/log/)"
    )

    usages = model_requests["usages"]
    our = sum_request_tokens(usages)
    print(f"[stat] 本次实际消耗: {our}")
    attach_json(our, "本次运行实际消耗(log stat)")

    # Poll the after-snapshot until the request_count delta reflects our run.
    with allure.step("查询 /api/log/stat 并轮询至本次 request_count 增量就绪"):
        raw = _poll_log_stat(
            admin_client, baseline, our["request_count"], win_start, win_end
        )
    assert isinstance(raw, dict)  # type-narrowing

    after = extract_log_stat(raw)
    _check(
        "/api/log/stat data 解析成功",
        expected=dict,
        actual=after,
        passed=after is not None,
        detail=f"failed to extract stat data: {raw!r}",
    )
    assert after is not None

    delta = delta_log_stat(after, baseline)
    print(f"[stat] after={after}, baseline={baseline}, delta={delta}")
    attach_json(
        {"baseline": baseline, "after": after, "delta": delta},
        "log stat 增量(基线/请求后/差值)",
    )

    # Tolerances: request_count is exact (COUNT); tokens allow a small margin
    # for any concurrent test01 traffic or propagation stragglers; cost uses the
    # billed==real path for test01, tight margin.
    count_tol = 0
    token_tol = max(100, our["total_input"] * 0.01)
    total_tokens = our["total_input"] + our["completion"]
    our_total_cost = round(
        sum(
            expected_amount(
                int(u.get("prompt_tokens", 0) or 0) - cached_of(u),
                cached_of(u),
                int(u.get("completion_tokens", 0) or 0),
                config.PRICE_INPUT,
                config.PRICE_CACHED,
                config.PRICE_OUTPUT,
            )
            for u in usages
        ),
        6,
    )
    cost_tol = max(1e-6, our_total_cost * 0.02)

    # (1) request_count delta == our request count.
    _delta_check(
        "delta request_count == 本次请求数",
        expected=len(usages),
        actual=delta["request_count"],
        tol=count_tol,
    )

    # (2) prompt_tokens delta == our prompt (full prompt, cached included).
    _delta_check(
        "delta prompt_tokens == 本次 prompt_tokens",
        expected=our["total_input"],
        actual=delta["prompt_tokens"],
        tol=token_tol,
    )

    # (3) completion_tokens delta == our completion.
    _delta_check(
        "delta completion_tokens == 本次 completion_tokens",
        expected=our["completion"],
        actual=delta["completion_tokens"],
        tol=token_tol,
    )

    # (4) total tokens (prompt + completion) delta == our total tokens.
    _delta_check(
        "delta (prompt + completion) == 本次总 token",
        expected=total_tokens,
        actual=delta["prompt_tokens"] + delta["completion_tokens"],
        tol=token_tol,
    )

    # (5) cost_amount delta == our expected total cost (test01 billed==real).
    _delta_check(
        "delta cost_amount == 本次预期总费用 (test01 billed==real)",
        expected=our_total_cost,
        actual=delta["cost_amount"],
        tol=cost_tol,
    )

    # (6) quota delta — diagnostic only (test01 订阅模式 → quota=0 per request).
    #     Not a user-requested assertion; recorded as a soft diagnostic so the
    #     test does not over-constrain on a plan-dependent field.
    record_assertion(
        "delta quota (诊断, test01 订阅模式应=0)",
        expected=0,
        actual=delta["quota"],
        passed=delta["quota"] == 0,
        detail=(
            f"delta quota={delta['quota']}; test01 订阅模式下每条 quota=0, delta 应为 0 "
            f"(若 test01 改为倍率计费模式则此项需重评; 非用户要求校验项, 软记录)"
        ),
    )

    # (7) avg_elapsed_ms cross-check vs the log rows' mean elapsed_time.
    #     Fetch this run's log rows (same window as test_log_list_entries), take
    #     the mean of their elapsed_time (exact, server-side), and compare with
    #     the reconstructed delta_avg. Both come from the same logs; for test01
    #     all rows have elapsed_time > 0, so the reconstruction is reliable.
    with allure.step("avg_elapsed_ms 交叉校验 (vs 本次日志行 elapsed_time 均值)"):
        rows = _fetch_run_log_entries(
            admin_client,
            model_requests["query_start_8d"],
            model_requests["query_end_8d"],
            model_requests["start_ts"],
            model_requests["end_ts"],
        )
        elapsed_values = [int(r.get("elapsed_time", 0) or 0) for r in rows]
        with_elapsed = [e for e in elapsed_values if e > 0]
        _check(
            "本次日志行均存在 elapsed_time > 0 (非流式已采)",
            expected=True,
            actual=f"{len(with_elapsed)}/{len(rows)} rows elapsed_time > 0",
            passed=len(rows) == 0 or len(with_elapsed) == len(rows),
            detail=(
                f"{len(rows) - len(with_elapsed)} rows have elapsed_time=0; "
                f"avg_elapsed_ms stat averages only elapsed_time>0 rows"
            ),
        )
        if rows and with_elapsed:
            log_mean_ms = sum(with_elapsed) / len(with_elapsed)
            delta_avg = delta["delta_avg_elapsed_ms"]
            # delta_avg reconstructed from int-truncated averages × counts; allow
            # generous tolerance for baseline truncation noise + the avg-over-
            # measured-vs-all-rows caveat.
            avg_tol = max(500.0, log_mean_ms * 0.25)
            if delta["request_count"] > 0:
                _check(
                    "delta avg_elapsed_ms ≈ 本次日志行 elapsed_time 均值",
                    expected=round(log_mean_ms, 1),
                    actual=round(delta_avg, 1),
                    passed=abs(delta_avg - log_mean_ms) <= avg_tol,
                    detail=(
                        f"delta_avg(reconstructed)={delta_avg}ms, "
                        f"log_rows_mean={log_mean_ms}ms, diff={abs(delta_avg - log_mean_ms)}, "
                        f"tol={avg_tol}; avg 基于 elapsed_time>0 行, "
                        f"count 为全部行, 重建有截断噪声"
                    ),
                    tolerance=avg_tol,
                )
                _check(
                    "delta avg_elapsed_ms > 0 (服务端有耗时记录)",
                    expected=True,
                    actual=delta_avg > 0,
                    passed=delta_avg > 0,
                    detail=f"delta_avg_elapsed_ms={delta_avg}ms, 应 > 0",
                )
            else:
                record_assertion(
                    "avg_elapsed_ms 交叉校验",
                    expected="delta request_count > 0",
                    actual=delta["request_count"],
                    passed=True,
                    detail="delta request_count=0, 跳过 avg 交叉校验",
                )
        else:
            record_assertion(
                "avg_elapsed_ms 交叉校验",
                expected="log rows available",
                actual=len(rows),
                passed=True,
                detail="本次日志行为空, 跳过 avg 交叉校验",
            )

    record_assertion(
        "本次运行 /api/log/stat 增量校验汇总",
        expected={
            "request_count": len(usages),
            "prompt_tokens": our["total_input"],
            "completion_tokens": our["completion"],
            "total_tokens": total_tokens,
            "cost_amount": our_total_cost,
        },
        actual={
            "request_count": delta["request_count"],
            "prompt_tokens": delta["prompt_tokens"],
            "completion_tokens": delta["completion_tokens"],
            "cost_amount": delta["cost_amount"],
            "avg_elapsed_ms": delta["delta_avg_elapsed_ms"],
        },
        passed=True,
        detail=(
            f"本次 {len(usages)} 次请求: prompt={our['total_input']}, "
            f"completion={our['completion']}, 预期总费用={our_total_cost}; "
            f"stat delta: count={delta['request_count']}, "
            f"cost={delta['cost_amount']}, avg_elapsed={delta['delta_avg_elapsed_ms']}ms"
        ),
    )
    print(
        f"[assert] log-stat delta PASSED: count={delta['request_count']}, "
        f"total_tokens={delta['prompt_tokens'] + delta['completion_tokens']}, "
        f"cost={delta['cost_amount']}, avg_elapsed={delta['delta_avg_elapsed_ms']}ms"
    )


# ---------------------------------------------------------------------------
# stress-metrics test (interface 3)
# ---------------------------------------------------------------------------


@allure.feature("压测指标")
@allure.story("test01 实际请求 vs 接口 stress-metrics 输入/输出 token 分布")
@allure.severity(allure.severity_level.CRITICAL)
def test_stress_metrics(model_requests, admin_client):
    """Baseline-delta cross-check of the stress-metrics token distribution.

    Endpoint ``GET /api/admin/usage/stress-metrics``
    (``controller/usage.go:GetStressMetrics``, releases/xb01). The handler loads
    all consume rows (``type=2``) with ``created_at BETWEEN start AND end``
    (inclusive) into Go memory and computes (``model/stress_metrics.go:91-207``):
      - ``summary``: ``n`` (count), ``intok_total``/``outtok_total`` (sums of
        ``prompt_tokens``/``completion_tokens``), ``intok_max``/``outtok_max``
        (maxes), ``peak_conc`` (scan-line), ``lat_avg``/``lat_max``
        (from ``elapsed_time``).
      - ``in_hist``: 9-bucket histogram of per-request ``prompt_tokens``.
      - ``out_hist``: 9-bucket histogram of per-request ``completion_tokens``.

    Bucket functions (``stress_metrics.go:283-310``, verified against
    ``stress_metrics_test.go:6-27``):
      - ``inputBucket``: 0→0, 1-100→1, 100-1k→2, 1k-5k→3, 5k-10k→4,
        10k-20k→5, 20k-50k→6, 50k-100k→7, >100k→8. Upper-inclusive bounds.
      - ``outputBucket``: 0→0, 1-32→1, 32-100→2, 100-500→3, 500-1k→4,
        1k-2k→5, 2k-5k→6, 5k-9999→7, >=10000→8 ("10000(满)", max_tokens cap).
        Upper-inclusive bounds; ``v>=10000`` is caught before the loop.

    The endpoint has no ``user_id``/``username`` filter (only ``model_name`` /
    ``token_name``), so the test uses a baseline-delta (same idea as log-stat).
    Because the endpoint filters rows by ``created_at BETWEEN start AND end``
    (NOT date-keyed like daily-cost), baseline and after MUST share the SAME
    frozen ``start`` for pre-existing traffic to cancel: the baseline is captured
    BEFORE requests over the page-default 近24小时 window
    (``start = now0 - 86400``, ``end = now0``) with ``model_name=glm-5.2`` filter,
    and the after-snapshot reuses that SAME frozen ``start`` and advances ``end``
    to a frozen post-propagation ``now1`` (captured once after PROPAGATION_WAIT,
    > all this run's row created_at). With the shared start, (after - baseline) =
    rows in (now0, now1] = exactly this run (mirrors test_log_stat_delta). A
    real-time sliding 24h window (``start = now_poll - 86400`` recomputed per
    poll) would let the ~24h-ago segment fall out of the after window and pollute
    the delta with negative counts (the delta_n=13<20 incident). The
    ``model_name`` filter narrows to glm-5.2 traffic and helps stay under the
    500k-row guard (``stressMaxRows``, ``stress_metrics.go:29``).

    However, concurrent glm-5.2 traffic from OTHER users that lands BETWEEN
    the baseline and after snapshots appears in the delta (no user_id filter to
    exclude it). The test therefore detects the environment:
      - **clean** (``delta_n == our request count``, no concurrent traffic):
        strict equality checks for A-E.
      - **concurrent** (``surplus = delta_n - our count > 0``): lower-bound /
        subset checks — our requests are a subset of the delta, so
        ``delta >= our values`` per field / per bucket. F/G/H remain exact.

    The histograms are computed from ``logs.prompt_tokens`` /
    ``logs.completion_tokens`` which are direct copies of
    ``usage.PromptTokens`` / ``usage.CompletionTokens`` (``helper.go:126``,
    ``model/log.go:25-26``, no rounding/clamping). So the expected histogram
    computed from our model ``usage`` per-request tokens (using the same bucket
    function) must exactly match the delta histogram in a clean environment.

    Assertions:
      (A) delta ``n``: == our count (clean) / >= our count (concurrent).
      (B) delta ``intok_total``: == our prompt sum (clean) / >= (concurrent).
      (C) delta ``outtok_total``: == our completion sum (clean) / >= (concurrent).
      (D) delta ``in_hist.counts``: == expected (clean) / >= expected per bucket
          (concurrent, subset check).
      (E) delta ``out_hist.counts``: == expected (clean) / >= expected per bucket
          (concurrent, subset check).
      (F) Histogram self-consistency: Σ delta counts == delta n (always exact).
      (G) ``intok_max``: after >= max(baseline, our_max_prompt) (always exact).
      (H) ``outtok_max``: after >= max(baseline, our_max_completion) (always exact).
    """
    baseline = model_requests.get("stress_baseline")
    if baseline is None:
        pytest.skip(
            "stress-metrics baseline unavailable (capture failed before requests)"
        )
    b_start = model_requests["stress_window_start"]
    b_end = model_requests["stress_window_end"]
    q_end = model_requests["stress_query_end"]
    print(
        f"[stress] 基线快照: n={baseline['n']} (window [{b_start},{b_end}]); "
        f"after window [{b_start},{q_end}] (shared frozen start)"
    )

    usages = model_requests["usages"]
    our = sum_request_tokens(usages)
    print(f"[stress] 本次实际消耗: {our}")
    attach_json(our, "本次运行实际消耗(stress-metrics)")

    # Per-request token lists for histogram computation. logs.prompt_tokens =
    # usage.PromptTokens (helper.go:126, model/log.go:25, no transform), so
    # bucketing our usage values with the same inputBucket yields the exact
    # expected histogram the server produces from the log rows.
    our_prompts = [int(u.get("prompt_tokens", 0) or 0) for u in usages]
    our_completions = [int(u.get("completion_tokens", 0) or 0) for u in usages]
    expected_in = expected_hist_counts(our_prompts, input_bucket, len(IN_HIST_LABELS))
    expected_out = expected_hist_counts(
        our_completions, output_bucket, len(OUT_HIST_LABELS)
    )
    our_max_prompt = max(our_prompts) if our_prompts else 0
    our_max_completion = max(our_completions) if our_completions else 0

    print(f"[stress] expected in_hist: {dict(zip(IN_HIST_LABELS, expected_in))}")
    print(f"[stress] expected out_hist: {dict(zip(OUT_HIST_LABELS, expected_out))}")
    attach_json(
        {
            "expected_in_hist": dict(zip(IN_HIST_LABELS, expected_in)),
            "expected_out_hist": dict(zip(OUT_HIST_LABELS, expected_out)),
            "our_max_prompt": our_max_prompt,
            "our_max_completion": our_max_completion,
            "per_request_prompts": our_prompts,
            "per_request_completions": our_completions,
        },
        "预期分布(本次 usage 分桶)",
    )

    # Poll until the after-snapshot's n delta reflects our run. Uses the SHARED
    # frozen window (start frozen at baseline = now0 - 86400, end frozen
    # post-propagation = now1) so pre-existing glm-5.2 traffic in the overlap
    # cancels and the delta equals exactly this run — mirroring the frozen-window
    # pattern in test_log_stat_delta. Polling retries the SAME frozen query (not
    # a real-time sliding 24h window, which would let the ~24h-ago segment fall
    # out of the after window and pollute the delta with negative counts).
    with allure.step("查询 stress-metrics 并轮询至本次数据就绪"):
        raw = _poll_stress_metrics(
            admin_client, baseline, our["request_count"], b_start, q_end
        )
    print(f"[admin] stress-metrics raw (after): {raw}")
    attach_json(raw, "stress-metrics 原始响应(请求后)")

    _check(
        "stress-metrics 接口成功",
        expected=True,
        actual=bool(isinstance(raw, dict) and raw.get("success", True)),
        passed=isinstance(raw, dict) and raw.get("success", True),
        detail=f"stress-metrics call failed: {raw!r}",
    )
    assert isinstance(raw, dict)  # type-narrowing

    after = extract_stress_snapshot(raw)
    _check(
        "stress-metrics data 解析成功",
        expected=dict,
        actual=after,
        passed=after is not None,
        detail=f"failed to extract stress-metrics data: {raw!r}",
    )
    assert after is not None
    attach_json(after, "stress-metrics 快照(请求后)")

    # Schema guard: verify the interface-returned histogram labels and bucket
    # counts match the hardcoded constants used by the delta math and the D/E
    # assertions below. Those computations rely on ``zip(IN_HIST_LABELS,
    # delta_in_hist)`` aligning label-by-count; if the backend changes a label
    # string (e.g. ">100k" → ">100000") or the bucket count (9 → 8/10), ``zip``
    # would silently misalign/truncate and the histogram checks could pass
    # against the wrong mapping. Asserting the schema explicitly surfaces such
    # drift as a clear, localized failure before the delta math runs.
    with allure.step("直方图 schema 校验: labels / 桶数"):
        _check(
            "after in_hist.labels == 预期输入分桶标签",
            expected=IN_HIST_LABELS,
            actual=after["in_hist_labels"],
            passed=after["in_hist_labels"] == IN_HIST_LABELS,
            detail=(
                f"after in_hist.labels={after['in_hist_labels']} != "
                f"expected={IN_HIST_LABELS}"
            ),
        )
        _check(
            "after out_hist.labels == 预期输出分桶标签",
            expected=OUT_HIST_LABELS,
            actual=after["out_hist_labels"],
            passed=after["out_hist_labels"] == OUT_HIST_LABELS,
            detail=(
                f"after out_hist.labels={after['out_hist_labels']} != "
                f"expected={OUT_HIST_LABELS}"
            ),
        )
        _check(
            "after in_hist.counts 桶数 == 9",
            expected=len(IN_HIST_LABELS),
            actual=len(after["in_hist_counts"]),
            passed=len(after["in_hist_counts"]) == len(IN_HIST_LABELS),
            detail=(
                f"after in_hist.counts length={len(after['in_hist_counts'])} != "
                f"expected={len(IN_HIST_LABELS)}"
            ),
        )
        _check(
            "after out_hist.counts 桶数 == 9",
            expected=len(OUT_HIST_LABELS),
            actual=len(after["out_hist_counts"]),
            passed=len(after["out_hist_counts"]) == len(OUT_HIST_LABELS),
            detail=(
                f"after out_hist.counts length={len(after['out_hist_counts'])} != "
                f"expected={len(OUT_HIST_LABELS)}"
            ),
        )
        # Baseline must share the same schema, otherwise the element-wise delta
        # (after - baseline) is meaningless.
        _check(
            "baseline in_hist.counts 桶数 == 9",
            expected=len(IN_HIST_LABELS),
            actual=len(baseline["in_hist_counts"]),
            passed=len(baseline["in_hist_counts"]) == len(IN_HIST_LABELS),
            detail=(
                f"baseline in_hist.counts length="
                f"{len(baseline['in_hist_counts'])} != "
                f"expected={len(IN_HIST_LABELS)}"
            ),
        )
        _check(
            "baseline out_hist.counts 桶数 == 9",
            expected=len(OUT_HIST_LABELS),
            actual=len(baseline["out_hist_counts"]),
            passed=len(baseline["out_hist_counts"]) == len(OUT_HIST_LABELS),
            detail=(
                f"baseline out_hist.counts length="
                f"{len(baseline['out_hist_counts'])} != "
                f"expected={len(OUT_HIST_LABELS)}"
            ),
        )

    # Compute deltas (fixed window → pre-existing traffic cancels).
    delta_n = after["n"] - baseline["n"]
    delta_intok = after["intok_total"] - baseline["intok_total"]
    delta_outtok = after["outtok_total"] - baseline["outtok_total"]
    delta_in_hist = delta_hist_counts(
        after["in_hist_counts"], baseline["in_hist_counts"]
    )
    delta_out_hist = delta_hist_counts(
        after["out_hist_counts"], baseline["out_hist_counts"]
    )

    print(
        f"[stress] after n={after['n']}, baseline n={baseline['n']}, delta n={delta_n}"
    )
    print(
        f"[stress] delta intok_total={delta_intok}, delta outtok_total={delta_outtok}"
    )
    print(f"[stress] delta in_hist={dict(zip(IN_HIST_LABELS, delta_in_hist))}")
    print(f"[stress] delta out_hist={dict(zip(OUT_HIST_LABELS, delta_out_hist))}")

    attach_json(
        {
            "baseline": baseline,
            "after": after,
            "delta_n": delta_n,
            "delta_intok_total": delta_intok,
            "delta_outtok_total": delta_outtok,
            "delta_in_hist": dict(zip(IN_HIST_LABELS, delta_in_hist)),
            "delta_out_hist": dict(zip(OUT_HIST_LABELS, delta_out_hist)),
            "expected_in_hist": dict(zip(IN_HIST_LABELS, expected_in)),
            "expected_out_hist": dict(zip(OUT_HIST_LABELS, expected_out)),
        },
        "stress-metrics 增量(基线/请求后/差值/预期)",
    )

    # Whether the environment is clean (no concurrent glm-5.2 traffic landed in
    # the window between baseline and after).  The stress-metrics endpoint has
    # NO user_id/username filter (only model_name/token_name,
    # stress_metrics.go:94), so concurrent glm-5.2 requests from OTHER users
    # appear in the delta.  In a clean env delta_n == our request count;
    # otherwise surplus > 0 and we switch to lower-bound/subset checks (A-E)
    # because our requests are a subset of the delta.  F/G/H remain exact
    # (self-consistency and max-aggregate invariants hold regardless of
    # concurrent traffic).
    clean_env = delta_n == our["request_count"]
    surplus = max(0, delta_n - our["request_count"])
    if surplus:
        print(
            f"[stress] concurrent traffic detected: delta_n={delta_n}, "
            f"our={our['request_count']}, surplus={surplus}; "
            f"using lower-bound/subset checks for A-E"
        )

    # (A) delta n
    if clean_env:
        _delta_check(
            "delta n == 本次请求数 (无并发, 精确)",
            expected=len(usages),
            actual=delta_n,
            tol=0,
        )
    else:
        # Our requests are a subset of the delta → delta_n >= our count.
        _check(
            "delta n >= 本次请求数 (含并发流量, 下界校验)",
            expected=len(usages),
            actual=delta_n,
            passed=delta_n >= len(usages),
            detail=(
                f"delta_n={delta_n} < our_count={len(usages)}; our requests not "
                f"fully landed in the stress-metrics window. surplus={surplus}"
            ),
        )
        record_assertion(
            "并发流量诊断 (surplus)",
            expected=len(usages),
            actual=delta_n,
            passed=True,  # diagnostic
            detail=(
                f"surplus={surplus} concurrent glm-5.2 requests from other users "
                f"landed in the fixed window; A-E use lower-bound/subset checks, "
                f"F/G/H remain exact"
            ),
        )

    # (B) delta intok_total — prompt_tokens has no billed/real split.
    if clean_env:
        _delta_check(
            "delta intok_total == 本次 prompt_tokens (无并发, 精确)",
            expected=our["total_input"],
            actual=delta_intok,
            tol=max(100, our["total_input"] * 0.01),
        )
    else:
        _check(
            "delta intok_total >= 本次 prompt_tokens (含并发, 下界)",
            expected=our["total_input"],
            actual=delta_intok,
            passed=delta_intok >= our["total_input"],
            detail=(
                f"delta_intok={delta_intok} < our_total_input={our['total_input']}; "
                f"our prompt tokens not fully reflected in the delta"
            ),
        )

    # (C) delta outtok_total
    if clean_env:
        out_tol = max(100, our["completion"] * 0.01) if our["completion"] else 100
        _delta_check(
            "delta outtok_total == 本次 completion_tokens (无并发, 精确)",
            expected=our["completion"],
            actual=delta_outtok,
            tol=out_tol,
        )
    else:
        _check(
            "delta outtok_total >= 本次 completion_tokens (含并发, 下界)",
            expected=our["completion"],
            actual=delta_outtok,
            passed=delta_outtok >= our["completion"],
            detail=(
                f"delta_outtok={delta_outtok} < our_completion={our['completion']}; "
                f"our completion tokens not fully reflected in the delta"
            ),
        )

    # (D) delta in_hist — clean: exact equality; concurrent: subset check
    #     (our requests contribute expected_in[i] to bucket i; concurrent
    #     traffic may add more → delta_in_hist[i] >= expected_in[i]).
    with allure.step("输入 token 分布校验"):
        if clean_env:
            bad_in = []
            for i, label in enumerate(IN_HIST_LABELS):
                exp = expected_in[i] if i < len(expected_in) else 0
                act = delta_in_hist[i] if i < len(delta_in_hist) else 0
                if act != exp:
                    bad_in.append({"label": label, "expected": exp, "actual": act})
            _check(
                "delta in_hist.counts == 预期输入分布 (无并发, 精确)",
                expected=dict(zip(IN_HIST_LABELS, expected_in)),
                actual=dict(zip(IN_HIST_LABELS, delta_in_hist)),
                passed=not bad_in,
                detail=f"in_hist bucket mismatches (exact): {bad_in}",
            )
        else:
            bad_in = []
            for i, label in enumerate(IN_HIST_LABELS):
                exp = expected_in[i] if i < len(expected_in) else 0
                act = delta_in_hist[i] if i < len(delta_in_hist) else 0
                if act < exp:
                    bad_in.append({"label": label, "expected": exp, "actual": act})
            _check(
                "delta in_hist.counts >= 预期输入分布 (含并发, 子集校验)",
                expected=dict(zip(IN_HIST_LABELS, expected_in)),
                actual=dict(zip(IN_HIST_LABELS, delta_in_hist)),
                passed=not bad_in,
                detail=(
                    f"in_hist buckets below expected (subset check): {bad_in}; "
                    f"concurrent surplus={surplus}"
                ),
            )

    # (E) delta out_hist — same pattern as (D)
    with allure.step("输出 token 分布校验"):
        if clean_env:
            bad_out = []
            for i, label in enumerate(OUT_HIST_LABELS):
                exp = expected_out[i] if i < len(expected_out) else 0
                act = delta_out_hist[i] if i < len(delta_out_hist) else 0
                if act != exp:
                    bad_out.append({"label": label, "expected": exp, "actual": act})
            _check(
                "delta out_hist.counts == 预期输出分布 (无并发, 精确)",
                expected=dict(zip(OUT_HIST_LABELS, expected_out)),
                actual=dict(zip(OUT_HIST_LABELS, delta_out_hist)),
                passed=not bad_out,
                detail=f"out_hist bucket mismatches (exact): {bad_out}",
            )
        else:
            bad_out = []
            for i, label in enumerate(OUT_HIST_LABELS):
                exp = expected_out[i] if i < len(expected_out) else 0
                act = delta_out_hist[i] if i < len(delta_out_hist) else 0
                if act < exp:
                    bad_out.append({"label": label, "expected": exp, "actual": act})
            _check(
                "delta out_hist.counts >= 预期输出分布 (含并发, 子集校验)",
                expected=dict(zip(OUT_HIST_LABELS, expected_out)),
                actual=dict(zip(OUT_HIST_LABELS, delta_out_hist)),
                passed=not bad_out,
                detail=(
                    f"out_hist buckets below expected (subset check): {bad_out}; "
                    f"concurrent surplus={surplus}"
                ),
            )

    # (F) Histogram self-consistency: Σ counts == delta n (each request
    #     contributes exactly one in_hist bucket and one out_hist bucket).
    with allure.step("直方图自洽: Σ counts == delta n"):
        sum_in = sum(delta_in_hist)
        sum_out = sum(delta_out_hist)
        _check(
            "Σ delta in_hist.counts == delta n",
            expected=delta_n,
            actual=sum_in,
            passed=sum_in == delta_n,
            detail=f"sum(in_hist)={sum_in} != delta_n={delta_n}",
        )
        _check(
            "Σ delta out_hist.counts == delta n",
            expected=delta_n,
            actual=sum_out,
            passed=sum_out == delta_n,
            detail=f"sum(out_hist)={sum_out} != delta_n={delta_n}",
        )

    # (G) intok_max: after >= max(baseline, our_max_prompt).
    #     The window is fixed, so baseline rows are still present; our rows are
    #     new. after_max = max over all rows >= max of any subset.
    with allure.step("intok_max 校验: after >= max(baseline, 本次最大)"):
        exp_max = max(baseline["intok_max"], our_max_prompt)
        _check(
            "after intok_max >= max(baseline intok_max, 本次最大 prompt)",
            expected=exp_max,
            actual=after["intok_max"],
            passed=after["intok_max"] >= exp_max,
            detail=(
                f"after intok_max={after['intok_max']} < expected="
                f"max(baseline={baseline['intok_max']}, our={our_max_prompt})={exp_max}"
            ),
        )

    # (H) outtok_max: after >= max(baseline, our_max_completion).
    with allure.step("outtok_max 校验: after >= max(baseline, 本次最大)"):
        exp_max = max(baseline["outtok_max"], our_max_completion)
        _check(
            "after outtok_max >= max(baseline outtok_max, 本次最大 completion)",
            expected=exp_max,
            actual=after["outtok_max"],
            passed=after["outtok_max"] >= exp_max,
            detail=(
                f"after outtok_max={after['outtok_max']} < expected="
                f"max(baseline={baseline['outtok_max']}, our={our_max_completion})={exp_max}"
            ),
        )

    # Summary diagnostic (peak_conc / lat_avg / lat_max are informational).
    record_assertion(
        "本次运行 stress-metrics 增量校验汇总",
        expected={
            "request_count": len(usages),
            "prompt_tokens": our["total_input"],
            "completion_tokens": our["completion"],
            "in_hist": dict(zip(IN_HIST_LABELS, expected_in)),
            "out_hist": dict(zip(OUT_HIST_LABELS, expected_out)),
        },
        actual={
            "delta_n": delta_n,
            "delta_intok_total": delta_intok,
            "delta_outtok_total": delta_outtok,
            "delta_in_hist": dict(zip(IN_HIST_LABELS, delta_in_hist)),
            "delta_out_hist": dict(zip(OUT_HIST_LABELS, delta_out_hist)),
            "intok_max": after["intok_max"],
            "outtok_max": after["outtok_max"],
            "peak_conc": after["peak_conc"],
            "lat_avg": after["lat_avg"],
            "lat_max": after["lat_max"],
        },
        passed=True,
        detail=(
            f"本次 {len(usages)} 次请求: prompt={our['total_input']}, "
            f"completion={our['completion']}; stress delta n={delta_n}, "
            f"intok={delta_intok}, outtok={delta_outtok}; "
            f"intok_max={after['intok_max']}, outtok_max={after['outtok_max']}"
        ),
    )
    print(
        f"[assert] stress-metrics PASSED: delta n={delta_n}, "
        f"intok={delta_intok}, outtok={delta_outtok}"
    )
