# -*- coding: utf-8 -*-
"""快捷指令 API 全链路测试（只走 127.0.0.1 回环，不发任何局域网/多播包）。

模拟快捷指令的真实四步：
  1. GET /api/shortcut/status      → 有文件，拿到 fileUrl / fileName / taskId
  2. GET /api/shortcut/download/<taskId> → 流式下载
  3. 校验下载内容 SHA256 == 状态里给的 sha256
  4. GET /api/shortcut/complete    → 回报完成，服务端判 done
外加「无文件时 complete」与「POST 版 complete」分支。
"""
import hashlib
import io
import json
import os
import sys
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)
sys.path.insert(0, SRC)
sys.stdout.reconfigure(encoding="utf-8")

import mt_server as S

PORT = 8797
BASE = "http://127.0.0.1:%d" % PORT
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

results = []


def ok(label, cond, extra=""):
    results.append(cond)
    print("%s %s %s" % ("✓" if cond else "✗", label, extra))


def main():
    # 准备测试文件：3.5MB（跨两个 4MB 分块边界以内，再补一个大点的也行）
    path = os.path.join(HERE, "_tmp_apitest.bin")
    data = os.urandom(3 * 1024 * 1024 + 700 * 1024)
    with open(path, "wb") as f:
        f.write(data)
    src_hash = hashlib.sha256(data).hexdigest()

    eng = S.Engine(S.local_ip(), PORT)
    S.start(eng, PORT)
    import time
    time.sleep(0.3)
    eng.start_task(path)
    for _ in range(50):
        if eng.task.state == S.STATE_READY:
            break
        time.sleep(0.1)

    # [1] status
    st, b = _get("/api/shortcut/status")
    d = json.loads(b.decode("utf-8"))
    ok("status hasFile=true", d.get("hasFile") == "true", str(d))
    ok("status fileName", d.get("fileName") == os.path.basename(path))
    ok("status fileSize", d.get("fileSize") == str(len(data)))
    ok("status sha256", d.get("sha256") == src_hash)
    ok("status fileUrl 含 download/<id>", ("/api/shortcut/download/" + d["taskId"]) in d.get("fileUrl", ""))

    # [2] 下载
    st, body = _get("/api/shortcut/download/" + d["taskId"])
    ok("download 200 + 长度一致", st == 200 and len(body) == len(data),
       "%s %d/%d" % (st, len(body), len(data)))
    dl_hash = hashlib.sha256(body).hexdigest()
    ok("下载内容 SHA256 == 源", dl_hash == src_hash)

    # [3] complete（GET 版，带 taskId）
    the_task = eng.task
    st, b = _get("/api/shortcut/complete?taskId=" + d["taskId"])
    r = json.loads(b.decode("utf-8"))
    ok("complete GET → done", st == 200 and r.get("result") == "done", str(r))
    ok("任务状态 = done", the_task.state == S.STATE_DONE, the_task.verdict_text())
    ok("完成后队列弹出", eng.task is None, "剩余 %d" % len(eng.tasks))

    # [4] complete 再来一次（幂等）
    st, b = _get("/api/shortcut/complete?taskId=" + d["taskId"])
    ok("complete 幂等", b'"ok"' in b or b"ok" in b, b.decode("utf-8"))

    # [5] 无文件 status
    eng.clear()
    st, b = _get("/api/shortcut/status")
    d2 = json.loads(b.decode("utf-8"))
    ok("无文件 status hasFile=false", d2.get("hasFile") == "false")

    # [6] 无文件 complete
    st, b = _get("/api/shortcut/complete")
    ok("无文件 complete 不炸", st == 200)

    # [7] POST 版 complete（新任务）
    eng.start_task(path)
    for _ in range(50):
        if eng.task.state == S.STATE_READY:
            break
        time.sleep(0.1)
    st, b = _get("/api/shortcut/status")
    d3 = json.loads(b.decode("utf-8"))
    st, body = _get("/api/shortcut/download/" + d3["taskId"])
    st, b = _post("/api/shortcut/complete", {"taskId": d3["taskId"]})
    r = json.loads(b.decode("utf-8"))
    ok("POST complete → done", st == 200 and r.get("result") == "done", str(r))
    ok("POST 后任务状态 = done", d3 and eng.get_task(d3["taskId"]) is None)


    # [8] 多文件队列
    eng.start_task(path)
    eng.start_task(path)
    st, b = _get("/api/shortcut/status")
    d4 = json.loads(b.decode("utf-8"))
    ok("多文件 count=2", d4.get("count") == "2", str(d4.get("count")))
    ok("多文件 files 数组", isinstance(d4.get("files"), list) and len(d4["files"]) == 2)
    ok("旧字段=队首", d4.get("taskId") == d4["files"][0]["taskId"])
    # 逐个下载+回报（模拟快捷指令循环）
    all_done = True
    for f in d4["files"]:
        st, body = _get("/api/shortcut/download/" + f["taskId"])
        if st != 200 or len(body) != len(data):
            all_done = False
        _get("/api/shortcut/complete?taskId=" + f["taskId"])
    ok("多文件逐个下载+确认", all_done)
    st, b = _get("/api/shortcut/status")
    d5 = json.loads(b.decode("utf-8"))
    ok("发完后队列清空", d5.get("hasFile") == "false" and d5.get("count") == "0")

    os.remove(path)
    print()
    print("通过 %d / %d" % (sum(1 for x in results if x), len(results)))
    return 0 if all(results) else 1


def _get(p, method="GET"):
    req = urllib.request.Request(BASE + p, method=method)
    try:
        r = op.open(req, timeout=15)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post(p, obj):
    req = urllib.request.Request(BASE + p, method="POST",
                                 data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        r = op.open(req, timeout=15)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


if __name__ == "__main__":
    sys.exit(main())
