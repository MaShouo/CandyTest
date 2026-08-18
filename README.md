# CandyTest：中转站 GPT 智商检测

CandyTest 是一个仅在本机回环地址上运行的轻量 Web 页面，用同一道糖果题测试多个 OpenAI 兼容 **Responses API** 中转站的回答稳定性。它不让浏览器直接访问中转站，而是由本机已经安装的 **pi CLI** 或 **Codex CLI** 单独发起每一次调用。

- 多选中转站，可选**串行**或**并行**测试。
- 每站默认 5 轮；并行时为“**站间并行、站内串行**”。
- 回答中出现独立数字 `21` 即判为正确；`121`、`210` 不算正确。
- 显示本轮任务和按中转站汇总的历史正确率。低于 **80%** 会显示红色提示；ERROR 单独计数，**不计入正确率分母**。
- 完整回答、耗时、token 和错误信息会保存在本机历史中，可展开复核。

## 环境与安装

需要 Python 3.10 或更高版本。项目本身仅使用 Python、Flask、纯 Python 的 Waitress WSGI 服务、SQLite 与原生前端；**不会编译 Rust，也不会产生 Rust 构建产物**。Codex 若已安装，可能是其自身的外部程序，本项目不会构建它。

### Windows 一键启动

直接双击项目根目录中的 `start.bat`。脚本会自动：

1. 检测 Python；
2. 首次运行时创建 `.venv`；
3. 检查并安装缺失依赖；
4. 启动 CandyTest 并自动打开浏览器。

关闭启动脚本的窗口即可停止服务。也可以按下面的步骤手动启动。

Windows PowerShell 示例：

```powershell
cd F:\My-github\CandyTest
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python run.py
```

其他系统可使用：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python run.py
```

启动后会打开 `http://127.0.0.1:8765/`。服务默认只监听回环地址，不能从局域网访问。可通过环境变量修改端口或切换到 IPv6 回环地址：

```powershell
$env:CANDYTEST_PORT = "9000"
$env:CANDYTEST_HOST = "::1"       # 只能使用回环 IP，例如 127.0.0.1 或 ::1
python run.py
```

`CANDYTEST_HOST` 不能设置为公网或局域网地址，避免意外暴露保存的 Key。

## Clash 代理设置

页面顶部提供“Clash 网络代理”面板。启用后填写 Clash 的 **HTTP 代理端口**，例如：

```text
http://localhost:7890
```

保存后，代理会应用于**之后启动**的所有 pi/Codex 测试。应用会同时为 CLI 子进程设置 `HTTP_PROXY` 和 `HTTPS_PROXY`；即使中转站是 HTTPS，也会通过 Clash HTTP 端口建立 CONNECT 隧道。

- 代理是全局设置，所有中转站共用。
- 当前仅支持 `http://` 或 `https://` 代理 URL，不支持 SOCKS5。
- 不支持在代理 URL 中携带用户名或密码。
- 关闭代理后，新任务会清除继承的代理环境变量并直接连接。
- 已经运行中的任务使用启动时的代理快照，不会因中途修改设置而改变。
- 代理配置保存在同一个本地 SQLite 数据库中。

## pi / Codex CLI 前置条件

至少安装并配置下列任一个 CLI，并确保对应命令在当前终端的 `PATH` 中：

- `pi`：用于 **pi** 引擎；
- `codex`：用于 **Codex** 引擎。

页面顶部会显示两者的“可用/未安装”状态。未安装的引擎不能启动任务，但不会影响另一个引擎。应用调用时为每轮创建独立临时配置、会话和工作目录，并在结束后清理；不会读取当前项目的上下文或使用工具。请先在终端确认，例如：

```powershell
pi --help
codex --help
```

不同 CLI 版本的参数或第三方中转站兼容性可能不同；如果某个 CLI 报协议错误，请优先升级该 CLI，并确认中转站确实兼容 Responses API。

## 添加中转站

在页面“添加中转站”区域填写：

1. **名称**：仅用于页面与历史记录显示；
2. **Responses API Base URL**：例如 `https://api.example.com/v1`；
3. **API Key**；
4. **默认模型 ID**：例如中转站实际提供的 `gpt-4.1-mini`。

URL 必须以 `http://` 或 `https://` 开头。程序保留你填写的路径，仅清除末尾多余 `/`，**不会擅自补上 `/v1`**。HTTP 地址可以保存，但页面会提示其传输不安全；生产使用应优先使用 HTTPS。

编辑中转站时，API Key 留空表示**保留现有 Key**；如果填写新值则替换。删除中转站是软删除：配置不再出现在可选列表中，历史运行记录仍会保留。

## 重要安全提示：API Key 以本地明文保存

为满足“保存中转站配置”的需求，API Key 会以**明文**写入当前用户的数据目录的 SQLite 数据库。应用会尽力将数据目录权限限制为当前用户，并采取以下措施：

- 查询 API 和页面不会返回 Key，只显示“已保存”；
- Key 不写入命令参数、临时配置文件或应用日志；仅通过每次子进程的专用环境变量传给 CLI；
- 保存错误时会对出现的 Key 进行脱敏。

但这些措施并不等于加密。能够读取你本机用户数据的人仍可能读取数据库。请仅在可信赖的个人设备中使用，使用权限受限、可随时撤销的 Key，并不要把数据目录、截图或数据库提交到版本控制。

## 数据目录与历史

默认数据目录统一为当前用户主目录下的 `.candytest`：

```text
~/.candytest/candytest.sqlite3
```

Windows 中通常对应：

```text
C:\Users\<用户名>\.candytest\candytest.sqlite3
```

可用 `CANDYTEST_DATA_DIR` 指定另一个目录：

```powershell
$env:CANDYTEST_DATA_DIR = "D:\PrivateData\CandyTest"
python run.py
```

其中的 `candytest.sqlite3` 保存中转站、任务和每轮结果。页面的“清空历史”只删除任务与运行记录，**不会删除中转站配置**。

## 测试语义

- **串行**：按所选站点顺序执行；一个站点的全部轮次完成后，才执行下一站。
- **并行（默认）**：每个所选站点一个 worker，不同站点可同时请求；同一站点始终一轮完成后再开始下一轮，以减少限流和相互干扰。
- 同时只能运行一个任务。重复启动会返回冲突提示。
- 运行中的任务可点击“中断测试”：应用会终止当前 pi/Codex 子进程树、停止后续轮次，并保留已经完成的结果；被中断轮次不计入正确率分母。
- 单次 CLI 调用默认超时 300 秒。超时、CLI 缺失、非零退出、401、429 或协议错误都会记录为 `ERROR`，不会自动补跑，也不会让该 ERROR 伪装为答错。
- 历史正确率只按中转站聚合，跨任务、CLI 和模型统计；更换模型后请结合运行明细判断。

## 故障排查

| 现象 | 建议 |
| --- | --- |
| 页面显示 pi/Codex“未安装” | 在启动 CandyTest 的同一终端运行 `pi --help` 或 `codex --help`；安装或修正 PATH 后重启服务。 |
| 启动提示 HOST 非法 | 仅设为 `127.0.0.1` 或 `::1`，不要填写域名、`0.0.0.0` 或局域网 IP。 |
| 任务全部 ERROR / 401 | 核对 Base URL、Key、模型 ID 以及中转站的 Responses API 兼容性；编辑站点时若不想改 Key 请留空。 |
| 429 或调用超时 | 降低每站轮数、切换串行、稍后重试，或检查中转站配额与限流规则。 |
| 正确率低于 80% | 展开历史中的回答和错误详情；ERROR 不在分母中，应同时关注 ERROR 数量。 |
| 端口被占用 | 设置未被占用的 `CANDYTEST_PORT` 后重新启动。 |

## 离线测试

自动化测试使用 Python 标准库 `unittest`，对 CLI 使用 mock/fake runner，**绝不会调用真实 API 或消耗额度**：

```bash
python -m unittest discover -s tests -v
```

测试覆盖判分、pi/Codex JSONL 解析、临时配置的 Key 隔离、SQLite CRUD/历史统计、串并行调度、80% 阈值和 Flask API 校验。运行 API 测试前需按上面的安装步骤安装 Flask；若环境中尚未安装 Flask，相关测试会标记为跳过，其余离线核心测试仍可执行。
