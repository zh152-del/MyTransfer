# -*- coding: utf-8 -*-
"""临时自检工具：起一次 MyTransfer 界面，用限速接收端把界面推到「传输中」，
再直接抓取窗口内容存成 ui-preview.png。只用于开发自检，不属于交付功能。"""
import ctypes
import ctypes.wintypes
import json
import os
import subprocess
import threading
import time
import urllib.request
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
gdi = ctypes.windll.gdi32
user = ctypes.windll.user32
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


def shot_window(hwnd, path):
    r = ctypes.wintypes.RECT()
    user.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = max(r.right - r.left, 10), max(r.bottom - r.top, 10)
    hdc = user.GetWindowDC(hwnd)
    mem = gdi.CreateCompatibleDC(hdc)
    bmp = gdi.CreateCompatibleBitmap(hdc, w, h)
    gdi.SelectObject(mem, bmp)
    ok = user.PrintWindow(hwnd, mem, 2)
    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight = w, -h
    bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi.DeleteObject(bmp)
    gdi.DeleteDC(mem)
    user.ReleaseDC(hwnd, hdc)
    if not ok:
        raise RuntimeError("PrintWindow 失败")
    from PIL import Image
    Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "RGBA", 0, 1).save(path)
    return w, h


def find_window():
    ctypes.windll.user32.EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int))
    found = []

    def cb(hwnd, _):
        n = user.GetWindowTextLengthW(hwnd)
        if n:
            b = ctypes.create_unicode_buffer(n + 1)
            user.GetWindowTextW(hwnd, b, n + 1)
            if b.value.startswith("MyTransfer"):
                found.append(hwnd)
        return True
    ctypes.windll.user32.EnumWindows(ctypes.windll.user32.EnumWindowsProc(cb), 0)
    return found


def slow_receive():
    """限速接收，让界面停在「传输中」，速度/剩余时间都是真实算出来的。"""
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
    time.sleep(1.5)
    info = None
    for host in ("127.0.0.1",):                          # Windows 不解析 .local，用本机地址推进界面
        try:
            info = json.loads(urllib.request.urlopen(
                "http://%s:8765/hello" % host, timeout=6).read().decode("utf-8"))
            print("mDNS 解析结果：", host, flush=True)
            break
        except Exception as e:
            print("  %s 失败：%r" % (host, e), flush=True)
    if not info:
        raise SystemExit("两个地址都连不上")
    total = info["task"]["size"]
    got = 0
    with urllib.request.urlopen(info["task"]["file_url"], timeout=60) as r:
        while got < total:
            buf = r.read(4 * 1024 * 1024)
            if not buf:
                break
            got += len(buf)
            time.sleep(0.10)


def main():
    log = open(os.path.join(ROOT, "ui-preview.log"), "w", encoding="utf-8", errors="replace")
    p = subprocess.Popen([r"C:\Program Files\Python313\pythonw.exe", "MyTransfer.py",
                          "--send", "testfiles/测试100MB.bin"],
                         cwd=ROOT, stdout=log, stderr=log)
    try:
        time.sleep(4)
        wins = find_window()
        if not wins:
            raise SystemExit("没找到 MyTransfer 窗口")
        threading.Thread(target=slow_receive, daemon=True).start()
        time.sleep(3.5)
        out = os.path.join(ROOT, "ui-preview.png")
        w, h = shot_window(wins[0], out)
        print("saved", out, os.path.getsize(out), "窗口 %dx%d" % (w, h))
    finally:
        p.terminate()
        try:
            p.wait(5)
        except Exception:
            p.kill()
        log.close()


if __name__ == "__main__":
    main()
