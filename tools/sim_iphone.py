# -*- coding: utf-8 -*-
"""模拟 iPhone 端：完整走一遍「接收电脑文件」快捷指令做的事。

  1. GET /hello           —— 相当于快捷指令第一个动作「获取 URL 内容」
  2. GET <task.file_url>  —— 相当于「下载文件」动作
  3. GET <task.done_url>  —— 相当于快捷指令最后一个回报动作

本脚本运行在电脑上，所以它有资格算 SHA256（真机快捷指令算不了，
真机只回报字节数）。用它来校验整条链路的正确性，并逐秒打印接收端内存，
用来证明接收侧不是「先把整个文件读进内存」。
"""
import argparse
import ctypes
import hashlib
import json
import os
import sys
import threading
import time
import urllib.request

# 局域网直连，不吃系统代理（本机开着代理也会直接连局域网地址）
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
urllib.request.install_opener(OPENER)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_pi = ctypes.WinDLL("psapi", use_last_error=True) if hasattr(ctypes, "WinDLL") else None
_kl = ctypes.windll.kernel32 if hasattr(ctypes, "windll") else None


class _PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PagefaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def rss(pid=None):
    try:
        h = _kl.OpenProcess(0x1000 | 0x0020, False, pid or os.getpid())
        c = _PMC()
        c.cb = ctypes.sizeof(_PMC)
        if _pi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
            return c.WorkingSetSize / 1048576.0
    except Exception:
        pass
    return -1.0


def watch(label, stop, out):
    while not stop.is_set():
        out.append((time.time(), rss()))
        stop.wait(1.0)


class Recv(urllib.request.HTTPRedirectHandler):
    """把响应体直接落盘，一小块一小块读，不整文件进内存。"""

    def http_response(self, req, resp):
        return resp


def download(url, dest, chunk=1024 * 1024):
    t0 = time.time()
    got = 0
    h = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=120) as r:
        total = int(r.headers.get("Content-Length", 0))
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            while True:
                buf = r.read(chunk)
                if not buf:
                    break
                f.write(buf)
                h.update(buf)
                got += len(buf)
    if got != total:
        raise IOError("接收字节数 %d != Content-Length %d" % (got, total))
    os.replace(tmp, dest)
    return got, h.hexdigest(), time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://MyTransfer.local:8765")
    ap.add_argument("--out", default="./recv")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    stop = threading.Event()
    samples = []
    threading.Thread(target=watch, args=("recv", stop, samples), daemon=True).start()

    base = args.url.rstrip("/")
    info = json.loads(urllib.request.urlopen(base + "/hello", timeout=20).read().decode("utf-8"))
    task = info.get("task")
    if not task:
        print("[FAIL] /hello 没有任务：state=%s" % info.get("state"))
        sys.exit(1)
    print("[1/3] 发现服务 %s（%s:%s），任务：%s  %.1f MB" %
          (info.get("service"), info.get("host"), info.get("port"),
           task["name"], task["size"] / 1048576.0))

    dest = os.path.join(args.out, task["name"])
    got, sha, secs = download(task["file_url"], dest)
    print("[2/3] 下载完成：%.1f MB / %.2f 秒（%.1f MB/s）" %
          (got / 1048576.0, secs, got / max(secs, 1e-6) / 1048576.0))

    urllib.request.urlopen(task["done_url"] + "&bytes=%d" % got, timeout=20).read()
    time.sleep(0.4)
    print("[3/3] 已向 Windows 回报接收完成")
    if task.get("sha256") and task["sha256"] != "computing":
        print("[校验] 接收端 SHA256 %s" % ("与任务一致 ✓" if sha == task["sha256"] else "与任务不一致 ✗"))
    print("[内存] 接收端 WorkingSet 峰值 %.1f MB" % max(s[1] for s in samples))
    stop.set()
    if samples:
        print("[内存] 接收端最后 %.1f MB" % samples[-1][1])


if __name__ == "__main__":
    main()
