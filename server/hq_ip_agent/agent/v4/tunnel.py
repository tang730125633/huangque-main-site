"""出境代理隧道守护：OpenAI 等境外 API 国内直连不通，走 dapeng-server 的出境代理
（服务器侧 xray-egress Reality 通道，经本机 SSH 隧道映射到 127.0.0.1:<本地端口>）。
服务启动时自动建立并保活；HQ_PROXY_TUNNEL=0 禁用，HQ_PROXY_SSH_HOST / HQ_PROXY_LOCAL_PORT 覆盖。

从 app.py 抽出：与 Flask 路由无关，纯系统级守护。
"""
from __future__ import annotations

import os
import socket
import subprocess
import threading
import time


def port_listening(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def start_proxy_tunnel():
    if os.environ.get("HQ_PROXY_TUNNEL") == "0":
        return
    host = os.environ.get("HQ_PROXY_SSH_HOST", "dapeng-server")
    local_port = int(os.environ.get("HQ_PROXY_LOCAL_PORT", "17897"))
    # 服务器侧出境通道：xray-egress Reality 客户端（mihomo 套餐已过期，xray 是现行通道）
    remote = "127.0.0.1:10810"

    def supervisor():
        while True:
            if port_listening(local_port):
                # 已有隧道（本进程或外部进程建立），不重复起
                time.sleep(30)
                continue
            try:
                proc = subprocess.Popen(
                    ["ssh", "-N",
                     "-o", "ExitOnForwardFailure=yes",
                     "-o", "ServerAliveInterval=30",
                     "-o", "ServerAliveCountMax=3",
                     "-o", "ConnectTimeout=10",
                     "-L", "%d:%s" % (local_port, remote), host],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                proc.wait()
            except Exception:
                pass
            time.sleep(5)  # 掉线后 5 秒重连

    threading.Thread(target=supervisor, daemon=True, name="proxy-tunnel-supervisor").start()
