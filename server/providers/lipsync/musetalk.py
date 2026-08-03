"""HTTP adapter for a separately deployed MuseTalk GPU service.

Only Python's standard library is used so the content process keeps its current
dependency footprint. Large videos are streamed instead of buffered in memory.
"""

import http.client
import json
import mimetypes
import os
import ssl
import uuid
from pathlib import Path
from urllib import parse as urlparse

from .base import LipsyncProvider, ProviderSubmissionUnknownError


class MuseTalkProviderError(RuntimeError):
    pass


# The GPU wrapper contract currently guarantees that only 422 is rejected
# before a task can be created. Every other non-2xx status is conservative:
# the paid POST was already sent and must be recovered with the same key.
DEFINITIVE_CREATE_REJECTION_STATUSES = frozenset({422})


def _positive_int(name, default):
    try:
        value = int(os.environ.get(name, default) or default)
    except (TypeError, ValueError):
        value = int(default)
    return max(1, value)


class MuseTalkProvider(LipsyncProvider):
    name = "musetalk"
    model_version = "musetalk-1.5"
    supports_cancel = True
    supports_result_refetch = True
    requires_local_media = True

    def __init__(
        self, *, base_url, api_key="", api_prefix="/v1", timeout=120,
        max_result_bytes=512 * 1024 * 1024, connection_factory=None,
    ):
        parsed = urlparse.urlsplit(str(base_url or "").strip().rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("MUSETALK_API_BASE must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("MUSETALK_API_BASE must not contain credentials or query")
        if parsed.scheme == "http" and str(os.environ.get(
            "MUSETALK_ALLOW_HTTP", "0"
        )).strip() != "1":
            raise ValueError(
                "plain HTTP MuseTalk endpoints require MUSETALK_ALLOW_HTTP=1"
            )
        self.scheme = parsed.scheme
        self.hostname = parsed.hostname
        self.port = parsed.port
        self.base_path = parsed.path.rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.api_prefix = "/" + str(api_prefix or "/v1").strip("/")
        self.timeout = max(1, int(timeout))
        self.max_result_bytes = max(1024, int(max_result_bytes))
        self.connection_factory = connection_factory

    def _connection(self):
        if self.connection_factory:
            return self.connection_factory()
        if self.scheme == "https":
            return http.client.HTTPSConnection(
                self.hostname, self.port, timeout=self.timeout,
                context=ssl.create_default_context(),
            )
        return http.client.HTTPConnection(
            self.hostname, self.port, timeout=self.timeout
        )

    def _path(self, value):
        path = self.base_path + self.api_prefix + "/" + str(value or "").lstrip("/")
        return path if path.startswith("/") else "/" + path

    def _headers(self, extra=None):
        headers = {"Accept": "application/json", "User-Agent": "huangque-musetalk/1"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        headers.update(dict(extra or {}))
        return headers

    @staticmethod
    def _close(connection, response=None):
        try:
            if response is not None:
                response.close()
        finally:
            connection.close()

    def _checked_response(self, connection):
        try:
            response = connection.getresponse()
        except (OSError, TimeoutError) as error:
            connection.close()
            if isinstance(error, TimeoutError):
                raise TimeoutError("MuseTalk request timed out") from error
            raise MuseTalkProviderError("MuseTalk request failed") from error
        if 300 <= response.status < 400:
            self._close(connection, response)
            raise MuseTalkProviderError("MuseTalk redirects are not allowed")
        if not 200 <= response.status < 300:
            detail = response.read(8192).decode("utf-8", "replace")[:500]
            status = response.status
            self._close(connection, response)
            raise MuseTalkProviderError(
                "MuseTalk HTTP %s: %s" % (status, detail)
            )
        return response

    def _json(self, method, path, payload=None):
        connection = self._connection()
        body = None
        headers = self._headers()
        if payload is not None:
            body = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        try:
            connection.request(method, self._path(path), body=body, headers=headers)
            response = self._checked_response(connection)
            raw = response.read(1024 * 1024 + 1)
        except (OSError, http.client.HTTPException) as error:
            connection.close()
            raise MuseTalkProviderError("MuseTalk request failed") from error
        self._close(connection, response)
        if len(raw) > 1024 * 1024:
            raise MuseTalkProviderError("MuseTalk JSON response is too large")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise MuseTalkProviderError("MuseTalk returned invalid JSON") from error
        if not isinstance(value, dict):
            raise MuseTalkProviderError("MuseTalk JSON response must be an object")
        return value

    @staticmethod
    def _multipart_items(request, boundary):
        fields = {
            "face_target": json.dumps(
                request.get("face_target") or {}, ensure_ascii=False,
                separators=(",", ":"),
            ),
            "metadata": json.dumps(
                request.get("metadata") or {}, ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
        items = []
        for name, value in fields.items():
            header = (
                "--%s\r\nContent-Disposition: form-data; name=\"%s\""
                "\r\n\r\n" % (boundary, name)
            ).encode("utf-8")
            items.append((header, str(value).encode("utf-8"), b"\r\n"))
        for name, key in (("video", "video_path"), ("audio", "audio_path")):
            path = Path(request.get(key) or "")
            if not path.is_file() or path.stat().st_size <= 0:
                raise MuseTalkProviderError("MuseTalk input file is missing: " + name)
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            header = (
                "--%s\r\nContent-Disposition: form-data; name=\"%s\"; "
                "filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
                % (boundary, name, path.name.replace('"', ""), mime)
            ).encode("utf-8")
            items.append((header, path, b"\r\n"))
        return items

    @staticmethod
    def _send(connection, value):
        if isinstance(value, Path):
            with open(value, "rb") as source:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    connection.send(chunk)
        else:
            connection.send(value)

    def create_job(self, request, idempotency_key):
        if not isinstance(request, dict):
            raise MuseTalkProviderError("MuseTalk request must be an object")
        boundary = "huangque-" + uuid.uuid4().hex
        items = self._multipart_items(request, boundary)
        closing = ("--%s--\r\n" % boundary).encode("ascii")
        content_length = len(closing) + sum(
            len(header) + (body.stat().st_size if isinstance(body, Path) else len(body))
            + len(tail)
            for header, body, tail in items
        )
        headers = self._headers({
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "Content-Length": str(content_length),
            "Idempotency-Key": str(idempotency_key or ""),
        })
        connection = self._connection()
        response = None
        request_started = False
        try:
            connection.putrequest("POST", self._path("jobs"))
            for name, value in headers.items():
                connection.putheader(name, value)
            # endheaders() is the first operation that can put this paid
            # request on the wire. Any failure from here on has an unknown
            # external outcome and must not trigger an automatic refund.
            request_started = True
            connection.endheaders()
            for header, body, tail in items:
                self._send(connection, header)
                self._send(connection, body)
                self._send(connection, tail)
            self._send(connection, closing)
            response = connection.getresponse()
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            connection.close()
            if request_started:
                raise ProviderSubmissionUnknownError(
                    "MuseTalk create outcome is unknown"
                ) from error
            raise MuseTalkProviderError("MuseTalk create request failed") from error
        if 300 <= response.status < 400:
            self._close(connection, response)
            raise ProviderSubmissionUnknownError(
                "MuseTalk create redirect outcome is unknown"
            )
        if not 200 <= response.status < 300:
            try:
                detail = response.read(8192).decode("utf-8", "replace")[:500]
            except (OSError, TimeoutError, http.client.HTTPException):
                detail = ""
            status = response.status
            self._close(connection, response)
            if status in DEFINITIVE_CREATE_REJECTION_STATUSES:
                raise MuseTalkProviderError(
                    "MuseTalk HTTP %s: %s" % (status, detail)
                )
            raise ProviderSubmissionUnknownError(
                "MuseTalk create HTTP %s outcome is unknown" % status
            )
        try:
            raw = response.read(1024 * 1024 + 1)
        except (OSError, http.client.HTTPException) as error:
            connection.close()
            raise ProviderSubmissionUnknownError(
                "MuseTalk create response was lost"
            ) from error
        self._close(connection, response)
        if len(raw) > 1024 * 1024:
            raise ProviderSubmissionUnknownError(
                "MuseTalk create response is too large"
            )
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise ProviderSubmissionUnknownError(
                "MuseTalk returned invalid create JSON"
            ) from error
        if not isinstance(value, dict):
            raise ProviderSubmissionUnknownError(
                "MuseTalk create response must be an object"
            )
        job_id = value.get("job_id") or value.get("task_id") or value.get("id")
        if not job_id:
            raise ProviderSubmissionUnknownError(
                "MuseTalk create response omitted job_id"
            )
        return {
            "job_id": str(job_id),
            "status": self._status(value.get("status") or "queued"),
        }

    @staticmethod
    def _status(value):
        normalized = str(value or "unknown").strip().lower().replace("-", "_")
        return {
            "pending": "queued", "created": "queued", "queued": "queued",
            "processing": "running", "in_progress": "running", "running": "running",
            "completed": "succeeded", "complete": "succeeded", "success": "succeeded",
            "succeeded": "succeeded", "failed": "failed", "error": "failed",
            "cancelled": "cancelled", "canceled": "cancelled",
        }.get(normalized, "unknown")

    def get_job(self, provider_job_id):
        value = self._json(
            "GET", "jobs/" + urlparse.quote(str(provider_job_id), safe="")
        )
        progress = value.get("progress") or value.get("progress_percent") or 0
        try:
            progress = int(float(progress))
        except (TypeError, ValueError):
            progress = 0
        return {
            "status": self._status(value.get("status") or value.get("state")),
            "progress": max(0, min(100, progress)),
        }

    def cancel_job(self, provider_job_id):
        value = self._json(
            "DELETE", "jobs/" + urlparse.quote(str(provider_job_id), safe="")
        )
        return {"status": self._status(value.get("status") or "cancelled")}

    def fetch_result(self, provider_job_id, destination):
        connection = self._connection()
        response = None
        written = 0
        path = "jobs/%s/result" % urlparse.quote(str(provider_job_id), safe="")
        destination = Path(destination)
        partial = destination.with_name(destination.name + ".part")
        try:
            connection.request(
                "GET", self._path(path), headers=self._headers({"Accept": "video/mp4"})
            )
            response = self._checked_response(connection)
            destination.parent.mkdir(parents=True, exist_ok=True)
            partial.unlink(missing_ok=True)
            with open(partial, "wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > self.max_result_bytes:
                        raise MuseTalkProviderError(
                            "MuseTalk result exceeds size limit"
                        )
                    output.write(chunk)
            if written <= 0:
                raise MuseTalkProviderError("MuseTalk result is empty")
            os.replace(partial, destination)
        except (OSError, http.client.HTTPException) as error:
            partial.unlink(missing_ok=True)
            raise MuseTalkProviderError("MuseTalk result download failed") from error
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        finally:
            self._close(connection, response)
        return str(destination)


def create_provider():
    base_url = str(os.environ.get("MUSETALK_API_BASE") or "").strip()
    if not base_url:
        raise RuntimeError("MUSETALK_API_BASE is required")
    return MuseTalkProvider(
        base_url=base_url,
        api_key=os.environ.get("MUSETALK_API_KEY", ""),
        api_prefix=os.environ.get("MUSETALK_API_PREFIX", "/v1"),
        timeout=_positive_int("MUSETALK_TIMEOUT_SECONDS", 120),
        max_result_bytes=_positive_int(
            "MUSETALK_MAX_RESULT_BYTES", 512 * 1024 * 1024
        ),
    )
