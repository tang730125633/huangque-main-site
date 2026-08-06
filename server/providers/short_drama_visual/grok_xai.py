"""xAI/Grok video adapter for billable short-drama shot generation."""

import base64
import json
import os
import urllib.parse

from .base import ShotVisualCapability, ShotVisualProvider, VisualProviderError


class GrokXaiShotProvider(ShotVisualProvider):
    name = "grok"
    default_model = "grok-imagine-video"

    @property
    def capability(self):
        return ShotVisualCapability(
            provider=self.name,
            ratios=("16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3"),
            minimum_seconds=1,
            maximum_seconds=15,
            supports_cancel=False,
            supports_result_refetch=True,
        )

    @property
    def configured(self):
        try:
            from content_domains import provider_keys

            return provider_keys.has_candidate("xai")
        except Exception:
            return False

    def validate_request(self, request):
        if not isinstance(request, dict):
            raise VisualProviderError(
                "visual_request_invalid", "镜头请求格式不正确"
            )
        prompt = str(request.get("prompt") or "").strip()
        ratio = str(request.get("ratio") or "").strip()
        resolution = str(request.get("resolution") or "720p").strip().lower()
        model = str(
            request.get("model")
            or os.getenv("HQ_SHORT_DRAMA_GROK_MODEL")
            or self.default_model
        ).strip()
        try:
            duration = int(request.get("duration_seconds") or 0)
        except (TypeError, ValueError) as error:
            raise VisualProviderError(
                "visual_duration_invalid", "镜头时长必须是整数秒"
            ) from error
        if not prompt:
            raise VisualProviderError(
                "visual_prompt_required", "镜头缺少可执行的画面提示词"
            )
        if ratio not in self.capability.ratios:
            raise VisualProviderError(
                "visual_ratio_unsupported", "果肉视频不支持当前画幅"
            )
        if resolution not in {"480p", "720p"}:
            raise VisualProviderError(
                "visual_resolution_unsupported", "果肉短剧镜头仅支持 480p 或 720p"
            )
        if not self.capability.minimum_seconds <= duration <= self.capability.maximum_seconds:
            raise VisualProviderError(
                "visual_duration_unsupported", "镜头时长超出果肉视频支持范围"
            )
        if model not in {"grok-imagine-video", "grok-imagine-video-1.5"}:
            raise VisualProviderError(
                "visual_model_unsupported", "果肉视频模型配置不受支持"
            )
        reference_url = str(request.get("reference_image_url") or "").strip()
        reference_file = str(request.get("reference_image_file") or "").strip()
        if model == "grok-imagine-video-1.5" and not (reference_url or reference_file):
            raise VisualProviderError(
                "visual_reference_required", "Grok Video 1.5 需要角色参考图"
            )
        return {
            "provider": self.name,
            "prompt": prompt,
            "ratio": ratio,
            "resolution": resolution,
            "duration_seconds": duration,
            "model": model,
            "reference_image_url": reference_url,
            "reference_image_file": reference_file,
        }

    @staticmethod
    def _encode_job_id(key_id, request_id):
        raw = json.dumps(
            {"key_id": str(key_id or "env"), "request_id": str(request_id)},
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_job_id(provider_job_id):
        value = str(provider_job_id or "").strip()
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            payload = json.loads(raw.decode("utf-8"))
            request_id = str(payload.get("request_id") or "").strip()
            if request_id:
                return str(payload.get("key_id") or "env"), request_id
        except Exception:
            pass
        # Compatibility with early/manual rows that stored the xAI id directly.
        return "env", value

    @staticmethod
    def _claim_key():
        from content_domains import provider_keys

        return provider_keys.claim_candidate("xai")

    @staticmethod
    def _bound_key(key_id):
        try:
            from content_domains import provider_keys

            candidates = provider_keys.candidates(
                "xai", preferred_id=str(key_id or "env")
            )
            return candidates[0] if candidates else None
        except Exception:
            return None

    def prepare_job(self, request):
        """Bind a durable vault key before the caller performs billing."""
        candidate = self._claim_key()
        if not candidate or not candidate.get("secret"):
            raise RuntimeError("果肉视频没有可用的 xAI 密钥")
        prepared = dict(request or {})
        prepared["_provider_key_id"] = str(candidate["id"])
        return prepared

    @staticmethod
    def _reference_url(payload):
        value = str(payload.get("reference_image_url") or "").strip()
        if value.startswith(("http://", "https://")):
            return value
        relative = str(payload.get("reference_image_file") or "").strip()
        if not relative:
            return ""
        from content_domains.core import public_url

        value = str(public_url(relative) or "").strip()
        if not value.startswith(("http://", "https://")):
            raise VisualProviderError(
                "visual_reference_publish_failed",
                "角色参考图无法转换为果肉 Provider 可访问的公网地址",
            )
        return value

    def create_job(self, request):
        if not self.configured:
            raise VisualProviderError(
                "provider_not_configured", "果肉视频尚未配置 XAI_API_KEY"
            )
        prepared_key_id = str((request or {}).get("_provider_key_id") or "").strip()
        payload = self.validate_request(request)
        candidate = (
            self._bound_key(prepared_key_id)
            if prepared_key_id
            else self._claim_key()
        )
        if not candidate or not candidate.get("secret"):
            raise VisualProviderError(
                "provider_not_configured", "果肉视频没有可用的 xAI 密钥"
            )
        body = {
            "model": payload["model"],
            "prompt": payload["prompt"],
            "duration": payload["duration_seconds"],
            "aspect_ratio": payload["ratio"],
            "resolution": payload["resolution"],
        }
        reference_url = self._reference_url(payload)
        if reference_url:
            body["reference_images"] = [{"url": reference_url}]
        from content_domains import video_xai

        try:
            created = video_xai._create(
                video_xai._opener(),
                "/videos/generations",
                body,
                api_key=candidate["secret"],
            )
        except video_xai.XaiCredentialError as error:
            raise VisualProviderError(
                "provider_not_configured", str(error), submitted=False
            ) from error
        except video_xai.XaiCreateRejected as error:
            raise VisualProviderError(
                "provider_submit_rejected", str(error), submitted=False
            ) from error
        except Exception as error:
            # A lost create response is financially ambiguous and must not be retried.
            raise VisualProviderError(
                "provider_submit_unknown",
                "果肉视频提交结果不确定，请先人工核对，禁止自动重试",
                submitted=True,
            ) from error
        request_id = str((created or {}).get("request_id") or "").strip()
        if not request_id:
            raise VisualProviderError(
                "provider_job_id_missing",
                "果肉 Provider 已接受请求但没有返回任务 ID",
                submitted=True,
            )
        return {
            "provider_job_id": self._encode_job_id(candidate["id"], request_id),
            "raw": {"request_id": request_id, "provider_key_id": candidate["id"]},
        }

    def get_job(self, provider_job_id):
        key_id, request_id = self._decode_job_id(provider_job_id)
        candidate = self._bound_key(key_id)
        if not candidate or not candidate.get("secret"):
            raise VisualProviderError(
                "provider_key_unavailable", "果肉任务绑定的 xAI 密钥不可用", submitted=True
            )
        from content_domains import video_xai

        try:
            data = video_xai._request_json(
                video_xai._opener(),
                "GET",
                "/videos/" + urllib.parse.quote(request_id),
                timeout=60,
                api_key=candidate["secret"],
            )
        except Exception as error:
            raise VisualProviderError(
                "provider_poll_failed", "查询果肉视频任务失败", submitted=True
            ) from error
        status = str((data or {}).get("status") or "unknown").lower()
        video = (data or {}).get("video") or {}
        result_url = str(video.get("url") or "") if isinstance(video, dict) else ""
        normalized = {
            "done": "succeeded",
            "expired": "failed",
        }.get(status, status)
        return {"status": normalized, "result_url": result_url, "raw": data or {}}

    def fetch_result(self, provider_job_id, result_url):
        if not str(result_url or "").strip():
            raise VisualProviderError(
                "provider_result_missing", "果肉 Provider 尚未返回成片地址", submitted=True
            )
        from content_domains import video

        try:
            relative = video._download_video_file_direct(
                result_url, prefix="short_drama_grok"
            )
        except Exception as error:
            raise VisualProviderError(
                "provider_result_download_failed",
                "果肉视频已出片，但下载结果失败，可使用原任务 ID 重拉",
                submitted=True,
            ) from error
        return {
            "provider_job_id": str(provider_job_id),
            "file": relative,
            "url": "/api/gen/file/" + relative,
        }
