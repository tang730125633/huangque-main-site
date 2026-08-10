"""Official MiniMax H3 adapter for billable short-drama shots."""

import base64
import json
import urllib.parse

from .base import ShotVisualCapability, ShotVisualProvider, VisualProviderError


MINIMAX_RESULT_HOSTS = {
    "cdn.hailuoai.com",
    "cdn.minimax.chat",
    "file.cdn.minimax.io",
    "filecdn.minimax.chat",
}
MINIMAX_RESULT_MAX_BYTES = 250 * 1024 * 1024


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
    def _encode_job_id(key_id, task_id):
        raw = json.dumps(
            {"key_id": str(key_id or "env"), "task_id": str(task_id)},
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_job_id(provider_job_id):
        value = str(provider_job_id or "").strip()
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            payload = json.loads(raw.decode("utf-8"))
            task_id = str(payload.get("task_id") or "").strip()
            if task_id:
                return str(payload.get("key_id") or "env"), task_id
        except Exception:
            pass
        return "env", value

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
        except Exception as error:
            raise VisualProviderError(
                "provider_key_read_failed",
                "MiniMax 任务绑定的 API Key 暂时无法读取，请稍后重试",
                submitted=True,
            ) from error

    @staticmethod
    def _reference_value(item):
        if isinstance(item, str):
            value = item.strip()
            relative = ""
        else:
            value = str((item or {}).get("url") or "").strip()
            relative = str((item or {}).get("file") or "").strip()
        if value.startswith(("http://", "https://", "data:image/")):
            return value
        if not relative:
            raise VisualProviderError(
                "visual_reference_required", "麦克视频缺少可用的人物参考图"
            )
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
            from content_domains.video_minimax_h3 import _image_item

            return _image_item(value)["image_url"]["url"]
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
        if len(refs) > 5:
            raise VisualProviderError("visual_reference_count_invalid", "麦克视频每个镜头最多使用 5 张参考图")
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
            "resolution": "768p",
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
            refs = [self._reference_value(item) for item in payload["reference_images"]]
            body = video_minimax_h3.build_request(
                payload["prompt"], refs, payload["ratio"],
                payload["duration_seconds"], "768P",
            )
            created = video_minimax_h3._request_json(
                video_minimax_h3._opener(), "POST", "/v2/video_generation",
                body, timeout=120, api_key=candidate["secret"],
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
            "provider_job_id": self._encode_job_id(candidate["id"], task_id),
            "raw": {"task_id": task_id, "provider_key_id": candidate["id"]},
        }

    def get_job(self, provider_job_id):
        key_id, task_id = self._decode_job_id(provider_job_id)
        candidate = self._bound_key(key_id)
        if not candidate or not candidate.get("secret"):
            raise VisualProviderError("provider_key_unavailable", "MiniMax 任务绑定的 API Key 不可用", submitted=True)
        from content_domains import video_minimax_h3

        try:
            data = video_minimax_h3.query_task(task_id, candidate["secret"])
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
        return {"status": normalized, "result_url": result_url, "raw": data or {}}

    def fetch_result(self, provider_job_id, result_url):
        if not str(result_url or "").strip():
            raise VisualProviderError("provider_result_missing", "MiniMax 尚未返回成片地址", submitted=True)
        from content_domains import video

        try:
            relative = video._download_video_file_direct(
                result_url,
                prefix="short_drama_minimax_h3",
                allowed_hosts=MINIMAX_RESULT_HOSTS,
                max_bytes=MINIMAX_RESULT_MAX_BYTES,
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
