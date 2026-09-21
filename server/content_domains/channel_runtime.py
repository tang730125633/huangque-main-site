"""Bounded managed-channel execution and tests.

付费 POST 仍然不重发：只有供应商**明确未受理**（提交前失败，或明确拒绝受理）时才按优先级
切换到下一候选渠道；超时、限流、5xx、结果未知以及已受理之后的失败一律收敛为终态、不重发。

存储层：执行记录与调度排期统一经 ``channel_manager`` / ``channel_store`` 分发；
``HQ_CHANNEL_STORE=postgres`` 时本模块的读写走 ``channel_store``（routing schema），
SQLite 路径与行为逐字节不变。本模块不再直连 ``channel_manager.db()``。
"""
import base64
import io
import json
import os
import pathlib
import time
import threading
import urllib.parse
import uuid
from contextlib import closing

from . import channel_manager as store, channel_store, runtime_observability as trace, safe_http


class OutcomeUnknown(RuntimeError):
    pass


class CheckUnsupported(ValueError):
    pass


class ProviderError(RuntimeError):
    definitive_rejection = True


class SafeChannelFailover(RuntimeError):
    """A paid provider request is known not to have been accepted."""


class PreSubmissionFailure(SafeChannelFailover):
    pass


class SubmissionRejected(SafeChannelFailover, ProviderError):
    pass


SAFE_POST_REJECTION_STATUSES = {400, 401, 402, 403, 404, 405, 413, 415, 422}
# Gemini 原生协议同步返回 inlineData 图片，2K/4K 的 base64 体量远超 safe_http 的 8MB 默认上限。
GEMINI_RESPONSE_MAX_BYTES = 24 * 1024 * 1024


def validate_payload(cfg, payload):
    if any(k.startswith('_short_drama') for k in payload) or payload.get('short_drama_binding'):
        raise ValueError('短剧绑定任务不支持通用渠道映射，请使用专用渠道配置')
    # 换装/换背景按图像素材工作，没有提示词；提示词规则只对文本驱动型协议生效。
    if cfg['adapter'] not in ('wavespeed_tryon', 'cosyvoice_tts',
                               'heygen_mcp_video', 'heygen_mcp_cinematic'):
        if not str(payload.get('prompt') or '').strip():
            raise ValueError('提示词不能为空')
        if len(str(payload['prompt'])) > 7000:
            raise ValueError('提示词不得超过7000字')
    if int(payload.get('n') or payload.get('count') or 1) != 1:
        raise ValueError('受管理渠道当前每个任务只生成一个产物')
    if payload.get('operation') not in {None,'','generate'}:
        raise ValueError('受管理视频渠道暂不支持编辑操作')
    refs = payload.get('reference_images') or ([] if not payload.get('image') else [payload['image']])
    if not isinstance(refs, list):
        raise ValueError('参考图必须为数组')
    if cfg['adapter'] == 'openai_image':
        spec = cfg.get('parameters') or {}
        mask_present = bool(payload.get('mask'))
        if spec.get('reference_max', 0) >= 1:
            if len(refs) > spec['reference_max']:
                raise ValueError('参考图数量超出当前模型支持范围')
        elif refs or payload.get('images') or payload.get('mode') in {'img2img','edit'}:
            raise ValueError('此适配器仅支持文生图，参考图编辑请使用已有专用渠道')
        if mask_present:
            if not spec.get('mask'):
                raise ValueError('此渠道未启用局部修图（蒙版）')
            if len(refs) != 1:
                raise ValueError('局部修图需要恰好 1 张参考图')
    if cfg['adapter'] == 'gemini_image':
        from .banana_provider import MAX_REFERENCE_IMAGES, MAX_TOTAL_REFERENCE_BYTES, RATIOS
        if payload.get('video') or payload.get('reference_videos') or payload.get('mask'):
            raise ValueError('Gemini 官方生图不支持视频参考或蒙版')
        if len(refs) > MAX_REFERENCE_IMAGES:
            raise ValueError('Gemini 官方生图参考图最多 14 张')
        normalized = _gemini_references(refs)
        if sum(item['bytes'] for item in normalized) > MAX_TOTAL_REFERENCE_BYTES:
            raise ValueError('Gemini 官方生图参考图总大小超过 48MB')
        if str(payload.get('ratio') or '1:1') not in RATIOS:
            raise ValueError('Gemini 官方生图不支持该画面比例')
    if cfg['adapter'] == 'wavespeed_tryon':
        # 线路二换装：人物图 + 衣服图，不支持换背景（与 validate_tryon_payload 同一套规则）。
        from . import video as video_domain
        person = str(payload.get('person_image_data') or payload.get('image_data') or '').strip()
        clothes = str(payload.get('clothes_data') or '').strip()
        if not person or not video_domain._is_valid_data_url(person, video_domain.VALID_IMAGE_MIMES):
            raise ValueError('线路二换装请上传有效的人物照片')
        if not clothes or not video_domain._is_valid_data_url(clothes, video_domain.VALID_IMAGE_MIMES):
            raise ValueError('请上传衣服图')
        if str(payload.get('background_data') or '').strip():
            raise ValueError('线路二不支持换背景，请改用线路一')
        if payload.get('person_video_data'):
            raise ValueError('线路二换装不接受人物视频，请改用线路一')
        seconds = video_domain._tryon_seconds(payload, '2')
        if not 5 <= seconds <= 15:
            raise ValueError('线路二换装时长须为5～15秒')
    if cfg['adapter'] == 'cosyvoice_tts':
        # 配音按文本工作：量纲与原厂 audio.py 一致（speed 0.5~2 / pitch -12~12 / volume -50~100）。
        text = str(payload.get('text') or '').strip()
        if not text:
            raise ValueError('配音文案不能为空')
        if len(text) > 5000:
            raise ValueError('配音文案不得超过 5000 字')
        if not str(payload.get('voice') or '').strip():
            raise ValueError('请选择配音音色')
        try:
            rate = float(payload.get('speed') or 1.0)
        except (TypeError, ValueError):
            raise ValueError('语速必须是数字')
        if not 0.5 <= rate <= 2.0:
            raise ValueError('语速需在 0.5~2.0 之间')
        try:
            pitch = int(payload.get('pitch') or 0)
        except (TypeError, ValueError):
            raise ValueError('音调必须是整数')
        if not -12 <= pitch <= 12:
            raise ValueError('音调需在 -12~12 之间')
        try:
            volume = int(payload.get('volume') or 0)
        except (TypeError, ValueError):
            raise ValueError('音量必须是整数')
        if not -50 <= volume <= 100:
            raise ValueError('音量需在 -50~100 之间')
        if refs:
            raise ValueError('配音不需要参考图')
        return
    if cfg['adapter'] in ('heygen_mcp_video', 'heygen_mcp_cinematic'):
        # HeyGen 专用流程：文案或音频 + 形象绑定，与图片/视频的 prompt 契约不同。
        # 凭据不在这里校验：capture 阶段的 cfg 不含密钥（密钥在保险箱，执行时才注入），
        # 真正读取凭据时（_heygen_credential_file）才检查并给出明确报错。
        if cfg['adapter'] == 'heygen_mcp_cinematic':
            cine_mode = str(payload.get('cine_mode') or '').strip().lower()
            if cine_mode and cine_mode not in ('motion', 'open'):
                raise ValueError('电影化身只支持 motion / open 两种模式')
        else:
            mode = str(payload.get('mode') or 'text').strip().lower()
            if mode not in ('text', 'audio', 'lipsync'):
                raise ValueError('数字人口播只支持 text / audio / lipsync 三种模式')
            if mode in ('text', 'audio') and not str(
                    payload.get('script') or payload.get('text') or '').strip():
                raise ValueError('数字人口播需要文案或音频')
        if refs:
            raise ValueError('HeyGen 流程不接受通用参考图参数，请使用形象/素材绑定')
        return
    if cfg['adapter'] == 'sora_video':
        from . import video as video_domain
        if len(refs) > 1:
            raise ValueError('Sora 适配器只支持 1 张首帧参考图')
        if payload.get('operation') not in {None, '', 'generate'}:
            raise ValueError('Sora 适配器不支持视频编辑操作')
        ratio = str(payload.get('ratio') or '9:16')
        if ratio not in video_domain.SORA_RATIOS:
            raise ValueError('Sora 适配器支持'+'、'.join(sorted(video_domain.SORA_RATIOS))+'比例')
        duration = int(payload.get('duration') or payload.get('seconds') or 4)
        if duration not in video_domain.SORA_SECONDS:
            raise ValueError('Sora 时长只能是'+'、'.join(str(x) for x in sorted(video_domain.SORA_SECONDS))+'秒')
    if cfg['adapter'] == 'minimax_h3':
        from .video_minimax_h3 import build_request
        build_request(payload['prompt'], refs, payload.get('ratio') or '9:16', payload.get('duration') or 5, payload.get('resolution') or '2K')
    if cfg['adapter'] == 'xai_video' and (len(refs)>1 or payload.get('video') or payload.get('reference_videos')):
        raise ValueError('此 Grok 适配器支持文生视频或单参考图，暂不支持视频编辑及多图')
    if cfg['adapter'] == 'xai_video':
        if str(payload.get('resolution') or '720p').lower() not in ({'720p'} | ({'1080p'} if cfg['model']=='grok-imagine-video-1.5' else set())):
            raise ValueError('此 Grok 模型不支持该分辨率，不能将更高分辨率任务降级执行')
        if not 1<=int(payload.get('duration') or 5)<=15:
            raise ValueError('Grok 视频时长须为1～15秒')
        if payload.get('ratio','9:16') not in {'9:16','16:9','1:1'}:
            raise ValueError('此适配器支持9:16、16:9或1:1比例')
        if cfg['model']=='grok-imagine-video-1.5' and not refs:
            raise ValueError('Grok 1.5需要参考图')
    if cfg['adapter'] == 'lechuang_image':
        if payload.get('video') or payload.get('reference_videos'):
            raise ValueError('乐创生图不支持视频参考')
        if payload.get('mask'):
            # 乐创协议的 input 只有 text_to_image / image_to_image，没有蒙版字段：
            # 静默忽略会让用户以为做了局部修改，实际是整图重画。
            raise ValueError('乐创生图协议不支持蒙版局部修改，请改用支持图片编辑（/images/edits）的渠道')
        if len(refs) > 9:
            raise ValueError('乐创生图参考图最多 9 张')
        if payload.get('background') not in (None,'','auto','opaque','transparent'):
            raise ValueError('乐创生图背景仅支持 auto/opaque/transparent')
    if cfg['adapter'] == 'lechuang_video':
        if payload.get('mask'):
            raise ValueError('乐创视频协议不支持蒙版')
        ref_max = 1 if cfg['model'] == 'grok-video-1.5' else 7
        if payload.get('video') or payload.get('reference_videos'):
            raise ValueError('乐创视频适配器暂不支持视频参考')
        if len(refs) > ref_max:
            raise ValueError('该模型参考图最多 %d 张' % ref_max)
        if str(payload.get('resolution') or '720p').lower() not in {'480p','720p','1080p'}:
            raise ValueError('乐创视频仅支持 480p/720p/1080p')
        if not 1<=int(payload.get('duration') or 5)<=15:
            raise ValueError('Grok 视频时长须为1～15秒')
        if payload.get('ratio','9:16') not in {'9:16','16:9','1:1'}:
            raise ValueError('乐创视频支持9:16、16:9或1:1比例')


def request(cfg, method, path, body=None, extra_headers=None, files=None):
    headers = (
        {'x-goog-api-key': cfg['secret']}
        if cfg['adapter'] == 'gemini_image'
        else {'Authorization': 'Bearer ' + cfg['secret']}
    )
    if extra_headers:
        headers.update(extra_headers)
    # Gemini 原生协议把图片塞在 inlineData 里返回：2K/4K 的 base64 远超 safe_http 的 8MB 默认上限，
    # 用默认值会把已计费的成品判成「供应商 HTTP 200」丢掉。给这个适配器单独放宽到 24MB。
    headroom = {'max_bytes': GEMINI_RESPONSE_MAX_BYTES} if cfg['adapter'] == 'gemini_image' else {}
    try:
        url = cfg['base_url']+'/'+path.lstrip('/')
        if files is not None:
            return safe_http.request_multipart_json(
                method, url, fields=body or {}, files=files,
                headers=headers,
                timeout=cfg['timeout'], proxy=cfg.get('proxy') or '',
            )
        return safe_http.request_json(
            method, url, body=body,
            headers=headers,
            timeout=cfg['timeout'], proxy=cfg.get('proxy') or '',
            **headroom,
        )
    except safe_http.SafeHttpError as exc:
        if method == 'POST' and (exc.status in {0,408,409,425,429} or exc.status>=500):
            raise OutcomeUnknown('提交结果未知，禁止自动重发') from None
        if method == 'POST' and exc.status in SAFE_POST_REJECTION_STATUSES:
            raise SubmissionRejected('供应商明确拒绝提交：HTTP %s' % exc.status) from None
        if exc.status:
            raise ProviderError('供应商 HTTP %s' % exc.status) from None
        if method == 'POST':
            raise OutcomeUnknown('提交响应未确认，禁止自动重发') from None
        raise RuntimeError('网络或响应异常：'+type(exc).__name__) from None
    except (OSError, ValueError) as exc:
        if method == 'POST':
            raise OutcomeUnknown('提交响应未确认，禁止自动重发') from None
        raise RuntimeError('网络或响应异常：'+type(exc).__name__) from None


def _download(cfg, url, headers=None):
    return safe_http.request_bytes(
        'GET', url, timeout=cfg['timeout'], max_bytes=100*1024*1024,
        headers=headers,
        proxy=cfg.get('proxy') or '',
    )


_LECHUANG_VIDEO_SIZES = {
    '9:16': {'480p': '480x848', '720p': '720x1280', '1080p': '1080x1920'},
    '16:9': {'480p': '848x480', '720p': '1280x720', '1080p': '1920x1080'},
    '1:1': {'480p': '480x480', '720p': '720x720', '1080p': '1080x1080'},
}


def _lechuang_refs(refs):
    """乐创 ReferenceMediaRequest：HTTP(S) 传 url，其余按 data_url。"""
    return [
        {'type': 'url' if str(r or '').startswith(('http://', 'https://')) else 'data_url', 'value': str(r)}
        for r in refs
    ]


def _lechuang_image_resolution(size):
    """按乐创 gpt-image-2 规格由像素尺寸推导 1k/2k/4k。"""
    try:
        edge = max(int(x) for x in str(size or '').split('x'))
    except ValueError:
        return '1k'
    return '1k' if edge <= 1280 else ('2k' if edge <= 2304 else '4k')


def _lechuang_media_target(cfg, value):
    """乐创产物地址归一化：相对路径与同源绝对地址需要密钥，第三方 CDN 不带密钥。"""
    value = str(value or '').strip()
    base = urllib.parse.urlsplit(cfg['base_url'])
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme in ('http', 'https'):
        return value, bool(parsed.netloc) and parsed.netloc == base.netloc
    if value.startswith('/'):
        # 平台返回根相对路径（自带 /api/v1 前缀），须接在源站 origin 上。
        return base.scheme + '://' + base.netloc + value, True
    return cfg['base_url'].rstrip('/') + '/' + value.lstrip('/'), True


def _download_lechuang(cfg, value):
    url, needs_auth = _lechuang_media_target(cfg, value)
    headers = {'Authorization': 'Bearer ' + cfg['secret']} if needs_auth else None
    return _download(cfg, url, headers=headers)


def _decode_data_url(value, field):
    value = str(value or '').strip()
    if not value.startswith('data:') or ',' not in value:
        raise ValueError(field + ' 必须为 data URL')
    meta, b64 = value.split(',', 1)
    if ';base64' not in meta:
        raise ValueError(field + ' 必须为 base64 编码')
    cleaned = ''.join(b64.split())
    cleaned += '=' * ((4 - len(cleaned) % 4) % 4)
    try:
        raw = base64.b64decode(cleaned, validate=True)
    except Exception:
        raise ValueError(field + ' 必须是合法 base64') from None
    if not raw:
        raise ValueError(field + ' 内容为空')
    if len(raw) > 10 * 1024 * 1024:
        raise ValueError(field + ' 超过 10MB，请先压缩图片再上传')
    return raw


def _gemini_references(refs):
    """Normalize references with the shared Nano Banana validator."""
    from .banana_provider import _validated_reference
    return [_validated_reference(value, index) for index, value in enumerate(refs)]


def _gemini_image_bytes(response):
    parts = ((response.get('candidates') or [{}])[0].get('content') or {}).get('parts') or []
    inline = next((part.get('inlineData') for part in parts if part.get('inlineData')), None)
    if not inline or not inline.get('data'):
        detail = (response.get('error') or {}).get('message') or '供应商未返回图片'
        raise ValueError('Gemini 未返回图片：' + str(detail)[:180])
    try:
        return base64.b64decode(inline['data'], validate=True)
    except Exception:
        raise ValueError('Gemini 返回的图片数据无效') from None


def _edits_parts(cfg, payload, refs, placeholder=False):
    spec = cfg.get('parameters') or {}
    fields = {'prompt': str(payload.get('prompt') or ''), 'size': str(payload.get('size') or '1024x1024'), 'n': '1'}
    for key, default in (('output_format', 'png'), ('background', 'opaque')):
        if spec and key in {f['key'] for f in spec.get('fields', [])}:
            fields[key] = str(payload.get(key) or default)
    files = []
    if placeholder:
        files.append(('image', 'reference.png', b''))
        if payload.get('mask'):
            files.append(('mask', 'mask.png', b''))
    else:
        files.append(('image', 'image.png', _decode_data_url(refs[0], '参考图')))
        if payload.get('mask'):
            files.append(('mask', 'mask.png', _decode_data_url(payload['mask'], '蒙版')))
    return fields, files


def build_generation_request(cfg, payload, preview=False):
    from .channel_parameters import image_request
    validate_payload(cfg,payload)
    refs = payload.get('reference_images') or ([] if not payload.get('image') else [payload['image']])
    adapter=cfg['adapter']
    if adapter == 'openai_image':
        if refs:
            body, files = _edits_parts(cfg, payload, refs, placeholder=preview)
            return '/images/edits', body, files
        return '/images/generations', image_request(cfg,payload), None
    if adapter == 'gemini_image':
        from .banana_provider import IMAGE_SIZES, MODELS, build_request_body
        from .channel_parameters import SIZES
        references = _gemini_references(refs)
        ratio = str(payload.get('ratio') or SIZES.get(str(payload.get('size') or ''), '1:1'))
        quality = str(payload.get('quality') or 'std').lower()
        if quality not in {'std','hd'}:
            raise ValueError('Gemini 官方生图清晰度仅支持 std/hd')
        model_key = next((key for key,value in MODELS.items() if value == cfg['model']), '')
        if not model_key:
            raise ValueError('Gemini 官方生图仅支持已登记的 Nano Banana 模型')
        image_size = IMAGE_SIZES[model_key][quality]
        body = build_request_body(payload['prompt'], ratio, references, image_size)
        path = '/v1beta/models/' + urllib.parse.quote(cfg['model'], safe='.-_') + ':generateContent'
        return path, body, None
    if adapter == 'minimax_h3':
        from .video_minimax_h3 import build_request
        body = build_request(payload['prompt'], refs, payload.get('ratio') or '9:16',payload.get('duration') or 5,payload.get('resolution') or '2K')
        return '/v2/video_generation', body, None
    if adapter == 'lechuang_image':
        from .channel_parameters import SIZES
        size = str(payload.get('size') or '1024x1024')
        image_input = {
            'prompt': payload['prompt'],
            'mode': 'image_to_image' if refs else 'text_to_image',
            'n': 1,
            'resolution': _lechuang_image_resolution(size),
            'aspect_ratio': SIZES.get(size, '1:1'),
            'quality': str(payload.get('quality') or 'auto'),
            'background': str(payload.get('background') or 'auto'),
        }
        if refs:
            image_input['reference_images'] = _lechuang_refs(refs)
        return '/generations', {'model': cfg['model'], 'input': image_input}, None
    if adapter == 'lechuang_video':
        ratio = str(payload.get('ratio') or '9:16')
        resolution = str(payload.get('resolution') or '720p').lower()
        video_input = {
            'prompt': payload['prompt'],
            'mode': 'image_to_video' if refs else 'text_to_video',
            'size': _LECHUANG_VIDEO_SIZES.get(ratio, {}).get(resolution, '720x1280'),
            'resolution': resolution,
            'duration_seconds': int(payload.get('duration') or 5),
        }
        if refs:
            video_input['reference_images'] = _lechuang_refs(refs)
        return '/generations', {'model': cfg['model'], 'input': video_input}, None
    if adapter == 'sora_video':
        # 预览/日志用：真实请求由 _generate_sora → video_openai 构造（与原厂一致）。
        size = payload.get('size') or _sora_size(cfg, payload)
        body = {'model': cfg['model'], 'prompt': payload['prompt'],
                'seconds': int(payload.get('duration') or payload.get('seconds') or 4),
                'size': size}
        if refs:
            body['input_reference'] = '<上传的首帧图片>'
        return '/videos', body, None
    body = {'model':cfg['model'],'prompt':payload['prompt'],'duration':int(payload.get('duration') or 5),'aspect_ratio':payload.get('ratio') or '9:16','resolution':str(payload.get('resolution') or '720p')}
    if refs:
        body['image'] = {'url':refs[0]}
    return '/videos/generations', body, None


def _tryon_material_resolver(local_rel):
    """素材转公网直链：生产走 COS；隔离测试可用环境变量替换（生产不设该变量）。"""
    base = os.environ.get('HQ_TRYON_MATERIAL_BASE')
    if base:
        return base.rstrip('/') + '/' + str(local_rel).replace('/', '-')
    from . import wavespeed
    return wavespeed._material_url(local_rel)


def _heygen_credential_file(cfg):
    """把渠道的 MCP 凭据落到渠道专属文件，供 video.heygen_credential_scope 读取。

    支持两种写法：直接给凭据 JSON，或给一个已存在的凭据文件路径。
    文件必须是 600（原厂读取时会校验），且按渠道 ID 固定路径 —— OAuth 刷新后
    原厂会回写同一个文件，因此下一次任务能直接用新 token。
    """
    from . import core
    raw = str(cfg.get('secret') or '').strip()
    if not raw:
        raise PreSubmissionFailure('HeyGen 渠道缺少 MCP 凭据')
    base = core.OUT_DIR.parent / 'heygen_credentials'
    base.mkdir(parents=True, exist_ok=True)
    path = base / (str(cfg['id']) + '.json')
    if raw.lstrip().startswith('{'):
        try:
            cred = json.loads(raw)
        except ValueError as exc:
            raise PreSubmissionFailure('HeyGen MCP 凭据不是合法 JSON') from exc
        path.write_text(json.dumps(cred, ensure_ascii=False), encoding='utf-8')
    else:
        src = pathlib.Path(raw)
        if not src.is_file():
            raise PreSubmissionFailure('HeyGen MCP 凭据既不是 JSON 也不是可读文件')
        path.write_text(src.read_text(encoding='utf-8'), encoding='utf-8')
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _generate_heygen(cfg, payload, rid, job_id, metadata):
    """托管 HeyGen：复用原厂 gen_video / gen_cinematic，只换凭据。

    原厂那一整条链路（形象绑定、素材上传、轮询、下载、成品核验）完全不动；
    渠道差异只有「用哪个 MCP 账号」，通过 heygen_credential_scope 注入。
    """
    from . import video as video_domain, core as core_domain
    kind = 'cinematic' if cfg['adapter'] == 'heygen_mcp_cinematic' else 'video'
    handler = video_domain.HANDLERS.get(kind)
    if not handler:
        raise PreSubmissionFailure('HeyGen 原厂入口不可用：%s' % kind)
    cred = _heygen_credential_file(cfg)
    store.finish(rid, 'running', '提交 HeyGen')
    scoped = dict(payload)
    scoped['_job_id'] = scoped.get('_job_id') or job_id
    with video_domain.heygen_credential_scope(str(cred)):
        result = trace.call(job_id, 'provider_submit',
                            lambda: handler(scoped), **metadata)
    if not isinstance(result, dict) or not (result.get('video_file') or result.get('file')):
        raise ProviderError('HeyGen 未返回成品')
    trace.record(job_id, 'artifact', 'passed', **metadata)
    binding = payload.get('_channel_binding') or {}
    out = dict(result)
    out.setdefault('type', 'video')
    out.setdefault('file', out.get('video_file'))
    out.update({
        'provider': cfg['name'], 'model': cfg['model'],
        'channel_id': cfg['id'], 'channel_version': cfg['version'],
        'operation_id': binding.get('operation_id'),
        'mapping_revision': binding.get('mapping_revision'),
        'invocation_source': binding.get('invocation_source'),
    })
    return out


def _generate_tts(cfg, payload, rid, job_id, metadata):
    """托管配音：复用原厂 cosyvoice.synth，只把渠道自己的 Key 与接入点注入进去。

    一个渠道 = 一套 DashScope 凭据（+ 可选自定义接入点），拖动即切换配音账号；
    音色、语速、音调、音量仍由任务参数决定，与渠道解耦。
    """
    from . import audio as audio_domain, cosyvoice, core
    text = str(payload.get('text') or '').strip()
    voice = str(payload.get('voice') or '').strip()
    speed = float(payload.get('speed') or 1.0)
    pitch = int(payload.get('pitch') or 0)
    volume = int(payload.get('volume') or 0)
    try:
        ws_host = urllib.parse.urlsplit(str(cfg.get('base_url') or '')).hostname or None
    except ValueError:
        ws_host = None
    store.finish(rid, 'running', '提交配音供应商')
    data = trace.call(
        job_id, 'provider_submit',
        lambda: cosyvoice.synth(
            voice, text, rate=speed,
            # 量纲换算与原厂 audio.py 的 CosyVoice 分支保持一致
            pitch=max(0.5, min(2.0, 1.0 + pitch / 24.0)),
            volume=max(0, min(100, 50 + volume // 2)),
            api_key=cfg['secret'], ws_host=ws_host),
        **metadata)
    if not data:
        raise ProviderError('配音未返回音频数据')
    fn = 'audio/aud_%d.mp3' % int(time.time() * 1000)
    out = audio_domain._out_path(fn)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    if not out.is_file() or out.stat().st_size <= 0:
        raise ProviderError('配音成品文件写入失败')
    trace.record(job_id, 'artifact', 'passed', **metadata)
    url = core.public_url(fn, 'audio/mpeg')
    binding = payload.get('_channel_binding') or {}
    return {'type': 'audio', 'status': 'done', 'file': fn, 'audio_file': fn,
            'url': url, 'audio_url': url, 'files': [fn], 'urls': [url], 'count': 1,
            'voice': voice, 'text': text,
            'provider': cfg['name'], 'model': cfg['model'],
            'channel_id': cfg['id'], 'channel_version': cfg['version'],
            'operation_id': binding.get('operation_id'),
            'mapping_revision': binding.get('mapping_revision'),
            'invocation_source': binding.get('invocation_source')}


def _generate_tryon_ws(cfg, payload, rid, job_id, metadata):
    """托管换装（线路二）：复用原厂 wavespeed.generate_tryon。

    两次素材上传与提交/轮询/下载全部由原厂模块完成，这里只把渠道自己的
    Key 与线路注入进去（wavespeed 已支持 credential / base_url）。
    """
    from . import video as video_domain, wavespeed, core
    person = video_domain._save_data_file(
        payload.get('person_image_data') or payload.get('image_data'),
        'tryon_person_img', ['.jpg', '.jpeg', '.png', '.webp'])
    if not person:
        raise PreSubmissionFailure('线路二换装请上传人物照片')
    clothes = video_domain._save_data_file(
        payload.get('clothes_data'), 'tryon_cloth', ['.jpg', '.jpeg', '.png', '.webp'])
    if not clothes:
        raise PreSubmissionFailure('请上传衣服图')
    seconds = video_domain._tryon_seconds(payload, '2')
    store.finish(rid, 'running', '提交换装供应商')
    wres = trace.call(
        job_id, 'provider_submit',
        lambda: wavespeed.generate_tryon(
            person, clothes, seconds, job_id=job_id,
            credential=cfg['secret'], base_url=cfg['base_url'],
            material_resolver=_tryon_material_resolver),
        **metadata)
    provider_id = str((wres or {}).get('provider_video_id') or '')
    video_file = (wres or {}).get('video_file')
    if not video_file:
        raise ProviderError('换装未返回成品文件')
    target = core.OUT_DIR / video_file
    if not target.is_file():
        raise ProviderError('换装成品文件不存在：%s' % str(video_file)[:120])
    try:
        import subprocess
        probe = subprocess.run(
            ['ffprobe', '-v', 'error', '-count_frames',
             '-show_entries', 'stream=codec_type,width,height,nb_read_frames',
             '-of', 'json', str(target)],
            capture_output=True, timeout=120, check=True)
        streams = json.loads(probe.stdout).get('streams', [])
        ok = any(s.get('codec_type') == 'video' and int(s.get('width') or 0) > 0
                 and int(s.get('height') or 0) > 0
                 and str(s.get('nb_read_frames') or '').isdigit()
                 and int(s['nb_read_frames']) > 0 for s in streams)
        if probe.stderr or not ok:
            raise ValueError('成品帧解码未通过，不能判定视频有效')
    except Exception:
        raise
    trace.record(job_id, 'artifact', 'passed', provider_task_id=provider_id, **metadata)
    url = core.public_url(video_file, 'video/mp4')
    binding = payload.get('_channel_binding') or {}
    return {'type': 'video', 'status': 'done', 'mode': 'tryon', 'tryon_mode': 'clothes_only',
            'file': video_file, 'video_file': video_file, 'url': url, 'video_url': url,
            'files': [video_file], 'urls': [url], 'count': 1,
            'person_image_file': person, 'clothes_file': clothes,
            'provider': cfg['name'], 'model': cfg['model'], 'request_id': provider_id,
            'channel_id': cfg['id'], 'channel_version': cfg['version'],
            'operation_id': binding.get('operation_id'),
            'mapping_revision': binding.get('mapping_revision'),
            'invocation_source': binding.get('invocation_source')}


def _sora_size(cfg, payload):
    """按原厂同一张 SORA_SIZE_MAP 推导尺寸，保证与原线路行为一致。"""
    from . import video as video_domain
    model = str(payload.get('model') or cfg['model'])
    ratio = str(payload.get('ratio') or '9:16')
    resolution = str(payload.get('resolution') or '720p')
    return str(payload.get('size') or video_domain.SORA_SIZE_MAP.get((model, resolution, ratio)) or '')


def _generate_sora(cfg, payload, rid, job_id, metadata, refs):
    """托管 Sora 任务：复用原厂 video_openai 客户端，不重写提交/轮询/下载。

    video_openai.generate / download_content / resume 都已接受注入的 api_key 与 api_base，
    因此渠道自己的凭据与线路直接生效；非幂等 POST 仍只发一次。
    """
    from . import video as video_domain, video_openai, core
    model = str(payload.get('model') or cfg['model'])
    seconds = int(payload.get('duration') or payload.get('seconds') or 4)
    size = _sora_size(cfg, payload)
    if not size:
        raise PreSubmissionFailure('Sora 无法根据模型/分辨率/比例确定尺寸')
    input_reference = None
    if refs:
        try:
            input_reference = video_domain._prepare_sora_input_reference(refs[0], size)
        except ValueError as exc:
            raise PreSubmissionFailure(str(exc)[:200]) from None
    store.finish(rid, 'running', '提交 Sora')
    rendered = trace.call(
        job_id, 'provider_submit',
        lambda: video_openai.generate(
            model, str(payload.get('provider_prompt') or payload['prompt']), seconds, size,
            job_id=job_id, api_key=cfg['secret'], api_base=cfg['base_url'],
            input_reference=input_reference),
        **metadata)
    provider_id = str((rendered or {}).get('video_id') or '').strip()
    if not provider_id:
        raise ProviderError('Sora 已完成但缺少 video_id')
    store.finish(rid, 'running', '下载生成视频', provider_id)
    filename = 'channel_' + uuid.uuid4().hex + '.mp4'
    target = core.OUT_DIR / filename
    trace.call(job_id, 'download',
               lambda: video_openai.download_content(
                   provider_id, target, api_key=cfg['secret'], api_base=cfg['base_url']),
               provider_task_id=provider_id, **metadata)
    try:
        import subprocess
        probe = subprocess.run(
            ['ffprobe', '-v', 'error', '-count_frames',
             '-show_entries', 'stream=codec_type,width,height,nb_read_frames',
             '-of', 'json', str(target)],
            capture_output=True, timeout=120, check=True)
        streams = json.loads(probe.stdout).get('streams', [])
        ok = any(s.get('codec_type') == 'video' and int(s.get('width') or 0) > 0
                 and int(s.get('height') or 0) > 0
                 and str(s.get('nb_read_frames') or '').isdigit()
                 and int(s['nb_read_frames']) > 0 for s in streams)
        if probe.stderr or not ok:
            raise ValueError('成品帧解码未通过，不能判定视频有效')
    except Exception:
        target.unlink(missing_ok=True)
        raise
    trace.record(job_id, 'artifact', 'passed', provider_task_id=provider_id, **metadata)
    url = core.public_url(filename, 'video/mp4')
    binding = payload.get('_channel_binding') or {}
    return {'type': 'video', 'file': filename, 'url': url, 'files': [filename],
            'urls': [url], 'count': 1, 'mode': 'generate',
            'provider': cfg['name'], 'model': cfg['model'], 'request_id': provider_id,
            'channel_id': cfg['id'], 'channel_version': cfg['version'],
            'operation_id': binding.get('operation_id'),
            'mapping_revision': binding.get('mapping_revision'),
            'invocation_source': binding.get('invocation_source')}


# 有专用执行器的适配器：请求体由各自的 _generate_* 构造，不走通用 build_generation_request。
_BESPOKE_ADAPTERS = ('sora_video', 'wavespeed_tryon', 'cosyvoice_tts',
                     'heygen_mcp_video', 'heygen_mcp_cinematic')


def generate(cfg, payload, rid, job_id):
    from .channel_parameters import apply, image_request
    try:
        payload,_=apply(cfg,payload,required=False)
        validate_payload(cfg, payload)
        refs = payload.get('reference_images') or ([] if not payload.get('image') else [payload['image']])
        adapter = cfg['adapter']
        is_lechuang = adapter in ('lechuang_image', 'lechuang_video')
        metadata = dict(provider=cfg['name'],model=cfg['model'],host=urllib.parse.urlsplit(cfg['base_url']).hostname,
                        transport='proxy' if cfg.get('proxy') else 'direct')
        # 复用原厂执行器的适配器自己构造请求（Sora→video_openai、换装→wavespeed、配音→cosyvoice），
        # 不能先走通用 HTTP 路径的 build_generation_request —— 那条按图片/视频契约读
        # payload['prompt']，而配音根本没有 prompt 字段，会在提交前抛 KeyError。
        path=body=files=None
        if adapter not in _BESPOKE_ADAPTERS:
            path,body,files=build_generation_request(cfg,payload)
    except (ValueError, KeyError) as exc:
        # KeyError 同属「提交前的载荷缺陷」：供应商未被调用，必须按可安全切换处理，
        # 而不是让它逃到外层被记成 failed（那会误报成渠道故障）。
        raise PreSubmissionFailure(str(exc)[:200]) from None
    trace.record(job_id,'route','recorded',**metadata)
    store.finish(rid,'running','提交供应商')
    # Sora 走原厂 video_openai（已支持注入 Key/base）；换装走原厂 wavespeed；其余走通用 HTTP 路径。
    if cfg['adapter'] == 'sora_video':
        return _generate_sora(cfg, payload, rid, job_id, metadata, refs)
    if cfg['adapter'] == 'wavespeed_tryon':
        return _generate_tryon_ws(cfg, payload, rid, job_id, metadata)
    if cfg['adapter'] == 'cosyvoice_tts':
        return _generate_tts(cfg, payload, rid, job_id, metadata)
    if cfg['adapter'] in ('heygen_mcp_video', 'heygen_mcp_cinematic'):
        return _generate_heygen(cfg, payload, rid, job_id, metadata)
    # 乐创付费创建请求要求 8-128 字符幂等键；run id 为 32 位 hex，天然幂等。
    extra_headers = {'Idempotency-Key': str(rid)} if is_lechuang else None
    result = trace.call(job_id,'provider_submit',lambda: request(cfg,'POST',path,body,extra_headers=extra_headers,files=files),**metadata)
    provider_id = str(result.get('task_id') or result.get('request_id') or '')
    if is_lechuang:
        envelope = (result or {}).get('data') if isinstance(result, dict) else {}
        provider_id = str(envelope.get('request_id') or '')
        if not provider_id:
            err = envelope.get('error') or {}
            if err.get('message'):
                raise ProviderError('乐创拒绝提交：' + str(err['message'])[:180])
            raise OutcomeUnknown('供应商未返回工单号，禁止自动重发')
        store.finish(rid,'running','乐创已接单',provider_id)
        trace.record(job_id,'provider_accepted','recorded',provider_task_id=provider_id,**metadata)
        _provider_submitted(provider_id)   # 记入终止台账，终止后仍可对账上游工单
        deadline = time.monotonic()+1800
        while time.monotonic()<deadline:
            _termination_check()   # 已提交的任务：停止等待并保留工单号；上游可能仍会跑完
            time.sleep(5)
            store.finish(rid,'running','查询乐创生成状态',provider_id)
            try:
                polled = trace.call(job_id,'provider_query',
                                    lambda:request(cfg,'GET','/generations/'+urllib.parse.quote(provider_id,safe='')),
                                    provider_task_id=provider_id,**metadata)
            except Exception:
                raise OutcomeUnknown('生成查询中断，供应商工单状态尚未确认') from None
            envelope = (polled or {}).get('data') if isinstance(polled, dict) else {}
            status = str(envelope.get('status') or '')
            if status in {'failed','error','expired','cancelled'}:
                err = envelope.get('error') or {}
                raise ProviderError('乐创明确报告生成失败：' + str(err.get('message') or status)[:160])
            if status == 'succeeded':
                break
        else:
            raise OutcomeUnknown('等待生成超时，供应商工单保留，请人工核查')
        out = envelope.get('output') or {}
        if adapter == 'lechuang_image':
            images = out.get('images') or []
            if not images or not images[0].get('url'):
                raise RuntimeError('乐创返回空图片产物')
            store.finish(rid,'running','下载生成图片',provider_id)
            raw = trace.call(job_id,'download',lambda:_download_lechuang(cfg,images[0]['url']),**metadata)
        else:
            videos = out.get('videos') or []
            if not videos:
                raise RuntimeError('乐创返回空视频产物')
            content = videos[0].get('content_url') or videos[0].get('url') or ''
            if not content:
                raise RuntimeError('乐创视频缺少下载地址')
            store.finish(rid,'running','下载生成视频',provider_id)
            raw = trace.call(job_id,'download',lambda:_download_lechuang(cfg,content),**metadata)
    elif adapter not in ('openai_image', 'gemini_image'):
        if not provider_id:
            raise OutcomeUnknown('供应商未返回工单号，禁止自动重发')
        store.finish(rid,'running','供应商已接单',provider_id)
        trace.record(job_id,'provider_accepted','recorded',provider_task_id=provider_id,**metadata)
        _provider_submitted(provider_id)
        deadline = time.monotonic()+1800
        while time.monotonic()<deadline:
            _termination_check()
            time.sleep(5)
            path = ('/v2/query/video_generation/' if adapter=='minimax_h3' else '/videos/')+urllib.parse.quote(provider_id,safe='')
            store.finish(rid,'running','查询供应商生成状态',provider_id)
            try:
                result = trace.call(job_id,'provider_query',lambda:request(cfg,'GET',path),provider_task_id=provider_id,**metadata)
            except Exception:
                raise OutcomeUnknown('生成查询中断，供应商工单状态尚未确认') from None
            task = result.get('task',{}) if adapter=='minimax_h3' else result
            status = task.get('status')
            if status in {'failed','expired','error'}:
                raise ProviderError('供应商明确报告生成失败')
            if status in {'succeeded','done'}:
                break
        else:
            raise OutcomeUnknown('等待生成超时，供应商工单保留，请人工核查')
        url = (task.get('content') or {}).get('url') if adapter=='minimax_h3' else (task.get('video') or {}).get('url')
        store.finish(rid,'running','下载生成视频',provider_id)
        raw = trace.call(job_id,'download',lambda:_download(cfg,url),**metadata)
    elif adapter == 'gemini_image':
        raw = _gemini_image_bytes(result)
    else:
        data = result.get('data') or []
        if not data:
            raise RuntimeError('供应商返回空产物')
        raw = base64.b64decode(data[0]['b64_json'],validate=True) if data[0].get('b64_json') else _download(cfg,data[0].get('url'))
    from . import core
    media = 'image' if adapter in ('openai_image','gemini_image','lechuang_image') else 'video'
    output_format=payload.get('output_format','png') if cfg.get('parameters') else 'png'
    filename = 'channel_'+uuid.uuid4().hex+('.'+output_format if media=='image' else '.mp4')
    target = core.OUT_DIR / filename
    store.finish(rid,'running','核验成品',provider_id)
    try:
        if media=='image':
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as img:
                img.load()
                if cfg.get('parameters') and payload.get('size') and tuple(map(int,payload['size'].split('x')))!=img.size:
                    raise ValueError('供应商返回图片尺寸与所选尺寸不一致，不能判定交付通过')
                if output_format=='jpeg':img=img.convert('RGB')
                img.save(target,{'png':'PNG','jpeg':'JPEG','webp':'WEBP'}[output_format])
        else:
            import subprocess
            target.write_bytes(raw)
            probe = subprocess.run(['ffprobe','-v','error','-count_frames','-show_entries','stream=codec_type,width,height,nb_read_frames','-of','json',str(target)],capture_output=True,timeout=120,check=True)
            streams=json.loads(probe.stdout).get('streams',[])
            if probe.stderr or not any(s.get('codec_type')=='video' and int(s.get('width') or 0)>0 and int(s.get('height') or 0)>0 and str(s.get('nb_read_frames') or '').isdigit() and int(s['nb_read_frames'])>0 for s in streams):
                raise ValueError('成品帧解码未通过，不能判定视频有效')
        trace.record(job_id,'artifact','passed',provider_task_id=provider_id,**metadata)
        url = core.public_url(filename,'image/'+output_format if media=='image' else 'video/mp4')
        trace.record(job_id,'delivery','unknown',**metadata)
        binding = payload.get('_channel_binding') or {}
        return {'type':media,'file':filename,'url':url,'files':[filename],'urls':[url],'count':1,
                'mode':('text2img' if not refs else 'img2img') if media=='image' else 'generate','provider':cfg['name'],'model':cfg['model'],
                'request_id':provider_id,'channel_id':cfg['id'],'channel_version':cfg['version'],
                'operation_id':binding.get('operation_id'),
                'mapping_revision':binding.get('mapping_revision'),
                'invocation_source':binding.get('invocation_source')}
    except Exception:
        target.unlink(missing_ok=True)
        raise


def _termination_check():
    """管理员终止检查点：无终止作用域时是空操作，只有托管渠道任务会被停手。"""
    from . import task_termination
    task_termination.check()


def _provider_submitted(provider_id):
    if not provider_id:
        return
    from . import task_termination
    task_termination.provider_submitted(provider_id)


def _mark_terminated(rid, detail='管理员终止任务'):
    """终止态独立于失败/未知：不参与渠道健康与告警，也不覆盖真实终态。"""
    if channel_store.enabled():
        return channel_store.mark_terminated(rid, detail)
    with closing(store.db()) as c:
        cur = c.execute(
            "UPDATE runs SET state='terminated',detail=?,updated=? "
            "WHERE id=? AND state NOT IN ('passed','failed','terminated')",
            (detail, time.time(), rid),
        )
        c.commit()
        return cur.rowcount > 0


def execute(rid, payload=None):
    if channel_store.enabled():
        row = channel_store.run_record(rid)
    else:
        with closing(store.db()) as c:
            row = dict(c.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone())
    cfg = store.version(row['channel'],row['version'])
    start = time.monotonic()
    try:
        cfg = store.version(row['channel'],row['version'],True)
        while True:
            _termination_check()   # 排队阶段终止：尚未提交供应商，零费用止损
            if channel_store.enabled():
                started = channel_store.try_start_run(rid, cfg, time.time()-2400)
                if started:
                    break
                if started is False:
                    return None
            else:
                with closing(store.db()) as c:
                    c.execute('BEGIN IMMEDIATE')
                    # Stale runs remain unknown; never resubmit after restart.
                    c.execute("UPDATE runs SET state='unknown',detail='执行进程中断或超时，需人工核查' WHERE state='running' AND updated<?",(time.time()-2400,))
                    active = c.execute("SELECT COUNT(*) FROM runs WHERE channel=? AND state='running'",(cfg['id'],)).fetchone()[0]
                    rate = c.execute("SELECT COUNT(*) FROM events WHERE target=? AND action='runtime.dispatch' AND created>?",(cfg['id'],time.time()-60)).fetchone()[0]
                    own = c.execute('SELECT state FROM runs WHERE id=?',(rid,)).fetchone()[0]
                    if own!='queued':
                        c.commit()
                        return None
                    if active<cfg['concurrency'] and rate<cfg['rpm']:
                        c.execute("UPDATE runs SET state='running',updated=? WHERE id=?",(time.time(),rid))
                        store._audit(c,'runtime.dispatch',cfg['id'],'runtime')
                        c.commit()
                        break
                    c.commit()
            if time.monotonic()-start>120:
                raise PreSubmissionFailure('渠道并发或限流等待超时，尚未提交供应商')
            time.sleep(1)
        _termination_check()       # 拿到闸门后再确认一次，避免终止后仍提交付费请求
        if row['kind']=='connection':
            try:
                safe_http.request_bytes(
                    'HEAD', cfg['base_url'], timeout=cfg['timeout'],
                    max_bytes=1024, proxy=cfg.get('proxy') or '',
                )
            except safe_http.SafeHttpError as exc:
                if not 400 <= exc.status < 500:
                    raise
            result, detail = None,'网络连接可达；不代表鉴权或生成成功'
        elif row['kind']=='auth':
            # 只有「模型列表型」协议能靠 GET /models 独立证明鉴权。
            # 其余协议（配音走 WebSocket、换装/HeyGen 走工作流或 OAuth）没有可用的
            # 鉴权端点：拿不存在的资源去探，只会得到 404 或「模型不在列表里」，
            # 那不是凭据被拒，不能据此判失败。按协议声明的验证要求如实标记不适用。
            from .channel_manager import ADAPTERS as _ADAPTERS
            _spec = _ADAPTERS.get(cfg['adapter']) or {}
            if 'auth' not in (_spec.get('verification') or ('connection', 'auth', 'full')):
                raise CheckUnsupported('此协议无独立鉴权端点，鉴权由完整生成证明')
            result = request(cfg,'GET','/v1beta/models' if cfg['adapter']=='gemini_image' else '/models')
            data = result.get('models') if cfg['adapter']=='gemini_image' else result.get('data')
            if isinstance(data, dict):
                # 乐创统一协议：{code,message,data:{list:[{id,...}]}}
                models = [m.get('id') for m in (data.get('list') or []) if isinstance(m, dict)]
            elif isinstance(data, list):
                models = [m.get('id') or str(m.get('name') or '').removeprefix('models/') for m in data if isinstance(m, dict)]
            else:
                models = []
            if not models or cfg['model'] not in models:
                raise ValueError('模型列表未包含指定模型，不能判定可用')
            detail = '鉴权与模型列表通过；不代表生成成功'
        else:
            result = generate(cfg,payload if payload is not None else cfg['fixture'],rid,row['job_id'] or 'channel-test:'+rid)
            detail = '成品已下载并核验；尚无用户接收证明'
        store.finish(rid,'passed',detail)
        _notify(row,'passed')
        return result
    except Exception as exc:
        from . import task_termination
        if isinstance(exc, task_termination.TaskTerminated):
            # 管理员终止：独立终态，不算渠道故障、不触发渠道告警；退款由终止台账按幂等规则处理。
            _mark_terminated(rid)
            raise
        state = 'blocked' if isinstance(exc,CheckUnsupported) else 'unknown' if isinstance(exc,OutcomeUnknown) else 'failed'
        # Raw provider errors/payloads and secrets never enter public diagnostics.
        if channel_store.enabled():
            phase = channel_store.execution_phase(rid)
        else:
            with closing(store.db()) as c:
                phase = c.execute('SELECT detail FROM runs WHERE id=?',(rid,)).fetchone()[0]
        # SafeChannelFailover 的文案是「为何判定未受理」的唯一证据，必须原样落库给后台看。
        detail = phase+'：'+str(exc)[:200] if isinstance(exc,(ValueError,OutcomeUnknown,ProviderError,SafeChannelFailover)) else (
            phase+'：SafeHttpError HTTP '+str(exc.status) if isinstance(exc, safe_http.SafeHttpError)
            else phase+'：'+type(exc).__name__)
        if cfg.get('secret'):
            detail = detail.replace(cfg['secret'],'[隐藏]')
        if row['kind'] == 'task' and isinstance(exc, SafeChannelFailover):
            try:
                store.finish_task_failover_safe(rid, detail)
            except ValueError:
                # 记账被拒（状态不允许等）：收敛成明确终态，别让裸 ValueError 逃出去把任务卡死。
                store.finish(rid, 'failed', detail)
                _notify(row, 'failed')
                raise RuntimeError(detail) from exc
            _notify(row, 'failed')
            raise SafeChannelFailover(detail) from None
        store.finish(rid,state,detail)
        _notify(row,state)
        raise RuntimeError(detail) from exc


def _notify(row,state):
    if state not in {'passed','failed','unknown'}:
        return
    if channel_store.enabled():
        action,occurred=channel_store.note_incident(row['channel'],row['kind'],state)
    else:
        with closing(store.db()) as c:
            c.execute('BEGIN IMMEDIATE')
            previous = c.execute('SELECT * FROM channel_incidents WHERE channel=? AND kind=?',(row['channel'],row['kind'])).fetchone()
            if previous and previous['state']==state:
                action,occurred=previous['action'],previous['occurred']
            else:
                action=('channel.recovered' if previous else '') if state=='passed' else 'channel.'+state
                occurred=time.time()
                c.execute('INSERT OR REPLACE INTO channel_incidents VALUES(?,?,?,?,?)',(row['channel'],row['kind'],state,action,occurred))
            c.commit()
    # Re-enqueue the same durable event identity after an interrupted write; INSERT OR IGNORE deduplicates.
    if action:
        trace.enqueue(action,row['channel'],occurred)


def run_task(binding,payload,job_id,job_db=None):
    """托管渠道任务入口；带上任务库句柄后，管理员终止可以在执行循环里安全停手。"""
    evidence = store.task_evidence(job_id)
    snapshot = evidence.get('execution_snapshot') or {}
    if (evidence.get('state') == 'queued'
            and snapshot.get('operation_id') == binding.get('operation_id')
            and int(snapshot.get('mapping_revision') or 0)
            == int(binding.get('mapping_revision') or 0)):
        binding = dict(binding, **snapshot)
    # 候选清单必须取【合并持久化快照之后】的那一份：切换过的任务只有快照里才记着新渠道。
    candidates = list(binding.get('route_candidates') or [{
        key: binding.get(key) for key in store.ROUTE_CANDIDATE_FIELDS
    }])
    current_id = binding.get('id')
    start_index = next((index for index, item in enumerate(candidates)
                        if item.get('id') == current_id), None)
    # 排队中的持久化记录若指向另一个渠道（典型：切换后进程挂掉，而映射已改版导致快照合不进来），
    # reserve 会以「该任务已有渠道执行记录」拒绝，任务就在排队与重排之间空转、既不出图也不退款。
    # 这种不一致必须当场收敛成终态：它从未提交供应商，退款是对的。
    durable_queued = (evidence.get('kind') == 'task' and evidence.get('state') == 'queued'
                      and bool(evidence.get('id')))
    stranded = start_index is None or (
        durable_queued and (
            str(evidence.get('channel') or '') != str(candidates[start_index].get('id') or '')
            or int(evidence.get('version') or 0)
            != int(candidates[start_index].get('version') or 0)))
    if stranded:
        if durable_queued:
            store.finish(evidence['id'], 'failed', '已固化渠道与候选清单不一致，请人工核查')
        raise RuntimeError('任务的已固化渠道不在候选清单中，请人工核查')
    cfg = store.version(candidates[start_index]['id'], candidates[start_index]['version'])
    attempt_binding = dict(binding, **candidates[start_index], route_attempt=start_index + 1)
    rid = store.reserve(cfg['id'],'task',str(job_id),cfg,execution_snapshot=attempt_binding)

    def run_once():
        if job_db is None:
            return execute(rid,payload)
        from . import task_termination
        try:
            with task_termination.scope(int(job_id), job_db):
                return execute(rid,payload)
        except task_termination.TaskTerminated:
            _mark_terminated(rid)
            raise

    reason = ''
    skipped = ''
    for index in range(start_index, len(candidates)):
        if index > start_index:
            try:
                store.prepare_task_failover(rid, candidates[index], reason)
            except ValueError as exc:
                # 这一跳被跳过（候选不可用）：不覆盖「为什么离开上一渠道」的原因，
                # 只在全部候选都不可用时作为兜底文案。
                skipped = str(exc)
                continue
        try:
            return run_once()
        except SafeChannelFailover as exc:
            reason = str(exc)
            continue
    raise RuntimeError(reason or skipped or '所有候选渠道均无法安全接单')


def start_test(actor,body):
    kind = body.get('kind')
    if kind not in {'connection','auth','full'}:
        raise ValueError('未知测试类型')
    cid = str(body.get('id') or '')
    cfg = store.version(cid)
    # 协议不支持的检测类型直接拒绝，并且不落库——否则会留下一条误导性的失败记录，
    # 让管理员以为是凭据或渠道坏了。历史遗留下来的这类记录仍然保留、仍然展示，
    # 只是不参与总体判定（见 channel_manager.checks_supported）。
    from .channel_manager import checks_supported, verification_required
    supported = checks_supported(cfg.get('adapter'))
    if kind not in supported:
        label = {'connection': '连接', 'auth': '鉴权', 'full': '完整生成'}.get(kind, kind)
        raise ValueError(
            '此协议不支持「%s」检测，无法生成有效证据（当前协议支持的检测项：%s）'
            % (label, '、'.join({'connection': '连接', 'auth': '鉴权', 'full': '完整生成'}.get(k, k) for k in supported))
        )
    # 提交去重：进行中的同渠道/同版本/同类型任务直接复用，不新建。
    # 重复点击、提交响应丢失后重试都不会重复创建收费任务。
    active = (channel_store.find_active(cid, cfg['version'], kind) if channel_store.enabled()
              else store.find_active(cid, cfg['version'], kind))
    if active:
        return {'run_id': active['id'], 'state': active.get('state') or 'queued',
                'channel': cid, 'version': cfg['version'], 'kind': kind,
                'adapter': cfg.get('adapter'),
                'required': list(verification_required(cfg.get('adapter'))),
                'supported': list(supported), 'deduplicated': True,
                'started': active.get('started')}
    if kind=='full':
        validate_payload(cfg,cfg['fixture'])
    rid = store.reserve(cid,kind)
    if channel_store.enabled():
        channel_store.record_audit('test.'+kind,cid,actor)
    else:
        with closing(store.db()) as c:
            store._audit(c,'test.'+kind,cid,actor)
            c.commit()
    def work():
        try:
            execute(rid)
        except Exception:
            pass
    threading.Thread(target=work,daemon=True).start()
    # 返回足够前端立即显示「排队中」并据此轮询的字段：
    # 任务 ID、渠道 ID、被测配置版本、检测类型、初始状态。
    return {'run_id':rid,'state':'queued','channel':cid,'version':cfg['version'],'started':time.time(),
            'kind':kind,'adapter':cfg.get('adapter'),
            'required':list(verification_required(cfg.get('adapter'))),
            'supported':list(supported)}


def run_state(actor, body):
    """按验证任务 ID 查进度。只读，不触发任何执行。"""
    rid = str(body.get('run_id') or body.get('id') or '').strip()
    if not rid:
        raise ValueError('缺少验证任务 ID')
    row = None
    row = channel_store.run_status(rid) if channel_store.enabled() else store.run_status(rid)
    if not row:
        raise ValueError('找不到这条验证任务')
    state = str(row.get('state') or '')
    phase = str(row.get('phase') or '')
    if not phase and channel_store.enabled():
        phase = str(channel_store.execution_phase(rid) or '')
    terminal = state in ('passed', 'failed', 'unknown', 'terminated')
    label = {'queued': '排队中', 'running': '执行中', 'passed': '已完成', 'failed': '已失败',
             'unknown': '结果未知', 'terminated': '已终止', 'blocked': '条件未满足'}.get(state, state or '未知')
    return {
        'run_id': row.get('id') or rid,
        'channel': row.get('channel'),
        'version': row.get('version'),
        'kind': row.get('kind'),
        'state': state,
        'label': label,
        'phase': phase or '',
        'detail': str(row.get('detail') or '')[:400],
        'updated': row.get('updated'),
        'started': row.get('started'),
        'finished': bool(terminal),
    }


def monitor_cycle():
    now = time.time()
    due = []
    if channel_store.enabled():
        incidents = channel_store.pending_incidents()
    else:
        with closing(store.db()) as c:
            incidents=[dict(r) for r in c.execute("SELECT channel,action,occurred FROM channel_incidents WHERE action!=''")]
    for incident in incidents:
        trace.enqueue(incident['action'],incident['channel'],incident['occurred'])
    if channel_store.enabled():
        due = channel_store.poll_schedule(now)
    else:
        with closing(store.db()) as c:
            c.execute('BEGIN IMMEDIATE')
            for row in c.execute('SELECT s.*,ch.version FROM schedule s JOIN channels ch ON ch.id=s.channel WHERE ch.enabled=1').fetchall():
                cfg = store.version(row['channel'],row['version'])
                for kind, column, enabled in [('connection','light_due',cfg['monitor']),('full','full_due',cfg['daily_test'])]:
                    if enabled and row[column]<=now:
                        due.append((row['channel'],kind))
                        next_at = now+cfg['poll_seconds'] if kind=='connection' else store._next_daily(cfg['daily_hour'],now)
                        c.execute('UPDATE schedule SET '+column+'=? WHERE channel=?',(next_at,row['channel']))
            c.commit()
    for cid,kind in due:
        try:
            start_test('scheduler',{'id':cid,'kind':kind})
        except Exception as exc:
            trace.enqueue('channel.schedule_blocked',cid,int(now))
            if channel_store.enabled():
                channel_store.record_audit('scheduler.blocked.'+kind,cid,'scheduler')
            else:
                with closing(store.db()) as c:
                    store._audit(c,'scheduler.blocked.'+kind,cid,'scheduler')
                    c.commit()
    # Resume only work that has never been submitted. Running/unknown calls are not replayed.
    if channel_store.enabled():
        queued = channel_store.queued_test_run_ids(8)
    else:
        with closing(store.db()) as c:
            queued = [r[0] for r in c.execute("SELECT id FROM runs WHERE state='queued' AND kind!='task' ORDER BY started LIMIT 8")]
    for rid in queued:
        def work(run_id=rid):
            try:
                execute(run_id)
            except Exception:
                pass
        threading.Thread(target=work,daemon=True).start()
