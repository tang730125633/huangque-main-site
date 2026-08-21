"""Official MiniMax H3 adapter for billable short-drama shots."""

import base64
import json

from .base import ShotVisualCapability, ShotVisualProvider, VisualProviderError


MINIMAX_ORIGIN_METASO = "metaso"
MINIMAX_ORIGIN_LEGACY = "legacy"
MINIMAX_ORIGINS = {MINIMAX_ORIGIN_METASO, MINIMAX_ORIGIN_LEGACY}


class MiniMaxH3ShotProvider(ShotVisualProvider):
    name = "minimax_h3"
    default_model = "MiniMax-H3"

    @property
    def capability(self):
        return ShotVisualCapability(
            provider=self.name,
            ratios=("21:9", "16:9", "4:3", "1:1", "3:4", "9:16", "adaptive"),
            minimum_seconds=4,
            maximum_seconds=15,
            supports_cancel=False,
            supports_result_refetch=True,
        )

    @property
    def configured(self):
        try:
            from content_domains import provider_keys

            return provider_keys.has_candidate("minimax")
        except Exception:
            return False

    @staticmethod
    def _encode_job_id(key_id, task_id, origin=MINIMAX_ORIGIN_METASO):
        origin = str(origin or "").strip().lower()
        if origin not in MINIMAX_ORIGINS:
            raise ValueError("unsupported MiniMax task origin")
        raw = json.dumps(
            {
                "key_id": str(key_id or "env"),
                "task_id": str(task_id),
                "origin": origin,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _historical_origin():
        from content_domains import video_minimax_h3

        try:
            return video_minimax_h3.historical_origin_from_environment()
        except video_minimax_h3.MiniMaxOriginUnknown as error:
            raise VisualProviderError(
                "provider_job_origin_unknown",
                str(error), submitted=True,
            ) from error

    @staticmethod
    def _decode_job_id(provider_job_id):
        value = str(provider_job_id or "").strip()
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            payload = json.loads(raw.decode("utf-8"))
            task_id = str(payload.get("task_id") or "").strip()
            if task_id:
                origin = str(
                    payload.get("origin") or MiniMaxH3ShotProvider._historical_origin()
                ).strip().lower()
                if origin not in MINIMAX_ORIGINS:
                    raise VisualProviderError(
                        "provider_job_origin_invalid",
                        "MiniMax 任务来源无效，已停止自动恢复",
                        submitted=True,
                    )
                return str(payload.get("key_id") or "env"), task_id, origin
        except VisualProviderError:
            raise
        except Exception:
            pass
        return (
            "env", value, MiniMaxH3ShotProvider._historical_origin()
        )

    @staticmethod
    def _claim_key():
        from content_domains import provider_keys

        return provider_keys.claim_candidate("minimax")

    @staticmethod
    def _bound_key(key_id):
        try:
            from content_domains import provider_keys

            candidates = provider_keys.candidates(
                "minimax", preferred_id=str(key_id or "env")
            )
            return candidates[0] if candidates else None
        except Exception:
            return None

    @staticmethod
    def _reference_value(item):
        if isinstance(item, str):
            value = item.strip()
            relative = ""
        else:
            value = str((item or {}).get("url") or "").strip()
            relative = str((item or {}).get("file") or "").strip()
        if not relative:
            if not value:
                raise VisualProviderError(
                    "visual_reference_required", "麦克视频缺少可用的人物参考图"
                )
            try:
                from content_domains import video_minimax_h3

                return video_minimax_h3.validate_reference_input(value)
            except ValueError as error:
                raise VisualProviderError(
                    "visual_reference_invalid",
                    "麦克视频参考图必须是已上传并归属当前项目的本地图片",
                ) from error
        from content_domains.core import _out_path

        try:
            path = _out_path(relative)
            if not path.is_file():
                raise FileNotFoundError(relative)
            size = path.stat().st_size
            if size <= 0 or size > 30 * 1024 * 1024:
                raise ValueError("unsupported image size")
            raw = path.read_bytes()
        except Exception as error:
            raise VisualProviderError(
                "visual_reference_unavailable",
                "角色标准图文件不可用，请重新选择或上传",
            ) from error
        if raw.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif raw.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            raise VisualProviderError(
                "visual_reference_invalid",
                "角色标准图不是有效的 JPG、PNG 或 WebP 图片",
            )
        value = "data:%s;base64,%s" % (
            mime, base64.b64encode(raw).decode("ascii")
        )
        try:
            from content_domains.video_minimax_h3 import validate_reference_input

            return validate_reference_input(value)
        except ValueError as error:
            raise VisualProviderError(
                "visual_reference_invalid",
                "角色标准图损坏、格式不符或尺寸不在 256～5760 像素范围内",
            ) from error

    def validate_request(self, request):
        if not isinstance(request, dict):
            raise VisualProviderError("visual_request_invalid", "镜头请求格式不正确")
        prompt = str(request.get("prompt") or "").strip()
        ratio = str(request.get("ratio") or "").strip()
        model = str(request.get("model") or self.default_model).strip()
        try:
            duration = int(request.get("duration_seconds") or 0)
        except (TypeError, ValueError) as error:
            raise VisualProviderError("visual_duration_invalid", "镜头时长必须是整数秒") from error
        refs = list(request.get("reference_images") or [])
        if not prompt:
            raise VisualProviderError("visual_prompt_required", "镜头缺少可执行的画面提示词")
        if ratio not in self.capability.ratios:
            raise VisualProviderError("visual_ratio_unsupported", "麦克视频不支持当前画面比例")
        if not self.capability.minimum_seconds <= duration <= self.capability.maximum_seconds:
            raise VisualProviderError("visual_duration_unsupported", "麦克视频镜头时长必须为 4 至 15 秒")
        if model != self.default_model:
            raise VisualProviderError("visual_model_unsupported", "短剧当前固定使用麦克视频")
        if not 1 <= len(refs) <= 5:
            raise VisualProviderError("visual_reference_count_invalid", "麦克视频每个镜头需要 1 至 5 张人物参考图")
        normalized_refs = []
        for item in refs:
            if isinstance(item, str):
                normalized_refs.append({"url": item.strip(), "file": ""})
            elif isinstance(item, dict):
                normalized_refs.append({
                    "url": str(item.get("url") or "").strip(),
                    "file": str(item.get("file") or "").strip(),
                    "character_key": str(item.get("character_key") or "").strip(),
                    "name": str(item.get("name") or "").strip(),
                })
            else:
                raise VisualProviderError("visual_reference_invalid", "角色标准图格式不正确")
            if not normalized_refs[-1]["url"] and not normalized_refs[-1]["file"]:
                raise VisualProviderError("visual_reference_required", "角色标准图尚未准备完成")
            # Validate local files during free preflight. Keep compact paths in
            # storage and only encode the bytes when the provider is submitted.
            self._reference_value(normalized_refs[-1])
        return {
            "provider": self.name,
            "prompt": prompt,
            "ratio": ratio,
            "resolution": "2k",
            "duration_seconds": duration,
            "requested_duration_seconds": duration,
            "model": self.default_model,
            "reference_images": normalized_refs,
        }

    def prepare_job(self, request):
        candidate = self._claim_key()
        if not candidate or not candidate.get("secret"):
            raise VisualProviderError(
                "provider_not_configured", "没有可用的 MiniMax 开放平台 API Key"
            )
        from content_domains import provider_keys, video_minimax_h3

        try:
            video_minimax_h3.check_credentials(candidate["secret"])
        except video_minimax_h3.MiniMaxCredentialRejected as error:
            provider_keys.set_health(candidate["id"], False, error=str(error))
            raise VisualProviderError("provider_not_configured", str(error)) from error
        prepared = dict(request or {})
        prepared["_provider_key_id"] = str(candidate["id"])
        prepared["_minimax_origin"] = video_minimax_h3.new_task_origin()
        return prepared

    def create_job(self, request):
        if not self.configured:
            raise VisualProviderError(
                "provider_not_configured", "尚未配置 MiniMax 开放平台 API Key"
            )
        key_id = str((request or {}).get("_provider_key_id") or "").strip()
        payload = self.validate_request(request)
        candidate = self._bound_key(key_id) if key_id else self._claim_key()
        if not candidate or not candidate.get("secret"):
            raise VisualProviderError("provider_not_configured", "没有可用的 MiniMax 开放平台 API Key")
        from content_domains import video_minimax_h3

        try:
            task_origin = video_minimax_h3.origin_from_payload(request)
            refs = [self._reference_value(item) for item in payload["reference_images"]]
            body = video_minimax_h3.build_request(
                payload["prompt"], refs, payload["ratio"],
                payload["duration_seconds"], payload["resolution"],
            )
            created = video_minimax_h3._request_json(
                video_minimax_h3._opener(), "POST", "/v2/video_generation",
                body, timeout=120, api_key=candidate["secret"],
                api_base=video_minimax_h3.api_base_for_origin(task_origin),
            )
        except video_minimax_h3.MiniMaxCredentialRejected as error:
            raise VisualProviderError("provider_not_configured", str(error)) from error
        except video_minimax_h3.MiniMaxRejected as error:
            raise VisualProviderError("provider_submit_rejected", str(error)) from error
        except video_minimax_h3.CreateOutcomeUnknown as error:
            raise VisualProviderError("provider_submit_unknown", str(error), submitted=True) from error
        task_id = str((created or {}).get("task_id") or "").strip()
        if not task_id:
            raise VisualProviderError(
                "provider_job_id_missing", "MiniMax 已接受请求但未返回任务 ID", submitted=True
            )
        return {
            "provider_job_id": self._encode_job_id(
                candidate["id"], task_id, task_origin,
            ),
            "raw": {
                "task_id": task_id,
                "provider_key_id": candidate["id"],
                "task_origin": task_origin,
            },
        }

    def bind_reconciled_job_id(self, provider_job_id, request):
        from content_domains import video_minimax_h3

        task_id = str(provider_job_id or "").strip()
        if not task_id:
            raise VisualProviderError(
                "provider_job_id_invalid", "MiniMax 上游任务 ID 无效", submitted=True,
            )
        key_id = str((request or {}).get("_provider_key_id") or "").strip()
        if not key_id:
            raise VisualProviderError(
                "provider_key_binding_missing",
                "MiniMax 提交记录缺少已绑定的 API Key，无法安全恢复",
                submitted=True,
            )
        try:
            origin = video_minimax_h3.origin_from_payload(request)
        except video_minimax_h3.MiniMaxOriginUnknown:
            origin = self._historical_origin()
        return self._encode_job_id(
            key_id, task_id, origin,
        )

    def get_job(self, provider_job_id):
        key_id, task_id, origin = self._decode_job_id(provider_job_id)
        candidate = self._bound_key(key_id)
        if not candidate or not candidate.get("secret"):
            raise VisualProviderError("provider_key_unavailable", "MiniMax 任务绑定的 API Key 不可用", submitted=True)
        from content_domains import video_minimax_h3

        try:
            data = video_minimax_h3.query_task(
                task_id, candidate["secret"],
                api_base=video_minimax_h3.api_base_for_origin(origin),
            )
        except Exception as error:
            raise VisualProviderError("provider_poll_failed", "查询 MiniMax 任务失败", submitted=True) from error
        task = (data or {}).get("task") or {}
        status = str(task.get("status") or "unknown").strip().lower()
        result_url = ""
        if status == "succeeded":
            content = task.get("content") or {}
            if isinstance(content, dict):
                result_url = str(content.get("url") or "").strip()
        normalized = {
            "preparing": "queued", "queueing": "queued", "processing": "running",
            "running": "running", "failed": "failed", "cancelled": "failed",
        }.get(status, status)
        failure = None
        if normalized == "failed":
            raw_error = (
                task.get("error") or task.get("message")
                or task.get("error_message") or task.get("fail_reason")
                or (data or {}).get("error") or (data or {}).get("base_resp")
            )
            if isinstance(raw_error, dict):
                message = str(
                    raw_error.get("message") or raw_error.get("detail")
                    or raw_error.get("error_msg") or raw_error.get("status_msg")
                    or raw_error.get("reason") or ""
                ).strip()
                code = str(
                    raw_error.get("code") or raw_error.get("error_code")
                    or raw_error.get("status_code") or ""
                ).strip()
            else:
                message = str(raw_error or "").strip()
                code = ""
            failure = {
                "code": code[:120],
                "message": video_minimax_h3._safe(
                    message, api_key=candidate["secret"], limit=500,
                ),
            }
        return {
            "status": normalized,
            "result_url": result_url,
            "failure": failure,
            "raw": data or {},
        }

    def fetch_result(self, provider_job_id, result_url):
        if not str(result_url or "").strip():
            raise VisualProviderError("provider_result_missing", "MiniMax 尚未返回成片地址", submitted=True)
        from content_domains import video, video_minimax_h3

        try:
            relative = video._download_video_file_direct(
                result_url,
                prefix="short_drama_minimax_h3",
                allowed_hosts=video_minimax_h3.RESULT_HOSTS,
                max_bytes=video_minimax_h3.RESULT_MAX_BYTES,
            )
        except Exception as error:
            raise VisualProviderError(
                "provider_result_download_failed",
                "MiniMax 已完成生成，但下载结果失败，可使用原任务 ID 重拉",
                submitted=True,
            ) from error
        return {
            "provider_job_id": str(provider_job_id),
            "file": relative,
            "url": "/api/gen/file/" + relative,
        }
