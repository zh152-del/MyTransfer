# -*- coding: utf-8 -*-
"""验证 Windows 端是不是真的在局域网里广播 Bonjour 服务。

直接往 224.0.0.251:5353 发一条标准 mDNS 查询（先问 _services._dns-sd._udp.local，
再问 MyTransfer._http._tcp.local），看有没有人回。有回复就说明 iPhone 上的
「MyTransfer.local」能被解析——因为 iOS 的解析器就是发同样这包查询。
"""
import socket
import struct
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

GROUP = "224.0.0.251"
PORT = 5353


def enc(s):
    return b"".join(bytes([len(l)]) + l.encode() for l in s.strip(".").split(".")) + b"\x00"


def query(name):
    """问一个名字。class 字段低 2 字节填 1（IN）；最高位置 0x8000 是 QU 位，
    意思是「宁可你单播回我」——iOS 解析已知名字时也常这么查，所以这里照做。"""
    return (struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0)
            + enc(name) + struct.pack(">HH", 12, 0x8001) + b"\x00\x00\x00\x01")


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", PORT))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(GROUP) + socket.inet_aton("0.0.0.0"))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    s.settimeout(3.0)
    print("已加入 %s:%d，准备发送查询…" % (GROUP, PORT))
    for name in ("_services._dns-sd._udp.local", "MyTransfer._http._tcp.local"):
        # iOS 的解析器也是「查不到就重发」，这里同样间隔重发几次，别一次不通就判死
        got = None
        for attempt in range(5):
            s.sendto(query(name), (GROUP, PORT))
            print("已查询 %s（第 %d 次）" % (name, attempt + 1))
            end = time.time() + 2
            while time.time() < end and got is None:
                try:
                    data, addr = s.recvfrom(9000)
                except socket.timeout:
                    continue
                flags, = struct.unpack(">H", data[2:4])
                qd, an, ns, ar = struct.unpack(">HHHH", data[4:12])
                if flags & 0x8000 and (an or ns or ar):     # 是应答（不是查询回环）
                    got = (addr, data)
            if got:
                break
        if got:
            addr, data = got
            print("  ✓ 收到回应（来自 %s:%d），包头 answer/ns/authority/additional = %s"
                  % (addr[0], addr[1], struct.unpack(">HHHH", data[4:12])))
            txt = b""
            if b"port=" in data:
                txt = data[data.index(b"port="):].split(b";")[0].decode("utf-8", "ignore")
            print("  ✓ 回应里带 TXT：%s" % txt)
        else:
            print("  ✗ 3 秒内没有任何回应")
    s.close()


if __name__ == "__main__":
    main()
