# CandyTest

批量测试多个 OpenAI 兼容 **Responses API** 中转站，记录正确率、耗时、Token、完整回答和 API 错误。支持 pi、Codex、并行测试、WebDAV、Clash 代理，以及 Docker 服务器部署。

## Docker 部署

服务器只拉取 GHCR 镜像，不在本地构建。需要 Docker 与 Docker Compose v2。

```bash
git clone https://github.com/MaShouo/CandyTest.git
cd CandyTest
chmod +x deploy.sh
./deploy.sh
```

`deploy.sh` 会自动：

1. 检测正在运行的 Nginx Proxy Manager（NPM）；
2. 复用 NPM 已有的 Docker 网络；
3. 拉取 `ghcr.io/mashouo/candytest:latest`；
4. 启动容器并等待健康检查通过；
5. 输出反向代理参数。

如果 GHCR 镜像是私有的，部署前先登录：

```bash
docker login ghcr.io
```

### Nginx Proxy Manager

检测到 NPM 后，脚本会让 CandyTest 加入同一个 Docker 网络。NPM 的 Proxy Host 填写：

```text
Scheme: http
Forward Hostname: candytest
Forward Port: 8765
```

不要填写宿主机回环地址。NPM 和 CandyTest 必须位于同一个 Docker 网络，否则会返回 502。

NPM 有多个网络时，明确指定一个：

```bash
./deploy.sh --mode npm --npm-network npm_default
```

### 其他部署模式

宿主机已有 Nginx 等反向代理：

```bash
./deploy.sh --mode host
```

默认监听 `[IP]:8765`，可在 `.env` 修改 `CANDYTEST_BIND_ADDRESS` 和 `CANDYTEST_PORT`。

没有反向代理时，可让 Caddy 自动配置 HTTPS：

```bash
./deploy.sh --mode caddy --domain candy.example.com
```

域名必须已解析到服务器，且 80/443 端口可用。

### 登录

首次登录：

```text
用户名：admin
密码：admin
```

登录后立即在 **设置 → 登录账号** 修改密码。服务器部署默认启用 Secure Cookie，应通过 HTTPS 访问；仅临时 HTTP 调试时才在 `.env` 设置 `COOKIE_SECURE=0`。

### 更新、备份与恢复

```bash
git pull
./deploy.sh
```

已有部署在更新前会自动备份 `/data`。新镜像健康检查失败时会恢复旧镜像，但不会自动覆盖数据。

常用命令：

```bash
./deploy.sh status
./deploy.sh logs -f
./deploy.sh backup
./deploy.sh restore .deploy/backups/candytest-YYYYmmddTHHMMSSZ.tar.gz
```

生产环境可固定镜像版本：

```bash
./deploy.sh --tag vX.Y.Z
```

数据保存在 Docker volume 中。**不要执行 `docker compose down -v`。**

## Compose 结构

部署脚本会按场景选择一组配置：

| 文件 | 用途 |
| --- | --- |
| `compose.yaml` | 公共应用配置，无端口、无外部共享网络 |
| `compose.npm.yaml` | 加入已有 NPM 网络 |
| `compose.host.yaml` | 绑定宿主机回环端口 |
| `compose.caddy.yaml` | 启动 Caddy HTTPS |

通常不需要手动执行 Compose 命令，直接使用 `deploy.sh`。

## Windows 本地运行

需要 Python 3.10+，并在 `PATH` 中安装 pi 或 Codex：

```powershell
pi --version
codex --version
```

双击 `start.bat`。首次运行会创建 `.venv`、安装依赖并打开：

```text
http://[IP]:8765/
```

本地模式不需要登录，只监听回环地址。

## 基本使用

1. 添加中转站，填写 Responses API Base URL、API Key 和模型。
2. 选择一个或多个中转站。
3. 设置 pi/Codex、轮数、Reasoning effort 和串行/并行模式。
4. 开始测试并查看当前任务、API 错误和历史正确率。
5. 点击当前任务的中转站统计条，可筛选下方记录；全部取消后显示所有记录。

中转站 URL 不会自动追加 `/v1`。编辑时 API Key 留空表示保留原值。

## 设置与数据

- **Clash 代理**：Docker 中访问宿主机代理通常填写 `http://host.docker.internal:7890`。
- **WebDAV**：仅在点击 Push/Pull 时同步，不会自动同步。Pull 会覆盖本机数据。
- **登录账号**：仅服务器模式显示。

主要数据：

```text
candytest.sqlite3   中转站、API Key、任务和历史
webdav.json         WebDAV 配置和明文密码
auth.json           管理员密码哈希和会话密钥
```

这些文件包含敏感信息。不要提交 `.env`、数据库、备份或配置文件。

## 常见问题

| 问题 | 处理 |
| --- | --- |
| NPM 返回 502 | 重新运行 `./deploy.sh`，确认 NPM 使用脚本输出的网络和 `candytest:8765`。 |
| GHCR 拉取失败 | 运行 `docker login ghcr.io`，确认账号有包读取权限。 |
| 登录后返回登录页 | 使用 HTTPS；仅 HTTP 调试时设置 `COOKIE_SECURE=0`。 |
| Docker 中 Clash 无法连接 | 使用 `host.docker.internal`，并允许 Docker 网桥访问代理。 |
| API 全部报错 | 检查 Base URL、API Key、模型 ID 和 Responses API 兼容性。 |
| Caddy 无法签发证书 | 检查 DNS 及 80/443 端口，并确认没有其他服务占用端口。 |

## 开发与测试

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

CI 在 Windows/Linux 运行测试和容器 smoke test，并向 `ghcr.io/mashouo/candytest` 发布 `linux/amd64`、`linux/arm64` 镜像。`main` 发布 `latest`，Git 标签发布对应的 `vX.Y.Z` 镜像。
