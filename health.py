"""Combined health check endpoint for the Yargi MCP Server."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import ssl
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse

from runtime_state import ServerRuntimeState, runtime_state

logger = logging.getLogger(__name__)

SERVICE_NAME = "yargi-mcp"
SERVICE_VERSION = "0.2.2"

STATUS_OK = "ok"
STATUS_ERROR = "error"

# bedesten.adalet.gov.tr hosts several unrelated features behind one domain.
# search_bedesten_unified (the actual MCP tool) calls /emsal-karar/searchDocuments
# to search court decisions, so that is the endpoint probed here -- pinging an
# unrelated Bedesten feature (e.g. mevzuat-mcp probes /mevzuat/mevzuatTypes,
# a legislation-types lookup it actually depends on) would say nothing about
# whether *this* server's own feature is working.
BEDESTEN_URL = "https://bedesten.adalet.gov.tr/emsal-karar/searchDocuments"
BEDESTEN_BODY = {
    "data": {
        "pageSize": 5,
        "pageNumber": 1,
        "itemTypeList": ["YARGITAYKARARI"],
        "phrase": "karar",
        "sortFields": ["KARAR_TARIHI"],
        "sortDirection": "desc",
    },
    "applicationName": "UyapMevzuat",
    "paging": True,
}
BEDESTEN_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 Health Check",
}

# The other institutions each have their own site/API (see CLAUDE.md's module
# list). A plain root-page GET is enough to know "is this government site up"
# without reverse-engineering every search payload; KVKK and BDDK are probed
# directly here rather than through Brave/Tavily, since those third-party
# search APIs have their own separate monthly quotas that a 5-minute health
# poll would burn through fast.
ROOT_PROBE_TARGETS: tuple[tuple[str, str], ...] = (
    ("emsal", "https://emsal.uyap.gov.tr"),
    ("uyusmazlik", "https://kararlar.uyusmazlik.gov.tr"),
    ("anayasa", "https://normkararlarbilgibankasi.anayasa.gov.tr"),
    ("kik", "https://ekapv2.kik.gov.tr"),
    ("rekabet", "https://www.rekabet.gov.tr"),
    ("sayistay", "https://www.sayistay.gov.tr"),
    ("btk", "https://www.btk.tr"),
    ("gib", "https://gib.gov.tr"),
    ("sigortaTahkim", "https://www.sigortatahkim.org"),
    ("kvkk", "https://www.kvkk.gov.tr"),
    ("bddk", "https://www.bddk.org.tr"),
)
ROOT_PROBE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
    ),
}


def _legacy_ssl_context() -> ssl.SSLContext:
    """Same relaxed TLS context kik_mcp_module/client_v2.py uses for
    ekapv2.kik.gov.tr -- that site needs OP_LEGACY_SERVER_CONNECT, so a plain
    verify=False probe fails the TLS handshake and reports a false outage."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
        context.options |= ssl.OP_LEGACY_SERVER_CONNECT
    context.set_ciphers("ALL:!aNULL:!eNULL:!EXPORT:!DES:!RC4:!MD5:!PSK:!SRP:!CAMELLIA")
    return context


# Institution name -> TLS verify override for _probe_root (default is verify=False).
ROOT_PROBE_TLS_OVERRIDES: dict[str, Any] = {"kik": _legacy_ssl_context()}

PROBE_TIMEOUT_SECONDS = 20.0

# 28 tools currently registered (see mcp_server_main.py); kept as the gating floor.
MINIMUM_TOOL_COUNT = 28

_STARTED_AT = time.monotonic()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uptime_seconds() -> int:
    return round(time.monotonic() - _STARTED_AT)


def _environment_mode() -> str:
    return os.getenv("ENVIRONMENT", "production").strip().lower() or "production"


RETRY_ATTEMPTS = 2


async def _probe(make_request: Any) -> dict[str, Any]:
    """Run make_request() up to RETRY_ATTEMPTS times, retrying only on
    connection-level exceptions (timeouts, refused connections, etc.).
    With several institutions probed concurrently, a single transient blip on
    one of them (observed live: outbound connections occasionally stall under
    that fan-out) shouldn't be reported as that institution being down."""
    last_latency_ms = 0
    last_error_name = "UnknownError"
    for _ in range(RETRY_ATTEMPTS):
        started = time.perf_counter()
        try:
            response = await make_request()
        except Exception as error:
            last_latency_ms = round((time.perf_counter() - started) * 1000)
            # Only the exception type is exposed; messages can leak internal details.
            last_error_name = type(error).__name__
            continue

        latency_ms = round((time.perf_counter() - started) * 1000)
        healthy = response.status_code == 200
        return {
            "status": STATUS_OK if healthy else STATUS_ERROR,
            "details": {"httpStatus": response.status_code, "latencyMs": latency_ms},
        }

    return {
        "status": STATUS_ERROR,
        "details": {"error": last_error_name, "latencyMs": last_latency_ms, "attempts": RETRY_ATTEMPTS},
    }


async def _probe_upstream(url: str, *, json_body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    """POST a small, already-proven search payload and report reachability."""

    async def make_request() -> httpx.Response:
        async with httpx.AsyncClient(
            headers=headers, timeout=PROBE_TIMEOUT_SECONDS, verify=False
        ) as client:
            return await client.post(url, json=json_body)

    return await _probe(make_request)


async def _check_bedesten() -> dict[str, Any]:
    return await _probe_upstream(BEDESTEN_URL, json_body=BEDESTEN_BODY, headers=BEDESTEN_HEADERS)


async def _probe_root(url: str, *, verify: Any = False) -> dict[str, Any]:
    """Plain reachability GET, used for institutions whose search APIs need
    a full session/viewstate (uyusmazlik, kik) or third-party quota (kvkk,
    bddk) that a health probe should not spend."""

    async def make_request() -> httpx.Response:
        async with httpx.AsyncClient(
            headers=ROOT_PROBE_HEADERS,
            timeout=PROBE_TIMEOUT_SECONDS,
            verify=verify,
            follow_redirects=True,
        ) as client:
            return await client.get(url)

    return await _probe(make_request)


async def _count_tools(mcp: Any) -> int:
    # FastMCP 3.x exposes list_tools(); 2.x exposes get_tools().
    for method_name in ("list_tools", "get_tools"):
        getter = getattr(mcp, method_name, None)
        if getter is None:
            continue
        tools = getter()
        if inspect.isawaitable(tools):
            tools = await tools
        return len(tools)
    raise RuntimeError("tool registry unavailable")


async def _check_mcp_tools(mcp: Any) -> dict[str, Any]:
    try:
        tool_count = await _count_tools(mcp)
    except Exception as error:
        return {"status": STATUS_ERROR, "details": {"error": type(error).__name__}}

    return {
        "status": STATUS_OK if tool_count >= MINIMUM_TOOL_COUNT else STATUS_ERROR,
        "details": {"toolCount": tool_count, "minimumToolCount": MINIMUM_TOOL_COUNT},
    }


def _check_runtime(
    state: ServerRuntimeState, *, include_error_message: bool = False
) -> dict[str, Any]:
    return {
        "status": STATUS_OK if state.is_healthy() else STATUS_ERROR,
        "details": state.status(include_error_message=include_error_message),
    }


def _build_payload(
    gating_checks: dict[str, dict[str, Any]],
    informational_checks: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    # `status`/HTTP 200 vs 503 covers both this server being broken (runtime,
    # mcpTools) AND any single institution being unreachable -- a single
    # Azure Availability Test with ExpectedHttpStatusCode=200 is enough to
    # alert on either case, no separate content-match rule needed.
    # `dependenciesStatus` narrows that down to "was it an institution".
    degraded = any(
        check.get("status") == STATUS_ERROR
        for check in (*gating_checks.values(), *informational_checks.values())
    )
    deps_degraded = any(
        check.get("status") == STATUS_ERROR for check in informational_checks.values()
    )
    return {
        "status": "degraded" if degraded else STATUS_OK,
        "dependenciesStatus": "degraded" if deps_degraded else STATUS_OK,
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "timeUtc": _now_iso(),
        "uptimeSeconds": _uptime_seconds(),
        "environmentMode": _environment_mode(),
        **gating_checks,
        **informational_checks,
    }


async def _run_named_checks(
    specs: tuple[tuple[str, Any], ...]
) -> dict[str, dict[str, Any]]:
    names = [name for name, _ in specs]
    results = await asyncio.gather(*(factory() for _, factory in specs), return_exceptions=True)
    checks: dict[str, dict[str, Any]] = {}
    for name, result in zip(names, results):
        if isinstance(result, BaseException):
            logger.warning("Health check %s raised %s", name, type(result).__name__)
            checks[name] = {
                "status": STATUS_ERROR,
                "details": {"error": type(result).__name__},
            }
        else:
            checks[name] = result
    return checks


async def _collect_health_checks(
    mcp: Any,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    gating, informational = await asyncio.gather(
        _run_named_checks((
            ("mcpTools", lambda: _check_mcp_tools(mcp)),
        )),
        _run_named_checks((
            ("bedesten", _check_bedesten),
            *(
                (
                    name,
                    (
                        lambda target=url, tls=ROOT_PROBE_TLS_OVERRIDES.get(name, False):
                            _probe_root(target, verify=tls)
                    ),
                )
                for name, url in ROOT_PROBE_TARGETS
            ),
        )),
    )
    # include_error_message stays False: /health is unauthenticated so
    # Application Insights can reach it, so raw exception text -- which can
    # carry upstream URLs/response bodies -- must not appear in a response
    # anyone on the internet can read. lastErrorType/lastErrorTool still say
    # what broke without leaking the message itself.
    gating["runtime"] = _check_runtime(runtime_state, include_error_message=False)
    return gating, informational


async def collect_health_status(mcp: Any) -> dict[str, Any]:
    """Return internal + external-dependency checks.

    `status` (and the HTTP code) reflects both this server's own state
    (runtime, mcpTools) and every one of the 12 government/upstream
    dependencies -- any single failure is enough to report `degraded`/503.
    `dependenciesStatus` narrows that down to just the external institutions.
    """
    gating_checks, informational_checks = await _collect_health_checks(mcp)
    return _build_payload(gating_checks, informational_checks)


def register_health_routes(mcp: Any) -> None:
    """Attach the combined /health endpoint to a FastMCP server."""

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        payload = await collect_health_status(mcp)
        status_code = 503 if payload["status"] != STATUS_OK else 200
        return JSONResponse(payload, status_code=status_code)
