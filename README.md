# CandyTest：中转站 GPT 智商检测

CandyTest 用同一道糖果题测试多个 OpenAI 兼容 **Responses API** 中转站的回答稳定性。浏览器不会直接请求中转站；每一轮都由运行 CandyTest 的机器通过 **pi CLI** 或 **Codex CLI** 发起。

- 多选中转站，可选串行或并行测试；并行为“站间并行、站内串行”。
- 回答中出现独立数字 `21` 即判为正确；`121`、`210` 不算。
- 显示本轮和按中转站汇总的历史正确率；低于 80% 显示红色提示。
- ERROR 单独计数，不计入正确率分母；完整回答、耗时、token 和错误可展开复核。
- 支持中断运行中的 pi/Codex 子进程并保留已完成结果。
- 支持手动 WebDAV 完整镜像 Push/Pull；**不会自动同步**。
- 同时支持 Windows 本地运行和 Linux Docker 服务器运行。

项目使用 Python、Flask、Waitress、Requests、SQLite 和原生前端。CandyTest 自身不编译 Rust，也不需要 Node 前端构建链；服务器镜像直接安装已发布的 pi/Codex npm 包。

## 一、本地运行

需要 Python 3.10 或更高版本，并在本机 `PATH` 中安装至少一个 CLI：

```powershell
pi --version
codex --version
```

### Windows 一键启动

直接双击根目录的 `start.bat`。首次运行会创建 `.venv`、安装 Python 依赖，然后在浏览器打开：

```text
http://127.0.0.1:8765/
```

也可手动启动：

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

本地模式默认只监听回环地址且不要求登录。可调整端口或切换 IPv6 回环：

```powershell
$env:CANDYTEST_PORT = "9000"
$env:CANDYTEST_HOST = "::1"
python run.py
```

`local` 模式拒绝监听公网或局域网地址。

## 二、Linux Docker 服务器部署

服务器版是一个独立 CandyTest 容器：

```text
浏览器 → 你自己的 Nginx/HTTPS → 127.0.0.1:8765 → CandyTest 容器
```

仓库**不包含 Nginx、Caddy、证书或反向代理配置**。宿主机端口默认只绑定 `127.0.0.1`，请让你自己的 Nginx 转发到该地址。

镜像内固定安装：

- `@earendil-works/pi-coding-agent@0.84.2`
- `@openai/codex@0.147.0`

它们运行在容器内部，不调用宿主机上的 CLI。可在 `.env` 中覆盖构建版本，然后重新构建镜像。

### 1. 准备文件和工具镜像

```bash
git clone https://github.com/MaShouo/CandyTest.git
cd CandyTest
cp .env.example .env
docker build --tag candytest:local .
```

先直接构建工具镜像，是为了在尚未填写运行凭据时安全生成密码哈希和会话密钥。

### 2. 生成登录凭据

生成 **Base64 编码的 Werkzeug 密码哈希**；命令会交互式读取密码：

```bash
docker run --rm -it \
  --entrypoint /opt/venv/bin/python candytest:local \
  -c "import base64; from getpass import getpass; from werkzeug.security import generate_password_hash; h=generate_password_hash(getpass('Password: ')); print(base64.b64encode(h.encode()).decode())"
```

生成会话签名密钥：

```bash
docker run --rm \
  --entrypoint /opt/venv/bin/python candytest:local \
  -c "import secrets; print(secrets.token_hex(32))"
```

编辑 `.env`，空值必须全部替换：

```dotenv
ADMIN_USERNAME=你的管理员用户名
PASSWORD_HASH_B64=上一步生成的Base64字符串
SECRET_KEY=上一步生成的64位十六进制字符串
COOKIE_SECURE=1
```

Base64 只是避免 Compose 误解析 Werkzeug 哈希中的 `$`，不是对密码哈希的加密。管理员密码明文本身不会写入 `.env`，但容器环境仍可被宿主机 root 通过 Docker 管理接口读取。不要提交 `.env`。

### 3. 启动

```bash
docker compose up -d --build

docker compose ps
docker compose logs --tail=100 candytest
curl http://127.0.0.1:8765/healthz
```

健康检查只返回：

```json
{"status":"ok"}
```

将你自己的 Nginx upstream 指向：

```text
http://127.0.0.1:8765
```

`compose.yaml` 将宿主端口固定绑定到回环地址，并默认限制容器内存为 2 GiB；可通过 `.env` 的 `CANDYTEST_PORT` 和 `CANDYTEST_MEMORY_LIMIT` 调整端口及内存上限。

服务器模式默认使用 Secure Session Cookie，因此应从你的 **HTTPS 域名**登录。若只为临时排查而直接通过 HTTP 访问，可暂时设置 `COOKIE_SECURE=0`；公网使用时必须改回 `1`。

### 4. 验证容器内 CLI

```bash
docker compose exec candytest pi --version
docker compose exec candytest codex --version
```

页面顶部也会显示两个 CLI 是否可用。CandyTest 每轮都会创建隔离的临时 pi/Codex 配置，将中转站 Key 仅通过子进程环境变量传入。

### 5. 更新与停止

```bash
git pull
docker compose up -d --build
```

停止但保留数据：

```bash
docker compose down
```

不要执行：

```bash
docker compose down -v
```

`-v` 会删除保存 SQLite 数据库的 named volume。

### 服务器登录安全

- `server` 模式启动时必须提供管理员用户名、Werkzeug 密码哈希和强随机 Secret Key，否则拒绝启动。
- 除 `/login`、静态文件和最小化 `/healthz` 外，页面及 API 都要求登录。
- 所有数据修改请求都有 CSRF 校验。
- Session Cookie 为 HttpOnly、SameSite=Lax，并默认开启 Secure。
- 应用增加 CSP、禁止 iframe、`nosniff`、no-referrer 和 Permissions-Policy。
- 登录限速按直接连接 IP 在进程内计算；应用不会信任客户端伪造的 `X-Forwarded-For`。
- HTTPS、域名、访问日志和外层限流仍由你自己的 Nginx 管理。

## Clash 代理

页面可配置全局 HTTP/HTTPS 代理，例如：

```text
http://localhost:7890
```

代理应用于之后启动的 pi/Codex 测试和 WebDAV 请求。只支持 `http://` 或 `https://` 代理 URL，不支持 SOCKS5、代理认证、路径或查询参数。

Docker 中 `localhost` 指容器自身。若 Clash 在 Docker 宿主机运行，请通常填写：

```text
http://host.docker.internal:7890
```

`compose.yaml` 已提供 `host.docker.internal:host-gateway` 映射；同时需要确保宿主机 Clash 允许来自 Docker 网桥的连接。

## WebDAV 手动完整同步

WebDAV 同步只会在页面点击按钮后执行，**没有启动自动 Pull，也没有退出自动 Push**。

完整镜像包含：

- 中转站和明文 API Key；
- Clash 代理设置；
- 全部任务、完整回答和历史；
- 软删除记录及其他 SQLite 数据。

WebDAV 连接配置保存在运行设备的数据目录中的 `webdav.json`，包含 URL、用户名和明文密码，但 API 不会返回密码。这个 sidecar 不会上传。

远端布局：

```text
<远端目录>/
├── manifest.json
└── revisions/
    └── <revision UUID>/
        └── candytest.sqlite3
```

使用步骤：

1. 填写 WebDAV 服务父地址、远端目录、用户名和密码；
2. 点击“保存并测试连接”；
3. 第一台设备点击“Push 覆盖云端”；
4. 其他设备配置相同远端后点击“Pull 覆盖本机”。

重要语义：

- Push 使用 SQLite Backup API 生成包含 WAL 已提交数据的一致快照，上传并回读校验 SHA-256，最后才激活 manifest。
- 普通 Push 检测到远端 revision 变化时会拒绝；必须再次确认才能强制覆盖。
- Pull 校验 manifest、大小、SHA-256、SQLite 完整性和数据库版本后原子替换本地数据库。
- Pull **不会自动备份**，会完整覆盖当前设备数据。
- 测试任务与同步互斥。
- 单个快照上限为 500 MiB；WebDAV 服务需支持 `PROPFIND`、`MKCOL`、`PUT`、`GET` 和 `DELETE`。

OpenList 的 WebDAV 入口通常是：

```text
https://你的域名/dav/
```

例如挂载路径是 `/quark` 时，可填写：

```text
WebDAV 服务地址：https://你的域名/dav/quark/
远端目录：CandyTest
```

普通网页路径 `/quark/` 不是 WebDAV 入口，使用它通常会得到 HTTP 405。

## 中转站与测试语义

每个中转站需要：名称、Responses API Base URL、API Key 和默认模型 ID。程序保留 URL 路径且不会自动追加 `/v1`。编辑时 Key 留空表示保留；删除为软删除，历史仍保留。

- **串行**：一个站点全部轮次完成后再执行下一站。
- **并行**：不同站点并行，同一站点轮次始终串行。
- 同时只能运行一个测试任务。
- 中断会终止当前 CLI 子进程树、停止后续轮次，并保留已完成结果。
- 单轮默认超时 300 秒；超时、401、429、CLI 缺失和协议错误记录为 ERROR，不计入正确率分母。
- 历史正确率按中转站聚合，跨任务、CLI 和模型统计。

## 数据和明文凭据

API Key、WebDAV 密码和测试回答都不会加密：

- 本地模式默认目录：`~/.candytest/`
- Windows 通常为：`C:\Users\<用户名>\.candytest\`
- Docker 服务器模式：named volume 中的 `/data/`

主要文件：

```text
candytest.sqlite3   # 中转站、API Key、代理、任务与历史
webdav.json         # WebDAV URL、用户名、密码和同步 revision
```

API 不返回真实中转站 Key 或 WebDAV 密码，错误信息会尝试脱敏；但能读取数据目录或 Docker volume 的管理员仍能看到明文。请勿提交 `.env`、数据库、`webdav.json`、截图或备份到公开位置。

本地模式可通过 `CANDYTEST_DATA_DIR` 改目录；容器模式固定使用 `/data`。

## 故障排查

| 现象 | 建议 |
| --- | --- |
| 本地页面显示 CLI 未安装 | 在启动 CandyTest 的同一终端运行 `pi --version`、`codex --version` 并检查 PATH。 |
| 容器页面显示 CLI 未安装 | 运行 `docker compose exec candytest pi --version` 和 `codex --version`；重新构建镜像。 |
| server 模式拒绝启动 | 检查 `.env` 中用户名、Werkzeug 密码哈希、至少 32 字节的 Secret Key。 |
| 登录后立即回到登录页 | 通过 HTTPS 域名访问；仅临时 HTTP 调试时将 `COOKIE_SECURE=0`。 |
| Nginx 返回 502 | 确认容器 healthy，并从宿主机访问 `http://127.0.0.1:8765/healthz`。 |
| WebDAV HTTP 405 | 使用服务的 WebDAV 入口；OpenList 通常为 `/dav/`，不是普通文件网页路径。 |
| WebDAV 401/403 | 核对 WebDAV 用户名、密码和目录写权限。 |
| 任务全部 401/ERROR | 核对中转站 Base URL、API Key、模型 ID 和 Responses API 兼容性。 |
| Docker 中 Clash 连接失败 | 不要填写容器内的 `localhost`；尝试 `host.docker.internal` 并允许网桥访问。 |
| 端口占用 | 修改 `.env` 的 `CANDYTEST_PORT`，Nginx upstream 同步修改。 |

## 测试与 CI

离线测试不会调用真实模型 API 或真实 WebDAV：

```bash
python -m unittest discover -s tests -v
```

测试覆盖判分、CLI JSONL、Key 隔离、SQLite 快照、WebDAV mock Push/Pull、冲突、任务调度/中断、登录、Session、CSRF、安全响应头和 Docker 静态策略。GitHub Actions 还会实际构建容器，检查登录跳转、健康检查以及镜像内 pi/Codex 版本命令。
