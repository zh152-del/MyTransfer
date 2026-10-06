# -*- coding: utf-8 -*-
"""量「带桌面界面」时 500MB 传输全程的进程内存峰值，验证不随文件大小增长。

  python tools/mem_ui.py            # 默认 500MB
  python tools/mem_ui.py 300        # 换 300MB

进程用 pythonw 起（和双击 .bat 跑起来一样，无控制台），
用 tools/sim_iphone.py 那套三步协议把它推成「传输中」，过程里每 0.3 秒采一次工作集。
"""
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
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


def main():
    mb = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    rel = os.path.join(ROOT, "testfiles", "测试%dMB.bin" % mb)
    log = open(os.path.join(ROOT, "logs", "mem_ui_%dMB.log" % mb), "w",
               encoding="utf-8", errors="replace")
    p = subprocess.Popen([r"C:\Program Files\Python313\pythonw.exe", "MyTransfer.py",
                          "--send", "testfiles/测试%dMB.bin" % mb],
                         cwd=ROOT, stdout=log, stderr=log)
    info = None
    for _ in range(100):
        try:
            info = json.loads(NO_PROXY.open(
                "http://127.0.0.1:8765/hello", timeout=3).read().decode("utf-8"))
            if info.get("task"):
                break
        except Exception:
            time.sleep(0.2)

    peak = [rss(p.pid)]
    base = peak[0]
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            v = rss(p.pid)
            if v > 0:
                peak[0] = max(peak[0], v)
            time.sleep(0.3)
    threading.Thread(target=watch, daemon=True).start()

    t0 = time.time()
    got = 0
    try:
        with NO_PROXY.open(info["task"]["file_url"], timeout=180) as r:
            while True:
                buf = r.read(1024 * 1024)
                if not buf:
                    break
                got += len(buf)
    except Exception as e:
        print("下载中断：%r" % (e,))
    secs = time.time() - t0
    try:
        NO_PROXY.open(info["task"]["done_url"] + "&bytes=%d" % got, timeout=10).read()
    except Exception as e:
        print("回报 done 失败：%r" % (e,))
    time.sleep(1.0)
    stop.set()
    after = json.loads(NO_PROXY.open(
        "http://127.0.0.1:8765/hello", timeout=5).read().decode("utf-8"))
    p.terminate()
    try:
        p.wait(5)
    except Exception:
        p.kill()
    log.close()

    print("文件 %.0f MB，带界面传输耗时 %.2f 秒（%.1f MB/s）" %
          (mb, secs, got / max(secs, 1e-6) / 1048576.0))
    print("进程工作集：起始 %.1f MB → 峰值 %.1f MB → 传完 %.1f MB" %
          (base, peak[0], rss(p.pid) if p.poll() is None else -1))
    print("服务端最终 state：%s" % after.get("state"))


if __name__ == "__main__":
    main()
