# -*- coding: utf-8 -*-
"""按 iPhone 快捷指令（方案 C）的真实顺序自动跑一遍 /url?w= 接口。

快捷指令里的 6 个动作，这里用对应的请求一一对照：
  1. GET /url?w=name  -> 文件名（保存文件时用）
  2. GET /url?w=file  -> 下载地址
  3. GET <下载地址>   -> 真正下载（内容类型=文件）
  4. GET /url?w=done  -> 一步完成「回报字节数」（新版自动回报）
  5. GET /url?w=fail  -> 失败入口（单独一个场景验）
  6. GET /url?w=乱填  -> 提示串

  python tools/url_flow.py 300
"""
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
urllib.request.install_opener(NO_PROXY)

PORT = 8791
FAILPORT = 8792


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


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def get(url, timeout=30):
    return NO_PROXY.open(url, timeout=timeout).read()


def one_field(w, port=PORT, raw_query=None):
    """快捷指令第 1/2 步：取一个字段。中文要百分号编码（手机端也是这么发的）。"""
    if raw_query is not None:      # 故意发个大写的 / 带空格的 query，验服务端容错
        q = raw_query
    else:
        q = "w=" + urllib.parse.quote(w)
    raw = get("http://127.0.0.1:%d/url?" % port + q)
    return raw.decode("utf-8").strip()


def serve(port, rel):
    """起一个只跑服务端（--no-ui）的实例。"""
    logfile = os.path.join(ROOT, "logs", "urllow_%d.log" % port)
    f = open(logfile, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([sys.executable, "-u", "MyTransfer.py", "--no-ui",
                             "--port", str(port), "--send", rel],
                            cwd=ROOT, stdout=f, stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            if json.loads(get("http://127.0.0.1:%d/hello" % port,
                              timeout=3)).get("task"):
                return proc, f
        except Exception:
            time.sleep(0.2)
    proc.kill()
    f.close()
    raise RuntimeError("服务端 %d 起不来：%s" % (port, open(logfile, encoding="utf-8").read()))


def stop(proc, f):
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()
    f.close()


def main():
    size = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    rel = os.path.join(ROOT, "testfiles", "测试%dMB.bin" % size)
    gen(rel, size * 1024 * 1024)
    src_sha = sha(rel)
    src_size = os.path.getsize(rel)

    proc, f = serve(PORT, rel)
    try:
        print("== 快捷指令 6 个动作逐步对照（文件 %d MB）==" % size)
        print("-" * 66)

        name = one_field("name")
        print("[1] 获取URL内容 w=name  ->  %s" % name)

        file_url = one_field("file")
        print("[2] 获取URL内容 w=file  ->  %s" % file_url)

        # 手机上手打网址最常犯的错：W= 写成大写，这里验服务端认不认
        upper = one_field("FILE", raw_query="W=FILE")
        print("[2b] 手抖打成 W=FILE（大写）->  %r" % upper)

        dest = os.path.join(ROOT, "recv", name)
        if os.path.exists(dest):
            os.remove(dest)
        t0 = time.time()
        got = 0
        with NO_PROXY.open(file_url, timeout=120) as r:
            with open(dest, "wb") as fp:
                while True:
                    buf = r.read(1024 * 1024)
                    if not buf:
                        break
                    fp.write(buf)
                    got += len(buf)
        secs = time.time() - t0
        print("[3] 获取URL内容（用上一个动作的输出，类型=文件）-> 收 %d 字节 / %.2f 秒"
              % (got, secs))

        done_txt = one_field("done")
        print("[4] 获取URL内容 w=done  ->  %s" % done_txt)
        time.sleep(0.3)
        after = json.loads(get("http://127.0.0.1:%d/hello" % PORT)).get("state", "")
        print("      这一问就已经把「回报字节数」交了 -> 服务端 state = %s" % after)
        print("[5] 已经 done 之后，再问 w=fail ->  %r（返回空是对的，任务已结束）"
              % one_field("fail"))
        print("[6] w=乱填 ->  %s" % one_field("乱填"))
        print("-" * 66)

        checks = [
            ("w=name 是纯文本文件名", name == os.path.basename(rel)),
            ("w=file 是指向 /file/<id> 的完整网址",
             file_url.startswith("http") and "/file/" in file_url),
            ("下载地址返回字节数等于源", got == src_size == size * 1024 * 1024),
            ("落盘 SHA256 与源一致", sha(dest) == src_sha),
            # 队列化之后，回报完任务立即弹出，/hello 回到 waiting；
            # 改看 w=done 的响应文本（finish 成功才会说「接收完成」）
            ("只问一次 w=done 就完成回报", done_txt.startswith("接收完成")),
            ("w=乱填给出提示串", one_field("乱填").startswith("网址里应该带 ?w=")),
            ("大写 W=FILE 也认", upper == file_url),
            ("任务结束后 w=fail 返回空", one_field("fail") == ""),
        ]
    finally:
        stop(proc, f)

    print()
    print("结果表：")
    for k, v in checks:
        print("  %-32s %s" % (k, "✓" if v else "✗ 失败"))

    # 单独一个场景验失败入口：字节还没发完时就问 w=fail
    proc2, f2 = serve(FAILPORT, rel)
    try:
        fail_txt = one_field("fail", port=FAILPORT)
        time.sleep(0.3)
        st = json.loads(get("http://127.0.0.1:%d/hello" % FAILPORT)).get("state", "")
    finally:
        stop(proc2, f2)
    print()
    print("== 场景 2：快捷指令报告接收失败（场景 A 传完之后才问才有效，这里单独验）==")
    print("   w=fail 返回 %r -> 服务端 state = %s" % (fail_txt, st))
    checks.append(("w=fail 让服务端判 error", st == "error"))

    print()
    print("结论：%s" % ("通过 ✓" if all(x[1] for x in checks) else "有失败项 ✗"))


if __name__ == "__main__":
    main()
