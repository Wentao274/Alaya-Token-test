"""Admin / operation API client.

Handles login (obtains the session cookie) and the two data queries used by the
tests: TPM-capacity and daily-cost. The login cookie is reused for every
subsequent request, as noted in the requirement: "this login cookie can be used
globally".

Every call is recorded into the Allure report (request params + response) via
``report.allure_utils``.
"""

import httpx

from report.allure_utils import record_admin_call, record_request, record_response


class AdminClient:
    def __init__(self, base_url, username, password, timeout=30.0):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.session_cookie = None
        self.client = httpx.Client(timeout=timeout)

    def login(self):
        """POST /api/user/login, capture Set-Cookie, apply it to all later calls."""
        url = f"{self.base_url}/api/user/login"
        body = {"username": self.username, "password": "***masked***"}
        record_request("POST", url, body=body)
        resp = self.client.post(
            url, json={"username": self.username, "password": self.password}
        )
        resp.raise_for_status()
        set_cookie = resp.headers.get("set-cookie", "")
        if not set_cookie:
            raise RuntimeError(
                "Login response had no Set-Cookie header; cannot authenticate admin calls"
            )
        # e.g. "session=MTc4...; Path=/; HttpOnly" -> keep the "session=..." part
        self.session_cookie = set_cookie.split(";", 1)[0].strip()
        self.client.headers["Cookie"] = self.session_cookie
        record_response(
            resp.status_code, resp.headers, {"cookie": self.session_cookie[:24] + "..."}
        )
        return self.session_cookie

    def _get(self, path, params):
        url = f"{self.base_url}{path}"
        record_request("GET", url, params=params, headers=dict(self.client.headers))
        resp = self.client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        record_response(resp.status_code, resp.headers, data)
        record_admin_call("GET", path, resp.status_code, params=params, body=data)
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(f"Admin API {path} returned success=false: {data}")
        return data

    def get_tpm_capacity(self, start_ts, end_ts, user_id):
        """GET /api/admin/usage/tpm-capacity.

        ``data.summary`` = totals across all users; ``data.series`` contains the
        per-user (``user_tpm``) data, test01's record is matched by ``user_id``.
        """
        return self._get(
            "/api/admin/usage/tpm-capacity",
            {"start_timestamp": start_ts, "end_timestamp": end_ts, "user_id": user_id},
        )

    def get_daily_cost(self, start_ts, end_ts, user_id):
        """GET /api/log/daily-cost. Returns a list of per-day/per-user rows."""
        return self._get(
            "/api/log/daily-cost",
            {"start_timestamp": start_ts, "end_timestamp": end_ts, "user_id": user_id},
        )

    def get_cache_optimize_overview(self):
        """GET /api/admin/usage/cache-optimize/overview.

        Per-user cache-hit-rate optimization dashboard data
        (``controller/cache_optimize.go:GetCacheOptimizeOverview``): month & hour
        aggregated prompt/real-cached/billed-cached tokens, derived rates, active
        target config, and Redis period counters (``ctr_*``, incl. ``ctr_granted``
        = month-to-date concession). test01 matched by ``user_id``.
        """
        return self._get("/api/admin/usage/cache-optimize/overview", {})

    def get_logs(
        self,
        p=0,
        type_=0,
        model_name="",
        token_name="",
        start_ts=0,
        end_ts=0,
        username="",
        channel=0,
    ):
        """GET /api/log/ — paginated operation log list (controller/log.go:GetAllLogs).

        ``p`` is the 0-based page (server applies ``p * ItemsPerPage`` offset;
        ``ItemsPerPage=10`` per ``common/config``). ``type=0`` (LogTypeUnknown)
        returns ALL log types; our model requests produce type=2 (consume).

        Server orders by ``id desc`` (newest first). ``data`` is the page slice,
        ``total`` is the full unpaginated count. Each row's ``cost`` is filled by
        ``fillLogCosts`` using the **real** ``cached_tokens`` (computeLogCost,
        ``controller/log.go:225-228``), NOT the billed value.
        """
        return self._get(
            "/api/log/",
            {
                "p": p,
                "type": type_,
                "model_name": model_name,
                "token_name": token_name,
                "start_timestamp": start_ts,
                "end_timestamp": end_ts,
                "username": username,
                "channel": channel,
            },
        )

    def get_stress_metrics(self, start_ts, end_ts, model_name="", token_name=""):
        """GET /api/admin/usage/stress-metrics — stress-test dashboard
        (controller/usage.go:GetStressMetrics, releases/xb01).

        Aggregates ``logs(type=consume, created_at BETWEEN start AND end)`` in
        Go memory (``model/stress_metrics.go:GetStressMetrics``): per-minute
        ``series`` (req/intok/outtok/lat/conc), top ``summary`` card
        (n/intok_total/outtok_total/avg/max/peak_conc/lat_*), and token-distribution
        histograms (``in_hist`` for prompt tokens, ``out_hist`` for completion
        tokens). Optional ``model_name``/``token_name`` narrow the SQL filter.
        Default window is last 24h when ``start_ts``/``end_ts`` are 0.

        A 500 000-row guard (``stressMaxRows``, stress_metrics.go:29) caps the
        query; exceeding it returns ``success=false``. Callers should keep the
        window narrow (or filter by model_name) in busy environments.
        """
        return self._get(
            "/api/admin/usage/stress-metrics",
            {
                "start_timestamp": start_ts,
                "end_timestamp": end_ts,
                "model_name": model_name,
                "token_name": token_name,
            },
        )

    def get_log_stat(
        self,
        type_=0,
        model_name="",
        token_name="",
        start_ts=0,
        end_ts=0,
        username="",
        channel=0,
    ):
        """GET /api/log/stat — aggregate stats over a window (controller/log.go:GetLogsStat).

        Always aggregates consume logs (type=2) regardless of the ``type`` param
        (handler discards it; ``logsConsumeFilterTx`` forces ``type=consume``).
        Returns ``request_count`` (COUNT), ``prompt_tokens``/``completion_tokens``/
        ``quota`` (SUM), ``avg_elapsed_ms``/``avg_ttft_ms`` (AVG over rows where the
        field > 0, truncated to int), and ``cost_amount`` (``sumModelCosts`` using
        the **billed** ``cached_tokens_billed``, fallback to real if 0).
        """
        return self._get(
            "/api/log/stat",
            {
                "type": type_,
                "model_name": model_name,
                "token_name": token_name,
                "start_timestamp": start_ts,
                "end_timestamp": end_ts,
                "username": username,
                "channel": channel,
            },
        )

    def close(self):
        self.client.close()
