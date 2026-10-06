# MyTransfer 快捷指令 API

Windows 端为 iPhone 快捷指令提供的专用接口（LocalSend 风格）。所有接口都在
`http://MyTransfer.local:8765` 下，手机端**不需要知道 IP、不需要填端口以外的任何东西**
（主机名 `MyTransfer.local` 由电脑端 mDNS/Bonjour 广播自动解析，电脑端会回答
主机名 A 记录 + 服务记录）。

单任务模型：电脑一次只放一个待传文件（在电脑 UI 里选文件即入队），手机来取。

## GET /api/shortcut/info

手机第一次连接时的「报家门」。

```json
{"name": "MyTransfer", "version": "1.0", "host": "MyTransfer.local",
 "ip": "192.168.0.200", "port": 8765, "hasFile": "false"}
```

## GET /api/shortcut/status

查询有没有待接收文件。**所有字段一律字符串**，快捷指令不用做类型转换。

有文件：
```json
{"hasFile": "true", "taskId": "9798d8d3", "fileName": "llama.exe",
 "fileSize": "3862528", "fileSizeText": "3.7 MB",
 "sha256": "36ccf9...", "state": "ready",
 "fileUrl": "http://MyTransfer.local:8765/api/shortcut/download/9798d8d3"}
```

没有文件：
```json
{"hasFile": "false", "fileName": "", "fileSize": "0",
 "taskId": "", "fileUrl": "", "state": "idle"}
```

## GET /api/shortcut/download/{taskId}

下载文件本体。`Content-Length` 固定，服务端 4MB 分块流式发送，
内存占用不随文件大小增长。taskId 与当前任务不符时返回 404。

## GET 或 POST /api/shortcut/complete

手机报告「接收完成」。POST 版可带 JSON `{"taskId": "..."}`，GET 版带
`?taskId=...`，都不带也行（单任务模型）。

服务端收到后做最终校验（不伪造成功）：
- 实际发出字节 < 文件大小 → 判 `error`（手机谎报也没用）；
- 发出流的 SHA256 与源文件不一致 → 判 `error`；
- 手机回报后字节数对上 → `done`。

响应：
```json
{"result": "done", "verdict": "字节数 3862528/3862528 一致；发送流 SHA256 与源文件一致"}
```

重复调用幂等：`{"result": "ok", "note": "已经确认过了"}`。

## 与快捷指令动作的对应

| 快捷指令动作 | 调用的 API |
|---|---|
| 获取 URL 内容（status） | `GET /api/shortcut/status` |
| 获取词典值 hasFile / fileName / fileUrl | （解析 status 的 JSON） |
| 获取 URL 内容（fileUrl） | `GET /api/shortcut/download/{taskId}` |
| 保存文件 | （本地保存到「文件 → 我的 iPhone → MyTransfer」） |
| 获取 URL 内容（complete） | `GET /api/shortcut/complete` |
| 显示通知 | （本地提示） |

## 自动发现（为什么手机不用填 IP）

电脑端在 5353/UDP 广播 Bonjour 服务 `MyTransfer._http._tcp.local`，并同时回答
`MyTransfer.local` 的 A 记录（主机名 → IP）。iOS 拿到 `http://MyTransfer.local:8765/...`
后由系统自动解析出电脑 IP，快捷指令里固定写这个域名即可，换网络、换 IP 都不用改。

## 测试

`tools/test_shortcut_api.py`（只走 127.0.0.1 回环，不发局域网包）覆盖：
status 有/无文件、流式下载、SHA256 校验、GET/POST complete、幂等、taskId 不匹配。
最新结果：**14/14 通过**。
