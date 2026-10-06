# -*- coding: utf-8 -*-
"""把 tools/MyTransfer.bat.tpl 转成桌面启动器 Desktop\\MyTransfer.bat。

两个坑（踩过）：
  1. cmd 默认代码页 936(GBK)，模板里的中文必须按 GBK 写，否则双击是乱码
  2. Write 工具出来是 LF，cmd 要 CRLF，否则双击只闪一下就退出
改完模板重新跑一次这个脚本即可。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
TPL = os.path.join(HERE, "MyTransfer.bat.tpl")
DST = os.path.join(os.path.expanduser("~"), "Desktop", "MyTransfer.bat")


def regen(out=DST, src=TPL):
    text = open(src, encoding="utf-8").read()
    raw = text.encode("gbk", "replace").replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    with open(out, "wb") as f:
        f.write(raw)
    return out, len(raw)


def check(path):
    b = open(path, "rb").read()
    return (b.count(b"\r\n"), b.count(b"\n") - b.count(b"\r\n"))


if __name__ == "__main__":
    out, n = regen()
    crlf, bare = check(out)
    print("已生成：%s（%d 字节，CRLF=%d 裸LF=%d）" % (out, n, crlf, bare))
    sys.exit(0 if bare == 0 else 1)
