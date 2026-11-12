from unittest.mock import call, patch

import pytest
import requests

from rivals.services.fpl_api_service import FplApiService

URL = f"{FplApiService.BASE_URL}/leagues-classic/1587276/standings/?page_standings=440"


def response(status, body=b'{"ok": true}'):
    result = requests.Response()
    result.status_code = status
    result.url = URL
    result._content = body
    return result


@pytest.fixture
def http():
    with (
        patch("rivals.services.fpl_api_service.requests.get") as get,
        patch("rivals.services.fpl_api_service.time.sleep") as sleep,
        patch("rivals.services.fpl_api_service.logger") as logger,
    ):
        yield get, sleep, logger


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_temporary_server_error_recovers_without_error_log(http, status):
    get, sleep, logger = http
    get.side_effect = [response(status), response(200)]

    assert FplApiService().fetch_mini_league_page("1587276", 440) == {"ok": True}

    assert (
        get.call_args_list
        == [call(URL, headers=FplApiService.HEADERS, timeout=FplApiService.TIMEOUT)] * 2
    )
    sleep.assert_called_once_with(1)
    logger.error.assert_not_called()


@pytest.mark.parametrize("error", [requests.ConnectionError, requests.Timeout])
def test_connection_failure_uses_bounded_exponential_backoff(http, error):
    get, sleep, logger = http
    get.side_effect = [
        error("temporary failure"),
        error("temporary failure"),
        response(200),
    ]
    api = FplApiService()
    api.TIMEOUT = 5

    assert api.make_request(URL) == {"ok": True}

    assert get.call_count == 3
    assert all(c.kwargs["timeout"] == 5 for c in get.call_args_list)
    assert sleep.call_args_list == [call(1), call(2)]
    logger.error.assert_not_called()


def test_persistent_server_failure_returns_none_after_retry_budget(http):
    get, sleep, logger = http
    get.side_effect = [response(502) for _ in range(3)]

    assert FplApiService().make_request(URL) is None

    assert get.call_count == 3
    assert sleep.call_args_list == [call(1), call(2)]
    assert logger.warning.call_count == 2
    logger.error.assert_called_once()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429])
def test_non_retryable_http_error_fails_immediately(http, status):
    get, sleep, logger = http
    get.return_value = response(status)

    assert FplApiService().make_request(URL) is None

    get.assert_called_once()
    sleep.assert_not_called()
    logger.error.assert_called_once()


def test_invalid_json_returns_none_without_retry(http):
    get, sleep, logger = http
    get.return_value = response(200, b"invalid JSON")

    assert FplApiService().make_request(URL) is None

    get.assert_called_once()
    sleep.assert_not_called()
    logger.error.assert_called_once()
