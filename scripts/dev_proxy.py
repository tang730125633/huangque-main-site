#!/usr/bin/env python3
"""Serve local site files and proxy API calls to one fixed test backend."""

import argparse
import functools
import http.client
import http.server
import json
import pathlib
import socket
import sys
import urllib.parse


HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
ROOT = pathlib.Path(__file__).resolve().parents[1]


def normalize_upstream(value):
    parsed = urllib.parse.urlsplit((value or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("upstream must be an http(s) origin")
    if parsed.username or parsed.password:
        raise ValueError("upstream credentials are not allowed")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("upstream must not contain a path, query, or fragment")
    return parsed._replace(path="")


def rewrite_set_cookie(value, local_http=True):
    pieces = [piece.strip() for piece in value.split(";")]
    kept = []
    for index, piece in enumerate(pieces):
        lowered = piece.lower()
        if index > 0 and lowered.startswith("domain="):
            continue
        if index > 0 and local_http and lowered == "secure":
            continue
        kept.append(piece)
    return "; ".join(kept)


def safe_request_path(value):
    return urllib.parse.urlsplit(value).path


def is_hop_by_hop(name):
    return name.lower() in HOP_BY_HOP


def _connection_header_names(value):
    return {item.strip().lower() for item in (value or "").split(",") if item.strip()}


class DevProxyHandler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _dispatch(self):
        if safe_request_path(self.path).startswith("/api/"):
            return self._proxy_api()
        if self.command == "GET":
            return super().do_GET()
        if self.command == "HEAD":
            return super().do_HEAD()
        return self._json_error(405, "method not allowed")

    do_GET = _dispatch
    do_HEAD = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_PATCH = _dispatch
    do_DELETE = _dispatch
    do_OPTIONS = _dispatch

    def _proxy_api(self):
        upstream = self.server.upstream
        connection_class = (
            http.client.HTTPSConnection
            if upstream.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_class(
            upstream.hostname,
            upstream.port,
            timeout=self.server.upstream_timeout,
        )
        try:
            length_header = self.headers.get("Content-Length")
            body = self.rfile.read(int(length_header)) if length_header else None
            request_connection_headers = _connection_header_names(
                self.headers.get("Connection")
            )
            outgoing_headers = {}
            for name, value in self.headers.items():
                lowered = name.lower()
                if (
                    lowered == "host"
                    or is_hop_by_hop(name)
                    or lowered in request_connection_headers
                ):
                    continue
                outgoing_headers[name] = value
            outgoing_headers["Host"] = upstream.netloc
            outgoing_headers["X-Forwarded-Host"] = self.headers.get("Host", "")
            outgoing_headers["X-Forwarded-Proto"] = "http"

            connection.request(
                self.command,
                self.path,
                body=body,
                headers=outgoing_headers,
            )
            response = connection.getresponse()
            response_connection_headers = _connection_header_names(
                response.getheader("Connection")
            )
            self.send_response(response.status, response.reason)
            for name, value in response.getheaders():
                lowered = name.lower()
                if (
                    lowered in ("server", "date")
                    or is_hop_by_hop(name)
                    or lowered in response_connection_headers
                ):
                    continue
                if lowered == "set-cookie":
                    value = rewrite_set_cookie(value, local_http=True)
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except socket.timeout:
            self._json_error(504, "test backend timeout")
        except (OSError, http.client.HTTPException):
            self._json_error(502, "test backend unavailable")
        finally:
            connection.close()

    def _json_error(self, status, detail):
        payload = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)
        self.close_connection = True

    def log_message(self, _format, *_args):
        stream = getattr(self.server, "log_stream", sys.stderr)
        stream.write("[dev-proxy] %s %s\n" % (self.command, safe_request_path(self.path)))
        stream.flush()


def create_server(site_root, upstream, port=8097, log_stream=None):
    root = pathlib.Path(site_root).resolve()
    if not root.is_dir():
        raise ValueError("site root does not exist or is not a directory")
    parsed_upstream = normalize_upstream(upstream)
    handler = functools.partial(DevProxyHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    server.upstream = parsed_upstream
    server.upstream_timeout = 300
    server.log_stream = log_stream or sys.stderr
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Serve local site files with a fixed test-backend API proxy."
    )
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--site-root", default=str(ROOT / "site"))
    parser.add_argument("--port", type=int, default=8097)
    args = parser.parse_args(argv)
    server = create_server(args.site_root, args.upstream, args.port)
    print("local site: http://127.0.0.1:%d/workbench/" % args.port)
    print("test backend: %s" % normalize_upstream(args.upstream).geturl())
    print("WARNING: API calls use real test-server accounts, points and provider quota")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
