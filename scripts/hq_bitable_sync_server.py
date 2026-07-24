#!/usr/bin/env python3
"""黄雀日报 → 飞书多维表格（每 5 分钟同步当天，00:10 终算前一天）。

用法: python3 hq_bitable_sync_server.py [YYYY-MM-DD]
凭据: /home/ubuntu/.hq_feishu.env
"""
import collections
import datetime
import json
from pathlib import Path
import sqlite3
import sys
import urllib.request

HERE = Path(__file__).resolve().parent
for candidate in (HERE.parent / "server", HERE / "content-api"):
    if candidate.is_dir():
        sys.path.insert(0, str(candidate))

from func_names import func_name

APP = "WUBRbMYN0awpMhsYuQNcLCW1nyc"
TABLE = "tbl1XM63cxFuXSha"
DB = "/home/ubuntu/content-api/content_jobs.db"
ENV = "/home/ubuntu/.hq_feishu.env"
BASE = "https://open.feishu.cn/open-apis"


def env():
    values = {}
    with open(ENV, encoding="utf-8") as file:
        for line in file:
            if "=" in line:
                key, value = line.strip().split("=", 1)
                values[key] = value
    return values


def token():
    values = env()
    request = urllib.request.Request(
        BASE + "/auth/v3/tenant_access_token/internal",
        data=json.dumps({
            "app_id": values["FEISHU_APP_ID"],
            "app_secret": values["FEISHU_APP_SECRET"],
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    data = json.load(urllib.request.urlopen(request, timeout=15))
    if data.get("code") != 0:
        raise RuntimeError("取 token 失败: %s" % data)
    return data["tenant_access_token"]


def api(access_token, method, path, body=None):
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Bearer " + access_token,
            "Content-Type": "application/json",
        },
        method=method,
    )
    data = json.load(urllib.request.urlopen(request, timeout=30))
    if data.get("code") != 0:
        raise RuntimeError("%s %s 失败: %s" % (method, path, str(data)[:200]))
    return data


def channel_name(kind, payload):
    if kind == "xiaole_video":
        channel = {
            "grok": "果肉",
            "micro": "豆姐",
            "omni": "欧米",
        }.get(str(payload.get("channel") or "").lower(), "小乐其他")
        return "视频", channel
    if kind == "video":
        mode = str(payload.get("mode") or "").lower()
        if mode == "motion":
            line = str(payload.get("line") or "1")
            return "视频", "动作模仿·线一HeyGen" if line == "1" else "动作模仿·线二Wave"
        if mode in ("text", "audio"):
            return "视频", "口播-" + ("文案" if mode == "text" else "音频")
        return "视频", "视频其他"
    if kind == "tryon":
        line2 = str(payload.get("line") or "1") == "2"
        # ponytail: 旧看板按这个历史名称筛选；迁移完 12 张渠道图后再改成 RunningHub。
        return "视频", "换装·线二Wave" if line2 else "换装·线一HeyGen"
    if kind == "cinematic":
        mode = str(payload.get("cine_mode") or "motion")
        return "视频", {"duo": "双人动作模仿", "open": "开放式生成"}.get(mode, "动作模仿")
    if kind == "avatar":
        return "视频", ""
    if kind == "sora_video":
        return "视频", "OpenAI官方"
    if kind == "image":
        engine = str(payload.get("model") or payload.get("provider") or "openai").lower()
        channel = {
            "nb2": "NanoBanana2",
            "pro": "Pro高清",
            "zelong": "泽龙Ai",
            "zelong2": "泽龙2号池",
            "xiaole": "果肉生图",
            "seedream": "Seedream",
            "openai": "OpenAI官方",
        }.get(engine, engine)
        return "作图", channel
    return "", ""


def summarize(job_rows):
    grouped = collections.defaultdict(lambda: [0, 0, 0.0])
    for kind, raw_payload, status, cost, refunded in job_rows:
        try:
            payload = json.loads(raw_payload or "{}")
        except Exception:
            payload = {}
        category, channel = channel_name(kind, payload)
        feature = func_name(kind, payload)
        cell = grouped[(category, feature, channel)]
        cell[1 if status == "error" else 0] += 1
        if not (status == "error" and refunded):
            try:
                cell[2] += float(cost or 0)
            except (TypeError, ValueError):
                pass
    return [
        [category, feature, channel, ok, failed, round(cost)]
        for (category, feature, channel), (ok, failed, cost) in sorted(grouped.items())
    ]


def aggregate(day):
    start = datetime.datetime.strptime(day, "%Y-%m-%d")
    end = start + datetime.timedelta(days=1)
    with sqlite3.connect(DB) as database:
        rows = database.execute(
            "SELECT kind,payload,status,COALESCE(cost,0),COALESCE(refunded,0) "
            "FROM jobs WHERE created_at>=? AND created_at<? "
            "AND status IN ('done','error') AND COALESCE(deleted,0)=0",
            (start.timestamp(), end.timestamp()),
        ).fetchall()
    return summarize(rows)


def replace_day(access_token, day, rows):
    timestamp = int(datetime.datetime.strptime(day, "%Y-%m-%d").timestamp() * 1000)
    record_ids, page = [], ""
    while True:
        data = api(
            access_token,
            "GET",
            f"/bitable/v1/apps/{APP}/tables/{TABLE}/records?page_size=500"
            + (f"&page_token={page}" if page else ""),
        )["data"]
        for item in data.get("items") or []:
            if (item.get("fields") or {}).get("日期") == timestamp:
                record_ids.append(item["record_id"])
        if not data.get("has_more"):
            break
        page = data.get("page_token", "")
    if record_ids:
        api(
            access_token,
            "POST",
            f"/bitable/v1/apps/{APP}/tables/{TABLE}/records/batch_delete",
            {"records": record_ids},
        )

    records = []
    for category, feature, channel, ok, failed, cost in rows:
        fields = {
            "日期": timestamp,
            "功能": feature,
            "成功": ok,
            "失败": failed,
            "总数": ok + failed,
            "成功率": round(ok / (ok + failed), 4),
            "点数消耗": cost,
        }
        if category:
            fields["大类"] = category
        if channel:
            fields["渠道"] = channel
        records.append({"fields": fields})
    if records:
        api(
            access_token,
            "POST",
            f"/bitable/v1/apps/{APP}/tables/{TABLE}/records/batch_create",
            {"records": records},
        )
    print(day, "同步", len(records), "条，调用", sum(row[3] + row[4] for row in rows), "次")


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().isoformat()
    replace_day(token(), day, aggregate(day))


if __name__ == "__main__":
    main()
