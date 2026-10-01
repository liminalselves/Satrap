# Satrap Windows 便携发行包

适用于 Windows 10 / 11 x64。发行包内置 Python 3.13、所有运行依赖、官方插件和构建好的 Web 管理面板, 使用者无需安装 Python、Node.js、Git 或编译工具。

## 第一次使用

1. 将 `Satrap-v<版本>-windows-x64.zip` 完整解压到可写目录, 例如 `D:\Apps\Satrap`。请先解压, 不要在压缩软件内直接启动。
2. 双击 `start.bat`。启动器会创建默认配置, 启动控制服务、聊天服务和平台后端, 然后自动打开浏览器。
3. 默认管理面板地址为 `http://127.0.0.1:19871`。在“模型”页面添加模型名称、API 地址和 API Key, 再在聊天页面选择模型开始使用。
4. 接入 QQ / OneBot 或 Misskey 时, 在面板配置平台及会话, 按面板提示应用配置或重启后端。OneBot 平台服务需要单独部署。

启动窗口保持打开。按 `Ctrl+C`、关闭启动窗口, 或双击 `stop.bat` 可停止服务。重复双击 `start.bat` 会打开现有面板, 不会重复创建服务。`stop.bat` 通过本目录的启动器回收服务, 不会按名称批量终止 Python 进程。

启动前会识别并清理本目录运行时的旧控制服务、聊天服务和平台后端, 然后检查端口。停止时同样检查残留进程与端口释放; 即使启动器已经退出, `stop.bat` 仍会清理本目录遗留服务。其他目录的 Satrap 或其他程序占用端口时, 会显示进程名称和 PID 并报错, 不会自动终止它们。使用自定义端口的实例会从启动记录读取实际端口。

首次启动和读取文档不需要下载额外依赖。调用模型、联网搜索和远程 MCP 服务仍需要对应网络与凭据。MCP 服务若要求 `node`、`npx`、`uv` 或其他外部程序, 需要按该服务要求另行安装。

## 文件与数据

| 文件或目录 | 用途 |
| --- | --- |
| `start.bat` / `stop.bat` | 启动 / 停止 |
| `satrap.bat` | CLI, 例如 `satrap.bat status`、`satrap.bat --help` |
| `runtime/` | 独立 Python 与全部运行依赖 |
| `satrap/` | 应用源码、插件清单与技能资源 |
| `satrap-ui/dist/` | 已构建的 Web 管理面板 |
| `.satrap/` | 首次启动生成的配置、凭据、数据库、用户插件和日志 |
| `.satrap/release-logs/` | 控制服务和聊天服务的启动诊断日志 |
| `release.json` | 应用版本、Python 版本、源码提交与依赖清单 |
| `requirements.lock.txt` | 本次发行的精确依赖版本 |

升级时先运行旧目录的 `stop.bat`, 解压新版本到另一目录, 将旧目录的 `.satrap/` 整体复制过去, 再启动。若自行使用了根目录 `config.yaml`、外部工作区或外部数据目录, 请一并保留其配置与数据。不要用旧 `runtime/` 或旧源码覆盖新版本。

## 常见问题

- **启动失败**: 启动窗口会保留错误信息。查看 `.satrap/release-logs/control.log` 和 `chat.log`, 应用日志位于 `.satrap/`。
- **端口占用**: 默认使用 `19870`、`19871`、`19872`。停止占用端口的实例, 或执行 `start.bat --control-port 29871 --chat-port 29872`, 并在本目录 `.satrap/config.yaml` 的 `api.port` 中为平台后端设置不同端口。所有端口必须互不相同。
- **没有模型**: 在面板添加模型, 填写有效 API Key, 然后在聊天页选择模型。
- **第三方插件缺少依赖**: 在发行目录执行 `runtime\python.exe -X utf8 -m pip install --target runtime\Lib\site-packages <包名>`。安装需要联网, 避免覆盖已有核心依赖。
- **移动目录**: 默认数据与代码均使用便携目录。移动前停止服务; 自行配置的绝对路径需要同步调整。

## 开发者构建

在 Windows x64 安装 Python 3.13 与 Node.js 20 或更新版本, 在项目根目录执行:

```powershell
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
python scripts/build_release.py
```

构建会执行 `npm ci` 与前端构建, 下载并校验 Python 官方运行时, 根据应用的 `[all]` 依赖安装二进制 wheel, 验证独立运行环境, 再输出 `dist/` 下的 ZIP、SHA-256 文件与精确依赖清单。只使用 Git 跟踪的应用源码及明确选定的资源, 不打包本地 `.satrap/`、配置密钥、测试数据或开发环境。

已经构建前端时可加 `--skip-frontend`。默认使用仓库内 `scripts/release/constraints-windows-x64.txt` 锁定所有依赖; 使用 `--constraints <上一次的精确依赖清单>` 可指定其他版本清单。发行源码以当前工作树的文件内容为准, `release.json` 会标记是否存在未提交更改。

完整冒烟验证应在全新解压目录执行, 会使用三个独立空闲端口并创建测试配置:

```powershell
runtime\python.exe -X utf8 release\smoke_release.py
```

验证包括原生依赖、分词器、插件资源、CLI、管理面板、服务地址、启动、重复启动、停止和端口释放。测试配置仅供验证, 之后重新解压用于正式使用。

GitHub Actions 工作流 `.github/workflows/release.yml` 支持手动构建并上传下载附件; 推送与 `setup.py` 版本一致的 `v<版本>` 标签会自动创建 GitHub Release。工作流在含中文与空格的解压路径执行完整冒烟验证。
