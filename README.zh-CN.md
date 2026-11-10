# ChatGPT Local Tools：把本地工具接到 ChatGPT

[English](README.md) · [Agent Skill](skills/connect-local-tools/SKILL.md) · [配置说明](skills/connect-local-tools/references/configuration.md)

这个项目提供一个可复用的 Agent Skill，以及两个仅依赖 Python 标准库的脚本。
目标是让本地 Agent 识别工具接口、生成配置、检查连接，并把你指定的本地功能接到 ChatGPT 网页。

**已有 MCP 就直接复用；只有 API／CLI 时，先把明确的操作包装成 MCP，再接官方隧道。**

这是独立开源项目，不是 OpenAI／金山官方产品，不运营隧道服务，不承诺任何账号都有接入资格或隧道免费。
发布在 GitHub 不等于上架 ChatGPT 应用目录。

## MCP、API、CLI、Skill 分别负责什么？

| 概念 | 作用 | 例子 |
| --- | --- | --- |
| API | 程序通过请求调用功能 | 本地服务的 `/search` |
| CLI | 在终端调用功能 | `kdocs-cli drive search-files ...` |
| MCP | 向 Agent 描述工具、参数，并处理工具调用 | LLM Wiki 自带的 MCP；WPS365 的 `mcp serve` |
| Skill | 告诉 Agent 如何选择、配置和验证这些工具 | 本仓库的 `connect-local-tools` |
| Tunnel | 让云端产品能访问私有网络里的 MCP | OpenAI 官方 Secure MCP Tunnel |

Skill 本身不会给网页 ChatGPT 增加本地 Shell 权限。执行端必须能访问目标工具；云端环境里的
`127.0.0.1` 也不是你的电脑。每次调用返回的内容会发送给 ChatGPT，但项目没有全量同步知识库的逻辑。

## 仓库里有什么？

- `skills/connect-local-tools/SKILL.md`：给 Agent 的接入流程和判断规则。
- Skill 内的 `scripts/connect.py`：导入配置、检查 MCP、下载安装官方客户端、启动／查看／停止连接。
- Skill 内的 `scripts/tool_adapter.py`：把固定 HTTP 操作、固定 CLI 命令包装成 MCP 工具。
- Skill 内的 `assets/`：LLM Wiki、个人金山文档、WPS365、HTTP MCP、普通 API 和本地演示配置。
- `tests/`：不需要真实账号的本地协议与安全边界测试。

## 先在 Ubuntu 上运行一个本地示例

要求 Python 3.10+，不需要 pip 安装依赖。自动下载官方客户端支持 Linux amd64／arm64；
其他 POSIX 环境可以指定已安装的官方客户端，尚未做真实 macOS 验证；不支持原生 Windows 启动器。

```bash
git clone https://github.com/zhkzly/chatgpt-local-tools.git
cd chatgpt-local-tools
python3 skills/connect-local-tools/scripts/tool_adapter.py \
  --config skills/connect-local-tools/assets/demo.adapter.json --check
python3 skills/connect-local-tools/scripts/connect.py check \
  --config skills/connect-local-tools/assets/demo.connection.json --server demo
python3 skills/connect-local-tools/scripts/smoke_demo.py
```

前两步校验配置、执行本地 MCP 握手并发现工具。最后一步会通过启动器的 stdio 包装入口
实际调用无害的 `echo_text`，并核对结果与输入严格一致。这些命令不启动隧道，不能证明网页连接已成功。

## 作为 Agent Skill 安装

复制完整文件夹，保留脚本、示例和引用文档。以 Codex 用户级目录为例：

```bash
mkdir -p ~/.agents/skills
cp -R skills/connect-local-tools ~/.agents/skills/
```

如果已有同名安装，先检查或备份再替换。项目级目录为 `.agents/skills/`；Claude Code 可用
`.claude/skills/`。其他兼容 Agent Skills 格式的工具也可读取，但实际执行和浏览器权限由对应环境决定。

然后可以对本地 Agent 说：

> 使用 $connect-local-tools，把我电脑上的 LLM Wiki 接到 ChatGPT。先检查现有配置和工具列表，
> 再建立连接，最后用一次我指定的只读查询验证。不要把密钥写进仓库。

不要把 runtime key、金山 token 或 cookie 发到聊天里。安装 Skill 与在 ChatGPT 添加连接是两个步骤。

## 接入 LLM Wiki

保持 LLM Wiki 运行，并开启它的本地 HTTP API 和 MCP。复制示例到被 Git 忽略的本地配置：

```bash
cp -n skills/connect-local-tools/assets/llm-wiki.connection.json connections.local.json
python3 skills/connect-local-tools/scripts/connect.py check \
  --config connections.local.json --server llm-wiki
```

在 [OpenAI Tunnel 页面](https://platform.openai.com/settings/organization/tunnels) 创建／选择有权限使用的隧道，
关联目标 ChatGPT 工作区，然后在本地配置里填入真实 `tunnel_id`。

```bash
python3 skills/connect-local-tools/scripts/connect.py start \
  --config connections.local.json --server llm-wiki
python3 skills/connect-local-tools/scripts/connect.py status --server llm-wiki
```

运行密钥需要 Tunnels Read + Use。若没有设置 `OPENAI_TUNNEL_KEY`，脚本会在本地终端隐藏输入。
首次启动可以自动下载固定版本的官方客户端，校验 SHA-256 后再使用。

**通用版使用官方后台进程管理，关闭启动终端不会停止连接。停止请执行：**

```bash
python3 skills/connect-local-tools/scripts/connect.py stop --server llm-wiki
```

接着在 ChatGPT 的自定义 MCP 连接中选择同一个隧道，确认工具列表，完成一次授权的只读调用。
本地 `ready=true` 只说明本地状态，不能代替这次端到端验证。原 LLM Wiki MCP 含写入等工具，
直接接入时本项目不会过滤这些工具。

## 个人金山文档怎么接？

个人金山文档已有 [官方 kdocs-cli 和 Skill](https://github.com/kdocs-app/kdocs-skill)，
其认证文档明确支持个人 WPS 账号。它与企业 WPS365、处理本地文档的 WPS Office CLI 是不同产品。

先按官方仓库说明在本机安装，核对版本和帮助，然后：

```bash
kdocs-cli auth login
kdocs-cli auth status
python3 skills/connect-local-tools/scripts/connect.py check \
  --config skills/connect-local-tools/assets/kdocs.connection.json --server kdocs
```

本项目的金山示例提供搜索、个人根目录列表、星标列表、读取文档和继续异步读取。
认证由官方 CLI 的本地登录／钥匙串处理，不把登录或导出 token 暴露为工具。
列表示例只取第一页；`pending`、分页和部分读取警告都需要继续处理，不能当成已读完。

这是按官方文档制作的适配示例，尚未拿真实用户账号完成端到端验证。它不会自动把金山文档同步到 Google Drive。
企业 WPS365 已有原生 `wps365-cli mcp serve`，应优先直接连接。
详见 [金山接入说明与一手来源](skills/connect-local-tools/references/kdocs.md)。

## 接其他 API／CLI

优先找官方现成 MCP 或 `mcp serve` 模式。普通 REST 地址不能直接填成 MCP URL。
没有 MCP 时，定义固定 URL／方法或固定程序／参数，把用户可变输入放在有边界的 schema 里。
参考 [适配器说明](skills/connect-local-tools/references/adapters.md)。

已有桌面 MCP 配置可以先导入：

```bash
python3 skills/connect-local-tools/scripts/connect.py import-mcp \
  --input /绝对路径/mcp.json --output connections.local.json
```

导入只生成配置，不执行命令。原环境变量和认证头的值转换为引用，需要在本机补齐；
命令参数仍需人工审查。不要公开任意 Shell、任意 URL 或任意可执行程序选择。

## 文件、凭据和验证边界

默认状态目录是 `~/.local/share/chatgpt-local-tools`（可由 `XDG_DATA_HOME` 或全局
`--state-dir` 改变），里面有客户端、配置、进程记录、校验记录和运行日志。
不会修改防火墙、代理配置、Shell 启动文件或设置开机自启。

密钥使用环境引用；第三方程序自己打印的内容可能进入运行日志。删除本地状态前先停止连接；
云端 Tunnel、ChatGPT 连接和密钥撤销需要分别处理。

适配器默认只公开声明为只读的工具，显式 `--enable-writes` 才开放配置中的写工具。
这个声明不是操作系统沙箱，也不能证明底层程序没有副作用。当前是串行、文本型、有限 schema 的工具适配器。

同一个 stdio Tunnel ID 只保留一个有效客户端。本版本修改配置后需先停止旧连接，再使用新 alias。
不要移动仍被运行进程引用的 Skill 和配置文件。

运行本地测试：

```bash
python3 -m unittest discover -s tests -v
```

测试使用本地服务、子进程和模拟官方 CLI，没有替代真实 ChatGPT／金山账号验证。
另外，[CI 流程](.github/workflows/verify.yml) 会下载并校验真实官方客户端，实际测试后台生命周期，
并经过官方本地内存隧道完成一次工具调用；这部分不使用真实 OpenAI 或厂商账号。
详见 [验证记录](docs/validation.md)。原创代码和文档采用 MIT 协议；官方客户端与各厂商工具保留各自许可。
