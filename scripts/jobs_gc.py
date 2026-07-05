#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄雀 jobs.db 瘦身 GC（#191）。

终态(done/error)任务的输入 base64（image_data/clothes_data/person_video_data/
audio_data/reference_video_data/image/mask 等）生成完就无用(产出已落 COS/result)，
但整段塞在 jobs.payload 里永久留存 → DB 无界膨胀(实测 573MB)。

本脚本：把终态行 payload 里超长(>2000字符)的字符串字段替换为占位标记(保留元数据
如 mode/provider/model/prompt)，回收后 VACUUM。幂等——已清过的行占位很短、不再命中。
cron 每天跑，DB 体积稳定。

用法：python3 jobs_gc.py [--dry] [--no-vacuum]
"""
import os, sys, sqlite3, json, time

DB = "/home/ubuntu/content-api/content_jobs.db"
BIG = 2000   # 超此长度的字符串字段视为 base64 媒体、清理


def main():
    dry = "--dry" in sys.argv
    do_vacuum = "--no-vacuum" not in sys.argv
    before = os.path.getsize(DB)
    c = sqlite3.connect(DB, timeout=60)
    rows = c.execute("SELECT id, payload FROM jobs WHERE status IN ('done','error')").fetchall()
    cleaned = 0
    freed = 0
    for jid, pl in rows:
        if not pl:
            continue
        try:
            d = json.loads(pl)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        changed = False
        for k, v in list(d.items()):
            if isinstance(v, str) and len(v) > BIG:
                freed += len(v)
                d[k] = "[cleared:%dKB]" % (len(v) // 1024)
                changed = True
        if changed:
            cleaned += 1
            if not dry:
                c.execute("UPDATE jobs SET payload=? WHERE id=?",
                          (json.dumps(d, ensure_ascii=False), jid))
    if not dry:
        c.commit()
    print("%s 清理 %d 行，回收 payload %.0f MB" % ("[dry]" if dry else "[done]", cleaned, freed / 1024 / 1024))
    if not dry and do_vacuum and cleaned:
        t0 = time.time()
        c.execute("VACUUM")
        print("VACUUM 完成 %.1fs" % (time.time() - t0))
    c.close()
    after = os.path.getsize(DB)
    print("DB: %.0f MB → %.0f MB" % (before / 1024 / 1024, after / 1024 / 1024))


if __name__ == "__main__":
    main()
