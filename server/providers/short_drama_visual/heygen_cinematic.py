"""HeyGen cinematic adapter used by the standalone short-drama PoC."""

import os
import urllib.parse

from .base import ShotVisualCapability, ShotVisualProvider, VisualProviderError


class HeyGenCinematicShotProvider(ShotVisualProvider):
    name = "heygen_cinematic"

    @property
    def capability(self):
        return ShotVisualCapability(
            provider=self.name,
            ratios=("16:9", "9:16", "1:1"),
            minimum_seconds=1,
            maximum_seconds=30,
            supports_cancel=False,
            supports_result_refetch=True,
        )

    @property
    def configured(self):
        return bool(str(os.getenv("HEYGEN_API_KEY") or "").strip())

    def validate_request(self, request):
        if not isinstance(request, dict):
            raise VisualProviderError("visual_request_invalid", "镜头请求格式不正确")
        avatar_id = str(request.get("provider_avatar_id") or "").strip()
        prompt = str(request.get("prompt") or "").strip()
        ratio = str(request.get("ratio") or "").strip()
        try:
            duration = int(request.get("duration_seconds") or 0)
        except (TypeError, ValueError) as error:
            raise VisualProviderError(
                "visual_duration_invalid", "镜头时长必须是整数秒"
            ) from error
        if not avatar_id:
            raise VisualProviderError(
                "provider_avatar_required", "镜头角色尚未绑定 Provider 形象"
            )
        if not prompt:
            raise VisualProviderError(
                "visual_prompt_required", "镜头缺少可执行的画面提示词"
            )
        if ratio not in self.capability.ratios:
            raise VisualProviderError(
                "visual_ratio_unsupported", "Provider 不支持当前画幅"
            )
        if not self.capability.minimum_seconds <= duration <= self.capability.maximum_seconds:
            raise VisualProviderError(
                "visual_duration_unsupported", "镜头时长超出 Provider 支持范围"
            )
        return {
            "provider": self.name,
            "provider_avatar_id": avatar_id,
            "prompt": prompt,
            "ratio": ratio,
            "resolution": "720p",
            "duration_seconds": duration,
        }

    def create_job(self, request):
        if not self.configured:
            raise VisualProviderError(
                "provider_not_configured", "HeyGen API Key 尚未配置"
            )
        payload = self.validate_request(request)
        from content_domains import video

        try:
            result = video._heygen_retry_429(
                lambda: video._heygen_create_cinematic_video(
                    payload["provider_avatar_id"],
                    "",
                    payload["ratio"],
                    payload["resolution"],
                    payload["duration_seconds"],
                    payload["prompt"],
                    enhance_prompt=False,
                ),
                "short-drama visual PoC",
            )
        except Exception as error:
            # A lost POST response is financially ambiguous; never auto-submit.
            raise VisualProviderError(
                "provider_submit_unknown",
                "Provider 提交结果不确定，必须先人工对账，禁止自动重试",
                submitted=True,
            ) from error
        # The shared cinematic helper returns the provider video id as a plain
        # string. Also accept a mapping so this adapter stays compatible if the
        # helper later exposes the complete provider response.
        if isinstance(result, str):
            provider_job_id = result.strip()
            raw_result = {"video_id": provider_job_id}
        elif isinstance(result, dict):
            provider_job_id = str(
                result.get("video_id") or result.get("id") or ""
            ).strip()
            raw_result = result
        else:
            provider_job_id = ""
            raw_result = {"unexpected_result_type": type(result).__name__}
        if not provider_job_id:
            raise VisualProviderError(
                "provider_job_id_missing",
                "Provider 已接受请求但没有返回任务 ID，必须人工对账",
                submitted=True,
            )
        return {"provider_job_id": provider_job_id, "raw": raw_result}

    def get_job(self, provider_job_id):
        from content_domains import video

        try:
            data = video._heygen_request_json(
                "GET",
                "/videos/" + urllib.parse.quote(str(provider_job_id)),
                timeout=90,
            )
        except Exception as error:
            raise VisualProviderError(
                "provider_poll_failed", "查询 Provider 任务失败", submitted=True
            ) from error
        info = (data or {}).get("data") or {}
        return {
            "status": str(info.get("status") or "unknown").lower(),
            "result_url": str(
                info.get("video_url")
                or info.get("url")
                or info.get("video_url_caption")
                or ""
            ),
            "raw": info,
        }

    def fetch_result(self, provider_job_id, result_url):
        if not str(result_url or "").strip():
            raise VisualProviderError(
                "provider_result_missing", "Provider 尚未返回成片地址", submitted=True
            )
        from content_domains import video

        try:
            relative = video._download_video_file_direct(
                result_url, prefix="short_drama_visual"
            )
        except Exception as error:
            raise VisualProviderError(
                "provider_result_download_failed",
                "Provider 已出片，但下载结果失败，可使用原任务 ID 重拉",
                submitted=True,
            ) from error
        return {
            "provider_job_id": str(provider_job_id),
            "file": relative,
            "url": "/api/gen/file/" + relative,
        }
