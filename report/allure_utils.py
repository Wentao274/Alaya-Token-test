"""Allure reporting helpers.

Centralised, thin wrappers around allure-pytest so every test records:

* API calls           - ``record_request`` / ``record_response``  (attachments)
* Model calls         - ``record_model_call``                      (one step)
* Assertion outcomes  - ``record_assertion``                       (step + pass/fail status)

Each assertion is recorded with its **name**, **expected**, **actual** and a
pass/fail flag so the Allure report shows exactly what was checked and the
values involved - even when the assertion passes (regular ``assert`` only
surfaces data on failure).
"""

import json
import platform

import allure


def _to_jsonable(obj):
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return repr(obj)


def attach_json(body, name):
    """Attach an object as a pretty JSON attachment (string bodies kept as-is)."""
    if isinstance(body, (str, bytes)):
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
        allure.attach(text, name=name, attachment_type=allure.attachment_type.JSON)
    else:
        allure.attach(
            json.dumps(_to_jsonable(body), ensure_ascii=False, indent=2, default=str),
            name=name,
            attachment_type=allure.attachment_type.JSON,
        )


def attach_text(text, name):
    allure.attach(text, name=name, attachment_type=allure.attachment_type.TEXT)


def record_request(method, url, params=None, headers=None, body=None):
    """Attach the outbound HTTP request details."""
    rec = {
        "method": method,
        "url": url,
        "params": params,
        "headers": _safe_headers(headers),
        "body": _to_jsonable(body),
    }
    attach_json(rec, f"请求 {method} {url.split('?', 1)[0].split('/')[-1] or url}")


def record_response(status_code, resp_headers, body):
    """Attach the HTTP response details."""
    rec = {
        "status_code": status_code,
        "headers": dict(resp_headers) if resp_headers else {},
        "body": _to_jsonable(body),
    }
    attach_json(rec, "响应")


def _safe_headers(headers):
    if not headers:
        return {}
    safe = {}
    for k, v in dict(headers).items():
        kl = str(k).lower()
        if kl in ("authorization", "cookie", "set-cookie", "api-key"):
            safe[k] = "***masked***"
        else:
            safe[k] = v
    return safe


@allure.step("模型请求 #{0}: {1}")
def record_model_call(
    index, model, usage, done_ts, full_payload=None, raw_response=None
):
    """One Allure step per model request, with usage + (optionally) raw response."""
    if usage:
        detail = {k: v for k, v in usage.items()}
    else:
        detail = "<no usage>"
    attach_json(
        {
            "index": index,
            "model": model,
            "completed_at_unix": done_ts,
            "completed_at_local": _local_ts(done_ts),
            "usage": detail,
            "request": full_payload,
            "response": raw_response,
        },
        f"模型请求#{index} 详情",
    )


@allure.step("断言: {name}")
def record_assertion(
    name, expected, actual, passed, detail=None, tolerance=None, raise_on_fail=False
):
    """Record a single assertion with expected vs actual.

    Always attached (pass or fail). On failure the step is marked broken via an
    attached message so the report highlights it.

    When ``raise_on_fail`` is True and the assertion did not pass, an
    ``AssertionError`` is raised **inside** the ``@allure.step`` context so
    allure-pytest records the step as broken/failed (and the error is visible
    in the report). The exception propagates to pytest, failing the test.

    When ``raise_on_fail`` is False (default, used for soft diagnostics), the
    function merely records and returns ``passed`` without raising — the caller
    may continue the test regardless of the outcome.
    """
    rec = {
        "name": name,
        "expected": expected,
        "actual": actual,
        "passed": bool(passed),
    }
    if tolerance is not None:
        rec["tolerance"] = tolerance
    if detail:
        rec["detail"] = detail
    attach_json(rec, f"断言详情: {name}")
    if not passed:
        msg = detail or f"{name} 失败: expected={expected}, actual={actual}"
        allure.attach(
            msg, name=f"断言失败: {name}", attachment_type=allure.attachment_type.TEXT
        )
        if raise_on_fail:
            raise AssertionError(msg)
    return passed


@allure.step("管理后台请求: {0} {1}")
def record_admin_call(method, path, status_code, params=None, body=None):
    attach_json(
        {
            "method": method,
            "path": path,
            "params": params,
            "status_code": status_code,
            "response": _to_jsonable(body),
        },
        f"后台接口 {method} {path}",
    )


def _local_ts(ts):
    if not ts:
        return None
    import datetime as _dt

    return _dt.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")


def env_info():
    """Attach environment metadata shown in the report overview."""
    import config

    allure.attach(
        json.dumps(
            {
                "model_base_url": config.MODEL_BASE_URL,
                "model_name": config.MODEL_NAME,
                "model_user": config.MODEL_USER,
                "admin_base_url": config.ADMIN_BASE_URL,
                "test_user_id": config.TEST_USER_ID,
                "prices": {
                    "input": config.PRICE_INPUT,
                    "cached": config.PRICE_CACHED,
                    "output": config.PRICE_OUTPUT,
                },
                "request_count": config.REQUEST_COUNT,
                "prefix_tokens": config.PREFIX_TOKENS,
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        name="环境信息",
        attachment_type=allure.attachment_type.JSON,
    )
