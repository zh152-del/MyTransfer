# -*- coding: utf-8 -*-
"""MyTransfer 服务端：HTTP 传输引擎 + mDNS/Bonjour 发布 + 任务状态机 + 日志。

设计约束：
  * 流式发送，4MB 分块，任何时刻内存中只有当前分块，不随文件大小增长。
  * 进度 / 速度 / 剩余时间全部由真实已发送字节计算，不模拟。
  * 校验：源文件 SHA256 分块计算；发送时对「实际发出的字节流」同步算 SHA256；
    iPhone 只回报接收字节数（不假设快捷指令存在 Hash 动作）。三方比对，结论直说。
"""
import hashlib
import json
import os
import socket
import struct
import socketserver
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

SERVICE_NAME = "MyTransfer"
SERVICE_TYPE = "_http._tcp.local"
FQDN = SERVICE_NAME + "." + SERVICE_TYPE          # MyTransfer._http._tcp.local
DEFAULT_PORT = 8765
CHUNK = 4 * 1024 * 1024                            # 4MB
MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
DEBUG = os.environ.get("MYTRANSFER_DEBUG") == "1"   # 排障时设 1，把 mDNS 收发打出来
REANNOUNCE_EVERY = 20                               # 秒：周期性重播，掉包/晚开机也还能被发现

STATE_WAITING = "waiting"      # 已入队，等 iPhone 来取
STATE_READY = "ready"          # 哈希算完
STATE_TRANSFER = "transferring"
STATE_VERIFY = "verifying"
STATE_DONE = "done"
STATE_ERROR = "error"

# 这些状态下 iPhone 还能拿得到字段值。
# STATE_VERIFY 必须含在内：字节发完之后服务端就进 verifying 等回报，
# 而快捷指令恰恰是在发完之后才去问「回报地址」，漏掉它就会拿到空串。
LIVE_STATES = (STATE_WAITING, STATE_READY, STATE_TRANSFER, STATE_VERIFY)


def _silent_log(text, ok=False):
    """Handler 还没挂上 engine 之前的兜底：打控制台，别炸。"""
    try:
        print("%s  %s" % (time.strftime("%H:%M:%S"), text), flush=True)
    except Exception:
        pass


def hfmt(n):
    """字节数人类可读。"""
    n = float(n)
    if n >= 1073741824:
        return "%.2f GB" % (n / 1073741824.0)
    if n >= 1048576:
        return "%.1f MB" % (n / 1048576.0)
    if n >= 1024:
        return "%.0f KB" % (n / 1024.0)
    return "%d B" % n


# 扩展名 -> (HTTP Content-Type, 手机端归类)。photo/video 会被快捷指令存进相册。
EXT_KIND = {
    ".jpg": ("image/jpeg", "photo"), ".jpeg": ("image/jpeg", "photo"),
    ".png": ("image/png", "photo"), ".heic": ("image/heic", "photo"),
    ".heif": ("image/heif", "photo"), ".gif": ("image/gif", "photo"),
    ".webp": ("image/webp", "photo"), ".bmp": ("image/bmp", "photo"),
    ".tiff": ("image/tiff", "photo"),
    ".mp4": ("video/mp4", "video"), ".mov": ("video/quicktime", "video"),
    ".m4v": ("video/x-m4v", "video"),
}


def file_kind(name):
    """按扩展名归类：photo / video / file。"""
    return EXT_KIND.get(os.path.splitext(name)[1].lower(),
                        ("application/octet-stream", "file"))[1]


def content_type(name):
    return EXT_KIND.get(os.path.splitext(name)[1].lower(),
                        ("application/octet-stream", "file"))[0]


# ---------------------------------------------------------------- mDNS / Bonjour
def enc_name(s):
    out = b""
    for label in s.rstrip(".").split("."):
        lab = label.encode("utf-8")
        out += bytes([len(lab)]) + lab
    return out + b"\x00"


def dec_name(buf, i):
    """把 DNS 名字还原成带点的字符串（enc_name 的逆运算）。

    buf[i:] 形如  b'\\x0aMyTransfer\\x05local\\x00'
    返回 ("MyTransfer.local", 读到哪结束)。0xC0 开头的是压缩指针，这里用不到。
    """
    parts = []
    while i < len(buf):
        l = buf[i]
        if l == 0:
            i += 1
            break
        if l & 0xC0:                      # 压缩指针：本程序不发出这种包，遇到就停
            i += 2
            break
        if i + l >= len(buf):
            raise ValueError("名字段越界")
        parts.append(buf[i + 1:i + 1 + l].decode("utf-8", "ignore"))
        i += 1 + l
    return ".".join(parts), i


def _rr_a(name, ip):
    rdata = socket.inet_aton(ip)
    return enc_name(name) + struct.pack(">HHIH", 1, 1, 120, 4) + rdata


def _rr_ptr(name, target):
    td = enc_name(target)
    return enc_name(name) + struct.pack(">HHIH", 12, 1, 120, len(td)) + td


def _rr_srv(name, port, target):
    sd = struct.pack(">HHH", 0, 0, port) + enc_name(target)
    return enc_name(name) + struct.pack(">HHIH", 33, 1, 120, len(sd)) + sd


def _rr_txt(name, txt):
    return enc_name(name) + struct.pack(">HHIH", 16, 1, 120, len(txt)) + txt


class MdnsResponder(threading.Thread):
    """零依赖 mDNS responder：回答 PTR/SRV/TXT + **主机名 A 记录**，并主动公告。失败时降级。

    为什么要额外答 A 记录：iPhone 拿到的是 `http://MyTransfer.local:8765/...` 这种地址，
    它先得问「MyTransfer.local 这台机器 IP 是多少」。只答 PTR/SRV/TXT 的时候，
    iPhone 有时能靠 SRV 里的目标名绕过去（日志里就能看见请求进来），
    有时绕不过去就一直等到 30 秒超时 —— 表现是手机上「请求超时」，
    但服务器上连一条请求都没有，非常误导。所以 A 记录必须答。
    """

    def __init__(self, port):
        super().__init__(daemon=True)
        self.port = port
        self.sock = None
        self.ip = local_ip()
        self._stop = threading.Event()

    def run(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", MDNS_PORT))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                         socket.inet_aton(MDNS_GROUP) + socket.inet_aton("0.0.0.0"))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            s.settimeout(0.4)
            self.sock = s
        except Exception as e:                       # 5353 被 Bonjour 服务或其它程序占用
            print("[mdns] 不可用，发现降级为 IP 直连：%s" % e)
            return
        threading.Thread(target=self._announce, daemon=True).start()
        try:
            while not self._stop.is_set():
                try:
                    data, addr = self.sock.recvfrom(9000)
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    self._answer(data, addr)
                except Exception as ex:
                    if DEBUG:
                        print("[mdns] 应答出错：%r" % (ex,), flush=True)
        finally:
            try:
                self.sock.close()
            except Exception:
                pass

    def _announce(self):
        """开机连播 4 次（0.3/1.2/3/6 秒），之后每 20 秒重播一次。"""
        for delay in (0.3, 1.2, 3.0, 6.0):
            if self._stop.is_set():
                return
            time.sleep(delay)
            self._reply()
        while not self._stop.is_set():
            time.sleep(REANNOUNCE_EVERY)
            if self._stop.is_set():
                return
            self._reply()

    def _send(self, payload):
        try:
            self.sock.sendto(payload, (MDNS_GROUP, MDNS_PORT))
        except Exception as e:
            if DEBUG:
                print("[mdns] 广播失败：%r" % (e,), flush=True)

    def _build(self, qname, qtype, qclass, rrs, qid):
        """拼一个标准 DNS 报文：header + 可选的问题段(name+qtype+qclass) + 答案段(rrs)。

        这里是最早写错的地方：之前问题段只写了名字、漏了 qtype/qclass 这 4 个字节，
        还在答案前面多塞了一个名字，整包错位，iPhone 根本解析不了。现在严格按字节对齐。
        """
        qd = 1 if qname else 0
        hdr = struct.pack(">HHHHHH", qid, 0x8400, qd, len(rrs), 0, 0)
        body = b""
        if qname:
            body += enc_name(qname) + struct.pack(">HH", qtype, qclass & 0x7FFF)
        for rr in rrs:
            body += rr
        return hdr + body

    def _service_rrs(self):
        """服务的一套记录：A（主机 IP）+ PTR（服务类型）+ SRV（端口/目标）+ TXT。
        全部塞进公告和应答里，iPhone 一次拿全，不用来回问两轮。"""
        txt = b"port=%d;ver=1" % self.port
        host = SERVICE_NAME + ".local"
        return [
            _rr_a(host, self.ip),
            _rr_ptr(SERVICE_TYPE, FQDN),
            _rr_srv(FQDN, self.port, host),
            _rr_txt(FQDN, txt),
        ]

    def _send_response(self, payload, target):
        # ⚠ 只回单播（QU 查询本来就该单播答）；不再额外多播一份——
        # 之前「单播 + 多播复读」和安卓 NSD 的查询叠加，能把家用路由器
        # 的多播表打瘫（实测全家断网）。省掉这份复读，风暴源头就没了。
        if target:
            try:
                self.sock.sendto(payload, target)
                if DEBUG:
                    print("[mdns] 已单播回包给 %s:%d" % target, flush=True)
            except Exception as e:
                if DEBUG:
                    print("[mdns] 单播回包失败：%r" % (e,), flush=True)
        else:
            self._send(payload)

    def _reply_addr(self, target=None, qid=0, qname=None, qtype=1, qclass=1):
        """只答 A 记录（主机名 -> IP）。iPhone 问 MyTransfer.local 时走这儿。"""
        rrs = [_rr_a(SERVICE_NAME + ".local", self.ip)]
        self._send_response(self._build(qname, qtype, qclass, rrs, qid), target)

    def _reply(self, target=None, qid=0, qname=None, qtype=12, qclass=1):
        """答服务记录（PTR/SRV/TXT，外加一条 A）。无 qname 就是主动公告。"""
        self._send_response(self._build(qname, qtype, qclass, self._service_rrs(), qid), target)

    def _answer(self, data, addr=None):
        if len(data) < 12:
            return
        if DEBUG:
            print("[mdns] 收到查询包 %d 字节" % len(data), flush=True)
        qdcount = struct.unpack(">H", data[4:6])[0]
        qid = struct.unpack(">H", data[0:2])[0]      # 必须是整数，struct.pack 不吃 bytes
        i = 12
        host = (SERVICE_NAME + ".local").lower()
        stype = SERVICE_TYPE.rstrip(".").lower()
        fqdn = FQDN.rstrip(".").lower()
        for _ in range(qdcount):
            # ⚠ 大事记：问句里的域名是「1 字节长度 + 标签」拼起来的，不是带点的字符串。
            # 以前直接 data[i:end].decode() 读，把 0x05 这种长度字节当成了普通字符，
            # 所有分支永远匹配不上——也就是说我们一次查询都没回答过，只能靠往外广播
            # 让 iPhone 偶发撞上。dec_name() 才是正确读法。
            try:
                qname, end = dec_name(data, i)
            except Exception:
                return
            qtype, qclass = struct.unpack(">HH", data[end:end + 4])
            i = end + 4
            if DEBUG:
                print("[mdns]   问题：%s  type=%d class=0x%04x"
                      % (qname, qtype, qclass), flush=True)
            short = qname.rstrip(".").lower()
            unicast = addr if (qclass & 0x8000) else None     # QU 位：对方宁可收单播应答
            if qtype == 1 and short == host:
                # iPhone 在问「MyTransfer.local 这台机器在哪」，必须给 A 记录，
                # 拿不到 IP 它就会一路干等到「请求超时」。
                if DEBUG:
                    print("[mdns] 回答 A 记录 %s -> %s" % (SERVICE_NAME, self.ip), flush=True)
                self._reply_addr(target=unicast, qid=qid, qname=qname, qtype=qtype, qclass=qclass)
            elif qtype in (12, 33, 16, 255) and short in (stype, fqdn, host):
                if DEBUG:
                    print("[mdns] 回答服务记录 %s（端口 %d）" % (qname, self.port), flush=True)
                self._reply(target=unicast, qid=qid, qname=qname, qtype=qtype, qclass=qclass)
            elif short == "_services._dns-sd._udp.local" and qtype in (12, 255):
                rrs = [_rr_ptr("_services._dns-sd._udp.local", SERVICE_TYPE)]
                self._send_response(
                    self._build(qname, qtype, qclass, rrs, qid), unicast)

    def stop(self):
        self._stop.set()


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    if ip.startswith("169.254"):
        try:
            for _fam, _t, _p, _a, sa in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                if not sa[0].startswith("127."):
                    return sa[0]
        except Exception:
            pass
    return ip


# ---------------------------------------------------------------- 任务
class Task:
    def __init__(self, path):
        self.id = uuid.uuid4().hex[:8]
        self.path = path
        self.name = os.path.basename(path)
        self.size = os.path.getsize(path)
        self.state = STATE_WAITING
        self.sent = 0
        self.hash = ""
        self.sent_hash = ""
        self.error = ""
        self.verdict = "等待 iPhone"
        self.t0 = 0.0
        self.duration = 0.0
        self.report_bytes = -1
        self._samples = []                            # [(时间, 字节)]，用来算真实速度

    def note(self, n):
        self._samples.append((time.time(), n))
        if len(self._samples) > 400:
            self._samples.pop(0)

    def speed(self):
        """真实速度：最近 0.5 秒窗口内的字节数 / 时间。"""
        if not self._samples:
            return 0.0
        now = time.time()
        pts = [p for p in self._samples if now - p[0] <= 0.5]
        if len(pts) < 2:
            return 0.0
        dt = pts[-1][0] - pts[0][0]
        return ((pts[-1][1] - pts[0][1]) / dt) if dt > 0 else 0.0

    def eta(self):
        sp = self.speed()
        if sp <= 0:
            return 0.0
        return max(0.0, (self.size - self.sent) / sp)

    def verdict_text(self):
        """诚实的三方比对结论。"""
        if self.state == STATE_DONE:
            parts = []
            parts.append("字节数 %d/%d 一致" % (self.report_bytes, self.size)
                         if self.report_bytes == self.size else "字节数 %d/%d 不一致" % (self.report_bytes, self.size))
            if self.hash:
                parts.append("发送流 SHA256 与源文件一致" if self.sent_hash == self.hash
                             else "发送流 SHA256 与源文件不一致")
            return "；".join(parts)
        if self.state == STATE_ERROR:
            return self.error or "验证失败"
        return "发送中（尚未完成校验）"


# ---------------------------------------------------------------- 引擎
class Engine:
    """多文件队列：tasks[0] 是当前任务，其余排队等 iPhone 逐个来取。"""

    def __init__(self, ip, port):
        self.ip = ip
        self.port = port
        self.tasks = []                                # 队列：[Task, ...]
        self.lock = threading.Lock()
        self.logs = []
        self.client_state = "未连接"                   # 未连接/已连接/传输中/已完成
        self.cancel_flag = threading.Event()
        self.last_state = None                         # 队列弹空后，/hello 回报最后一个任务的状态

    # 兼容旧代码：self.task 永远指向队首
    @property
    def task(self):
        return self.tasks[0] if self.tasks else None

    def log(self, text, ok=False):
        line = (time.strftime("%H:%M:%S"), ("[OK] " if ok else "") + text)
        with self.lock:
            self.logs.append(line)
        try:
            print("%s  %s" % (line[0], line[1]), flush=True)
        except Exception as ex:                        # 日志打不出来也要让人看见
            sys.stderr.write("日志写入失败：%r\n" % (ex,))

    # ---- 任务 ----
    def start_task(self, path):
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        t = Task(path)
        self.cancel_flag.clear()
        with self.lock:
            first = not self.tasks
            self.tasks.append(t)
        if first:
            self.log("已选中文件：%s（%.1f MB）" % (t.name, t.size / 1048576.0))
        else:
            self.log("已加入队列（第 %d 个）：%s（%.1f MB）"
                     % (len(self.tasks), t.name, t.size / 1048576.0))
        threading.Thread(target=self._hash_worker, args=(t,), daemon=True).start()
        return t

    def get_task(self, tid):
        with self.lock:
            for t in self.tasks:
                if t.id == tid:
                    return t
        return None

    def _advance(self):
        """队首结束（done/error/移除）后让下一个顶上。"""
        with self.lock:
            if self.tasks and self.tasks[0].state in (STATE_DONE, STATE_ERROR):
                popped = self.tasks.pop(0)
                self.last_state = popped.state
                nxt = self.tasks[0] if self.tasks else None
                if nxt:
                    self.client_state = "未连接"
            else:
                nxt = None
        if nxt:
            self.log("下一个：%s（%.1f MB）" % (nxt.name, nxt.size / 1048576.0))

    def _hash_worker(self, t):
        if t.hash:
            return
        h0 = time.time()
        h = hashlib.sha256()
        with open(t.path, "rb") as f:
            while True:
                b = f.read(8 * 1024 * 1024)
                if not b:
                    break
                h.update(b)
        t.hash = h.hexdigest()
        if t.state == STATE_WAITING:
            t.state = STATE_READY
        self.log("SHA256 计算完成：%.1f MB，用时 %.2f 秒（分块读，不整文件进内存）"
                 % (t.size / 1048576.0, time.time() - h0))

    def cancel(self):
        """取消当前任务；队列里还有就自动顶上。"""
        with self.lock:
            if self.tasks:
                self.tasks[0].state = STATE_ERROR
                self.tasks[0].error = "用户取消"
                self.tasks[0].verdict = "已取消"
        self.cancel_flag.set()
        self.log("任务已取消")
        self._advance()
        self.client_state = "未连接"

    def clear(self):
        with self.lock:
            self.tasks = []
            self.cancel_flag.clear()
        self.client_state = "未连接"

    # ---- 传输 ----
    def begin_transfer(self, t=None):
        t = t or self.task
        if not t:
            return
        with self.lock:
            t.state = STATE_TRANSFER
            t.t0 = time.time()
            self.client_state = "已连接（正在接收）"
        self.log("iPhone 开始接收")

    def add_bytes(self, n, t=None):
        t = t or self.task
        if not t:
            return
        with self.lock:
            t.sent += n
            t.note(t.sent)

    def send_finished(self, t=None):
        """字节流发完（或者 iPhone 中途跑掉）。这一步立刻判断是「发完了」还是「断了」。"""
        t = t or self.task
        if not t:
            return
        with self.lock:
            broken = bool(t.hash) and bool(t.sent_hash) and t.sent_hash != t.hash
        if t.sent < t.size:                                  # 没发完 = iPhone 中途断线
            with self.lock:
                t.state = STATE_ERROR
                t.error = "iPhone 中途断开连接（已发 %.1f%%，%s）" % (
                    t.sent * 100.0 / max(t.size, 1), hfmt(t.sent))
            self.log(t.error, ok=False)
            return
        if broken:                                           # 发完了但流不对 = 数据损坏
            with self.lock:
                t.state = STATE_ERROR
                t.error = "传输流损坏：实际发出字节的 SHA256 与源文件不一致"
            self.log(t.error, ok=False)
            return
        with self.lock:
            t.state = STATE_VERIFY
            t.duration = time.time() - t.t0
        self.log("字节流发送完毕（%s），等待 iPhone 回报字节数" % hfmt(t.sent))

    def finish(self, reported_bytes, t=None):
        """iPhone 回报接收字节数，补齐最后一项校验；完了让队列顶上。"""
        t = t or self.task
        if not t:
            return
        with self.lock:
            tt = t
            tt.report_bytes = reported_bytes
            if tt.sent_hash and tt.hash and tt.sent_hash != tt.hash:
                tt.state = STATE_ERROR
                tt.error = "传输流损坏：发出字节的 SHA256 与源文件不一致"
            elif tt.report_bytes != tt.size:
                tt.state = STATE_ERROR
                tt.error = "验证失败：iPhone 回报 %d 字节，源文件 %d 字节" % (tt.report_bytes, tt.size)
            else:
                tt.state = STATE_DONE
            tt.duration = time.time() - tt.t0
            self.client_state = "已完成"
        ok = tt.state == STATE_DONE
        self.log("%s：%s" % (tt.name, tt.verdict_text()), ok)
        self.log("耗时 %.2f 秒，平均 %.1f MB/s" % (
            tt.duration, tt.size / max(tt.duration, 1e-6) / 1048576.0), ok)
        self._advance()

    def urls(self, t):
        base = "http://%s:%d" % (self.ip, self.port)
        return {
            "hello_url": base + "/hello",
            "file_url": base + "/file/" + t.id,
            "done_url": base + "/done?bytes=%d&id=%s" % (t.size, t.id),
            "fail_url": base + "/fail?error=iphone&id=%s" % t.id,
        }


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    engine = None

    # 快捷指令每走一步，在服务器这边叫什么。只为让人一眼看懂日志。
    STEP_LABEL = {
        "/hello":      "探测服务信息",
        "/hello.txt":  "探测服务信息（纯文本）",
        "w=name":      "第 1 步 取文件名",
        "w=file":      "第 2 步 取下载地址",
        "w=done":      "第 4 步 回报接收完成",
        "w=fail":      "第 4 步 回报接收失败",
    }

    def log_message(self, *a):
        pass

    def _tag(self):
        """把 path 翻译成一句人话，断在哪一步一眼能看出来。"""
        u = urlparse(self.path)
        q = u.query.lower()
        if u.path == "/url":
            # query 是百分号编码过的，先解出来再比对，日志里才看得出填了啥
            w = unquote(q.split("w=")[-1].split("&")[0].strip())
            return self.STEP_LABEL.get("w=" + w, "问了不认识的字段 w=%s" % w)
        if u.path.startswith("/file/"):
            return "第 3 步 下载文件 %s" % u.path[6:]
        if u.path == "/api/shortcut/info":
            return "快捷指令 报家门（info）"
        if u.path == "/api/shortcut/status":
            return "快捷指令 查询状态"
        if u.path == "/api/shortcut/complete":
            return "快捷指令 回报接收完成"
        if u.path.startswith("/api/shortcut/download/"):
            return "快捷指令 下载文件 %s" % u.path.rsplit("/", 1)[-1]
        if u.path == "/done":
            return "回报（旧版 /done）"
        if u.path == "/fail":
            return "回报失败（旧版 /fail）"
        return self.STEP_LABEL.get(u.path,
                                   "请求了不认识的地址 %s（多半是快捷指令里网址手打错了）" % u.path)

    def _who(self):
        try:
            ua = self.headers.get("User-Agent") or ""
        except Exception:
            ua = ""
        ua = ua[:40]
        return "%s（%s）" % (self.client_address[0], ua)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        (self.engine.log if self.engine else _silent_log)(
            "    应答 %s，%d 字节：%s" % (code, len(body),
                                    body.decode("utf-8", "replace")[:160]))
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _text(self, body):
        """纯文本应答。给 iPhone 快捷指令用：不用 JSON，直接按行拆。"""
        (self.engine.log if self.engine else _silent_log)(
            "    应答 200，%d 字节：%r" % (len(body), body[:160]))
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except Exception:
            pass

    def _hello_txt(self):
        """四行纯文本：文件名 / 字节数 / 下载地址 / 回报地址。

        快捷指令里的「从文本获取（按分隔符取第 N 段）」动作能直接拆，
        避开了系统动作库里搜不到「JSON」的坑。
        """
        e = self.engine
        with e.lock:
            t = e.task
        if t is None or t.state not in LIVE_STATES:
            e.log("iPhone 询问纯文本信息，但当前没有待传文件")
            self._text("等待电脑选一个文件\n0\n\n\n")
            return
        u = e.urls(t)
        self._text("%s\n%d\n%s\n%s\n" % (t.name, t.size, u["file_url"], u["done_url"]))

    def _one_field(self):
        """`GET /url?w=name|file|done|fail` — 只返回**一行**值。

        这样 iPhone 快捷指令连「拆分文本」这个动作都不用：
        「获取 URL 内容」拿到的就是它要用的那一段。
        """
        e = self.engine
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        # 网址是手机上手打的，大小写/多空格都会发生，这里宽容一点。
        w = ""
        for k, v in qs.items():
            if k.strip().lower() == "w":
                w = (v[0] if v else "").strip().lower()
        with e.lock:
            t = e.task
        if w in ("done", "fail"):
            self._auto_report(w, t, e)
            return
        if t is None or t.state not in LIVE_STATES:
            if w in ("name", "file", "done"):
                e.log("iPhone 来问 %s，但当前没有待传文件（电脑忘了选文件？）" % w)
                self._text("电脑还没选文件，请在电脑上选好要传的文件")
                return
            self._text("网址里应该带 ?w=name / ?w=file / ?w=done")
            return
        uu = e.urls(t)
        table = {"name": t.name, "file": uu["file_url"],
                 "done": uu["done_url"], "fail": uu["fail_url"]}
        body = table.get(w)
        if body is None:
            body = "网址里应该带 ?w=name / ?w=file / ?w=done"
        self._text(body)

    def _auto_report(self, w, t, e):
        """`?w=done` / `?w=fail` 直接把事办了，快捷指令不用再多走一次请求。

        原设计是「w=done 只把回报地址吐回来，让客户端自己再去访问一次」，
        那意味着快捷指令要多一个「用上一个动作的输出去获取 URL」的动作。
        手机上这个动作最容易接错（截图里用户就是插错了），
        所以改成：字节发完之后，问 w=done 就等于回报，一步闭环。
        """
        if t is None or t.state not in LIVE_STATES:
            e.log("iPhone 要回报 %s，但当前没有可回报的任务" % w)
            self._text("")
            return
        if w == "fail":
            with e.lock:
                t.state = STATE_ERROR
                t.error = "iPhone 端快捷指令报告接收失败"
            e.log(t.error, ok=False)
            self._text("已报告失败")
            return
        with e.lock:
            sent_ok = t.state == STATE_VERIFY and t.sent >= t.size
            got = t.size
        if not sent_ok:                      # 还没发完：把地址吐回去，让它自己访问
            e.log("iPhone 在字节没发完时就问 w=done，改把回报地址给它")
            self._text(e.urls(t)["done_url"])
            return
        e.finish(got)                        # 一步闭回报
        self._text("接收完成，已回报 %d 字节" % got)

    def do_GET(self):
        e = self.engine
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        tid = (e.task.id if e.task else "")
        # 每个请求都留一条痕：快捷指令走到第几步、从哪台设备来。
        # 之前「日志里只有 w=name，第二步开始一条都没有」就是这个看不见导致的。
        if e is not None:
            e.log("收到请求 %s  ← %s  [%s]" % (self.path, self._who(), self._tag()))
        else:
            _silent_log("收到请求 %s  ← %s  [%s]" % (
                self.path, self.client_address[0], self._tag()))
        try:
            if u.path == "/hello":
                self._hello()
            elif u.path == "/hello.txt":
                self._hello_txt()
            elif u.path == "/url":
                self._one_field()
            elif u.path == "/api/shortcut/info":
                self._api_info()
            elif u.path == "/api/shortcut/status":
                self._api_status()
            elif u.path.startswith("/api/shortcut/download/"):
                self._stream()
            elif u.path == "/api/shortcut/complete":
                self._api_complete(qs)
            elif u.path == "/file/" + tid:
                self._stream()
            elif u.path == "/done":
                self._done(qs)
            elif u.path == "/fail":
                self._done(qs, failed=True)
            else:
                self._json({"error": "not found", "path": u.path}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as ex:
            e.log("接口异常：%s" % ex)
            try:
                self._json({"error": str(ex)}, 500)
            except Exception:
                pass

    def do_POST(self):
        e = self.engine
        u = urlparse(self.path)
        if e is not None:
            e.log("收到 POST %s  ← %s" % (self.path, self._who()))
        try:
            if u.path == "/api/shortcut/complete":
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8", "replace") or "{}")
                except Exception:
                    body = {}
                tid = body.get("taskId") or (parse_qs(u.query).get("taskId", [""])[0])
                self._api_complete({"taskId": [tid]})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as ex:
            e.log("POST 接口异常：%s" % ex)
            try:
                self._json({"error": str(ex)}, 500)
            except Exception:
                pass

    # ---- 快捷指令专用 API（LocalSend 风格）----
    # 设计原则：快捷指令越笨越好。字段全部用字符串（"true"/"false"），
    # 下载地址直接给完整 URL（主机名用 MyTransfer.local，手机不用知道 IP）。
    def _api_info(self):
        """GET /api/shortcut/info — 手机第一次连接时的「报家门」。"""
        e = self.engine
        with e.lock:
            t = e.task
        self._json({
            "name": "MyTransfer",
            "version": "1.0",
            "host": SERVICE_NAME + ".local",
            "ip": e.ip,
            "port": e.port,
            "hasFile": "true" if (t and t.state in LIVE_STATES) else "false",
        })

    def _api_status(self):
        """GET /api/shortcut/status — 查询待接收文件。

        多文件队列：files 数组列出所有能取的文件（按队列顺序），
        同时保留 hasFile/fileUrl 等旧字段（=第一个文件），老快捷指令不用改也能用。
        """
        e = self.engine
        with e.lock:
            live = [t for t in e.tasks if t.state in (STATE_WAITING, STATE_READY)]
        if not live:
            e.log("快捷指令查询状态：当前没有待接收文件")
            self._json({"hasFile": "false", "fileName": "", "fileSize": "0",
                        "taskId": "", "fileUrl": "", "state": "idle",
                        "count": "0", "files": []})
            return
        base = "http://%s.local:%d" % (SERVICE_NAME, e.port)
        files = [{
            "kind": file_kind(t.name),
            "taskId": t.id,
            "fileName": t.name,
            "fileSize": str(t.size),
            "fileSizeText": hfmt(t.size),
            "sha256": t.hash or "computing",
            "state": t.state,
            "fileUrl": base + "/api/shortcut/download/" + t.id,
        } for t in live]
        first = files[0]
        e.log("快捷指令查询状态：待接收 %d 个文件（首个 %s）" % (len(files), first["fileName"]))
        self._json({
            "hasFile": "true",
            "count": str(len(files)),
            "files": files,
            # ---- 旧单文件字段（=队首），兼容老快捷指令 ----
            "taskId": first["taskId"],
            "fileName": first["fileName"],
            "fileSize": first["fileSize"],
            "fileSizeText": first["fileSizeText"],
            "sha256": first["sha256"],
            "state": first["state"],
            "kind": first["kind"],
            "fileUrl": first["fileUrl"],
        })

    def _api_complete(self, qs):
        """GET/POST /api/shortcut/complete — 手机报告「收完了」。

        按 taskId 找到队列里的任务确认；确认后把它弹出队列，下一个自动顶上。
        字节数以服务端实际发出的为准，手机不用（也拿不到）精确字节数。
        """
        e = self.engine
        claimed = (qs.get("taskId", [""])[0] or "").strip()
        t = e.get_task(claimed) if claimed else None
        if t is None and not claimed:
            with e.lock:
                live = [x for x in e.tasks if x.state in LIVE_STATES]
            t = live[0] if live else None
        if t is None:
            self._json({"result": "ok", "note": "没有可确认的任务"})
            return
        if claimed and claimed != t.id:
            self._json({"result": "ok", "note": "taskId 不匹配"})
            return
        if t.state == STATE_DONE:
            self._json({"result": "ok", "note": "已经确认过了"})
            return
        with e.lock:
            partial = t.sent < t.size
        if partial:
            with e.lock:
                t.state = STATE_ERROR
                t.error = "手机报告完成，但实际只发出 %.1f%%，不予采信" % (
                    t.sent * 100.0 / max(t.size, 1))
            e.log(t.error, ok=False)
            e._advance()
            self._json({"result": "error", "note": t.error})
            return
        e.finish(t.size, t)       # 用真实发出字节数补最后一项校验；finish 内部会弹出队首
        ok = t.state == STATE_DONE
        with e.lock:
            left = len([x for x in e.tasks if x.state in (STATE_WAITING, STATE_READY)])
        self._json({"result": "done" if ok else "error",
                    "remaining": str(left),
                    "verdict": t.verdict_text()})

    def _hello(self):
        e = self.engine
        with e.lock:
            t = e.task
            payload = {
                "service": SERVICE_NAME,
                "host": SERVICE_NAME + ".local",
                "port": e.port,
                "ip": e.ip,
                "state": (t.state if t else (e.last_state or "waiting")),
                "task": None,
            }
            if t and t.state in (STATE_WAITING, STATE_READY, STATE_TRANSFER):
                payload["task"] = {
                    "id": t.id, "name": t.name, "size": t.size, "sha256": t.hash or "computing",
                }
                payload["task"].update(e.urls(t))
        self._json(payload)

    def _stream(self):
        e = self.engine
        tid = self.path.rsplit("/", 1)[-1].split("?")[0]      # 最后一个路径段就是 taskId
        t = e.get_task(tid)
        if t is None:
            self._json({"error": "没有这个 taskId 的待发送文件"}, 404)
            return
        if t.state in (STATE_VERIFY, STATE_DONE) and t.sent >= t.size:
            # 上一次已经发完但没等到回报（手机中途失败等），这次来取就当重传
            with e.lock:
                t.state = STATE_READY
                t.sent = 0
                t.sent_hash = ""
            e.log("iPhone 重新取文件，按重传处理")
        elif t.state == STATE_ERROR and t.sent < t.size:        # iPhone 上次没接完，这次算重传
            with e.lock:
                t.state = STATE_READY
                t.sent = 0
                t.sent_hash = ""
            e.log("iPhone 重新连接，允许重发")
        if not os.path.isfile(t.path):
            e.log("源文件已不存在，传输中止")
            with e.lock:
                t.state = STATE_ERROR
                t.error = "源文件在发送前被删除"
            self._json({"error": "source gone"}, 410)
            return
        # 文件在选中之后可能还在被别的程序写入（微信/QQ 临时文件很典型），
        # 大小变了的话 Content-Length 就错了，手机会收到截断的坏文件。
        real = os.path.getsize(t.path)
        if real != t.size:
            e.log("警告：文件在选中后大小变了（%d → %d 字节），按当前大小发送" % (t.size, real))
            with e.lock:
                t.size = real
        e.begin_transfer(t)
        e.log("开始发送 %s" % t.name)
        f = open(t.path, "rb")
        h = hashlib.sha256()
        from urllib.parse import quote as _q
        self.send_response(200)
        # 必须按扩展名给真实类型并带文件名：否则 iOS 拿到的是无扩展名的
        # 二进制流，「获取文件扩展名」取出来是空，快捷指令没法分流存相册
        self.send_header("Content-Type", content_type(t.name))
        self.send_header("Content-Disposition",
                         "attachment; filename=\"file%s\"; filename*=UTF-8''%s"
                         % (os.path.splitext(t.name)[1] or "",
                            _q(os.path.basename(t.name))))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                if e.cancel_flag.is_set():
                    break
                buf = f.read(CHUNK)
                if not buf:
                    break
                h.update(buf)
                self.wfile.write(buf)
                self.wfile.flush()
                e.add_bytes(len(buf), t)
        except (BrokenPipeError, ConnectionResetError):
            e.log("iPhone 连接中断，传输停止（已发 %.1f%%）" % (t.sent * 100.0 / max(t.size, 1)))
        except Exception as ex:
            e.log("发送出错：%s" % ex)
        finally:
            try:
                f.close()
            except Exception:
                pass
            with e.lock:
                t.sent_hash = h.hexdigest()
            e.send_finished(t)

    def _done(self, qs, failed=False):
        e = self.engine
        with e.lock:
            t = e.task
        if t is None:
            self._json({"result": "ok"})
            return
        try:
            got = int(qs.get("bytes", ["-1"])[0])
        except ValueError:
            got = -1
        if failed:
            with e.lock:
                t.state = STATE_ERROR
                t.error = "iPhone 端快捷指令报告接收失败"
            e.log(t.error, ok=False)
            self._json({"result": "ok"})
            return
        if t.state == STATE_DONE:                         # 已回报过一次，别重复记账
            self._json({"result": "ok"})
            return
        with e.lock:
            partial = t.sent < t.size                     # 实际没发完，不许靠回报冒充成功
        if partial:
            with e.lock:
                t.state = STATE_ERROR
                t.error = "iPhone 中途断线（只发出 %.1f%%），它的完成回报不予采信" % (
                    t.sent * 100.0 / max(t.size, 1))
            e.log(t.error, ok=False)
            self._json({"result": "ok"})
            return
        e.finish(got)
        self._json({"result": "ok"})


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def get_request(self):
        c, addr = super().get_request()
        try:
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # 关 Nagle，少一截延迟
        except Exception:
            pass
        return c, addr


def start(engine, port=DEFAULT_PORT):
    Handler.engine = engine
    httpd = Server(("0.0.0.0", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd
