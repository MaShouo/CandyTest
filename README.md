# CandyTest

批量测试多个 OpenAI 兼容 **Responses API** 中转站的回答稳定性，支持 Windows 本地运行和 Docker 服务器部署。

CandyTest 会让 pi CLI 或 Codex CLI 用同一道题测试所选中转站，并汇总正确率、耗时、Token、完整回答和错误信息。浏览器不会直接连接中转站。

## 主要功能

- 同时选择多个中转站，支持一键全选。
- 支持串行或并行测试；并行模式为“站间并行、站内串行”。
- 回答中出现独立数字 `21` 即判为正确；`121`、`210` 不算。
- API 调用失败会保存为 ERROR 记录，但不计入当前任务或历史正确率；正确率低于 80% 时显示提醒。
- 可中断正在运行的 pi/Codex 子进程，并保留已完成结果。
- 支持 Clash HTTP/HTTPS 代理。
- 支持手动 WebDAV 完整镜像 Push/Pull。
- 服务器模式使用账号密码登录，初始用户名和密码均为 `admin`，登录后可修改。

项目使用 Python、Flask、Waitress、Requests、SQLite 和原生前端，不需要 Node 前端构建链。

## Windows 本地运行

需要 Python 3.10 或更高版本，并在 `PATH` 中安装至少一个 CLI：

```powershell
pi --version
codex --version
```

直接双击根目录的 `start.bat`。首次启动会自动创建 `.venv`、安装依赖并打开：

```text
http://127.0.0.1:8765/
```

也可以手动启动：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python run.py
```

Linux/macOS 本地启动：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python run.py
```

本地模式不要求登录，并且只允许监听回环地址。可用 `CANDYTEST_PORT` 修改端口。

## Docker 服务器部署

服务器版是单个 CandyTest 容器：

```text
浏览器 → 你的 HTTPS 反向代理 → 127.0.0.1:8765 → CandyTest
```

仓库**不包含 Nginx、Caddy、证书或反向代理配置**。Compose 默认只把端口绑定到宿主机 `127.0.0.1`，请使用你自己的 HTTPS 反向代理访问。

### 1. 启动

```bash
git clone https://github.com/MaShouo/CandyTest.git
cd CandyTest
cp .env.example .env
docker compose up -d --build
```

检查状态：

```bash
docker compose ps
docker compose logs --tail=100 candytest
curl http://127.0.0.1:8765/healthz
```

默认登录信息：

```text
用户名：admin
密码：admin
```

首次登录后，请立即进入 **设置 → 登录账号** 修改用户名和密码。新密码至少 8 个字符。修改后，其他旧会话会自动失效。

服务器模式默认启用 Secure Session Cookie，应通过 HTTPS 域名登录。仅在临时 HTTP 调试时将 `.env` 中的 `COOKIE_SECURE` 改为 `0`，公网使用时必须改回 `1`。

> 从旧版本升级时，`.env` 中已有的 `ADMIN_USERNAME`、`PASSWORD_HASH_B64` 和 `SECRET_KEY` 会在首次创建 `auth.json` 时迁移；之后以 `auth.json` 为准。新部署无需填写这些值。

### 2. 容器内 CLI

镜像内固定安装：

- `@earendil-works/pi-coding-agent@0.84.2`
- `@openai/codex@0.147.0`

验证命令：

```bash
docker compose exec candytest pi --version
docker compose exec candytest codex --version
```

可在 `.env` 中覆盖构建版本，然后重新构建镜像。

### 3. 更新或停止

```bash
git pull
docker compose up -d --build
```

停止但保留数据：

```bash
docker compose down
```

不要执行 `docker compose down -v`，否则会删除保存数据的 named volume。

## 使用方法

1. 点击“添加中转站”，填写名称、Responses API Base URL、API Key 和默认模型。
2. 勾选需要测试的中转站，或点击表头“全选”。停用的中转站不会被选中。
3. 选择 pi/Codex、测试轮数、Reasoning effort 和调度方式。
4. 点击“开始测试”，在当前任务和历史记录中查看结果。

中转站 URL 会保留你填写的路径，不会自动追加 `/v1`。编辑中转站时，API Key 留空表示保留原值；删除中转站不会删除历史结果。

## 设置

主界面的“设置”页面使用左侧分类：

- **WebDAV**：保存连接信息、测试连接并检查远端状态。
- **Clash 代理**：设置后续 pi/Codex 和 WebDAV 请求使用的代理。
- **登录账号**：仅服务器模式显示，用于修改管理员用户名和密码。

WebDAV 的 **Push** 和 **Pull** 按钮保留在主界面，并位于“设置”按钮旁边。

### Clash 代理

支持以下格式的 HTTP/HTTPS 代理：

```text
http://localhost:7890
```

不支持 SOCKS5、代理认证、路径或查询参数。Docker 容器中的 `localhost` 指容器自身；如果 Clash 在宿主机运行，通常应填写：

```text
http://host.docker.internal:7890
```

同时需要确保 Clash 允许来自 Docker 网桥的连接。

### WebDAV 同步

WebDAV **没有启动自动 Pull，也没有退出自动 Push**。只有点击主界面的 Push/Pull 后才会同步。

完整镜像包含：

- 中转站和明文 API Key；
- Clash 代理设置；
- 全部任务、回答和历史；
- SQLite 中的其他应用数据。

使用步骤：

1. 在 **设置 → WebDAV** 中填写服务地址、远端目录、用户名和密码。
2. 点击“保存并测试连接”。
3. 第一台设备在主界面点击“Push 覆盖云端”。
4. 其他设备配置相同远端后，点击“Pull 覆盖本机”。

注意：

- Pull 不会自动备份，会完整覆盖当前设备的 SQLite 数据。
- 普通 Push 检测到远端 revision 变化时会拒绝，需要再次确认才能强制覆盖。
- 测试任务与同步互斥。
- 单个快照上限为 500 MiB。
- WebDAV 服务需支持 `PROPFIND`、`MKCOL`、`PUT`、`GET` 和 `DELETE`。

OpenList 的 WebDAV 地址通常类似：

```text
服务地址：https://你的域名/dav/quark/
远端目录：CandyTest
```

普通网页路径不是 WebDAV 入口，填错时通常会返回 HTTP 405。

## 数据与安全

默认数据目录：

- 本地模式：`~/.candytest/`
- Windows：`C:\Users\<用户名>\.candytest\`
- Docker：named volume 中的 `/data/`

主要文件：

```text
candytest.sqlite3   # 中转站、API Key、代理、任务与历史
webdav.json         # WebDAV 地址、用户名、明文密码和同步 revision
auth.json           # 服务器用户名、密码哈希、会话密钥和认证版本
```

`auth.json` 和 `webdav.json` 都是当前设备的本地配置，不会随 WebDAV 数据库快照同步。管理员密码只保存 Werkzeug 哈希，不保存明文；API Key、WebDAV 密码和测试回答仍是明文数据。请保护数据目录和 Docker volume，不要把 `.env`、数据库或这些 sidecar 文件提交到仓库。

服务器模式还提供 CSRF 校验、登录限速、HttpOnly/SameSite Cookie、CSP、iframe 禁止、`nosniff`、no-referrer 和 Permissions-Policy。HTTPS、域名、访问日志和外层限流仍由你的反向代理负责。

## 常见问题

| 现象 | 处理方法 |
| --- | --- |
| 本地页面显示 CLI 未安装 | 在启动 CandyTest 的同一终端运行 `pi --version`、`codex --version` 并检查 PATH。 |
| 登录后立即回到登录页 | 使用 HTTPS；仅临时 HTTP 调试时设置 `COOKIE_SECURE=0`。 |
| Nginx 返回 502 | 检查容器状态，并从宿主机访问 `http://127.0.0.1:8765/healthz`。 |
| WebDAV 返回 405 | 使用服务的 WebDAV 入口；OpenList 通常是 `/dav/`。 |
| WebDAV 返回 401/403 | 检查 WebDAV 用户名、密码和目录写权限。 |
| 测试全部 401/ERROR | 检查中转站 URL、API Key、模型 ID 和 Responses API 兼容性。 |
| Docker 中 Clash 连接失败 | 使用 `host.docker.internal`，并允许 Docker 网桥访问。 |
| 端口被占用 | 修改 `.env` 中的 `CANDYTEST_PORT`，并同步修改反向代理 upstream。 |

## 测试

离线测试不会调用真实模型或 WebDAV：

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

CI 会在 Windows 和 Linux 上运行测试，并构建 Docker 镜像检查默认登录、账号修改、健康检查以及容器内 pi/Codex 命令。
