# -*- coding: utf-8 -*-
"""MyTransfer Windows 端界面（v3 纯 UI 改造）。

只改视觉与布局：Header / 卡片分区 / 主次按钮 / 日志折叠。
业务逻辑（选文件、发送、取消、二维码、日志、引擎调用）与 v2 完全一致。
"""
import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import mt_server as S
from mt_server import hfmt

# 待发送文件夹：把要发的文件丢进来，程序自动列出，勾选后排队发送
# PyInstaller onefile：冻结时用 exe 所在目录
APP_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
    else os.path.dirname(os.path.abspath(__file__))
SEND_DIR = os.path.join(APP_DIR, "待发送")
os.makedirs(SEND_DIR, exist_ok=True)

# ---- 设计语言（克制版液态玻璃：浅色系 + 细边框 + 大留白）----
BG      = "#F4F6F9"   # 窗口底：极浅灰蓝
CARD    = "#FFFFFF"   # 卡片
LINE    = "#E3E7ED"   # 分隔线 / 卡片描边
FG      = "#1B2430"   # 主文字
MUTED   = "#7A8699"   # 次级文字
ACCENT  = "#2F6FED"   # 品牌蓝（仅用于主按钮与强调点）
ACCENT_H = "#2761CC"
ACCENT_P  = "#1F51A8"
OK      = "#16A34A"
ERR     = "#C2413B"
WARN    = "#B45309"
DOT_OFF = "#B9C2CF"

FONT    = ("Microsoft YaHei UI", 9)
FONT_B  = ("Microsoft YaHei UI", 9, "bold")
FONT_T  = ("Microsoft YaHei UI", 17, "bold")
FONT_S  = ("Microsoft YaHei UI", 8)
FONT_M  = ("Consolas", 9)


def tfmt(sec):
    sec = max(0, sec)
    if sec < 1:
        return "不到 1 秒"
    sec = int(sec)
    if sec < 60:
        return "%d 秒" % sec
    if sec < 3600:
        return "%d 分 %02d 秒" % (sec // 60, sec % 60)
    return "%d 时 %02d 分" % (sec // 3600, (sec % 3600) // 60)


def card(parent, **kw):
    """玻璃感卡片：白底 + 1px 冷灰描边（GPU 零成本的轻量近似）。"""
    return tk.Frame(parent, bg=CARD, highlightbackground=LINE,
                    highlightthickness=1, **kw)


def section_title(parent, text):
    row = tk.Frame(parent, bg=parent["bg"])
    row.pack(fill="x")
    tk.Label(row, text=text, bg=parent["bg"], fg=MUTED,
             font=FONT_B).pack(side="left")
    return row


class FlatButton(tk.Label):
    """扁平按钮：悬停 / 按下 / 禁用 三态持续反馈。"""

    def __init__(self, parent, text, cmd, normal, hover, pressed,
                 fg="#FFFFFF", w=10, font=FONT, padx=14, pady=7, **kw):
        super().__init__(parent, text=text, fg=fg, bg=normal, font=font,
                         width=w, padx=padx, pady=pady, cursor="hand2",
                         anchor="center", **kw)
        self._n, self._h, self._p = normal, hover, pressed
        self._cmd = cmd
        self._on = True
        self._fg0 = fg
        self.bind("<Enter>", lambda e: self.config(bg=self._h) if self._on else None)
        self.bind("<Leave>", lambda e: self.config(bg=self._n) if self._on else None)
        self.bind("<ButtonPress-1>", lambda e: self.config(bg=self._p) if self._on else None)
        self.bind("<ButtonRelease-1>", lambda e: (self.config(bg=self._h if self._on else self._n),
                                                  self._fire()))
        self.bind("<FocusIn>", lambda e: self.config(bg=self._h) if self._on else None)
        self.bind("<FocusOut>", lambda e: self.config(bg=self._n) if self._on else None)

    def _fire(self):
        if self._on and self._cmd:
            self._cmd()

    def set_enabled(self, on):
        self._on = on
        self.config(bg=self._n if on else "#DDE2E9")
        self.config(fg=self._fg0 if on else "#AAB3BF")


class App:
    def __init__(self, root, engine, mdns, httpd):
        self.root = root
        self.e = engine
        self.mdns = mdns
        self.httpd = httpd
        self._log_shown = 0
        self._checks = {}          # 文件路径 -> tk.BooleanVar
        self._rows_key = None      # 上次目录快照，变了才重绘
        root.title("MyTransfer — 局域网文件传输")
        root.geometry("760x820")
        root.minsize(660, 720)
        root.configure(bg=BG)
        self.build()
        self.poll()

    # ---------------- 布局 ----------------
    def build(self):
        r = self.root

        # ===== Header =====
        head = tk.Frame(r, bg=BG)
        head.pack(fill="x", padx=26, pady=(20, 6))
        tk.Label(head, text="MyTransfer", bg=BG, fg=FG,
                 font=FONT_T).pack(side="left")
        tk.Label(head, text="局域网文件传输", bg=BG, fg=MUTED,
                 font=FONT).pack(side="left", padx=(12, 0), pady=(8, 0))
        self.dot = tk.Label(head, text="●", bg=BG, fg=DOT_OFF, font=("Segoe UI", 9))
        self.dot.pack(side="right")
        self.lbl_status = tk.Label(head, text="服务未启动", bg=BG, fg=MUTED, font=FONT)
        self.lbl_status.pack(side="right", padx=(6, 0))

        body = tk.Frame(r, bg=BG)
        body.pack(fill="both", expand=True, padx=26)

        # ===== 网络卡 + 设备卡（两列）=====
        top = tk.Frame(body, bg=BG)
        top.pack(fill="x", pady=(8, 0))
        top.columnconfigure(0, weight=11)
        top.columnconfigure(1, weight=9)

        # -- 网络卡 --
        net = card(top)
        net.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        inner = tk.Frame(net, bg=CARD)
        inner.pack(fill="both", expand=True, padx=18, pady=14)
        tk.Label(inner, text="网络", bg=CARD, fg=MUTED, font=FONT_B).pack(anchor="w")
        self.lbl_net = tk.Label(inner, text="● 局域网已连接", bg=CARD, fg=OK,
                                font=("Microsoft YaHei UI", 11, "bold"))
        self.lbl_net.pack(anchor="w", pady=(8, 2))
        self.lbl_ip = tk.Label(inner, text="—", bg=CARD, fg=FG, font=FONT_M)
        self.lbl_ip.pack(anchor="w")
        self.lbl_host = tk.Label(inner, text="—", bg=CARD, fg=MUTED, font=FONT_M)
        self.lbl_host.pack(anchor="w")
        btnrow = tk.Frame(inner, bg=CARD)
        btnrow.pack(anchor="e", pady=(8, 0))
        FlatButton(btnrow, "复制地址", self.copy_addr, "#F1F4F8", "#E7EBF1", "#DDE3EB",
                   fg="#3B4757", w=8, pady=5).pack(side="left")
        FlatButton(btnrow, "二维码", self.make_qr, "#F1F4F8", "#E7EBF1", "#DDE3EB",
                   fg="#3B4757", w=7, pady=5).pack(side="left", padx=(8, 0))

        # -- 设备卡 --
        dev = card(top)
        dev.grid(row=0, column=1, sticky="nsew")
        inner2 = tk.Frame(dev, bg=CARD)
        inner2.pack(fill="both", expand=True, padx=18, pady=14)
        tk.Label(inner2, text="设备", bg=CARD, fg=MUTED, font=FONT_B).pack(anchor="w")
        self.lbl_dev_dot = tk.Label(inner2, text="◉", bg=CARD, fg=DOT_OFF,
                                    font=("Segoe UI", 12))
        self.lbl_dev_dot.pack(anchor="w", pady=(8, 0))
        self.lbl_dev = tk.Label(inner2, text="iPhone\n未连接", bg=CARD, fg=MUTED,
                                font=("Microsoft YaHei UI", 11, "bold"), justify="left")
        self.lbl_dev.pack(anchor="w", pady=(0, 2))

        # ===== 当前任务卡 =====
        section_title(body, "当前任务").pack(fill="x", pady=(16, 4))
        task = card(body)
        task.pack(fill="x")
        ti = tk.Frame(task, bg=CARD)
        ti.pack(fill="both", padx=18, pady=14)
        # 空态
        self.boxEmpty = tk.Frame(ti, bg=CARD)
        self.boxEmpty.pack(fill="x")
        tk.Label(self.boxEmpty, text="暂无传输任务", bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(pady=(2, 0))
        tk.Label(self.boxEmpty, text="等待发送文件", bg=CARD, fg=DOT_OFF,
                 font=FONT).pack()
        # 有任务态
        self.boxTask = tk.Frame(ti, bg=CARD)
        self.lbl_file = tk.Label(self.boxTask, text="—", bg=CARD, fg=FG,
                                 font=("Microsoft YaHei UI", 11, "bold"),
                                 anchor="w", wraplength=600, justify="left")
        self.lbl_file.pack(fill="x")
        self.lbl_size = tk.Label(self.boxTask, text="—", bg=CARD, fg=MUTED, font=FONT_M)
        self.lbl_size.pack(anchor="w", pady=(2, 4))
        self.pb = ttk.Progressbar(self.boxTask, mode="determinate", maximum=100)
        self.pb.pack(fill="x", ipady=3)
        prow = tk.Frame(self.boxTask, bg=CARD)
        prow.pack(fill="x", pady=(4, 0))
        self.lbl_speed = tk.Label(prow, text="—", bg=CARD, fg=MUTED, font=FONT_M)
        self.lbl_speed.pack(side="left")
        self.lbl_eta = tk.Label(prow, text="—", bg=CARD, fg=MUTED, font=FONT_M)
        self.lbl_eta.pack(side="left", padx=(14, 0))
        self.lbl_verify = tk.Label(prow, text="", bg=CARD, fg=OK, font=FONT)
        self.lbl_verify.pack(side="left", padx=(14, 0))
        self.pct = tk.StringVar(value="0%")
        self.lbl_state = tk.Label(prow, textvariable=self.pct, bg=CARD, fg=FG, font=FONT_B)
        self.lbl_state.pack(side="right")
        # 取消传输（Danger）：仅任务进行时出现
        self.btn_cancel = FlatButton(ti, "取消传输", self.on_cancel,
                                     "#FBEDEB", "#F6DEDC", "#EFCFCC",
                                     fg=ERR, w=10, pady=5)

        # ===== 主操作：＋ 选择文件发送 =====
        prow2 = tk.Frame(body, bg=BG)
        prow2.pack(fill="x", pady=(14, 0))
        self.btn_send = FlatButton(prow2, "＋  选择文件发送", self.pick,
                                   ACCENT, ACCENT_H, ACCENT_P,
                                   fg="#FFFFFF", w=22, font=FONT_B, pady=9)
        self.btn_send.pack()

        # ===== 待发送文件卡 =====
        section_title(body, "待发送文件").pack(fill="x", pady=(16, 4))
        openrow = tk.Frame(body, bg=BG)
        openrow.pack(fill="x")
        tk.Label(openrow, text="自动监控文件夹", bg=BG, fg=MUTED,
                 font=FONT_S).pack(side="left")
        tk.Button(openrow, text="打开文件夹", command=lambda: os.startfile(SEND_DIR),
                  bd=0, relief="flat", bg=BG, fg=ACCENT, font=FONT,
                  cursor="hand2", activebackground=BG,
                  activeforeground=ACCENT_H).pack(side="right")

        files = card(body)
        files.pack(fill="both", expand=True, pady=(6, 0))
        fwrap = tk.Frame(files, bg=CARD)
        fwrap.pack(fill="both", expand=True, padx=12, pady=10)
        self.folder_canvas = tk.Canvas(fwrap, bg=CARD, highlightthickness=0, height=120)
        fvs = ttk.Scrollbar(fwrap, orient="vertical", command=self.folder_canvas.yview)
        self.folder_canvas.configure(yscrollcommand=fvs.set)
        self.folder_inner = tk.Frame(self.folder_canvas, bg=CARD)
        self.folder_inner.bind("<Configure>", lambda e:
                               self.folder_canvas.configure(scrollregion=self.folder_canvas.bbox("all")))
        self._inner_win = self.folder_canvas.create_window((0, 0), window=self.folder_inner, anchor="nw")
        self.folder_canvas.bind("<Configure>", lambda e:
                                self.folder_canvas.itemconfigure(self._inner_win, width=e.width))
        self.folder_canvas.pack(side="left", fill="both", expand=True)
        fvs.pack(side="right", fill="y")

        self.btn_queue = FlatButton(files, "发送选中文件", self.send_checked,
                                    ACCENT, ACCENT_H, ACCENT_P,
                                    fg="#FFFFFF", w=14, pady=6)
        self.btn_queue.pack(side="bottom", anchor="e", padx=14, pady=(0, 12))
        self.lbl_queue = tk.Label(files, text="", bg=CARD, fg=MUTED, font=FONT_S)
        self.lbl_queue.pack(side="bottom", anchor="e", padx=14, pady=(0, 4))

        # ===== 日志：默认折叠 =====
        self.log_open = False
        self.log_box = tk.Frame(r, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        self.log = scrolledtext.ScrolledText(self.log_box, height=7, bd=0, bg="#FBFCFE",
                                             fg="#3B4757", font=("Consolas", 8),
                                             wrap="word", highlightthickness=0)
        self.log.pack(fill="both", expand=True, padx=2, pady=2)
        self.log.configure(state="disabled")
        self.log.tag_config("ok", foreground="#16A34A")
        self.log.tag_config("bad", foreground="#C2413B")
        logrow = tk.Frame(r, bg=BG)
        logrow.pack(fill="x", padx=26, pady=(10, 12), side="bottom")
        self.lbl_log_toggle = tk.Label(logrow, text="高级信息 / 日志   ＋", bg=BG, fg=MUTED,
                                       font=FONT, cursor="hand2")
        self.lbl_log_toggle.pack(side="right")
        self.lbl_log_toggle.bind("<Button-1>", lambda e: self.toggle_log())

    def toggle_log(self):
        self.log_open = not self.log_open
        if self.log_open:
            self.log_box.pack(fill="x", padx=26, pady=(4, 0), before=self.lbl_log_toggle.master)
            self.lbl_log_toggle.config(text="高级信息 / 日志   −")
        else:
            self.log_box.pack_forget()
            self.lbl_log_toggle.config(text="高级信息 / 日志   ＋")

    # ---------------- 事件（与 v2 完全一致）----------------
    def pick(self):
        ps = filedialog.askopenfilenames(title="选择要发送到 iPhone 的文件（可多选）")
        if not ps:
            return
        errs = []
        for p in ps:
            try:
                self.e.start_task(p)
            except Exception as ex:
                errs.append("%s：%s" % (os.path.basename(p), ex))
        if errs:
            messagebox.showerror("部分文件无法发送", "\n".join(errs))

    def on_cancel(self):
        if not self.e.task:
            return
        if messagebox.askyesno("取消", "确定取消当前传输？"):
            self.e.cancel()

    def copy_addr(self):
        url = "http://%s.local:%d/hello" % (S.SERVICE_NAME, self.e.port)
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self.e.log("已复制完整地址 %s（快捷指令里如果 .local 解析失败，就用它替换一次）" % url)

    def make_qr(self):
        url = "http://%s.local:%d" % (S.SERVICE_NAME, self.e.port)
        try:
            import qrcode
            img = qrcode.make(url)
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "MyTransfer.png")
            img.save(path)
            self.e.log("已保存二维码 %s（手机相机可直接扫）" % path)
            # 现代弹窗展示（生成逻辑不变，只换展示方式）
            top = tk.Toplevel(self.root)
            top.title("连接 MyTransfer")
            top.configure(bg=CARD, padx=26, pady=20)
            top.resizable(False, False)
            tk.Label(top, text="连接 MyTransfer", bg=CARD, fg=FG,
                     font=("Microsoft YaHei UI", 13, "bold")).pack()
            try:
                self._qr_img = tk.PhotoImage(file=path)
                tk.Label(top, image=self._qr_img, bg=CARD).pack(pady=12)
            except Exception:
                tk.Label(top, text="二维码已保存到程序目录", bg=CARD, fg=MUTED).pack(pady=12)
            tk.Label(top, text="使用手机扫描连接", bg=CARD, fg=MUTED, font=FONT).pack()
            FlatButton(top, "关闭", top.destroy, "#F1F4F8", "#E7EBF1", "#DDE3EB",
                       fg="#3B4757", w=8, pady=5).pack(pady=(12, 0))
        except ImportError:
            messagebox.showinfo("二维码", "需要 qrcode 库：pip install qrcode\n地址：%s" % url)

    def on_exit(self):
        self.root.destroy()

    # ---------------- 待发送文件夹（逻辑与 v2 一致，仅换皮）----------------
    def scan_folder(self):
        try:
            entries = []
            for n in os.listdir(SEND_DIR):
                p = os.path.join(SEND_DIR, n)
                if os.path.isfile(p) and not n.startswith((".", "~$", "._")):
                    try:
                        entries.append((p, os.path.getsize(p), os.path.getmtime(p)))
                    except OSError:
                        pass
        except OSError:
            return
        entries.sort(key=lambda x: x[2], reverse=True)
        key = tuple((p, sz) for p, sz, _ in entries)
        if key == self._rows_key:
            return
        self._rows_key = key
        for w in self.folder_inner.winfo_children():
            w.destroy()
        if not entries:
            self._checks = {}
            tk.Label(self.folder_inner, text="文件夹是空的——把要发的文件丢进来",
                     bg=CARD, fg=DOT_OFF, font=FONT, anchor="w").pack(fill="x", pady=(8, 4))
            self.lbl_queue.config(text="")
            return
        keep = {e[0] for e in entries}
        self._checks = {k: v for k, v in self._checks.items() if k in keep}
        for p, sz, _mt in entries:
            var = self._checks.get(p)
            if var is None:
                var = tk.BooleanVar(value=False)
            self._checks[p] = var
            row = tk.Frame(self.folder_inner, bg=CARD)
            row.pack(fill="x", pady=1)
            cb = tk.Checkbutton(row, text=os.path.basename(p), variable=var, anchor="w",
                                bg=CARD, fg=FG, activebackground=CARD, activeforeground=FG,
                                font=FONT, highlightthickness=0, bd=0, cursor="hand2",
                                selectcolor="#EDF1F7")
            cb.pack(side="left", fill="x", expand=True)
            tk.Label(row, text=hfmt(sz), bg=CARD, fg=MUTED,
                     font=("Consolas", 8)).pack(side="right", padx=(0, 8))
        self.lbl_queue.config(text="共 %d 个文件" % len(entries))

    def send_checked(self):
        picked = [p for p, v in self._checks.items() if v.get()]
        if not picked:
            messagebox.showinfo("发送", "先勾选要发送的文件")
            return
        errs = []
        with self.e.lock:
            queued_paths = {t.path for t in self.e.tasks if t.state in
                            (S.STATE_WAITING, S.STATE_READY, S.STATE_TRANSFER, S.STATE_VERIFY)}
        for p in picked:
            if p in queued_paths:
                self.e.log("跳过（已在队列）：%s" % os.path.basename(p))
                continue
            try:
                self.e.start_task(p)
            except Exception as ex:
                errs.append("%s：%s" % (os.path.basename(p), ex))
        for v in self._checks.values():
            v.set(False)
        if errs:
            messagebox.showerror("部分文件无法发送", "\n".join(errs))

    # ---------------- 刷新（数据源不变，只改展示）----------------
    def poll(self):
        self.scan_folder()
        self.refresh()
        self.root.after(150, self.poll)

    def refresh(self):
        e = self.e
        t = e.task
        # 日志（只追加新增行）
        with e.lock:
            pending = list(e.logs)
        if len(pending) != self._log_shown:
            self.log.configure(state="normal")
            for ts, txt in pending[self._log_shown:]:
                tag = "ok" if txt.startswith("[OK] ") else (
                    "bad" if any(k in txt for k in ("失败", "错误", "出错", "中断", "取消")) else "")
                self.log.insert("end", "%s  %s\n" % (ts, txt.replace("[OK] ", "")), tag)
            self._log_shown = len(pending)
            self.log.see("end")
            self.log.configure(state="disabled")
        # Header 状态
        self.lbl_status.config(text="服务运行中")
        self.dot.config(fg=OK if e.client_state != "未连接" else DOT_OFF)
        # 网络卡
        self.lbl_ip.config(text=e.ip)
        self.lbl_host.config(text="MyTransfer.local:%d" % e.port)
        # 设备卡
        connected = e.client_state in ("已连接（正在接收）", "已连接", "已完成")
        self.lbl_dev.config(text="iPhone\n%s" % e.client_state,
                            fg=FG if connected else MUTED)
        self.lbl_dev_dot.config(fg=OK if connected else DOT_OFF)
        # 任务卡：空态 / 任务态切换
        busy = t is not None and t.state in (S.STATE_WAITING, S.STATE_READY,
                                             S.STATE_TRANSFER, S.STATE_VERIFY)
        if t is not None:
            self.boxEmpty.pack_forget()
            self.boxTask.pack(fill="x")
        else:
            self.boxTask.pack_forget()
            self.boxEmpty.pack(fill="x")
        # 取消传输按钮：只有任务进行中才出现
        if busy:
            self.btn_cancel.pack(anchor="e", pady=(8, 0))
        else:
            self.btn_cancel.pack_forget()
        # 主按钮可用性
        self.btn_send.set_enabled(not busy)
        if t is None:
            self.pb["value"] = 0
            self.pct.set("0%")
            self.lbl_speed.config(text="—")
            self.lbl_eta.config(text="—")
            self.lbl_verify.config(text="")
            return
        with e.lock:
            queued = len(e.tasks) - 1
        name = t.name + ("　（队列还有 %d 个）" % queued if queued else "")
        self.lbl_file.config(text=name)
        pctv = (t.sent * 100.0 / t.size) if t.size else 0
        self.pb["value"] = pctv
        self.pct.set("%.0f%%" % pctv)
        self.lbl_size.config(text="%s / %s" % (hfmt(t.sent), hfmt(t.size)))
        if t.state == S.STATE_TRANSFER:
            sp = t.speed()
            self.lbl_speed.config(text="%.1f MB/s" % (sp / 1048576.0))
            self.lbl_eta.config(text="剩余 %s" % tfmt(t.eta()))
            self.lbl_verify.config(text="")
        elif t.state == S.STATE_READY:
            self.lbl_speed.config(text="—")
            self.lbl_eta.config(text="—")
            self.lbl_verify.config(text="准备就绪，等待 iPhone 取文件", fg=OK)
        elif t.state == S.STATE_WAITING:
            self.lbl_speed.config(text="—")
            self.lbl_eta.config(text="—")
            self.lbl_verify.config(text="已入队，等待接收方", fg=WARN)
        elif t.state == S.STATE_VERIFY:
            self.lbl_speed.config(text="—")
            self.lbl_eta.config(text="—")
            self.lbl_verify.config(text="校验中：等待接收方回报字节数…", fg=WARN)
        elif t.state == S.STATE_DONE:
            self.lbl_speed.config(text="平均 %.1f MB/s" % (
                t.size / max(t.duration, 1e-6) / 1048576.0))
            self.lbl_eta.config(text="用时 %s" % tfmt(t.duration))
            self.lbl_verify.config(text="✓ " + t.verdict_text(), fg=OK)
        else:
            self.lbl_speed.config(text="已发 %s" % hfmt(t.sent))
            self.lbl_eta.config(text="—")
            self.lbl_verify.config(text="✕ " + t.verdict_text(), fg=ERR)
