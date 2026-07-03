#!/usr/bin/env python3
"""极简 MCP stdio server —— 给 OpenClaw agent 两个"爬取按钮"。

为什么存在：让"爬取号"(小冬)不再需要 exec(全键盘)。
- agent 只能调 douyin_crawl(关键词) / douyin_crawl_result(任务号) —— 两个按钮，传不了任意命令；
- 真正的爬取在本服务内部 POST :8090 队列完成；
- LEADGEN 密码由本服务自己读(env/secrets)，agent 永远看不到。
异步两段式(避免长任务超时 / 不阻塞 agent)：
  douyin_crawl(关键词)   → 提交,秒回任务号
  douyin_crawl_result(号) → 查一次,出结果就返回名单,没好就提示稍候
零依赖(仅标准库)。注册：openclaw mcp add douyin --command /usr/bin/python3 --arg <本文件>
"""
import sys, json, os, time, urllib.request, urllib.parse

LEADGEN_URL = os.environ.get("LEADGEN_SERVER", "http://127.0.0.1:8090")
SECRETS = os.environ.get("LEADGEN_SECRETS", "/etc/leadgen-secrets.env")


def _password():
    p = os.environ.get("LEADGEN_PASSWORD")
    if p:
        return p
    try:
        for line in open(SECRETS, encoding="utf-8"):
            if line.strip().startswith("LEADGEN_PASSWORD="):
                return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return ""


def submit_crawl(keyword, count=10):
    keyword = (keyword or "").strip()
    if not keyword:
        return "请提供关键词"
    data = urllib.parse.urlencode({
        "password": _password(), "keyword": keyword,
        "count": max(1, min(int(count or 10), 50)), "mode": "search",
    }).encode()
    try:
        req = urllib.request.Request(LEADGEN_URL + "/api/submit", data=data)
        job = json.loads(urllib.request.urlopen(req, timeout=20).read())
        return "✅ 已开始搜索『%s』，任务号 %s。约 2-3 分钟出结果——请用 douyin_crawl_result(\"%s\") 查询（出结果前可多查几次）。" % (
            keyword, job["job_id"], job["job_id"])
    except Exception as e:
        return "提交搜索失败：%s" % str(e)[:120]


def get_result(job_id):
    job_id = str(job_id).strip()
    # 内部轮询 ~25 秒(5×5s)再返回，避免 agent 狂查；出结果/出错立即返回。
    for i in range(5):
        try:
            r = json.loads(urllib.request.urlopen(LEADGEN_URL + "/api/job/%s" % job_id, timeout=12).read())
        except Exception as e:
            return "查询任务 %s 失败：%s" % (job_id, str(e)[:100])
        st = r.get("status")
        if st == "error":
            return "❌ 任务 %s 失败：%s" % (job_id, str(r.get("error"))[:200])
        if st == "done":
            res = r.get("result")
            if not res:
                return "任务 %s 完成，但没爬到客户（关键词可能太冷 / 登录态需检查）。" % job_id
            s = json.dumps(res, ensure_ascii=False) if not isinstance(res, str) else res
            return "✅ 任务 %s 完成。客户名单：\n%s" % (job_id, s[:3500])
        if i < 4:
            time.sleep(5)
    return "⏳ 任务 %s 还在搜索中。请立即再调用一次 douyin_crawl_result(\"%s\") 继续等待（出结果前多调几次，不要放弃）。" % (job_id, job_id)


TOOLS = [
    {"name": "douyin_crawl",
     "description": "抖音关键词获客【第1步·提交】：输入关键词，提交一个搜视频+扒评论+AI意图过滤的获客任务，立即返回任务号。提交后必须用 douyin_crawl_result 查结果。这是获客的唯一入口。",
     "inputSchema": {"type": "object", "properties": {
         "keyword": {"type": "string", "description": "获客关键词，如 美业获客 / 医美 / 皮肤管理"},
         "count": {"type": "integer", "description": "搜索视频数量，默认 10", "default": 10}},
         "required": ["keyword"]}},
    {"name": "douyin_crawl_result",
     "description": "抖音关键词获客【第2步·查结果】：用 douyin_crawl 返回的任务号查询。还在搜就提示稍候，搜完返回客户名单。出结果前可多查几次（间隔30-60秒）。",
     "inputSchema": {"type": "object", "properties": {
         "job_id": {"type": "string", "description": "douyin_crawl 返回的任务号"}},
         "required": ["job_id"]}},
]


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        mid = msg.get("id")
        method = msg.get("method")
        if method == "initialize":
            pv = (msg.get("params") or {}).get("protocolVersion", "2024-11-05")
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": pv,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "douyin-crawl", "version": "2.0.0"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            try:
                if name == "douyin_crawl":
                    out = submit_crawl(args.get("keyword", ""), args.get("count", 10))
                elif name == "douyin_crawl_result":
                    out = get_result(args.get("job_id", ""))
                else:
                    send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool: %s" % name}})
                    continue
            except Exception as e:
                out = "出错：%s" % str(e)[:200]
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": out}]}})
        elif method and method.startswith("notifications/"):
            pass
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found: %s" % method}})


if __name__ == "__main__":
    main()
