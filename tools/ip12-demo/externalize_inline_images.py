#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 showcase HTML 里内联的 base64 图片抽成独立文件。

## 为什么需要它

IP12 的 showcase 页面是一份 24MB 的单文件 HTML，其中 23.9MB 是 76 张图片被
base64 内联在 HTML 里。结果是：

* 每次打开/刷新，浏览器都要解析 24MB 文本、再解码 76 张图；
* 网络其实不慢（服务器开了 gzip，条件请求也正确返回 304），
  慢在浏览器解析这个巨型单文件上。

生成这份 showcase 的工具不在本仓库（是桌面端内部工具），所以这里做**后处理**：
把内联图片抽成 `media/inline-XXX.<ext>`，HTML 里改成普通 `<img src>` /
CSS `url()` 引用。HTML 从 24MB 降到几十 KB，图片走独立缓存。

## 用法

    python tools/ip12-demo/externalize_inline_images.py \
        --html  <showcase.html> \
        --out   <输出目录> \
        [--media-dir media] \
        [--report <json 路径>]

输出目录会包含改写后的 `showcase.html` 与抽出来的图片。`--report` 会写出
每个文件的相对路径、字节数、SHA-256，便于按发布清单的格式登记。
"""
import argparse
import base64
import hashlib
import json
import pathlib
import re
import sys

# 只处理这几种图片类型；其它 data: URI（字体等）保持原样，避免改动语义
IMAGE_RE = re.compile(r'data:(image/(?:jpeg|jpg|png|gif|webp));base64,([A-Za-z0-9+/=]+)')
EXT = {'image/jpeg': '.jpg', 'image/jpg': '.jpg', 'image/png': '.png',
       'image/gif': '.gif', 'image/webp': '.webp'}


def externalize(html, out_dir, media_dir='media'):
    """把内联图片写成文件，返回 (新 HTML, 文件清单)。"""
    media_dir_path = out_dir / media_dir
    media_dir_path.mkdir(parents=True, exist_ok=True)
    seen = {}          # base64 内容 → 相对路径，重复图片只落一份
    files = []
    counter = [0]

    def replace(match):
        mime, blob = match.group(1), match.group(2)
        if blob in seen:
            return seen[blob]
        counter[0] += 1
        try:
            raw = base64.b64decode(blob, validate=False)
        except Exception:
            return match.group(0)          # 解不开就原样保留，不破坏页面
        name = 'inline-%03d%s' % (counter[0], EXT.get(mime, '.bin'))
        rel = '%s/%s' % (media_dir, name)
        (out_dir / rel).write_bytes(raw)
        seen[blob] = rel
        files.append({'path': rel, 'size': len(raw),
                      'sha256': hashlib.sha256(raw).hexdigest()})
        return rel

    new_html = IMAGE_RE.sub(replace, html)
    return new_html, files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--html', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--media-dir', default='media')
    ap.add_argument('--report', default='')
    args = ap.parse_args()

    src = pathlib.Path(args.html)
    if not src.is_file():
        print('找不到输入文件: %s' % src, file=sys.stderr)
        return 2
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    raw = src.read_bytes()
    html = raw.decode('utf-8', 'replace')
    before = len(raw)

    new_html, files = externalize(html, out_dir, args.media_dir)

    target = out_dir / 'showcase.html'
    target.write_text(new_html, encoding='utf-8')
    after = target.stat().st_size

    print('  输入 : %s  (%d 字节)' % (src, before))
    print('  输出 : %s  (%d 字节，%.1f%% of 原大小)' % (target, after, 100.0 * after / max(before, 1)))
    print('  抽出 : %d 个图片文件，共 %d 字节' % (len(files), sum(f['size'] for f in files)))
    print('  省下 : %.1f MB' % ((before - after) / 1024 / 1024))

    if args.report:
        pathlib.Path(args.report).write_text(
            json.dumps({'html': {'path': 'showcase.html', 'size': after,
                                 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()},
                        'files': files}, ensure_ascii=False, indent=2),
            encoding='utf-8')
        print('  清单 : %s' % args.report)
    return 0


if __name__ == '__main__':
    sys.exit(main())
