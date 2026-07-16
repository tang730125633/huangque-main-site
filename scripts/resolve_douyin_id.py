#!/usr/bin/env python3
"""
抖音号 → sec_uid 解析器
creator(账号)模式只认 sec_uid，本脚本用登录态浏览器搜抖音号拿到 sec_uid。
复用 MediaCrawler 的 browser_data 登录态（须在没有其它爬取占用浏览器时运行）。

用法：python resolve_douyin_id.py <抖音号>
输出：stdout 打印 sec_uid（失败则打印空+exit 1）
"""
import sys
import os
import asyncio
from playwright.async_api import async_playwright

DOUYIN_ID = sys.argv[1].strip()
USER_DATA = os.path.expanduser(
    os.environ.get("MC_USER_DATA", "~/code/MediaCrawler/browser_data/dy_user_data_dir"))


async def main():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(USER_DATA, headless=True)
        page = await ctx.new_page()
        await page.goto(f"https://www.douyin.com/search/{DOUYIN_ID}?type=user",
                        wait_until="domcontentloaded")
        await page.wait_for_timeout(3500)
        data = await page.evaluate("""() => {
            const out=[];
            document.querySelectorAll('a[href*="/user/"]').forEach(a=>{
                const m=a.href.match(/\\/user\\/([^/?]+)/);
                if(m) out.push({sec:m[1], txt:(a.innerText||'')});
            });
            return out;
        }""")
        await ctx.close()
    # 优先匹配文本里含该抖音号的结果（精确匹配本人）
    sec = ""
    for d in data:
        if DOUYIN_ID in d["txt"] and d["sec"] != "self":
            sec = d["sec"]; break
    if not sec:
        for d in data:
            if d["sec"] != "self":
                sec = d["sec"]; break
    if sec:
        print(sec)
    else:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
