# MyTransfer（握手）

局域网文件传输工具：**Windows 电脑为主控端，iPhone 用一个快捷指令接收**。
不打开网页、不输 IP、不填端口，点一下快捷指令就完成「连接 → 下载 → 保存 → 校验」。

## 功能

- **自动发现**：电脑通过 mDNS/Bonjour 广播 `MyTransfer._http._tcp.local`（含主机名 A 记录），iPhone 直接用 `MyTransfer.local` 域名连接，换网络不用改任何配置。
- **多文件队列**：电脑一次排多个文件；支持「待发送」文件夹自动监控 + 勾选发送。
- **流式传输**：4MB 分块发送，内存占用不随文件大小增长（500MB 实测峰值 ~64MB）。
- **自动分流保存**：电脑按扩展名给文件标 `kind`（photo/video/file），
  iPhone 快捷指令按 kind 分流——照片/视频进**相册**，其它进**文件 App 的 MyTransfer**。
- **完整性校验不造假**：源文件 SHA256 + 实际发出流 SHA256 + 字节数三方比对；
  断线、谎报完成一律判失败。
- **每一步可见**：快捷指令的每次请求在电脑日志里都有人话标签（查询状态/下载文件/回报完成）。

## 快速开始

### 电脑（Windows 10）

1. 运行 `MyTransfer.py`（需 Python 3.10+ 与 tkinter），或用 PyInstaller 打包成单文件 exe（见下）。
2. 窗口里选文件，或把文件丢进程序目录的「待发送」文件夹再勾选发送。
3. 首次运行会自动添加防火墙规则（8765/TCP + 5353/UDP）。

### iPhone（快捷指令）

**不要手搭**，看 [`docs/快捷指令创建指南.md`](docs/快捷指令创建指南.md)——
17 个动作、每一步填什么都写好了（含全部踩坑对照表），照抄即可。

## 目录结构

```
├── MyTransfer.py            # 入口（UI/无 UI 模式、日志落盘）
├── mt_server.py             # HTTP 传输引擎 + mDNS Responder + 任务队列 + API
├── mt_ui.py                 # tkinter 界面
├── 启动 MyTransfer.bat      # 双击启动
├── 握手.ico                 # 图标
├── docs/
│   ├── 快捷指令创建指南.md    # ← iPhone 端照抄搭建，推荐
│   ├── API.md               # 快捷指令专用接口文档
│   ├── 使用说明.md / 测试结果.md / iPhone 快捷指令.md
└── tools/
    ├── test_shortcut_api.py # API 全链路测试（20/20）
    ├── run_test.py / url_flow.py
    └── ...
```

## 快捷指令 API 一览

| 接口 | 说明 |
|---|---|
| `GET /api/shortcut/status` | 队列查询：`files` 数组（taskId/fileName/fileSize/kind/fileUrl）+ 旧单文件字段 |
| `GET /api/shortcut/download/{taskId}` | 流式下载，真实 Content-Type + Content-Disposition |
| `GET/POST /api/shortcut/complete` | 回报完成，三方校验后判定 done/error，幂等 |
| `GET /api/shortcut/info` | 服务信息 |

详见 [`docs/API.md`](docs/API.md)。

## 从源码打包 exe

```bat
pip install pyinstaller
pyinstaller --onefile --noconsole --name 握手 --icon 握手.ico MyTransfer.py
```

（exe 与源码同目录运行：日志在 `logs\`，待发送文件夹在 `待发送\`。）

## 已知限制

- 一次性接收一个队列；不支持断点续传（中断后点重新运行即可，PC 端整段重发）。
- 速度取决于 Wi-Fi（电脑端回环实测 80+ MB/s，非瓶颈）。
