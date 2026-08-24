"""Authorized live Provider probes with request and CNY budget hard stops."""

import argparse
import copy
from contextlib import contextmanager
import json
import os
import pathlib
import tempfile
import time
import urllib.parse

import requests

import provider_compat

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None
    import msvcrt


SOL_INPUT_USD_PER_MTOK = 4.0
SOL_OUTPUT_USD_PER_MTOK = 20.0
CNY_PER_USD_BUDGET = 7.5
AUTHORIZED_MAX_REQUESTS = 1000
AUTHORIZED_MAX_CNY = 100.0


def _base_url(value, fallback):
    value = str(value or fallback).rstrip("/")
    parsed = urllib.parse.urlparse(value)
    path = parsed.path.rstrip("/")
    for suffix in ("/chat/completions", "/responses", "/models"):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
    if not path.endswith("/v1"):
        path += "/v1"
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", "")).rstrip("/")


def provider_configs():
    return {
        "openai_official": {
            "base_url": "https://api.openai.com/v1",
            "key": os.environ.get("OPENAI_API_KEY") or "",
        },
        "dashscope": {
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "key": os.environ.get("DASHSCOPE_API_KEY") or "",
        },
        "zelong_proxy": {
            "base_url": _base_url(os.environ.get("COPY_BASE"), "https://api.zelong.vip/v1"),
            "key": os.environ.get("COPY_API_KEY") or "",
        },
    }


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, max_requests=1000, max_cny=100.0, ledger_path=""):
        self.max_requests = min(int(max_requests), AUTHORIZED_MAX_REQUESTS)
        self.max_cny = min(float(max_cny), AUTHORIZED_MAX_CNY)
        self.ledger_path = pathlib.Path(ledger_path) if ledger_path else None
        self.requests = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.reserved_input_tokens = 0
        self.reserved_output_tokens = 0
        self.usage_reports = 0
        self.usage_missing = 0
        if self.ledger_path:
            with self._locked():
                pass

    def _reload_unlocked(self):
        if not self.ledger_path or not self.ledger_path.is_file():
            return
        try:
            saved = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise BudgetExceeded("live eval budget ledger is unreadable")
        for name in (
            "requests", "input_tokens", "output_tokens", "reserved_input_tokens",
            "reserved_output_tokens", "usage_reports", "usage_missing",
        ):
            setattr(self, name, max(0, int(saved.get(name) or 0)))

    @contextmanager
    def _locked(self):
        if not self.ledger_path:
            yield
            return
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.ledger_path.with_name(self.ledger_path.name + ".lock")
        with open(lock_path, "a+b") as handle:
            os.chmod(lock_path, 0o600)
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            else:  # pragma: no cover - Windows fallback
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                self._reload_unlocked()
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                else:  # pragma: no cover - Windows fallback
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def _save_unlocked(self):
        if not self.ledger_path:
            return
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        value = self.public()
        fd, temp_name = tempfile.mkstemp(
            prefix=".ip12-provider-budget-", dir=str(self.ledger_path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.ledger_path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    @property
    def estimated_cny(self):
        usd = (
            self.input_tokens * SOL_INPUT_USD_PER_MTOK
            + self.output_tokens * SOL_OUTPUT_USD_PER_MTOK
        ) / 1_000_000
        return round(usd * CNY_PER_USD_BUDGET, 6)

    @property
    def worst_case_cny(self):
        usd = (
            self.reserved_input_tokens * SOL_INPUT_USD_PER_MTOK
            + self.reserved_output_tokens * SOL_OUTPUT_USD_PER_MTOK
        ) / 1_000_000
        return round(usd * CNY_PER_USD_BUDGET, 6)

    def reserve(self, request=None):
        with self._locked():
            if self.usage_missing:
                raise BudgetExceeded("prior provider usage is missing; reconcile billing before continuing")
            request = request if isinstance(request, dict) else {}
            input_estimate = max(1, len(json.dumps(request, ensure_ascii=False)) // 2)
            output_limit = int(request.get("max_output_tokens") or 512)
            next_usd = (
                (self.reserved_input_tokens + input_estimate) * SOL_INPUT_USD_PER_MTOK
                + (self.reserved_output_tokens + output_limit) * SOL_OUTPUT_USD_PER_MTOK
            ) / 1_000_000
            if (self.requests >= self.max_requests
                    or next_usd * CNY_PER_USD_BUDGET > self.max_cny):
                raise BudgetExceeded("live eval budget exhausted")
            self.requests += 1
            self.reserved_input_tokens += input_estimate
            self.reserved_output_tokens += output_limit
            self._save_unlocked()

    def add_usage(self, value, *, required=False):
        with self._locked():
            value = value if isinstance(value, dict) else {}
            if value:
                self.usage_reports += 1
            else:
                self.usage_missing += 1
            self.input_tokens += int(value.get("input_tokens") or value.get("prompt_tokens") or 0)
            self.output_tokens += int(value.get("output_tokens") or value.get("completion_tokens") or 0)
            self._save_unlocked()
            if required and not value:
                raise BudgetExceeded("provider usage missing; monetary cap cannot be enforced")
            if self.estimated_cny > self.max_cny:
                raise BudgetExceeded("live eval cost limit exceeded")

    def seed_existing(self, *, requests_count, usage_missing,
                      reserved_input_tokens, reserved_output_tokens):
        with self._locked():
            if any((self.requests, self.input_tokens, self.output_tokens,
                    self.reserved_input_tokens, self.reserved_output_tokens,
                    self.usage_reports, self.usage_missing)):
                raise BudgetExceeded("live eval budget ledger is already initialized")
            self.requests = max(0, int(requests_count))
            self.usage_missing = max(0, int(usage_missing))
            self.reserved_input_tokens = max(0, int(reserved_input_tokens))
            self.reserved_output_tokens = max(0, int(reserved_output_tokens))
            if self.requests > self.max_requests or self.worst_case_cny > self.max_cny:
                raise BudgetExceeded("seed exceeds authorized live eval budget")
            self._save_unlocked()

    def public(self):
        return {
            "requests": self.requests, "max_requests": self.max_requests,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "reserved_input_tokens": self.reserved_input_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "estimated_cny": self.estimated_cny,
            "worst_case_cny": self.worst_case_cny,
            "usage_reports": self.usage_reports, "usage_missing": self.usage_missing,
            "cost_status": "reported" if self.usage_missing == 0 else "upper_bound_only",
            "max_cny": self.max_cny,
        }


def preflight(provider, model, config, budget=None):
    if not config.get("key"):
        return {"provider": provider, "status": "credential_blocked", "model": model}
    if budget:
        budget.reserve({"method": "GET", "path": "/models", "provider": provider})
    try:
        response = requests.get(
            config["base_url"] + "/models",
            headers={"Authorization": "Bearer " + config["key"]}, timeout=20,
        )
        payload = response.json() if response.content else {}
    except (requests.RequestException, ValueError) as exc:
        return {"provider": provider, "status": "unavailable", "model": model,
                "error": type(exc).__name__}
    models = {
        str(item.get("id") or "") for item in (payload.get("data") or [])
        if isinstance(item, dict)
    }
    return {
        "provider": provider, "status": "ready" if response.status_code == 200 else "unavailable",
        "http_status": response.status_code, "model": model,
        "model_available": model in models, "models_count": len(models),
        "endpoint_host": urllib.parse.urlparse(config["base_url"]).hostname,
    }


class LiveResponsesTransport:
    evidence_source = "live_capture"

    def __init__(self, provider, config, budget):
        self.provider = provider
        self.base_url = config["base_url"]
        self.key = config["key"]
        self.budget = budget
        self.capture_summary = []
        self._active_probe = ""

    def _headers(self):
        return {"Authorization": "Bearer " + self.key, "Content-Type": "application/json"}

    def _observation(self, response, request_fingerprint, payload=None, events=None):
        payload = payload if isinstance(payload, dict) else {}
        self.budget.add_usage(
            payload.get("usage") or {}, required=200 <= response.status_code < 300
        )
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        self.capture_summary.append({
            "probe": self._active_probe,
            "http_status": response.status_code,
            "error_code": str(error.get("code") or error.get("type") or "")[:80],
            "response_type": str(payload.get("object") or payload.get("type") or "")[:80],
            "model": str(payload.get("model") or "")[:120],
            "request_id_present": bool(response.headers.get("x-request-id") or payload.get("id")),
            "event_types": [str(item.get("type") or "")[:80] for item in (events or [])[:20]],
        })
        return {
            "status_code": response.status_code,
            "response": payload,
            "events": events or [],
            "provider_request_id": response.headers.get("x-request-id") or payload.get("id") or "",
            "request_fingerprint": request_fingerprint,
            "captured_at": int(time.time()),
        }

    def __call__(self, name, request):
        self._active_probe = str(name)
        request = copy.deepcopy(request)
        request.setdefault("max_output_tokens", 512)
        request_fingerprint = __import__("hashlib").sha256(json.dumps(
            request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        if name == "timeout_cancel":
            self.budget.reserve(request)
            try:
                response = requests.post(
                    self.base_url + "/responses", headers=self._headers(), json=request,
                    timeout=0.001,
                )
            except requests.Timeout:
                self.capture_summary.append({"probe": self._active_probe,
                                             "http_status": 0, "error_code": "timeout",
                                             "response_type": "", "model": "",
                                             "request_id_present": False, "event_types": []})
                return {
                    "terminal": "timeout", "request_fingerprint": request_fingerprint,
                    "captured_at": int(time.time()),
                }
            try:
                payload = response.json() if response.content else {}
            except ValueError:
                payload = {}
            observation = self._observation(response, request_fingerprint, payload)
            observation["terminal"] = "unexpected_response"
            return observation

        self.budget.reserve(request)
        stream = name == "stream"
        response = requests.post(
            self.base_url + "/responses", headers=self._headers(), json=request,
            timeout=60, stream=stream,
        )
        if stream:
            events, final = [], {}
            for line in response.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                value = line[5:].strip()
                if value == "[DONE]":
                    events.append({"type": "done"})
                    continue
                try:
                    event = json.loads(value)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    events.append({"type": str(event.get("type") or "")})
                    if isinstance(event.get("response"), dict):
                        final = event["response"]
            return self._observation(response, request_fingerprint, final, events)

        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {}
        observation = self._observation(response, request_fingerprint, payload)
        if name == "reasoning" and self.provider == "openai_official" and response.status_code == 200:
            observation["official_contract"] = True
        if name == "reasoning" and isinstance(payload.get("reasoning"), dict):
            observation["effective"] = payload["reasoning"]
        if name == "store_false" and payload.get("id"):
            self.budget.reserve({"method": "GET", "response_id": str(payload["id"])})
            fetched = requests.get(
                self.base_url + "/responses/" + urllib.parse.quote(str(payload["id"]), safe=""),
                headers=self._headers(), timeout=20,
            )
            observation["retrieval_status"] = fetched.status_code
        return observation


def run_compat(provider, model, config, budget):
    if not config.get("key"):
        return {"provider": provider, "model": model, "decision": "HOLD",
                "reason": "credential_blocked", "passed": False}
    transport = LiveResponsesTransport(provider, config, budget)
    report = provider_compat.run_suite(provider, model, transport)
    report["budget"] = budget.public()
    report["capture_summary"] = transport.capture_summary
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("seed", "preflight", "compat"), required=True)
    parser.add_argument("--provider", choices=("openai_official", "dashscope", "zelong_proxy", "all"), default="all")
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--max-requests", type=int, default=1000)
    parser.add_argument("--max-cny", type=float, default=100.0)
    parser.add_argument(
        "--budget-ledger",
        default="/home/ubuntu/hermes-preview-data/provider-eval-budget-gpt-5.6-sol.json",
    )
    parser.add_argument("--seed-existing-requests", type=int, default=0)
    parser.add_argument("--seed-usage-missing", type=int, default=0)
    parser.add_argument("--seed-reserved-input-tokens", type=int, default=0)
    parser.add_argument("--seed-reserved-output-tokens", type=int, default=0)
    args = parser.parse_args()
    configs = provider_configs()
    providers = list(configs) if args.provider == "all" else [args.provider]
    budget = Budget(args.max_requests, args.max_cny, args.budget_ledger)
    if args.mode == "seed":
        budget.seed_existing(
            requests_count=args.seed_existing_requests,
            usage_missing=args.seed_usage_missing,
            reserved_input_tokens=args.seed_reserved_input_tokens,
            reserved_output_tokens=args.seed_reserved_output_tokens,
        )
        print(json.dumps({"mode": "seed", "budget": budget.public()}, sort_keys=True))
        return
    results = {}
    for provider in providers:
        readiness = preflight(provider, args.model, configs[provider], budget)
        if args.mode == "preflight" or not readiness.get("model_available"):
            results[provider] = readiness
            continue
        results[provider] = run_compat(provider, args.model, configs[provider], budget)
    print(json.dumps({"mode": args.mode, "model": args.model, "results": results,
                      "budget": budget.public()}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
