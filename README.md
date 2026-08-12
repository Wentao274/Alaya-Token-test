# GLM-5.2 缓存命中与计费自动化测试

以用户 `test01` 对 `glm-5.2` 模型发起长上下文请求，触发并命中 Prompt Cache，再通过运营管理后台接口核对 **每日收入**、**TPM 用量**、**缓存命中率优化**、**日志查询**、**压测指标** 六个维度，自动断言缓存生效、计费公式正确、用量数据一致。

所有断言均以 nexus `releases/xb01` 分支源码为口径基准，行号引用于各测试 docstring 与注释中。

## 目录结构

```
alaya-token-test/
├── requirements.txt              # 依赖：httpx、pytest、allure-pytest
├── pytest.ini                    # pytest 配置（pythonpath=.、testpaths、日志）
├── run.py                         # 运行入口：时间戳分目录 + 转发参数到 pytest
├── config.py                     # 全部配置项，支持 ALAYA_* 环境变量覆盖
├── conftest.py                   # session 级 fixture + 4 个基线快照 + Allure 报告自动生成
├── clients/
│   ├── admin_client.py           # 运营后台：登录 + tpm-capacity/daily-cost/cache-optimize/log/stat/stress-metrics
│   └── model_client.py           # 模型侧：OpenAI 兼容 /v1/chat/completions
├── data/
│   ├── long_context.py           # 生成固定长前缀（~3000 token），用于命中缓存
│   └── metrics.py                # 共享指标工具: 基线/增量、请求用量汇总、收入公式、直方图分桶
├── report/
│   └── allure_utils.py           # Allure 步骤/附件/断言记录工具
├── tests/
│   └── test_glm52_cache_billing.py   # 6 个断言测试
└── reports/                      # 运行后自动生成（已 gitignore），每次运行一个时间戳子目录
    └── <YYYYMMDD-HHMMSS>/        # 单次运行结果目录（run.py 自动创建）
        ├── allure-results/       # Allure 原始 JSON 结果
        └── allure-report/        # Allure HTML 报告（index.html）
```

## 安装与运行

```bash
pip install -r requirements.txt

# 安装 Allure CLI（生成 HTML 报告所需，需 Java）
npm install -g allure-commandline

# 跑全部用例 —— run.py 会按时间戳自动分目录，报告生成到
#   reports/<YYYYMMDD-HHMMSS>/allure-report/index.html
# 多次运行互不覆盖（每次落到独立时间戳目录）
python run.py

# 只跑单个用例（参数原样转发给 pytest）
python run.py tests/test_glm52_cache_billing.py::test_daily_cost_revenue -s
python run.py tests/test_glm52_cache_billing.py::test_stress_metrics -s

# 用 -k 过滤
python run.py -k tpm

# 跳过报告自动生成（只产出原始 JSON）
ALAYA_SKIP_ALLURE_GEN=1 python run.py

# 自定义本次运行目录名（默认时间戳；设置后不再追加时间戳）
ALAYA_RUN_DIR=my-run-42 python run.py

# 手动重新生成 HTML 报告（替换 <run> 为实际时间戳目录）
allure generate reports/<run>/allure-results -o reports/<run>/allure-report --clean

# 本地预览报告（启动临时服务器，浏览器自动打开）
allure serve reports/<run>/allure-results
```

> 运行会真实调用模型 API（产生计费）并访问内网管理平台 `10.220.75.84:8080`，请确认网络可达后再执行。`-s` 用于查看实时日志输出。
>
> 也可直接用 `pytest` 运行（此时需自行传 `--alluredir=<dir>`，且 `run.py` 的时间戳分目录与 `ALAYA_RUN_DIR` 同步逻辑不会生效）。

## 配置

所有配置集中在 `config.py`，默认值即测试需求中的值，可用环境变量（前缀 `ALAYA_`）覆盖：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `ALAYA_MODEL_BASE_URL` | `https://token-bj07.alayanew.com:26443` | 模型服务地址 |
| `ALAYA_MODEL_API_KEY` | `sk-08c1ebd3...` | test01 的 api-key |
| `ALAYA_MODEL_NAME` | `glm-5.2` | 被测模型 |
| `ALAYA_MODEL_CHAT_PATH` | `/v1/chat/completions` | OpenAI 兼容路径 |
| `ALAYA_ADMIN_BASE_URL` | `http://10.220.75.84:8080` | 运营管理后台地址 |
| `ALAYA_ADMIN_USERNAME` / `ALAYA_ADMIN_PASSWORD` | `root` / `tEscuYb3aDp_OWwhU4` | 后台登录账号 |
| `ALAYA_TEST_USER_ID` | `10` | test01 在后台的用户 id |
| `ALAYA_PRICE_INPUT` / `ALAYA_PRICE_CACHED` / `ALAYA_PRICE_OUTPUT` | `8.0` / `2.0` / `28.0` | glm-5.2 单价（元/百万 token） |
| `ALAYA_REQUEST_COUNT` | `10` | 每轮请求数（第 1 次填充缓存，其余命中） |
| `ALAYA_ROUND_COUNT` | `2` | 每次运行执行的轮数（默认 2 轮共 20 次请求） |
| `ALAYA_INTER_ROUND_WAIT` | `120` | 轮次间隔秒数（验证缓存跨轮持久化） |
| `ALAYA_RUN_TAG` | 自动生成 | 嵌入前缀的唯一运行标记，区分不同运行；留空则按时间自动生成 |
| `ALAYA_PREFIX_TOKENS` | `3000` | 长前缀目标 token 数 |
| `ALAYA_MAX_TOKENS` | `64` | 单次补全上限（控制成本） |
| `ALAYA_REQUEST_PACING` | `1.0` | 请求间隔秒数 |
| `ALAYA_PROPAGATION_WAIT` | `120` | 所有轮次结束后等待数据落地秒数（2 分钟） |
| `ALAYA_POLL_RETRIES` / `ALAYA_POLL_INTERVAL` | `6` / `15` | 拉取后台数据的轮询次数与间隔 |
| `ALAYA_SKIP_ALLURE_GEN` | `0` | 设为 `1` 则跳过测试结束后的 HTML 报告自动生成 |
| `ALAYA_RUN_DIR` | 自动时间戳 | 单次运行结果目录名（`reports/<ALAYA_RUN_DIR>/`）；设为 `my-run-42` 则用该名且不再追加时间戳 |

> 接口时间窗口已改为页面默认口径，不再使用 `ALAYA_TPM_START/END`、`ALAYA_DAILY_START/END` 等静态窗口变量（`config.py` 中保留定义但测试不再引用）。

### 接口时间窗口口径

各后台接口的时间窗口均与运营平台页面默认一致：

| 接口 | 窗口口径 | start / end 来源 |
|---|---|---|
| `daily-cost` + `tpm-capacity` | 近24小时，同一页面共享相同 start/end | `conftest` 在落地等待后捕获一次 `now1`：`page_start_24h = now1 − 86400`，`page_end_24h = now1`；两接口所有查询（含轮询）复用这一对值 |
| `log list` + `log stat` | 近8天减1秒（`LOG_WINDOW_SPAN = 691199`），同一页面共享相同 start/end | `conftest` 基线时冻结一次 `start = now0 − 691199`；落地等待后捕获一次 `end = now1`；两接口复用这一对值（`query_start_8d` / `query_end_8d`） |
| `stress-metrics` | 近24小时，每次请求实时 `now` | 基线与轮询各调 `config.last_24h_window()`，`start = end − 86400`，`end = int(time.time())` |
| `cache-optimize overview` | 无时间窗口参数 | 月度+小时聚合 |

> **设计要点**：同一页面共享一对 `start`/`end` 是因为页面发出请求时只用一个捕获的 `now`；`log stat` 采用基线-增量法，冻结 `start`、推进 `end`，使 `(after − baseline)` 精确抵消窗口内既有流量，仅剩本次运行贡献。

## 测试流程

6 个测试共享 session 级 fixture `model_requests`（整个会话只执行一次）：

### 1. 请求前捕获 4 个基线快照（best-effort，失败不阻塞）

| 基线 | 接口 | 窗口（页面默认口径） | 捕获内容 |
|---|---|---|---|
| daily-cost | `GET /api/log/daily-cost` | 近24小时实时：`config.last_24h_window()`（start=now−86400, end=now） | `{uncached, cached, completion, amount}` |
| cache-opt overview | `GET /api/admin/usage/cache-optimize/overview` | 无参（月+小时聚合） | `{month_prompt, month_real, month_billed, real_rate, billed_rate, hour_*, ctr_granted, ...}` |
| log-stat | `GET /api/log/stat` | 近8天减1秒（`LOG_WINDOW_SPAN=691199`）：`start=now0−691199`（冻结）, `end=now0` | `{request_count, prompt, completion, cost_amount, avg_elapsed_ms}` |
| stress-metrics | `GET /api/admin/usage/stress-metrics` | 近24小时实时：`config.last_24h_window()`，`model_name=glm-5.2` | `{n, intok_total, outtok_total, in_hist_counts, out_hist_counts, ...}` |

> daily-cost 与 log-stat 的**请求前基线**用各自页面默认窗口实时取 `end`；log-stat 的 `start` 在基线时冻结，落地后复用同一 `start`、推进 `end`，使增量精确隔离本次运行。

### 2. 发起 2 轮 × 10 次模型请求

- 每轮第 1 次填充缓存（`prompt_tokens_details=None`），第 2~10 次命中（`cached_tokens=3712`）
- 轮间等待 `INTER_ROUND_WAIT=120s` 验证缓存跨轮持久化
- 每条请求记录完成时刻 unix 时间戳与 `usage` 用量

### 3. 等待数据落地并捕获共享时间窗口

所有轮次结束后等待 `PROPAGATION_WAIT=120s`（2 分钟），随后捕获本次会话剩余的共享时间窗口：

- **`query_end_8d`** = `int(time.time())`（此刻），与基线冻结的 `query_start_8d` 配对 → 供 `/api/log/` 与 `/api/log/stat` 共享
- **`page_end_24h`** = `int(time.time())`，`page_start_24h = page_end_24h − 86400` → 供 `/api/daily-cost` 与 `/api/tpm-capacity` 共享

这些窗口随 fixture 返回给 6 个测试，确保同一页面的两个接口使用完全相同的 `start`/`end` 时间戳。

### 4. 运营后台登录

`admin_client.login()` 用 root 账号 POST `/api/user/login`，从 `Set-Cookie` 提取 `session=...`，后续所有请求复用该 Cookie。

## 本次请求实际数据汇总

以一次典型运行（2 轮 × 10 次）为例，`sum_request_tokens(usages)` 汇总：

| 请求 | prompt | completion | cached |
|---|---|---|---|
| R1#1（填充） | 3745 | 64 | 0（None） |
| R1#2-10（9 次命中） | 3745×9 | 64×9 | 3712×9 |
| R2#1（部分命中） | 3745 | 64 | 320 |
| R2#2-10（9 次命中） | 3745×9 | 64×9 | 3712×9 |

| 汇总字段 | 值 | 说明 |
|---|---|---|
| `total_input` | **74900** | Σ prompt_tokens（全 prompt，含 cached） |
| `cached_input` | **67136** | Σ cached_of（billed 口径，test01 billed==real） |
| `uncached_input` | **7764** | total − cached |
| `completion` | **1280** | Σ completion_tokens |
| `total_tokens` | **76180** | prompt + completion（nexus `helper.go:237`） |
| `request_count` | **20** | 2 轮 × 10 次 |

## 核心口径概念

### 两套 cached token（关键）

| 口径 | 字段 | 来源 | 用途 |
|---|---|---|---|
| **real** | `logs.cached_tokens` | 真实 KV-prefix-cache 命中，clamped to prompt（`helper.go:193-198`） | daily-cost / log list / cache-opt overview 的 real_* |
| **billed** | `logs.cached_tokens_billed` | cache-hit-rate 优化调整值（`helper.go:204-210`） | 客户 `usage.cached_input_tokens` / quota 扣减 / cache-opt overview 的 billed_* |

- 客户响应 `usage.cached_input_tokens` 是 **billed** 值（`usage_filter.go:38-83` 改写）
- test01 `has_target=false` → `ComputeBilledCachedTokens` 返回 realCached 不变（`cache_optimize.go:300-310`）→ **billed == real**，所有口径一致

### 三条 cost 计算路径

| 接口 | 用途 | cached 口径 | 源码 |
|---|---|---|---|
| daily-cost `amount` | 运营收入 | **real** | `controller/log.go:582` 用 `r.CachedTokens` |
| log list `cost` | 逐行费用 | **real** | `fillLogCosts`（`log.go:225-228`）→ `computeLogCost` |
| log stat `cost_amount` | 聚合费用 | **billed**（fallback real） | `sumModelCosts`（`log.go:235-244`） |

test01 billed==real，三条路径数值一致。

## 断言详解

### 1. `test_daily_cost_revenue` — 每日收入计费

#### 实际数据（接口）

| 步骤 | 方法 | 说明 |
|---|---|---|
| 轮询 | `_poll_daily_cost` → `GET /api/log/daily-cost?user_id=10` | 轮询至当天 test01 记录出现 |
| 定位 | `find_today_entry(cost, 10, today_str())` | 从 `data[]` 找 `user_id==10 && date==today` |
| 提取 | `after = extract_snapshot(entry)` | `{uncached_input_tokens, cached_input_tokens, completion_tokens, amount}` |
| 增量 | `delta = delta_snapshots(after, baseline)` | `after − baseline`（clamp≥0） |

#### 预期数据（模型 usage 汇总）

| 字段 | 来源 | 值 |
|---|---|---|
| `our.total_input` | `sum_request_tokens(usages)` Σ prompt | 74900 |
| `our.cached_input` | Σ `cached_of(u)`（billed） | 67136 |
| `our.uncached_input` | total − cached | 7764 |
| `our.completion` | Σ completion | 1280 |
| `our_total_cost` | `expected_amount(7764, 67136, 1280, 8, 2, 28)` | — |
| `delta.amount` 预期 | `expected_amount(delta_uncached_real, delta_cached_real, delta_completion, 8, 2, 28)` | **用接口自己的 delta cached（real 口径）** |

#### 断言逻辑

| # | 断言 | 比较 | 容差 |
|---|---|---|---|
| A1 | cached ⊆ prompt | `our.cached ≤ our.total_input` | 精确 |
| A2 | uncached+cached==prompt | `our.uncached+our.cached == our.total_input` | 精确 |
| A3 | total==prompt+completion | `our.total_tokens == total_input+completion` | 精确 |
| **B1（强）** | delta prompt == 本次输入 | `delta(uncached+cached) == our.total_input` | max(1000, 2%) |
| **B2（强）** | delta completion == 本次输出 | `delta.completion == our.completion` | max(1000, 2%) |
| C1（参考） | delta cached ≈ 本次 cached | `delta.cached ≈ our.cached` | max(2000, 15%)，优化开启时软断言 |
| C2（参考） | delta uncached ≈ 本次 uncached | 同上 | 同上 |
| **D（强）** | **接口自洽** | `delta.amount == computeLogCost(delta_uncached_real, delta_cached_real, delta_completion, 8, 2, 28)` | 0.01 |
| E | glm-5.2 在模型清单 | `models` 含 `model_name=="glm-5.2"` | — |
| F | 每模型收入公式 | `m.amount == computeLogCost(m.tokens, m.prices)` | 0.01 |
| G | glm-5.2 单价 | `input==8, cached==2, output==28` | 精确 |
| H | 聚合 amount == Σ 模型 | `agg.amount == Σ m.amount`（或 `Σ computeLogCost` 全 priced 时） | 0.01 |
| I | 仅 glm-5.2 时聚合公式 | `agg.amount == computeLogCost(agg_tokens, 8, 2, 28)` | 0.01 |

**口径关键**：daily-cost `amount` 用 **real cached**（`controller/log.go:582`），`our.cached` 是 **billed**。test01 无优化时 billed==real，所以 C 组也精确相等。

### 2. `test_tpm_capacity` — TPM 容量

#### 实际数据（接口）

| 步骤 | 方法 | 说明 |
|---|---|---|
| 查询 | `GET /api/admin/usage/tpm-capacity?user_id=10&start=..&end=..` | 窗口=`page_start_24h`..`page_end_24h`（与 daily-cost 共享的近24小时） |
| 轮询 | 至 `matched` 桶 `user_tpm>0` | 复用同一对 start/end |
| 桶宽 | `tpm_bucket_size(q_end−q_start)` | 24h 窗口 → 300s（`tpm_capacity.go:79-88`） |
| 匹配 | `_overlapping_buckets(series, win_start, win_end, bucket_sec)` | 桶 `[t, t+bucket_sec)` 与**会话窗口** `[start_ts, end_ts]` 相交（隔离本次运行流量） |
| 汇总 | `reported_tokens = Σ bucket_min × user_tpm` | `bucket_min = bucket_sec/60` |

#### 预期数据

| 字段 | 来源 | 值 |
|---|---|---|
| `actual_tokens` | `Σ _usage_tokens(u)` = Σ (prompt+completion) | 76180 |

nexus `helper.go:237`：`totalTokens = promptTokens + completionTokens`，`RecordTPMUsage` 记录此值；`tpm_capacity.go:244` SQL `sum(prompt)+sum(completion)`。

#### 断言逻辑

| # | 断言 | 比较 | 容差 |
|---|---|---|---|
| A | 存在相交桶 | `matched` 非空 | — |
| B | 流量已记录 | `reported_tokens > 0` | — |
| **C** | **TPM 一致性** | `|actual − reported| ≤ tol` | max(25%, 2000) |
| D | user_tpm ≤ tpm | 每桶 `user_tpm ≤ tpm` | +1e-3 |

### 3. `test_cache_optimize_overview` — 缓存命中率优化

#### 实际数据（接口）

| 步骤 | 方法 | 说明 |
|---|---|---|
| 轮询 | `_poll_overview` → `GET /api/admin/usage/cache-optimize/overview` | 至 `month_prompt delta ≥ 95% × our.total_input` |
| 定位 | `find_overview_user(raw, 10)` | `data[]` 中 `user_id==10` |
| 提取 | `after = extract_overview_snapshot(row)` | 见下表 |
| 增量 | `delta = delta_overview_snapshots(after, baseline)` | `after − baseline`（9 个累计字段） |

`after` 字段：`has_target, target_rate, delta, jitter, budget_tokens, month_prompt, month_real, month_billed, real_rate, billed_rate, hour_prompt, hour_real, hour_billed, hour_real_rate, hour_billed_rate, ctr_prompt, ctr_billed, ctr_granted`

#### 本月让利（ctr_granted）计算规则

源码 `model/cache_optimize.go:227-245`：

```go
func RecordCacheOptUsage(userId int, prompt, real, billed int) {
    granted := billed - real       // line 232
    if granted < 0 { granted = 0 } // line 233-235  只统计上调部分
    // Redis HINCRBY cache_opt:{userId}:{YYYYMM} 'g' granted  // 累加, 不衰减
}
```

- **`ctr_granted` = Σ `max(billed - real, 0)` per request**，月度累计，只统计上调（让利）部分，下调不计
- Redis hash `cache_opt:{userId}:{YYYYMM}` 的 `g` 字段，`c/g 是纯统计量，不参与控制，不衰减`（line 240）
- 前端确认（`AdminCacheDiscount.jsx:144`）：`const granted = Math.max(0, billedSum - real)`

test01 无优化 → billed==real → `granted=0` 每请求 → `ctr_granted` 恒为 0。

#### 预期数据

| 字段 | 来源 | 值 |
|---|---|---|
| `our.total_input` | Σ prompt | 74900 |
| `our.cached_input` | Σ cached_of（billed==real） | 67136 |
| `our.completion` | Σ completion | 1280 |
| `baseline` | 请求前捕获 | `{month_prompt=37386, month_real=33408, ...}` |

#### 断言逻辑（6 组 A-F）

| 组 | # | 断言 | 比较 | 容差 |
|---|---|---|---|---|
| **A 无优化口径** | A1 | `has_target == false` | test01 无活跃目标 | 精确 |
| | A2 | `month_real == month_billed` | billed==real | 精确 |
| | A3 | `real_rate == billed_rate` | 同上 | 1e-9 |
| | A4 | `hour_real == hour_billed`（当 hour_prompt>0） | 同上 | 精确 |
| | A5 | `hour_real_rate == hour_billed_rate` | 同上 | 1e-9 |
| | A6 | **`ctr_granted == 0`** | `Σ max(billed−real,0)=0` | 精确 |
| **B 公式自洽** | B1 | `real_rate == month_real/month_prompt` | `buildCacheOptRow:104` | 1e-6 |
| | B2 | `billed_rate == month_billed/month_prompt` | 同上 | 1e-6 |
| | B3 | `hour_real_rate == hour_real/hour_prompt` | 同上 | 1e-6 |
| | B4 | `hour_billed_rate == hour_billed/hour_prompt` | 同上 | 1e-6 |
| **C 月度增量** | C1 | `delta month_prompt == our.total_input` | 74900 | max(1000, 2%) |
| | C2 | `delta month_real == our.cached_input` | 67136 | max(2000, 5%) |
| | C3 | `delta month_billed == our.cached_input` | 67136 | max(2000, 5%) |
| | C4 | `delta month_real == delta month_billed` | 无优化 | 精确 |
| **D 增量命中率** | D1 | `delta(month_real/month_prompt) == our(cached/prompt)` | 0.896 | 0.02 |
| **E 小时增量**（条件：同一北京自然小时） | E1 | `delta hour_prompt == our.total_input` | — | max(1000, 2%) |
| | E2 | `delta hour_real == our.cached_input` | — | max(2000, 5%) |
| | E3 | `delta hour_billed == our.cached_input` | — | max(2000, 5%) |
| | E4 | `delta hour_real == delta hour_billed` | — | 精确 |
| **F 让利增量** | F1 | **`delta ctr_granted == 0`** | 每请求 `max(billed−real,0)=0` | 精确 |

### 4. `test_log_list_entries` — 日志查询（逐条）

#### 实际数据（接口）

| 步骤 | 方法 | 说明 |
|---|---|---|
| 分页查询 | `GET /api/log/?p=0&type=0&model_name=glm-5.2&username=test01&start_ts&end_ts` | `start_ts=query_start_8d`、`end_ts=query_end_8d`（与 `/api/log/stat` 共享的近8天窗口）；`ItemsPerPage=10`，`ORDER BY id desc` |
| 翻页 | 至短页（`len(data)<10`）或 `max_pages=50`，或最旧行 `created_at < 会话窗口下界` 时提前终止 | 避免翻遍8天历史 |
| 过滤 | `filter_run_logs(collected, 10, "glm-5.2", s_start, s_end, pad=60)` | 按**会话窗口** `[start_ts, end_ts]`（±60s）隔离本次运行，type=2, user_id=10, model_name |

每行字段：`{id, created_at, model_name, username, prompt_tokens, completion_tokens, cached_tokens, cost, ...}`

#### 预期数据

| 字段 | 来源 | 值 |
|---|---|---|
| 行数 | `len(usages)` | 20 |
| 每行 (prompt, completion, cached) | `sig(u) = (prompt, completion, cached_of(u))` | 20 条三元组 |
| 每行 cost | `log_entry_cost(r, 8, 2, 28)` = `computeLogCost(prompt, completion, real_cached)` | — |
| 总 cost | `Σ log_entry_cost` 和 `Σ expected_amount(usage)` | — |

#### 断言逻辑

| # | 断言 | 比较 | 容差 |
|---|---|---|---|
| A | 日志非空 | `rows` 非空 | — |
| B | 行数 == 请求数 | `len(rows) == 20` | 精确 |
| C | 逐行：model/username/total/cost/created_at | model=="glm-5.2", user=="test01", `created_at∈[win−60,win+60]`, `cost==computeLogCost(real_cached)` | cost: 1e-6 |
| **D** | **多集合** `(prompt, completion, cached)` 一致 | `Counter(sig(u)) == Counter((row.prompt, row.completion, row.cached))` | 精确（test01 billed==real） |
| E1 | Σ row.cost == Σ computeLogCost | 接口自洽 | 1e-6 |
| E2 | Σ row.cost == our_expected_total_cost | billed==real | max(1e-6, 1%) |

### 5. `test_log_stat_delta` — 日志统计（增量）

#### 实际数据（接口）

| 步骤 | 方法 | 说明 |
|---|---|---|
| 轮询 | `_poll_log_stat` → `GET /api/log/stat?type=0&username=test01&start_ts&end_ts` | 至 `delta request_count ≥ 20`；`start=query_start_8d`（冻结）、`end=query_end_8d`（与 `/api/log/` 共享） |
| 提取 | `after = extract_log_stat(raw)` | `{request_count, prompt_tokens, completion_tokens, quota, avg_elapsed_ms, avg_ttft_ms, cost_amount}` |
| 增量 | `delta = delta_log_stat(after, baseline)` | avg 重建：`(after.avg×after.count − before.avg×before.count) / delta.count` |

窗口为页面默认近8天减1秒（`LOG_WINDOW_SPAN=691199`）：基线 `[now0−691199, now0]`，after 复用**同一冻结 start**、推进 `end` 至 `query_end_8d`。既有流量在 `[start, now0]` 段两快照都在、抵消，增量 = `(now0, query_end_8d]` 的本次运行。

#### 预期数据

| 字段 | 来源 | 值 |
|---|---|---|
| `our.request_count` | `len(usages)` | 20 |
| `our.total_input` | Σ prompt | 74900 |
| `our.completion` | Σ completion | 1280 |
| `total_tokens` | prompt+completion | 76180 |
| `our_total_cost` | `Σ expected_amount(prompt−cached, cached, completion, 8, 2, 28)` | billed==real |
| avg 交叉校验 | fetch log rows → `mean(elapsed_time)` | — |

#### 断言逻辑

| # | 断言 | 比较 | 容差 |
|---|---|---|---|
| A | `delta request_count == 20` | COUNT 精确 | 0 |
| B | `delta prompt_tokens == 74900` | Σ prompt | max(100, 1%) |
| C | `delta completion_tokens == 1280` | Σ completion | max(100, 1%) |
| D | `delta (prompt+completion) == 76180` | total | max(100, 1%) |
| E | `delta cost_amount == our_total_cost` | sumModelCosts (billed==real) | max(1e-6, 2%) |
| F | `delta quota == 0`（诊断） | 订阅模式 quota=0 | — |
| G | `delta avg_elapsed_ms ≈ log_rows mean(elapsed_time)` | 重建 vs 日志均值 | max(500, 25%) |
| G2 | `delta avg_elapsed_ms > 0` | 服务端有耗时 | — |

### 6. `test_stress_metrics` — 压测指标（输入/输出 token 分布）

#### 实际数据（接口）

| 步骤 | 方法 | 说明 |
|---|---|---|
| 轮询 | `_poll_stress_metrics` → `GET /api/admin/usage/stress-metrics?start_ts&end_ts&model_name=glm-5.2` | 至 `delta n ≥ 20`；每次轮询实时 `config.last_24h_window()`（start=now−86400, end=now） |
| 提取 | `after = extract_stress_snapshot(raw)` | `{n, intok_total, outtok_total, intok_max, outtok_max, in_hist_counts[9], out_hist_counts[9], peak_conc, lat_avg, lat_max}` |
| 增量 | `delta_n = after.n − baseline.n`<br>`delta_in_hist = delta_hist_counts(after.in_hist_counts, baseline.in_hist_counts)`<br>`delta_out_hist = 同理` | 近24小时窗口，窗口交集中的既有 glm-5.2 流量抵消 |

窗口为页面默认近24小时实时窗口（`start = end − 86400`，`end = int(time.time())`），`model_name=glm-5.2` 过滤（无 user_id 参数）。基线与轮询各自取实时 `end`，两次窗口的交集中既有流量抵消。

#### 直方图桶边界

源码 `model/stress_metrics.go:76-80, 283-310`（已通过 Go 测试用例 `stress_metrics_test.go:6-27` 验证）：

**输入 9 桶**（`inputBucket`，上界闭区间）：`0` / `1-100` / `100-1k` / `1k-5k` / `5k-10k` / `10k-20k` / `20k-50k` / `50k-100k` / `>100k`

**输出 9 桶**（`outputBucket`，`v≥10000` → 末桶）：`0` / `1-32` / `32-100` / `100-500` / `500-1k` / `1k-2k` / `2k-5k` / `5k-9999` / `10000(满)`

#### 预期数据

| 字段 | 来源 | 计算 | 值 |
|---|---|---|---|
| `our_prompts` | `[u.prompt_tokens for u in usages]` | 20 个 3745 | [3745]×20 |
| `our_completions` | `[u.completion_tokens for u in usages]` | 20 个 64 | [64]×20 |
| `expected_in` | `expected_hist_counts(our_prompts, input_bucket, 9)` | `input_bucket(3745)`: 3745≤5000→桶3(1k-5k) | `[0,0,0,20,0,0,0,0,0]` |
| `expected_out` | `expected_hist_counts(our_completions, output_bucket, 9)` | `output_bucket(64)`: 64≤100→桶2(32-100) | `[0,0,20,0,0,0,0,0,0]` |
| `our_max_prompt` | `max(our_prompts)` | — | 3745 |
| `our_max_completion` | `max(our_completions)` | — | 64 |
| `our.total_input` | Σ prompt | — | 74900 |
| `our.completion` | Σ completion | — | 1280 |

#### 断言逻辑

该接口**无 user_id 过滤**（仅 `model_name`/`token_name`，`stress_metrics.go:94`），因此并发 glm-5.2 流量会混入增量。测试自动检测环境并切换断言策略：

- **干净环境**（`delta_n == 本次请求数`，`surplus == 0`）：A-E 精确匹配
- **并发环境**（`surplus > 0`）：A-E 改为下界/子集校验（我们的请求是增量的子集，`delta >= 本次值`）
- **F/G/H 始终精确**（自洽性和 max 聚合在任何环境下都成立）

| # | 干净环境（精确） | 并发环境（下界/子集） | 比较 |
|---|---|---|---|
| A | `delta n == 20` | `delta n >= 20` | COUNT |
| B | `delta intok_total == 74900` | `delta intok_total >= 74900` | Σ prompt |
| C | `delta outtok_total == 1280` | `delta outtok_total >= 1280` | Σ completion |
| **D** | **`delta in_hist == [0,0,0,20,0,0,0,0,0]`** | **逐桶 `delta in_hist[i] >= expected[i]`** | 逐桶子集 |
| **E** | **`delta out_hist == [0,0,20,0,0,0,0,0,0]`** | **逐桶 `delta out_hist[i] >= expected[i]`** | 逐桶子集 |
| F1 | `Σ delta in_hist == delta n` | 同左 | 精确（始终） |
| F2 | `Σ delta out_hist == delta n` | 同左 | 精确（始终） |
| G | `after intok_max >= max(baseline, 3745)` | 同左 | max 聚合（始终） |
| H | `after outtok_max >= max(baseline, 64)` | 同左 | max 聚合（始终） |

> **为什么并发时用下界？** 固定窗口基线-增量法中，基线前已存在的行在两个快照中抵消。但基线与 after 之间**新落地**的并发 glm-5.2 请求（来自其他用户）会出现在增量中。我们的请求是增量的**子集**，故 `delta >= 本次值` 恒成立（token 非负）。精确匹配仅在无并发时可用。

## 总览表

| 测试 | 接口 | 实际数据 | 预期数据 | 核心断言 |
|---|---|---|---|---|
| 1 daily-cost | `/api/log/daily-cost` | today 行的 `uncached/cached/completion/amount` | `sum_request_tokens(usages)` 74900/67136/7764/1280 | delta==本次 + amount 公式自洽(real cached) |
| 2 tpm-capacity | `/api/admin/usage/tpm-capacity` | `Σ bucket_min×user_tpm` | `Σ (prompt+completion)=76180` | actual≈reported (25%) |
| 3 cache-opt overview | `/api/admin/usage/cache-optimize/overview` | `month_*/hour_*/ctr_granted` | `our.total_input/cached_input` + baseline | billed==real, ctr_granted==0, delta==本次 |
| 4 log list | `/api/log/` | 逐行 `{prompt,completion,cached,cost}` | `sig(u)=(prompt,completion,cached_of(u))` | 多集合一致 + 逐行 cost |
| 5 log stat | `/api/log/stat` | `delta(request_count,prompt,completion,cost_amount)` | `our(20/74900/1280/total_cost)` | delta==本次 + avg 交叉校验 |
| 6 stress-metrics | `/api/admin/usage/stress-metrics` | `delta n/intok/outtok + in_hist/out_hist` | `expected_hist_counts(prompts/completions)` | 干净=**分布精确匹配**; 并发=**下界/子集校验** + F/G/H 始终精确 |

## 数据流图

```
       test01 (api-key)
             │ POST /v1/chat/completions  (2轮×10次, 相同长前缀)
             ▼
  ┌─────────────────────┐
  │  glm-5.2 模型服务    │  → 每轮第1次填 cache，第2~10次命中
  └─────────────────────┘
             │ usage(prompt/completion/cached tokens) + 时间戳
             ▼
     conftest.model_requests  ──────────────────────────────┐
              │                                              │
              │ 等待 PROPAGATION_WAIT=120s 数据落地             │
              │  → 捕获共享窗口:                                 │
              │    page_start_24h / page_end_24h  (daily-cost + tpm)
              │    query_end_8d (与基线冻结的 query_start_8d 配对, log list + log stat)
              ▼                                              ▼
   GET /api/user/login (root)        6 个断言测试读取 (含上述共享窗口)
              │ Set-Cookie: session=..           │
              ▼                                  │
        admin_client (复用 Cookie)               │
              │                                  │
   ┌──────────┼──────────────────────────┐       │
   │          │                          │       │
   ▼          ▼                          ▼       ▼
  daily-cost  tpm-capacity          log list   stress-metrics
  (共享24h)   (共享24h)            (共享8d)   (实时24h)
  cache-optimize overview          log stat
                                  (共享8d)
```

## Allure 测试报告

每次执行 `python run.py` 会自动：

1. **创建本次运行目录** `reports/<YYYYMMDD-HHMMSS>/`（默认时间戳，可用 `ALAYA_RUN_DIR` 自定义），多次运行互不覆盖；
2. **记录原始结果**到 `reports/<run>/allure-results/`（由 `run.py` 动态传 `--alluredir`）；
3. **生成 HTML 报告**到 `reports/<run>/allure-report/index.html`（由 `conftest.py` 的 session 级 fixture `generate_allure_report` 在全部用例结束后调用 `allure generate`，通过 `ALAYA_RUN_DIR` 与 `run.py` 解析到同一目录）。

报告里记录的内容：

| 类别 | Allure 体现 | 来源 |
|---|---|---|
| **环境信息** | 附件「环境信息」 | `report/allure_utils.py:env_info` |
| **每次模型请求** | 步骤「模型请求 #N」 + 附件（usage、请求体、响应、完成时间戳） | `conftest.py:record_model_call` |
| **请求会话汇总** | 附件「请求会话汇总」（prompt/cached/completion token 合计、时间窗口） | `conftest.py` |
| **4 个基线快照** | 步骤「请求前: 查询 xxx 基线快照」+ 附件 | `conftest.py` |
| **后台登录** | 步骤「管理后台登录」+ 请求/响应附件 | `conftest.py` + `admin_client.py` |
| **每次后台接口调用** | 步骤「管理后台请求」+ 附件（method/path/params/status/响应体） | `admin_client.py:record_admin_call` |
| **断言详情** | 步骤「断言: <名称>」+ 附件（**expected / actual / passed / tolerance / detail**）；**失败时步骤标记为 broken** | `test_*.py:_check` → `record_assertion(raise_on_fail=True)` |

### 断言的预期 vs 实际

每个断言通过 `report/allure_utils.py:record_assertion` 记录，报告中可清晰看到：

- **name**：断言名称（如「delta cached_input_tokens == 本次实际缓存命中」、「delta in_hist.counts == 预期输入分布」）
- **expected**：预期值（本次实际消耗 token、公式计算金额、配置单价 8/2/28、预期直方图分布…）
- **actual**：实际值（接口 delta token、接口 amount、user_tpm 折算 token、响应单价、接口直方图分布…）
- **passed**：是否通过
- **tolerance**：容差（如 0.01 元、max(token×5%, 1000) token）
- **detail**：失败时的描述

> **失败步骤可见性**：`_check` 调用 `record_assertion(raise_on_fail=True)`，当断言失败时在 `@allure.step` 上下文**内部**抛出 `AssertionError`，allure-pytest 捕获异常并将该步骤标记为 broken/failed（而非 passed），失败原因在报告中清晰可见。软诊断（`passed=True` 或 `raise_on_fail=False`）仅记录不抛出，步骤显示为 passed 但附件中 `passed: false` 仍可见。

### 查看 HTML 报告

```bash
# 方式一：直接打开静态文件（替换 <run> 为实际时间戳目录）
reports/<run>/allure-report/index.html

# 方式二：启动本地服务器预览（实时刷新）
allure serve reports/<run>/allure-results
```

报告支持按 Feature/Story 分组、按 severity 筛选，断言步骤树形展开，附件可点开查看完整 JSON。

## 常见问题

- **TPM 测试报 `reported_tokens=0`**：后台数据未落地或查询窗口未覆盖请求时刻。调大 `ALAYA_PROPAGATION_WAIT` 与 `ALAYA_POLL_INTERVAL`。TPM 查询窗口现为近24小时实时（与 daily-cost 共享），覆盖请求时刻；若仍为 0 多为落地延迟。
- **daily-cost 增量对不上**：① baseline 与 after 之间 test01 有其它流量混入；② 数据落地延迟超过等待时间（调大 `ALAYA_PROPAGATION_WAIT`/轮询参数）。增量法已隔离本次贡献，正常应精确匹配容差内。
- **daily-cost 找不到当天记录**：多为落地延迟；轮询机制会自动重试，仍失败则调大轮询参数。注意 baseline 在请求前查询，若当天尚无记录则基线为 0（合法，delta 退化为 after）。
- **cache-opt overview 小时增量跳过**：baseline 与 after 跨了北京自然小时边界时 `hour_*` 窗口翻转，测试自动跳过小时增量校验（属预期行为，非失败）。
- **stress-metrics 返回 success=false**：窗口内 consume 行数超过 50 万行护栏（`stressMaxRows`）。缩小窗口或加 `model_name` 过滤。
- **stress-metrics 增量对不上（surplus > 0）**：该接口无 user_id 过滤，并发 glm-5.2 流量会混入增量。测试自动检测：干净环境精确匹配，有并发时自动切换为下界/子集校验（`delta >= 本次值`），F/G/H 始终精确。日志中会打印 `concurrent traffic detected: surplus=N`。
- **模型请求 4xx/5xx**：检查 `MODEL_BASE_URL` 与 `MODEL_API_KEY` 是否正确，网络是否可达。
- **后台接口 401**：Cookie 失效或登录失败，检查 `ALAYA_ADMIN_*` 配置及后台可达性。
- **缓存未命中**：确认同次运行内多次请求的 `system` 内容完全一致、`temperature=0`；注意每次运行前缀带唯一 `run-tag`，因此跨运行的缓存不会复用（这是设计使然：每次运行都独立验证"填缓存+命中"流程）。
- **多次测试会互相影响吗**：不同运行因 `run-tag` 不同，使用不同缓存 key，互不干扰，每次都从冷缓存独立验证。同一运行内：第 1 轮第 1 次填缓存，其余命中；第 2 轮是否命中取决于缓存 TTL 是否 ≥ `INTER_ROUND_WAIT`（默认 120 秒）。
- **报告未自动生成**：未安装 `allure` CLI（`npm i -g allure-commandline`，需 Java）。此时原始 JSON 仍会生成到 `reports/<run>/allure-results/`，可手动执行 `allure generate reports/<run>/allure-results -o reports/<run>/allure-report --clean`。设置 `ALAYA_SKIP_ALLURE_GEN=1` 可彻底跳过生成步骤。

## 注意事项

- 测试会产生真实计费，`ALAYA_MAX_TOKENS` 默认设为 64 以控制输出成本，输入侧因需要长上下文仍会消耗较多 token。
- 登录 Cookie 在整个 session 内复用（session 级 fixture），不会重复登录。
- `config.py` 中的明文凭据仅为测试环境默认值，生产环境请用环境变量覆盖，勿提交真实密钥到版本库。
- 所有断言以 nexus `releases/xb01` 分支源码为口径基准，行号引用于各测试 docstring 与注释中。
