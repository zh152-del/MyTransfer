# -*- coding: utf-8 -*-
"""MyTransfer 启动入口：起 HTTP 服务 + mDNS 发布 + 桌面界面。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import argparse
import threading
import time

import mt_server as S


class _Tee:
    """同时往两个流写：控制台（有就写）和日志文件（UI 模式下控制台是空的）。"""

    def __init__(self, *fs):
        self.fs = [f for f in fs if f is not None]

    def write(self, s):
        for f in self.fs:
            try:
                f.write(s)
            except Exception:
                pass

    def flush(self):
        for f in self.fs:
            try:
                f.flush()
            except Exception:
                pass


# PyInstaller onefile：__file__ 指向临时解压目录，程序数据必须放 exe 旁边
APP_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
    else os.path.dirname(os.path.abspath(__file__))


def start_logfile(no_ui):
    """把服务端日志原样落一份到 logs/server_<时间>.log。

    用的时候最常问的是「快捷指令到底走到第几步了」——
    有了这个文件，一眼就能看全，不用盯着窗口刷。
    """
    d = os.path.join(APP_DIR, "logs")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "server_%s.log" % time.strftime("%Y%m%d-%H%M%S"))
    f = open(path, "w", encoding="utf-8", errors="replace")
    old = sys.stdout
    try:
        old.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.stdout = _Tee(old, f)
    sys.stderr = sys.stdout
    return path


def open_firewall(port, done):
    """放行局域网访问（HTTP 端口 + mDNS 的 5353）。非管理员时照常显示地址。"""
    rules = [
        ("MyTransfer 传输端口", "TCP", str(port)),
        ("MyTransfer 服务发现", "UDP", "5353"),
    ]
    import subprocess
    why = []
    for name, proto, lport in rules:
        # 先按名字删干净再建：老代码只 add 不删，跑几十次就攒出上百条重复规则
        # （实测攒到 99 条，端口还混着 8790/8791/8795 各种测试端口）。
        try:
            subprocess.run(
                ["netsh", "advfirewall", "firewall", "delete", "rule",
                 "name=%s" % name],
                capture_output=True, timeout=20)
        except Exception:
            pass
        try:
            r = subprocess.run(
                ["netsh", "advfirewall", "firewall", "add", "rule",
                 "name=%s" % name, "dir=in", "action=allow",
                 "protocol=%s" % proto, "localport=%s" % lport],
                capture_output=True, timeout=20)
            if r.returncode != 0:
                why.append("%s(%s): %s" % (name, lport,
                           (r.stderr or r.stdout).decode("utf-8", "replace").strip()))
        except Exception as e:
            why.append("%s(%s): %s" % (name, lport, e))
    if why:
        done("防火墙放行未自动添加（%s）。若你只是偶尔用一下可无视；否则请用管理员身份打开"
             "「Windows PowerShell(管理员)」执行以下两条：\n"
             "netsh advfirewall firewall add rule name=MyTransfer dir=in action=allow protocol=TCP localport=%d\n"
             "netsh advfirewall firewall add rule name=MyTransfer dir=in action=allow protocol=UDP localport=5353" %
             ("；".join(why), port))
    else:
        done("已尝试添加防火墙放行规则：TCP %d（传文件）+ UDP 5353（服务发现）。"
             "若 iPhone 搜不到服务或连不上，多半是这条没生效，请按上一条提示用管理员身份执行。" % port)


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--port", type=int, default=S.DEFAULT_PORT)
    ap.add_argument("--no-ui", action="store_true", help="只跑服务端（测试用）")
    ap.add_argument("--send", help="启动时直接发送指定文件（测试用）")
    args = ap.parse_args()
    port = args.port

    ip = S.local_ip()
    engine = S.Engine(ip, port)
    logpath = start_logfile(args.no_ui)
    httpd = S.start(engine, port)
    mdns = S.MdnsResponder(port)
    mdns.start()

    engine.log("日志落盘：%s" % logpath)
    engine.log("服务已启动：http://%s.local:%d" % (S.SERVICE_NAME, port))
    engine.log("Bonjour 广播：%s.local -> %s（含 A 记录 + 服务记录）"
               % (S.SERVICE_NAME, mdns.ip))
    engine.log("本机局域网地址：%s（iphone 需要与电脑在同一 Wi-Fi）" % ip)
    threading.Thread(target=open_firewall, args=(port, engine.log), daemon=True).start()

    if args.send:
        engine.start_task(args.send)

    if args.no_ui:                                   # 测试模式：只留服务端
        try:
            while True:
                import time
                time.sleep(3600)
        except KeyboardInterrupt:
            os._exit(0)

    import mt_ui
    from tkinter import Tk
    root = Tk()
    app = mt_ui.App(root, engine, mdns, httpd)
    try:
        root.mainloop()
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass
        mdns.stop()


if __name__ == "__main__":
    main()
