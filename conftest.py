"""Shared pytest fixtures.

Session-scoped fixtures:

* ``admin_client``   - logs into the operation platform once and reuses the cookie.
* ``model_client``   - the OpenAI-compatible client used by test01.
* ``model_requests`` - sends REQUEST_COUNT identical long-context requests so
  that request #1 fills the prompt cache and the rest hit it, then waits for
  the usage pipeline to propagate before the assertion tests run.

Allure integration:
* Each model request is recorded as a step with its usage + raw response.
* The admin login is recorded.
* ``generate_allure_report`` (autouse, session) builds the HTML report into
  ``reports/allure-report`` after the whole session finishes, using the
  allure CLI. Set ``ALAYA_SKIP_ALLURE_GEN=1`` to skip generation.
"""

import os
import shutil
import subprocess
import time

import allure
import pytest

import config
from clients.admin_client import AdminClient
from clients.model_client import ModelClient
from data.long_context import build_long_context
from data.metrics import (
    cached_of,
    extract_log_stat,
    extract_overview_snapshot,
    extract_snapshot,
    extract_stress_snapshot,
    find_overview_user,
    find_today_entry,
)
from report.allure_utils import env_info, record_model_call

ALLURE_RESULTS_DIR = os.path.join("reports", "allure-results")
ALLURE_REPORT_DIR = os.path.join("reports", "allure-report")


@pytest.fixture(scope="session")
def admin_client():
    with allure.step("管理后台登录 (POST /api/user/login)"):
        client = AdminClient(
            base_url=config.ADMIN_BASE_URL,
            username=config.ADMIN_USERNAME,
            password=config.ADMIN_PASSWORD,
        )
        cookie = client.login()
        print(f"[admin] logged in, cookie={cookie[:32]}...")
    env_info()
    yield client
    client.close()


@pytest.fixture(scope="session")
def model_client():
    client = ModelClient(
        base_url=config.MODEL_BASE_URL,
        api_key=config.MODEL_API_KEY,
        chat_path=config.MODEL_CHAT_PATH,
    )
    yield client
    client.close()


@pytest.fixture(scope="session")
def model_requests(model_client, admin_client):
    """Fire the cache populating + hitting requests exactly once per session.

    Executes ``ROUND_COUNT`` rounds of ``REQUEST_COUNT`` requests each. The tag
    is fixed for the whole run, so within a round request #1 fills the prompt
    cache and #2..N hit it; the gap between rounds (``INTER_ROUND_WAIT``) tests
    whether the cache persists. After all rounds, ``PROPAGATION_WAIT`` lets the
    usage pipeline propagate before the assertion tests run.

    A daily-cost **baseline snapshot** is taken *before* any request is sent,
    so the assertion tests can compute the delta (after - before) attributable
    to this run and cross-check it against the per-request usage totals.
    """
    # ----- 请求前: 查询 daily-cost 基线快照 -----
    with allure.step("请求前: 查询 daily-cost 基线快照"):
        dc_start, dc_end = config.last_24h_window()
        baseline_cost = admin_client.get_daily_cost(
            dc_start,
            dc_end,
            config.TEST_USER_ID,
        )
        baseline_entry = find_today_entry(
            baseline_cost, config.TEST_USER_ID, config.today_str()
        )
        baseline = extract_snapshot(baseline_entry)
        print(
            f"[baseline] daily-cost 基线: {baseline} "
            f"(entry={'present' if baseline_entry else 'absent -> all 0'})"
        )
        allure.attach(
            f"基线快照(请求前):\n{baseline}\nraw today entry: {baseline_entry}\n",
            name="daily-cost 基线快照",
            attachment_type=allure.attachment_type.TEXT,
        )

    # ----- 请求前: 查询 cache-optimize overview 基线快照 -----
    # The overview month_* / ctr_* are cumulative within a month; capturing the
    # baseline BEFORE any request lets test_cache_optimize_overview compute the
    # delta attributable to this run (mirroring the daily-cost baseline). Capture
    # is best-effort: a transient overview-endpoint failure must not block the
    # whole session (daily-cost / TPM tests don't depend on it); the new test
    # skips itself if the baseline is unavailable.
    overview_baseline = None
    overview_baseline_ts = None
    try:
        with allure.step("请求前: 查询 cache-optimize overview 基线快照"):
            overview_baseline_ts = int(time.time())
            overview_raw = admin_client.get_cache_optimize_overview()
            overview_row = find_overview_user(overview_raw, config.TEST_USER_ID)
            overview_baseline = extract_overview_snapshot(overview_row)
            print(f"[baseline] cache-opt overview: {overview_baseline}")
            allure.attach(
                f"基线快照(请求前, cache-opt overview):\n{overview_baseline}\n"
                f"capture_ts={overview_baseline_ts}\n",
                name="cache-opt overview 基线快照",
                attachment_type=allure.attachment_type.TEXT,
            )
    except Exception as exc:  # noqa: BLE001 - best-effort baseline
        print(
            f"[baseline] cache-opt overview capture failed: {exc!r}; "
            f"test_cache_optimize_overview will skip"
        )

    # ----- 请求前: 查询 log stat 基线快照 -----
    # The stat (request_count/prompt/completion/cost_amount) is cumulative over a
    # fixed window; capturing the baseline BEFORE any request lets
    # test_log_stat_delta compute (after - baseline) = this run's contribution
    # (pre-existing rows cancel since the window is fixed and present in both
    # snapshots). A NARROW window around the session is used so the baseline is
    # small (low truncation noise for the avg reconstruction). Best-effort: a
    # transient stat-endpoint failure must not block the session; the test skips
    # itself if the baseline is unavailable.
    stat_baseline = None
    stat_baseline_ts = int(time.time())
    # Page-default window (近8天 minus 1s, span=6911199): frozen ``start`` shared with
    # /api/log/ (test_log_list_entries); ``end`` is real-time at the baseline
    # request. The after-snapshot in test_log_stat_delta reuses the SAME frozen
    # start and advances ``end`` to its own request time, so (after - baseline)
    # = rows in (now0, now1] = exactly this run (pre-existing rows cancel).
    stat_window_start = stat_baseline_ts - config.LOG_WINDOW_SPAN
    stat_window_end = stat_baseline_ts
    try:
        with allure.step("请求前: 查询 log stat 基线快照"):
            stat_raw = admin_client.get_log_stat(
                type_=0,
                model_name="",
                token_name="",
                start_ts=stat_window_start,
                end_ts=stat_window_end,
                username=config.MODEL_USER,
                channel=0,
            )
            stat_baseline = extract_log_stat(stat_raw)
            print(
                f"[baseline] log stat: {stat_baseline} (window [{stat_window_start},{stat_window_end}])"
            )
            allure.attach(
                f"基线快照(请求前, log stat):\n{stat_baseline}\n"
                f"window=[{stat_window_start},{stat_window_end}]\n",
                name="log stat 基线快照",
                attachment_type=allure.attachment_type.TEXT,
            )
    except Exception as exc:  # noqa: BLE001 - best-effort baseline
        print(
            f"[baseline] log stat capture failed: {exc!r}; "
            f"test_log_stat_delta will skip"
        )

    # ----- 请求前: 查询 stress-metrics 基线快照 -----
    # The stress-metrics dashboard (controller/usage.go:GetStressMetrics →
    # model/stress_metrics.go:GetStressMetrics) loads all consume rows in a
    # window into Go memory and computes summary + histograms.  The endpoint has
    # no user_id/username filter (only model_name/token_name), so the test uses a
    # baseline-delta (same idea as log-stat): the baseline is captured BEFORE
    # requests over the page-default 近24小时 window (start = now0 - 86400, end =
    # now0) and the after-snapshot uses the SAME format with its own real-time
    # end (now1).  Pre-existing glm-5.2 traffic in the 24h-overlap cancels in the
    # delta.  model_name=glm-5.2 narrows the SQL filter (also helps stay under
    # the 500k-row guard).  Best-effort: a failure must not block the session;
    # the test skips itself if the baseline is unavailable.
    stress_baseline = None
    stress_window_start = None
    stress_window_end = None
    try:
        with allure.step("请求前: 查询 stress-metrics 基线快照"):
            # Page-default 近24小时 window: start = now - 86400, end = real-time
            # now at the baseline request. The after-snapshot (polled in
            # test_stress_metrics) uses the SAME format with its own real-time
            # end, so pre-existing glm-5.2 traffic in the 24h-overlap cancels in
            # the delta; in a clean test env the delta equals exactly this run.
            stress_window_start, stress_window_end = config.last_24h_window()
            stress_raw = admin_client.get_stress_metrics(
                start_ts=stress_window_start,
                end_ts=stress_window_end,
                model_name=config.MODEL_NAME,
            )
            stress_baseline = extract_stress_snapshot(stress_raw)
            print(
                f"[baseline] stress-metrics: n={stress_baseline['n'] if stress_baseline else '?'} "
                f"(window [{stress_window_start},{stress_window_end}])"
            )
            allure.attach(
                f"基线快照(请求前, stress-metrics):\n{stress_baseline}\n"
                f"window=[{stress_window_start},{stress_window_end}]\n",
                name="stress-metrics 基线快照",
                attachment_type=allure.attachment_type.TEXT,
            )
    except Exception as exc:  # noqa: BLE001 - best-effort baseline
        print(
            f"[baseline] stress-metrics capture failed: {exc!r}; "
            f"test_stress_metrics will skip"
        )

    tag = config.generate_run_tag()
    prefix = build_long_context(approx_tokens=config.PREFIX_TOKENS, tag=tag)
    print(
        f"[model] run_tag={tag} prefix_chars={len(prefix)} "
        f"(~{config.PREFIX_TOKENS} tokens target); "
        f"{config.ROUND_COUNT} rounds x {config.REQUEST_COUNT} reqs"
    )

    # Identical messages on every call => identical prefix => cache hits after #1.
    messages = [
        {"role": "system", "content": prefix},
        {"role": "user", "content": "请用一句话概括上文的核心观点。"},
    ]
    request_payload = {
        "model": config.MODEL_NAME,
        "max_tokens": config.MAX_TOKENS,
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": f"<long prefix, {len(prefix)} chars>"},
            {"role": "user", "content": messages[1]["content"]},
        ],
    }

    usages = []
    request_points = []  # (completion_unix_ts, usage_dict) per request
    rounds = []  # per-round summary dicts
    req_index = 0
    window_start = time.time()
    for round_idx in range(1, config.ROUND_COUNT + 1):
        round_usages = []
        round_start_ts = None
        round_end_ts = None
        with allure.step(
            f"第 {round_idx}/{config.ROUND_COUNT} 轮: 发起 {config.REQUEST_COUNT} 次模型请求"
        ):
            for i in range(config.REQUEST_COUNT):
                result = model_client.chat(
                    messages,
                    model=config.MODEL_NAME,
                    max_tokens=config.MAX_TOKENS,
                    temperature=0.0,
                )
                done_ts = int(time.time())
                usage = result.get("usage", {})
                usages.append(usage)
                round_usages.append(usage)
                request_points.append((done_ts, usage))
                req_index += 1
                if round_start_ts is None:
                    round_start_ts = done_ts
                round_end_ts = done_ts
                if usage:
                    detail = ", ".join(f"{k}={v}" for k, v in usage.items())
                else:
                    detail = "<no usage>"
                print(
                    f"[model] round{round_idx} req#{i + 1}/{config.REQUEST_COUNT} "
                    f"(#{req_index} total) @ts={done_ts}: {detail}"
                )
                record_model_call(
                    req_index,
                    config.MODEL_NAME,
                    usage,
                    done_ts,
                    request_payload,
                    result,
                )
                time.sleep(config.REQUEST_PACING)
        rounds.append(
            {
                "round": round_idx,
                "start_ts": round_start_ts,
                "end_ts": round_end_ts,
                "usages": round_usages,
            }
        )

        # Wait between rounds (not after the last one).
        if round_idx < config.ROUND_COUNT and config.INTER_ROUND_WAIT > 0:
            print(
                f"[model] round {round_idx} done; waiting {config.INTER_ROUND_WAIT}s "
                f"before round {round_idx + 1} (cache-persistence gap)..."
            )
            with allure.step(
                f"轮次间隔: 等待 {config.INTER_ROUND_WAIT}s (测试缓存跨轮持久化)"
            ):
                time.sleep(config.INTER_ROUND_WAIT)
    window_end = int(time.time())

    # Cache-hit summary across all requests.  Some usages carry
    # ``prompt_tokens_details: None`` (cache-filler requests with no hit), so
    # use ``cached_of`` which guards against None/non-dict values rather than
    # the raw ``.get("prompt_tokens_details", {}).get(...)`` chain that crashes
    # when the key is present but None.
    cached = sum(cached_of(u) for u in usages)
    uncached_total = sum(int(u.get("prompt_tokens") or 0) for u in usages)
    completion_total = sum(int(u.get("completion_tokens") or 0) for u in usages)
    allure.attach(
        f"run_tag={tag}\n"
        f"rounds={config.ROUND_COUNT} x {config.REQUEST_COUNT} = {len(usages)} requests\n"
        f"inter_round_wait={config.INTER_ROUND_WAIT}s\n"
        f"window=[{int(window_start)}..{window_end}] ({window_end - int(window_start)}s)\n"
        f"prompt_tokens_sum={uncached_total}\n"
        f"cached_input_tokens_sum={cached}\n"
        f"completion_tokens_sum={completion_total}\n",
        name="请求会话汇总",
        attachment_type=allure.attachment_type.TEXT,
    )

    print(
        f"[model] done: {len(usages)} reqs over {config.ROUND_COUNT} rounds, "
        f"window=[{int(window_start)}..{window_end}] "
        f"(duration={window_end - int(window_start)}s)"
    )
    print(f"[admin] waiting {config.PROPAGATION_WAIT}s for usage propagation...")
    with allure.step(f"等待 {config.PROPAGATION_WAIT}s 数据落地"):
        time.sleep(config.PROPAGATION_WAIT)

    # Shared query END for /api/log/ & /api/log/stat (page-default 近8天 window).
    # Captured ONCE here (post-run + post-propagation) so both interfaces — which
    # run as separate tests at different times — use IDENTICAL start AND end.
    # ``start`` was frozen at baseline (= now0 - LOG_WINDOW_SPAN, shared above);
    # ``end`` is a real "now" captured now (post-run → covers all this run's
    # rows; their ``created_at`` is the request time, which is < this end). For
    # test_log_stat_delta: baseline window [start, now0], after window [start,
    # now1] with the SAME start → (after - baseline) = rows in (now0, now1] = run.
    query_end_8d = int(time.time())

    # Shared query (start, end) for /api/daily-cost & /api/tpm-capacity (same
    # operation page → identical timestamps; page-default 近24小时 window, span
    # = 86400). Captured ONCE here so both interfaces — which run as separate
    # tests at different times — use the SAME start and end (mirroring how the
    # page sends both requests with one captured "now"). ``start`` = this end
    # - 86400; both post-run queries (daily-cost poll + tpm-capacity query)
    # reuse these frozen values (the run's rows were created earlier so are
    # always inside the window).
    page_end_24h = int(time.time())
    page_start_24h = page_end_24h - 86400
    return {
        "usages": usages,
        "request_points": request_points,
        "start_ts": int(window_start),
        "end_ts": window_end,
        "rounds": rounds,
        "run_tag": tag,
        "baseline": baseline,
        "baseline_entry_present": baseline_entry is not None,
        "overview_baseline": overview_baseline,
        "overview_baseline_ts": overview_baseline_ts,
        "stat_baseline": stat_baseline,
        "stat_baseline_ts": stat_baseline_ts,
        "stat_window_start": stat_window_start,
        "stat_window_end": stat_window_end,
        "query_start_8d": stat_window_start,  # shared frozen page-default 8d start for /api/log/ & /api/log/stat
        "query_end_8d": query_end_8d,  # shared frozen page-default 8d end (captured post-propagation)
        "page_start_24h": page_start_24h,  # shared frozen 24h start for /api/daily-cost & /api/tpm-capacity (same page)
        "page_end_24h": page_end_24h,  # shared frozen 24h end (same value for both interfaces)
        "stress_baseline": stress_baseline,
        "stress_window_start": stress_window_start,
        "stress_window_end": stress_window_end,
    }


@pytest.fixture(scope="session", autouse=True)
def generate_allure_report(request):
    """Auto-build the Allure HTML report after the session ends.

    Raw JSON results are written to ``reports/allure-results`` by pytest's
    ``--alluredir``. This fixture runs ``allure generate`` once the session is
    done to produce ``reports/allure-report/index.html``.
    """
    yield
    if os.environ.get("ALAYA_SKIP_ALLURE_GEN") == "1":
        print("[allure] ALAYA_SKIP_ALLURE_GEN=1, skipping HTML report generation")
        return
    if not shutil.which("allure"):
        print("[allure] allure CLI not found; raw results in reports/allure-results")
        print(
            "[allure] install it (`npm i -g allure-commandline`) then run: "
            "allure generate reports/allure-results -o reports/allure-report --clean"
        )
        return
    os.makedirs("reports", exist_ok=True)
    allure_bin = shutil.which("allure")
    cmd = [
        allure_bin or "allure",
        "generate",
        ALLURE_RESULTS_DIR,
        "-o",
        ALLURE_REPORT_DIR,
        "--clean",
    ]
    # On Windows the npm shim is a .cmd/.bat; subprocess needs shell=True to run it.
    use_shell = bool(
        os.name == "nt" and allure_bin and allure_bin.lower().endswith((".cmd", ".bat"))
    )
    print(f"[allure] generating HTML report: {' '.join(cmd)}")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300, shell=use_shell
        )
        print(proc.stdout)
        if proc.returncode == 0:
            print(
                f"[allure] report ready: {os.path.abspath(ALLURE_REPORT_DIR)}/index.html"
            )
        else:
            print(
                f"[allure] generation failed (code={proc.returncode}):\n{proc.stderr}"
            )
    except subprocess.TimeoutExpired:
        print("[allure] generation timed out")
