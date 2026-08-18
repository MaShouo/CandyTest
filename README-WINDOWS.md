# CandyTest Windows 本地版

这是 CandyTest 的 Windows 本地运行包，只监听当前电脑的回环地址，不包含 Docker/Caddy 服务器部署文件。

## 启动

1. 安装 Python 3.10 或更高版本，并勾选 **Add Python to PATH**；
2. 按需安装 `pi` 或 `codex` CLI，并确认 `pi --help` / `codex --help` 可运行；
3. 双击 `start.bat`；
4. 首次启动会创建 `.venv` 并安装 Flask/Waitress，然后自动打开浏览器。

默认地址：

```text
http://127.0.0.1:8765/
```

关闭启动窗口即可停止服务。

## 数据与安全

数据保存在：

```text
%USERPROFILE%\.candytest\candytest.sqlite3
```

中转站 API Key 在 SQLite 中明文保存，但页面/API 不会返回真实 Key，CLI 调用错误也会对 Key 脱敏。不要共享该数据库或把它提交到版本控制。

Clash 代理只支持 HTTP/HTTPS 代理地址，例如 `http://localhost:7890`。运行中的任务不会因中途修改代理而改变。

## 校验下载文件

Release 同时提供 `.zip.sha256`。PowerShell 可执行：

```powershell
Get-FileHash .\CandyTest-local-windows-x64-*.zip -Algorithm SHA256
```

将输出与 `.sha256` 文件比较。

## 完整文档与服务器部署

项目源码、问题反馈和 Linux Docker 服务器部署说明：

```text
https://github.com/MaShouo/CandyTest
```
