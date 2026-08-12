"""Configuration for the GLM-5.2 cache & billing test suite.

Every value can be overridden through an environment variable (prefix ALAYA_),
e.g. ``ALAYA_TEST_USER_ID=10`` or ``ALAYA_REQUEST_COUNT=10``.
"""

import os
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
# Each run executes ROUND_COUNT rounds of REQUEST_COUNT requests; the first
# request of a round fills the prompt cache, the rest hit it. Rounds are
# separated by INTER_ROUND_WAIT (tests cache persistence across the gap).
REQUEST_COUNT = _env_int("ALAYA_REQUEST_COUNT", 10)  # requests per round
ROUND_COUNT = _env_int("ALAYA_ROUND_COUNT", 2)  # rounds per run
INTER_ROUND_WAIT = _env_int(
    "ALAYA_INTER_ROUND_WAIT", 120
)  # wait between rounds (seconds); 0 only after the last round
PREFIX_TOKENS = _env_int("ALAYA_PREFIX_TOKENS", 3000)  # long-context prefix length
MAX_TOKENS = _env_int("ALAYA_MAX_TOKENS", 64)  # short completions (cheap)
REQUEST_PACING = _env_float("ALAYA_REQUEST_PACING", 1.0)  # seconds between model calls
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


def today_str():
    """Today's date as YYYYMMDD (matches the daily-cost ``date`` field)."""
    return datetime.now().strftime("%Y%m%d")
