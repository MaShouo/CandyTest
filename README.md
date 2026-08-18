# CandyTest：中转站 GPT 智商检测

CandyTest 使用同一道糖果题测试多个 OpenAI 兼容 **Responses API** 中转站的回答稳定性。浏览器不会直接请求中转站；每一轮都由运行 CandyTest 的机器通过已经安装的 **pi CLI** 或 **Codex CLI** 发起。

项目支持两种运行方式：

- **本地模式**：默认只监听回环地址，Windows 可双击 `start.bat`。
- **服务器模式**：Linux Docker 单实例运行，由 Caddy 提供域名、自动 HTTPS 和 Basic Auth，多台设备访问同一份服务器数据。

主要功能：

- 多选中转站，可选**串行**或**并行**测试。
- 每站默认 5 轮；并行时为“**站间并行、站内串行**”。
- 回答中出现独立数字 `21` 即判为正确；`121`、`210` 不算正确。
- 显示当前任务和按中转站汇总的历史正确率；低于 **80%** 会显示红色提示。
- ERROR 单独计数，**不计入正确率分母**。
- 完整回答、耗时、token 和错误信息写入 SQLite，可展开复核。
- 运行中的任务可以中断；已完成结果会保留。

## 本地运行

### GitHub Release Windows 包

推送 `v*` 版本标签后，GitHub Actions 会创建 Release，并附带：

```text
CandyTest-local-windows-x64-<版本>.zip
CandyTest-local-windows-x64-<版本>.zip.sha256
```

ZIP 只包含本地运行所需的 `candytest/`、`run.py`、`start.bat`、`requirements.txt` 和本文档，不包含 Docker/Caddy 部署文件。它不是 PyInstaller 独立程序，因此仍需安装 **Python 3.10+**，并按需安装 pi/Codex CLI。

下载 ZIP、校验 SHA-256、解压后双击 `start.bat`。脚本会自动：

1. 检测 Python；
2. 首次运行时创建 `.venv`；
3. 安装缺失的 Flask/Waitress 依赖；
4. 启动 CandyTest 并打开浏览器。

### 从源码启动

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python run.py
```

Linux/macOS：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python run.py
```

默认打开 `http://127.0.0.1:8765/`。本地模式只能绑定回环 IP：

```powershell
$env:CANDYTEST_PORT = "9000"
$env:CANDYTEST_HOST = "::1"
python run.py
```

可通过 `CANDYTEST_OPEN_BROWSER=0` 禁止自动打开浏览器。不要将本地模式改造成公网监听；需要远程访问时使用下面经过认证反代保护的服务器模式。

## Linux Docker 服务器部署

### 架构

```text
浏览器
   │ HTTPS + Basic Auth
   ▼
Caddy（唯一暴露 80/443）
   │ Docker 内部网络
   ▼
CandyTest / Waitress（单进程，内部 8765）
   ├── pi CLI 0.84.2
   ├── Codex CLI 0.147.0
   └── /data/candytest.sqlite3（Docker named volume）
```

服务器镜像同时包含 Python、pi 和 Codex，不需要在宿主机安装这些 CLI。镜像不会安装 Rust、Cargo 或编译工具链。

### 前置条件

- 一台安装了 Docker Engine 和 Docker Compose v2 的 Linux 服务器；
- 一个域名，DNS `A`/`AAAA` 记录指向服务器；
- 防火墙允许 TCP 80、443；
- 服务器的 80/443 未被其他反向代理占用；
- 能从服务器访问中转站 API 和 GitHub Container Registry。

### 1. 准备配置

```bash
git clone https://github.com/MaShouo/CandyTest.git
cd CandyTest
cp .env.example .env
```

生成 Basic Auth bcrypt 哈希。命令不传明文参数，会交互式读取密码：

```bash
docker run --rm -it caddy:2.8.4-alpine caddy hash-password
```

编辑 `.env`：

```dotenv
GITHUB_OWNER=mashouo
CANDYTEST_VERSION=latest
CANDYTEST_DOMAIN=candy.example.com
CADDY_ACME_EMAIL=admin@example.com
CADDY_BASIC_AUTH_USER=your_user
CADDY_BASIC_AUTH_HASH='$2a$14$这里替换为生成的完整哈希'
```

注意：

- `GITHUB_OWNER` 必须使用小写；
- bcrypt 哈希含 `$`，在 `.env` 中必须使用**单引号**；
- `.env` 已被 `.gitignore` 排除，禁止提交；
- `.env` 只保存用户名和不可逆哈希，不保存 Basic Auth 明文密码。

### 2. 登录私有 GHCR

本仓库和默认 GHCR 包为 private 时，服务器需要具有 `read:packages` 权限的 GitHub token：

```bash
export CR_PAT='在当前终端临时设置，不要写入仓库'
printf '%s' "$CR_PAT" | docker login ghcr.io -u YOUR_GITHUB_USER --password-stdin
unset CR_PAT
```

也可以把 GHCR package 调整为 public，此时无需登录。

### 3. 启动

```bash
docker compose config
docker compose pull
docker compose up -d
```

检查状态：

```bash
docker compose ps
docker compose logs -f candytest caddy
```

随后访问：

```text
https://你的域名/
```

Caddy 会自动申请和续期 HTTPS 证书。CandyTest 的 8765 端口没有映射到宿主机，外部只能经过 Caddy Basic Auth 访问。

### 4. 更新

发布新标签后，将 `.env` 中的 `CANDYTEST_VERSION` 改为对应镜像版本（例如 `1.2.0`），或者继续使用 `latest`：

```bash
docker compose pull
docker compose up -d
```

数据库位于 Docker named volume `candytest-data`，重新创建容器不会删除数据。除非确认要永久删除所有中转站、API Key 和历史，**不要执行**：

```bash
docker compose down -v
```

项目不自动创建数据库备份。

### 本地构建服务器镜像

如果不使用 GHCR，可在 `compose.yaml` 中取消 `build:` 两行的注释，然后执行：

```bash
docker compose build --pull
docker compose up -d
```

### 服务器部署安全边界

- 必须保持 CandyTest 服务没有 `ports:` 映射；只有 Caddy 暴露 80/443。
- CandyTest 自身没有账户系统；安全性依赖“不发布 8765 端口”和 Caddy HTTPS 认证。任何能进入 Docker 网络或宿主机的管理员仍可直接访问容器，因此不要给不受信任的容器加入该网络。
- Caddy 对包括 `/healthz` 在内的所有外部路径启用 Basic Auth，并在转发前移除浏览器的 `Authorization` 请求头。
- CandyTest 使用只读根文件系统、非 root 用户、删除 Linux capabilities，并将运行数据限制在 named volume 和临时目录。
- 只运行一个 CandyTest 容器。`JobManager` 和中断事件保存在进程内，多个副本会产生状态不一致。
- API Key 以服务器 SQLite 明文保存；查询 API 不会返回 Key，但能读取服务器 volume 的管理员可以读取它。
- `localhost` Clash 地址在服务器模式下指服务器/容器自身，不是访问者的电脑。容器中默认没有 Clash；不需要代理时请关闭该设置。
- 如果服务器已使用 Nginx、Traefik 或其他反代，应只保留一个公网入口，并实现等效的 HTTPS 和认证保护。

## Release 与 GHCR 发布

`.github/workflows/release.yml` 在推送 `v*` 标签时自动：

1. 安装依赖并运行全部离线测试；
2. 构建 Windows 本地运行 ZIP 和 SHA-256 文件；
3. 构建服务器 Docker 镜像并推送到 `ghcr.io/<owner>/candytest`；
4. 创建 GitHub Release 并附加本地 ZIP。

示例：

```bash
git tag v1.0.0
git push origin v1.0.0
```

发布工作流需要仓库 Actions 具有 `contents: write` 和 `packages: write` 权限；工作流已经声明最小 job 权限。Docker 镜像会发布 `1.0.0`、`1.0` 和 `latest` 标签。

## Clash 代理设置

页面提供全局 Clash HTTP/HTTPS 代理设置，例如：

```text
http://localhost:7890
```

- 应用于之后启动的所有 pi/Codex 测试；运行中的任务不受中途修改影响。
- 同时设置 CLI 子进程的 `HTTP_PROXY` 和 `HTTPS_PROXY`。
- 只支持 HTTP/HTTPS 代理，不支持 SOCKS5 和代理认证。
- 关闭代理后，新任务会清除继承的代理变量并直接连接。
- 本地模式中的 `localhost` 是当前电脑；Docker 模式中的 `localhost` 是 CandyTest 容器。

## pi / Codex CLI

本地模式至少安装一个 CLI，并确保命令位于启动 CandyTest 的 `PATH`：

```text
pi --help
codex --help
```

服务器 Docker 镜像已经固定安装：

```text
@earendil-works/pi-coding-agent 0.84.2
@openai/codex 0.147.0
```

应用为每轮创建独立临时配置、会话和工作目录；API Key 仅通过子进程环境变量传入，不写到命令参数或临时配置正文。不同 CLI 版本和第三方中转站可能存在 Responses API 兼容差异。

## 添加中转站

填写：

1. 显示名称；
2. Responses API Base URL，例如 `https://api.example.com/v1`；
3. API Key；
4. 默认模型 ID。

程序保留 URL 中用户填写的路径，只删除末尾多余 `/`，不会自动添加 `/v1`。编辑时 API Key 留空表示保留原 Key。删除中转站采用软删除，历史记录仍然保留。

## 数据与 Key

默认数据目录：

```text
~/.candytest/candytest.sqlite3
```

Windows 通常为：

```text
C:\Users\<用户名>\.candytest\candytest.sqlite3
```

本地可通过 `CANDYTEST_DATA_DIR` 修改，例如：

```powershell
$env:CANDYTEST_DATA_DIR = "D:\\PrivateData\\CandyTest"
python run.py
```

服务器镜像固定使用 `/data` named volume。

API Key 明文保存在 SQLite 中，但：

- 查询 API 和页面永不返回真实 Key；
- Key 不写入 CLI 参数和应用日志；
- 错误信息中的 Key 会被脱敏；
- 编辑中转站时空白 Key 不会清除原 Key。

“清空历史”只删除任务和运行记录，不删除中转站及 API Key。

## 测试语义

- **串行**：按所选站点顺序执行，一个站点全部轮次完成后再运行下一站。
- **并行**：每个站点一个 worker；不同站点同时请求，同一站点内部始终逐轮执行。
- 同时只能运行一个任务。
- 中断会终止当前 pi/Codex 子进程树、停止后续轮次并保留已完成结果。
- 单次 CLI 默认超时 300 秒。
- 超时、CLI 缺失、非零退出、401、429 和协议错误记录为 ERROR，不进入正确率分母。
- 历史正确率只按中转站聚合，跨任务、CLI 和模型统计。

## 故障排查

| 现象 | 建议 |
| --- | --- |
| 本地页面显示 CLI 未安装 | 在启动 CandyTest 的同一终端运行 `pi --help`/`codex --help`，修正 PATH 后重启。 |
| Docker 页面显示 CLI 未安装 | 查看镜像构建日志，确认使用官方发布镜像；在容器中运行 `pi --version`/`codex --version`。 |
| Caddy 无法申请证书 | 检查域名 DNS、80/443 防火墙、端口占用和 Caddy 日志。 |
| GHCR pull denied | 登录 GHCR，并确认 token 有 `read:packages` 且可访问 private package。 |
| 502 Bad Gateway | 使用 `docker compose ps` 检查 candytest 健康状态，再查看两个服务的日志。 |
| 任务全部 ERROR / 401 | 核对 Base URL、Key、模型 ID 和 Responses API 兼容性。 |
| 429 或超时 | 减少轮数、切换串行、稍后重试或检查服务端配额。 |
| 服务器代理连接失败 | Docker 中的 `localhost` 不是宿主机；关闭代理或填写容器可访问的代理地址。 |
| 端口冲突 | 本地修改 `CANDYTEST_PORT`；服务器检查宿主机 80/443 是否被占用。 |

## 离线测试

测试不会请求真实模型 API，也不会消耗额度：

```bash
python -m unittest discover -s tests -v
```

测试覆盖判分、pi/Codex JSONL、临时配置隔离、SQLite CRUD、历史统计、串并行调度、任务中断、代理注入、API 校验、本地/服务器绑定边界、安全响应头和部署文件策略。
