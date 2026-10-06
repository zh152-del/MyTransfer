# -*- coding: utf-8 -*-
"""自动化测试：真实起服务端 + 真实传文件 + 逐秒采样内存 + 校验 + 异常场景。

  python tools/run_test.py 100
  python tools/run_test.py 500 --break 40         # 传到 40% 时掐断连接（测异常）
  python tools/run_test.py 500 --lie              # iPhone 谎报字节数（测校验是否诚实）
  python tools/run_test.py 500 --fail             # iPhone 下载成功但主动报失败

输出一张表：文件大小 / 耗时 / 速度 / 服务端内存峰值 / 接收端内存峰值 / SHA256 是否一致 /
服务端最终结论。
"""
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
urllib.request.install_opener(NO_PROXY)

_pi = ctypes.WinDLL("psapi")
_kl = ctypes.windll.kernel32


class _PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PagefaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def rss(pid):
    try:
        h = _kl.OpenProcess(0x1000 | 0x0020, False, pid)
        c = _PMC()
        c.cb = ctypes.sizeof(_PMC)
        if _pi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
            return c.WorkingSetSize / 1048576.0
    except Exception:
        pass
    return -1.0


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def gen(path, size):
    if os.path.exists(path) and os.path.getsize(path) == size:
        return
    with open(path, "wb") as f:
        f.write(os.urandom(1024 * 1024))
        pat = os.urandom(1024) * 512
        rem = size - 1024 * 1024
        while rem > 0:
            w = min(len(pat), rem)
            f.write(pat[:w])
            rem -= w


def start_server(rel, port, logfile):
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    f = open(logfile, "w", encoding="utf-8", errors="replace")
    p = subprocess.Popen([sys.executable, "-u", "MyTransfer.py", "--no-ui",
                          "--port", str(port), "--send", rel],
                         cwd=ROOT, stdout=f, stderr=subprocess.STDOUT, env=env)
    for _ in range(100):
        try:
            info = json.loads(NO_PROXY.open(
                "http://127.0.0.1:%d/hello" % port, timeout=3).read().decode("utf-8"))
            if info.get("task"):
                return p, info
        except Exception:
            time.sleep(0.2)
    p.kill()
    f.close()
    raise RuntimeError("服务端 %d 端口起不来：%s" % (port, open(logfile, encoding="utf-8").read()))


def run_case(size, port, mode="normal", break_at=0):
    rel = os.path.join(ROOT, "testfiles", "测试%dMB.bin" % size)
    gen(rel, size * 1024 * 1024)
    src_sha = sha(rel)
    src_size = os.path.getsize(rel)

    logfile = os.path.join(ROOT, "logs", "server_%dMB_%s.log" % (size, mode))
    os.makedirs(os.path.dirname(logfile), exist_ok=True)
    proc, info = start_server(rel, port, logfile)
    task = info["task"]
    peak_srv = [rss(proc.pid)]

    def watch():
        while proc.poll() is None:
            peak_srv[0] = max(peak_srv[0], rss(proc.pid))
            time.sleep(0.3)
    th = threading.Thread(target=watch, daemon=True)
    th.start()

    # 这一步相当于 iPhone 上的「下载文件」动作
    dest = os.path.join(ROOT, "recv", task["name"])
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest):
        os.remove(dest)
    t0 = time.time()
    got = 0
    err = ""
    try:
        with NO_PROXY.open(task["file_url"], timeout=120) as r:
            total = int(r.headers.get("Content-Length", 0))
            with open(dest + ".part", "wb") as f:
                while True:
                    buf = r.read(1024 * 1024)
                    if not buf:
                        break
                    f.write(buf)
                    got += len(buf)
                    if break_at and got * 100.0 / total >= break_at:
                        raise IOError("测试注入：模拟 iPhone 中途断线（已收 %.0f%%）" % break_at)
            os.replace(dest + ".part", dest)          # 传完才正式命名，和 iPhone 侧语义一致
    except Exception as e:
        err = str(e)
    secs = time.time() - t0

    from urllib.parse import urlsplit, urlencode, parse_qs, urlunsplit
    done_url = task["done_url"]
    if mode == "lie":                                      # iPhone 谎报接收字节数
        u = urlsplit(done_url)
        q = parse_qs(u.query)
        q["bytes"] = [str(src_size + 12345)]
        done_url = urlunsplit((u.scheme, u.netloc, u.path,
                               urlencode(q, doseq=True), u.fragment))
    if mode == "fail":
        done_url = task["fail_url"]
    verdict = ""
    if not err:
        try:
            NO_PROXY.open(done_url, timeout=10).read()
            time.sleep(0.3)
            after = json.loads(NO_PROXY.open(
                "http://127.0.0.1:%d/hello" % port, timeout=5).read().decode("utf-8"))
            verdict = after.get("state", "")
        except Exception as e:
            verdict = "done 请求异常：" + str(e)
    else:
        # 断线场景：绝不能再发 /done，只查看服务端自己记了什么
        time.sleep(0.5)
        after = json.loads(NO_PROXY.open(
            "http://127.0.0.1:%d/hello" % port, timeout=5).read().decode("utf-8"))
        verdict = after.get("state", "")

    # 服务端自己打的日志（错误日志落盘）
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()
        proc.wait()
    f.close()
    lines = [l.rstrip() for l in open(logfile, encoding="utf-8", errors="replace")
             if l.strip()]

    dst_sha = sha(dest) if os.path.exists(dest) else ""
    want = {"normal": "done", "lie": "error", "fail": "error", "break": "error"}[mode]
    if mode == "break":
        ok = bool(err) and verdict == want
    elif mode == "lie":
        ok = (got == src_size) and (dst_sha == src_sha) and verdict == want
    elif mode == "fail":
        ok = verdict == want
    else:
        ok = (not err) and (got == src_size) and (dst_sha == src_sha) and verdict == want
    print("┌ 场景：%s  文件：%.0f MB" % (mode, src_size / 1048576.0))
    print("│ 耗时 %.2f 秒，平均 %.1f MB/s" % (secs, got / max(secs, 1e-6) / 1048576.0))
    print("│ 服务端内存峰值 %.1f MB（进程工作集，任务期间每 0.3 秒采样）" % peak_srv[0])
    print("│ 接收字节 %d / 源字节 %d%s" % (got, src_size, "   注入：" + err if err else ""))
    print("│ 接收端落盘 SHA256：%s" % ("与源文件一致 ✓" if dst_sha == src_sha else "不一致 ✗"))
    print("│ 服务端最终 state（期望 %s）：%s" % (want, verdict))
    for l in lines[-8:]:
        print("│  > " + l)
    print("└ 结论：%s\n" % ("通过 ✓" if ok else "失败 ✗"))
    return ok


def main():
    args = sys.argv[1:]
    size = int(args[0]) if args else 100
    mode = "normal"
    break_at = 0
    port = 8790
    for i, a in enumerate(args):
        if a == "--break" and len(args) > i + 1:
            break_at = float(args[i + 1])
            mode = "break"
        if a == "--lie":
            mode = "lie"
        if a == "--fail":
            mode = "fail"
    ok = run_case(size, port, mode, break_at)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
