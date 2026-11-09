"""HTTP на стандартной библиотеке: JSON-запросы и чтение потока SSE."""
from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, Optional

USER_AGENT = "lokalka/1.0"
TIMEOUT_SECONDS = 180
RETRY_STATUSES = (429, 500, 502, 503, 529)
RETRY_DELAYS = (2, 6)
ERROR_BODY_LIMIT = 4000
ERROR_TEXT_LIMIT = 400

STATUS_HINTS = {
    400: "запрос отклонён (часто — неверное имя модели)",
    401: "неверный или просроченный API-ключ (/key — ввести заново)",
    402: "на балансе нет средств",
    403: "доступ запрещён (ключ без прав или регион заблокирован — попробуй OpenRouter или свой адрес через /url)",
    404: "не найдено (проверь имя модели: /model)",
    413: "слишком длинный диалог (/clear — начать заново)",
    429: "превышен лимит запросов, подожди немного",
}


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """Не идём за перенаправлением на другой сервер: вместе с запросом туда ушёл бы API-ключ."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        old_host = urllib.parse.urlsplit(req.full_url).netloc.lower()
        if urllib.parse.urlsplit(newurl).netloc.lower() != old_host:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SameHostRedirects)


class ApiError(Exception):
    """Ошибка обращения к провайдеру; status == 0, если ответа не было вовсе."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _extract_message(body: str) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return body.strip()[:ERROR_TEXT_LIMIT]
    items = data if isinstance(data, list) else [data]
    for item in items:
        err = item.get("error") if isinstance(item, dict) else None
        if isinstance(err, dict):
            return str(err.get("message") or err)[:ERROR_TEXT_LIMIT]
        if isinstance(err, str):
            return err[:ERROR_TEXT_LIMIT]
    return body.strip()[:ERROR_TEXT_LIMIT]


def _http_error(err: urllib.error.HTTPError) -> ApiError:
    try:
        body = err.read(ERROR_BODY_LIMIT).decode("utf-8", "replace")
    except OSError:
        body = ""
    hint = STATUS_HINTS.get(err.code, "ошибка сервера" if err.code >= 500 else "")
    parts = ["HTTP {}".format(err.code)]
    if hint:
        parts.append(hint)
    detail = _extract_message(body)
    if detail:
        parts.append(detail)
    return ApiError(" — ".join(parts), status=err.code)


def _open(url: str, headers: Dict[str, str], payload: Optional[Dict[str, Any]]) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    all_headers = dict(headers)
    all_headers["User-Agent"] = USER_AGENT
    if data is not None:
        all_headers["Content-Type"] = "application/json"
    host = urllib.parse.urlsplit(url).netloc or url
    for attempt in range(len(RETRY_DELAYS) + 1):
        request = urllib.request.Request(url, data=data, headers=all_headers,
                                         method="GET" if data is None else "POST")
        try:
            return _OPENER.open(request, timeout=TIMEOUT_SECONDS)
        except urllib.error.HTTPError as err:
            if err.code in RETRY_STATUSES and attempt < len(RETRY_DELAYS):
                err.close()
                time.sleep(RETRY_DELAYS[attempt])
                continue
            raise _http_error(err)
        except urllib.error.URLError as err:
            raise ApiError("Нет связи с {}: {}".format(host, err.reason))
        except (OSError, ValueError, http.client.HTTPException) as err:
            raise ApiError("Сбой соединения с {}: {}".format(host, err))
    raise ApiError("Сервер {} не ответил.".format(host))


def get_json(url: str, headers: Dict[str, str]) -> Any:
    try:
        with _open(url, headers, None) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except ValueError:
        raise ApiError("Сервер вернул не JSON: {}".format(url))
    except (OSError, http.client.HTTPException) as err:
        raise ApiError("Сбой чтения ответа: {}".format(err))


def stream_events(url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """POST и разбор ответа: поток SSE по событиям либо один обычный JSON-ответ."""
    try:
        with _open(url, headers, payload) as response:
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                body = json.loads(response.read().decode("utf-8", "replace"))
                if isinstance(body, dict):
                    yield body
                return
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    event = json.loads(data)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    yield event
    except ValueError:
        raise ApiError("Сервер вернул непонятный ответ.")
    except (OSError, http.client.HTTPException) as err:
        raise ApiError("Соединение оборвалось: {}".format(err or type(err).__name__))
