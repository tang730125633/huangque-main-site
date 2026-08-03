# -*- coding: utf-8 -*-
"""腾讯云 COS 成片存储（可选）。

用途：把生成成片（如换装/换背景视频）上传到 COS，前端用 COS/CDN 直链播放下载，
省服务器磁盘、更持久。**所有密钥走环境变量，绝不进 git。** 配置不全时自动禁用、
调用方回退本地链接，因此本模块对现有流程零影响。

需要的环境变量（写在服务器 EnvironmentFile，如 /home/ubuntu/content-api/content.env）：
    COS_SECRET_ID      腾讯云 SecretId
    COS_SECRET_KEY     腾讯云 SecretKey（机密）
    COS_REGION         地域，如 ap-guangzhou
    COS_BUCKET         完整桶名（带 APPID 后缀），如 xxx-1250000000
可选：
    COS_PREFIX         对象键前缀，如 huangque/   （默认空）
    COS_DOMAIN         自定义/CDN 访问域名，如 https://cdn.example.com （默认用 myqcloud 默认域名）
    COS_PUBLIC         桶是否公有读：1(默认)=返回直链；0=私有，返回带签名的临时链接
    COS_SIGN_EXPIRE    私有读签名有效期(秒)，默认 604800(7天)
    COS_DELETE_LOCAL   上传成功后是否删除本地成片：0(默认)=保留(可回退)；1=删除省磁盘

服务器前置：`pip install cos-python-sdk-v5`（提供 qcloud_cos）。
"""
import os

_SECRET_ID   = os.environ.get("COS_SECRET_ID", "").strip()
_SECRET_KEY  = os.environ.get("COS_SECRET_KEY", "").strip()
_REGION      = os.environ.get("COS_REGION", "").strip()
_BUCKET      = os.environ.get("COS_BUCKET", "").strip()
_PREFIX      = os.environ.get("COS_PREFIX", "").strip().strip("/")
_DOMAIN      = os.environ.get("COS_DOMAIN", "").strip().rstrip("/")
_PUBLIC      = os.environ.get("COS_PUBLIC", "1").strip().lower() not in ("0", "false", "no", "")
_SIGN_EXPIRE = int(os.environ.get("COS_SIGN_EXPIRE", "604800") or 604800)
_DELETE_LOCAL = os.environ.get("COS_DELETE_LOCAL", "0").strip().lower() in ("1", "true", "yes")

_client_singleton = None


def enabled():
    """四个必填项齐全才启用 COS。"""
    return bool(_SECRET_ID and _SECRET_KEY and _REGION and _BUCKET)


def delete_local_after_upload():
    return _DELETE_LOCAL


def _client():
    global _client_singleton
    if _client_singleton is None:
        from qcloud_cos import CosConfig, CosS3Client  # 服务器 pip 装；本地/CI 不触发 import
        cfg = CosConfig(Region=_REGION, SecretId=_SECRET_ID, SecretKey=_SECRET_KEY, Scheme="https")
        _client_singleton = CosS3Client(cfg)
    return _client_singleton


def _object_key(rel):
    rel = str(rel).lstrip("/")
    return (_PREFIX + "/" + rel) if _PREFIX else rel


def _url(full_key, private=False):
    if private:
        return _client().get_presigned_url(Method="GET", Bucket=_BUCKET, Key=full_key, Expired=_SIGN_EXPIRE)
    if _DOMAIN:
        return _DOMAIN + "/" + full_key
    if _PUBLIC:
        return "https://%s.cos.%s.myqcloud.com/%s" % (_BUCKET, _REGION, full_key)
    # 私有读：返回带签名的临时链接（会过期，故推荐公有读或挂 CDN）
    return _client().get_presigned_url(Method="GET", Bucket=_BUCKET, Key=full_key, Expired=_SIGN_EXPIRE)


def object_url(rel_key, private=False):
    """为已上传对象生成访问地址；私有对象每次调用都会刷新短期签名。"""
    if not enabled():
        raise RuntimeError("COS 未配置")
    return _url(_object_key(rel_key), private=private)


def upload(local_path, rel_key, content_type=None, private=False, metadata=None):
    """把本地文件上传到 COS，返回可访问 URL。未启用或失败会抛异常，由调用方回退本地。"""
    if not enabled():
        raise RuntimeError("COS 未配置")
    full_key = _object_key(rel_key)
    with open(local_path, "rb") as fp:
        kwargs = {"Bucket": _BUCKET, "Key": full_key, "Body": fp}
        if content_type:
            kwargs["ContentType"] = content_type
        if private:
            kwargs["ACL"] = "private"
        if metadata:
            normalized_metadata = {}
            for key, value in metadata.items():
                name = str(key).strip().lower()
                if not name.startswith("x-cos-meta-"):
                    raise ValueError(
                        "COS 自定义元数据必须以 x-cos-meta- 开头"
                    )
                normalized_metadata[name] = str(value)
            kwargs["Metadata"] = normalized_metadata
        _client().put_object(**kwargs)
    return _url(full_key, private=private)


def head(rel_key):
    """读取对象元数据，用于正式成片上传后的长度与哈希校验。"""
    if not enabled():
        raise RuntimeError("COS 未配置")
    return _client().head_object(Bucket=_BUCKET, Key=_object_key(rel_key))


def put_bytes(data, rel_key, content_type=None, private=False):
    """把内存字节直接上传到 COS，返回可访问 URL。用于转存远程 URL(如采集视频 CDN 直链)。
    未启用或失败会抛异常，由调用方回退原链接。"""
    if not enabled():
        raise RuntimeError("COS 未配置")
    full_key = _object_key(rel_key)
    kwargs = {"Bucket": _BUCKET, "Key": full_key, "Body": data}
    if content_type:
        kwargs["ContentType"] = content_type
    if private:
        kwargs["ACL"] = "private"
    _client().put_object(**kwargs)
    return _url(full_key, private=private)


def put_file(path, rel_key, content_type=None, private=False):
    """从磁盘文件上传到 COS，返回可访问 URL。

    采集视频最大 100MB，用 put_bytes 得先把整段读进内存。put_object 接受文件对象，
    SDK 会分块读，内存恒定。未启用或失败会抛异常，由调用方回退原链接。
    """
    if not enabled():
        raise RuntimeError("COS 未配置")
    full_key = _object_key(rel_key)
    with open(path, "rb") as f:
        kwargs = {"Bucket": _BUCKET, "Key": full_key, "Body": f}
        if content_type:
            kwargs["ContentType"] = content_type
        if private:
            kwargs["ACL"] = "private"
        _client().put_object(**kwargs)
    return _url(full_key, private=private)
