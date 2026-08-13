"""Configuration for the GLM-5.2 cache & billing test suite.

Every value can be overridden through an environment variable (prefix ALAYA_),
e.g. ``ALAYA_TEST_USER_ID=10`` or ``ALAYA_REQUEST_COUNT=10``.
"""

import os
import time
from datetime import datetime, timedelta


def _env(name, default=""):
    return os.environ.get(name, default)


def _env_int(name, default):
    v = os.environ.get(name)
    return int(v) if v else default


def _env_float(name, default):
    v = os.environ.get(name)
    return float(v) if v else default


# ---------- Model API (as user test01) ----------
MODEL_BASE_URL = _env("ALAYA_MODEL_BASE_URL", "https://token-bj07.alayanew.com:26443")
MODEL_API_KEY = _env(
    "ALAYA_MODEL_API_KEY", "sk-08c1ebd3f7a4a1ecce91fe42fadf4ae7aba43131ac09ae39"
)
MODEL_USER = _env("ALAYA_MODEL_USER", "test01")
MODEL_NAME = _env("ALAYA_MODEL_NAME", "glm-5.2")
MODEL_CHAT_PATH = _env("ALAYA_MODEL_CHAT_PATH", "/v1/chat/completions")

# ---------- Admin API ----------
ADMIN_BASE_URL = _env("ALAYA_ADMIN_BASE_URL", "http://10.220.75.84:8080")
ADMIN_USERNAME = _env("ALAYA_ADMIN_USERNAME", "root")
ADMIN_PASSWORD = _env("ALAYA_ADMIN_PASSWORD", "tEscuYb3aDp_OWwhU4")

# test01's user_id in the admin/operation system
TEST_USER_ID = _env_int("ALAYA_TEST_USER_ID", 10)

# ---------- Pricing (CNY per million tokens) ----------
# glm-5.2 system settings: input=8.0, cached=2.0, output=28.0
PRICE_INPUT = _env_float("ALAYA_PRICE_INPUT", 8.0)  # uncached input
PRICE_CACHED = _env_float("ALAYA_PRICE_CACHED", 2.0)  # cached input
PRICE_OUTPUT = _env_float("ALAYA_PRICE_OUTPUT", 28.0)  # completion

# ---------- Test behavior ----------
# Each run executes ROUND_COUNT rounds of REQUEST_COUNT long-context requests
# (rounds 1-2) plus a ROUND3_COUNT round of short independent requests (round 3).
# Rounds 1-2: a FIXED system prefix (PREFIX_TOKENS, cached → cache hits after
# request #1) plus a variable tail (after the prefix, always uncached) so total
# prompt_tokens falls in one of INPUT_BUCKET_TARGETS, spreading requests across
# stress-metrics in_hist buckets. Rounds are separated by INTER_ROUND_WAIT;
# round 2 → 3 by ROUND2_TO_ROUND3_WAIT.
REQUEST_COUNT = _env_int("ALAYA_REQUEST_COUNT", 10)  # long-context requests per round
ROUND_COUNT = _env_int("ALAYA_ROUND_COUNT", 2)  # long-context rounds per run
INTER_ROUND_WAIT = _env_int(
    "ALAYA_INTER_ROUND_WAIT", 120
)  # wait between long-context rounds (seconds)
MAX_TOKENS = _env_int("ALAYA_MAX_TOKENS", 64)  # short completions (rounds 1-2)
REQUEST_PACING = _env_float("ALAYA_REQUEST_PACING", 1.0)  # seconds between model calls

# Fixed system prefix (rounds 1-2): byte-identical across all requests with the
# same run-tag → request #1 fills the prompt cache, the rest hit it (cache-hit
# tests rely on this). The tail (in the user message, after the prefix) varies
# per request to cover multiple input buckets.
PREFIX_TOKENS = _env_int("ALAYA_PREFIX_TOKENS", 3000)  # long-context prefix length

# Input bucket targets for rounds 1-2: (label, lo, hi) total prompt range.
# Each request is randomly assigned a bucket; the tail length is chosen so
# total prompt_tokens ≈ [lo, hi] (tail = target - PREFIX_TOKENS). Boundaries
# avoid the exact inputBucket edges (1000/5000/10000/20000/50000/100000) so
# tokenizer jitter doesn't spill into an adjacent bucket. Maps to stress-metrics
# in_hist buckets 3-7 (1k-5k .. 50k-100k).
INPUT_BUCKET_TARGETS = [
    ("1k-5k", 2000, 4500),
    ("5k-10k", 5500, 9500),
    ("10k-20k", 11000, 19000),
    ("20k-50k", 22000, 48000),
    ("50k-100k", 55000, 95000),
]

# Round 3: short independent requests (no shared prefix). Each request uses a
# random-length user-only message (no system prefix) with random max_tokens, so
# both input (1-1k) and output (1-1k) vary; rounds-1-2's long-context cache
# mechanism does not apply here.
ROUND3_COUNT = _env_int("ALAYA_ROUND3_COUNT", 10)
ROUND3_MIN_INPUT_TOKENS = _env_int("ALAYA_ROUND3_MIN_INPUT_TOKENS", 1)
ROUND3_MAX_INPUT_TOKENS = _env_int("ALAYA_ROUND3_MAX_INPUT_TOKENS", 900)
ROUND3_MIN_OUTPUT_TOKENS = _env_int("ALAYA_ROUND3_MIN_OUTPUT_TOKENS", 1)
ROUND3_MAX_OUTPUT_TOKENS = _env_int("ALAYA_ROUND3_MAX_OUTPUT_TOKENS", 1000)
ROUND2_TO_ROUND3_WAIT = _env_int("ALAYA_ROUND2_TO_ROUND3_WAIT", 120)
# A unique tag embedded in the prefix so each run uses different content (each
# run independently fills then hits cache) and is distinguishable in the data.
# Empty -> auto-generate from the start time.
RUN_TAG = _env("ALAYA_RUN_TAG", "")
PROPAGATION_WAIT = _env_int(
    "ALAYA_PROPAGATION_WAIT", 120
)  # wait after all rounds before polling admin
POLL_RETRIES = _env_int("ALAYA_POLL_RETRIES", 6)
POLL_INTERVAL = _env_int("ALAYA_POLL_INTERVAL", 15)


def generate_run_tag():
    """Auto-generate a short, per-run unique tag if RUN_TAG is not set."""
    import uuid

    from datetime import datetime

    if RUN_TAG:
        return RUN_TAG
    return "run-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


# ---------- Time window (defaults to covering today) ----------
def _default_window():
    now = datetime.now()
    start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = now + timedelta(hours=1)
    return int(start.timestamp()), int(end.timestamp())


_DEFAULT_START, _DEFAULT_END = _default_window()

TPM_START_TIMESTAMP = _env_int("ALAYA_TPM_START", _DEFAULT_START)
TPM_END_TIMESTAMP = _env_int("ALAYA_TPM_END", _DEFAULT_END)
DAILY_COST_START_TIMESTAMP = _env_int("ALAYA_DAILY_START", _DEFAULT_START)
DAILY_COST_END_TIMESTAMP = _env_int("ALAYA_DAILY_END", _DEFAULT_END)

# Page-default window span for /api/log/ and /api/log/stat (近8天 minus 1s).
# 8 * 24 * 3600 - 1 = 691199. Both interfaces share a single frozen ``start``
# (= now0 - LOG_WINDOW_SPAN, captured once at the baseline) and a single frozen
# ``end`` (= now1, captured once post-propagation); the frozen start lets the
# baseline-delta in test_log_stat_delta cancel all pre-existing rows exactly.
LOG_WINDOW_SPAN = 691199


def last_24h_window():
    """Return (start, end) unix timestamps covering the last 24h ending at now.

    Matches the operation-platform page default (近24小时). ``end`` is the
    real-time timestamp at the moment of the call, so each API request gets a
    fresh window: start = now - 86400, end = now.
    """
    end = int(time.time())
    start = end - 86400
    return start, end


def today_str():
    """Today's date as YYYYMMDD (matches the daily-cost ``date`` field)."""
    return datetime.now().strftime("%Y%m%d")
