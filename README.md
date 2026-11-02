# 📸 Lightroom MCP（Windows）

**让 Claude / Codex 帮你看 Adobe Lightroom Classic 里的照片。**
一个 Lightroom 插件加一个小型 MCP 服务：能看到你选中的照片、读取调整参数，并把预览图和直方图数字交给 AI。
还能对受限的调整参数做小步修改、整理目录信息，并保存旧值用于还原。

![License: MIT](https://img.shields.io/badge/license-MIT-blue) ![Platform: Windows](https://img.shields.io/badge/platform-Windows-lightgrey) ![Status: early](https://img.shields.io/badge/status-early%200.11.1-orange)

[English](README.en.md) · [更新日志](CHANGELOG.md) · [安全说明](SECURITY.md) · [参与贡献](CONTRIBUTING.md)

> 🤖 **你是 AI 助手，用户让你看这个仓库？** 请读 [`AI_GUIDE.md`](AI_GUIDE.md)，里面有完整的安装和使用步骤。

> ⚠️ **早期版本（0.11.1）**：只在 Windows 11 + Lightroom Classic 15.2 上测过。安装包已发布但尚未在干净电脑上做过安装/卸载测试（待验证）。与 Adobe 无关联。

## ✨ 能做什么

| 工具 | 给 AI 的内容 |
|---|---|
| `lr_status` | Lightroom 是否连上、版本、当前目录、选中几张 |
| `lr_list_selected` | 选中的照片：文件名、格式、评分、尺寸、ISO/光圈/快门/焦距/镜头/机身 |
| `lr_get_develop` | 当前调整参数：曝光、对比度、高光、阴影、白平衡、HSL 等 |
| `lr_get_preview` | 1024 像素预览（可选 256–2048）**加数字**：亮度、过曝/欠曝比例、冷暖偏向 |
| `lr_doctor` | 中文自检：哪里有问题、怎么处理 |
| `lr_set_develop` | **修改**一张照片的受支持参数，有范围和单次限幅；`preview=true` 返回修改前后两张预览及统计差异 |
| `lr_review_selected`（开发中） | 一次查看最多 10 张已选照片的预览与统计，用于对比挑片；不会自动改星级或旗标 |
| `lr_restore` | 把某一次修改涉及的滑块写回旧值 |
| `lr_restore_original` | 让照片回到 LrMCP 第一次改它之前的样子（`dry_run` 只显示会做什么） |
| `lr_get_metadata` / `lr_set_metadata` | 读取和修改目录信息：星级、旗标（留用/排除）、颜色标签、标题、说明文字、关键词；改之前旧值先存盘，可还原 |
| `lr_batch_set_develop` | 批量：把同样的相对改动加到多张照片（一次最多 10 张）。先预演（什么都不改，返回每张的结果和计划编号），确认后才执行；可一次还原整批 |
| `lr_list_presets` / `lr_apply_preset` | 查找并套用不带蒙版的开发预设；套用前保存设置，`preview=true` 返回前后对比 |
| `lr_plan_from_edit`（开发中） | 从一次已验证的单张滑块修改提取相对改动，先预演到其他照片；确认后用批量工具执行 |
| `lr_list_edits` | 列出 LrMCP 做过的修改（编号、时间、照片、滑块、是否已还原） |

例子：选中一张照片，对 AI 说「看看我选中的这张，曝光有什么问题」。它拿到图片和 `平均亮度 77.6`、`冷暖指数 −9.9%`
这样的数字，就能有依据地说「偏暗、偏冷」。

### 「修改」是什么意思，怎么保证安全

- **只改滑块，只改一小张清单**：89 个滑块（曝光、上面列的 10 个全局滑块、24 个 HSL 值、**RAW 照片的色温 / 色调**、**颜色分级**、**相机校准**、**参数曲线**、**锐化 / 降噪 / 暗角 / 颗粒 / 去边**、**裁剪与拉直**），外加 **4 条点曲线**（亮度和红绿蓝）。蒙版不能改（实测通过 SDK 写蒙版不可靠）；预设可以套用（不带蒙版的）；JPEG 没有开尔文值，不能改白平衡。
- **范围限制**（曝光 ±5，其余 ±100）和**单次限幅**（曝光 ±1，其余 ±30）：跳得太大会被拒绝，并提示 AI 分几步改、每步看一眼预览。
  Lightroom 自己**不检查范围**（实测：越界值它照存），所以由本项目来检查。
- **写入之前先把旧值存到磁盘**（`C:\Users\<你>\LrMcp\edits`）。还原就是把它们写回去；Lightroom 的快照和撤销实测不可靠，所以不用。
- **只读开关**：创建文件 `C:\Users\<你>\LrMcp\readonly`，所有修改都会被服务端和插件拒绝；删掉文件就恢复。
- 每次只改一张照片。插件会把所有规则再检查一遍，不信任服务端。
- 修改会在 Lightroom 的「历史记录」里显示为 `LrMCP <编号>` 步骤。**在真实作品上用之前，请先备份目录（`.lrcat`），并先在测试目录上试。**
  开发分支的还原工具会检查后续改动；发现冲突时默认拒绝覆盖，需要用户明确确认才可用 `force=true` 继续。

它会创建的文件：预览图（不会加入你的目录）、口令、日志和保存下来的修改记录，都在 `C:\Users\<你>\LrMcp`。

## 🏗️ 结构

```
Claude / Codex ──(MCP, stdio)──▶ lrmcp（Python 服务，本机队列 127.0.0.1:58770）
                                          ▲
        Lightroom 插件（Lua）来取任务，执行白名单命令，再把结果送回
```

- 只在本机：服务独占绑定 `127.0.0.1`，请求要带随机口令，`Host` 必须匹配，带 `Origin` 头（浏览器）的请求一律拒绝；可同时开多个 AI 会话（每个会话一个服务，端口 58770–58789，插件自动连上全部）。
- 插件只执行固定的白名单命令，不接受请求里的文件路径。写入需要：白名单里的滑块、范围内的值、不超过单次限幅、没开只读开关。
- 注意：工具返回的图片和照片信息会发给你使用的 AI 服务，见 [SECURITY.md](SECURITY.md)。

## 🧰 需要什么

- Windows、Adobe Lightroom Classic（在 15.2 上开发；SDK 15.x）、Python 3.10+（用 3.12 测试）。
- Claude Code、Codex 或其他 MCP 客户端（测试过 Claude Code，没测过 Codex）。

## 📦 用安装包安装（免装 Python）

1. 到 [Releases](https://github.com/Denvolatos/lightroom-mcp-win/releases/latest) 下载 `LrMCP-Setup-*.exe`，并核对页面上的 SHA-256。
2. **在你自己的资源管理器里双击运行**（不要通过 Claude 桌面版启动，见下面的 MSIX 提醒）。程序没有代码签名，Windows SmartScreen 会提示"未知发布者"。
3. 安装完重启 Lightroom 和你的 AI 助手，让它调用 `lr_status`。

## 🚀 从源码安装

1. **服务**（普通 PowerShell）：
   ```powershell
   pip install ./server
   lrmcp register --dry-run   # 先看它会做什么
   lrmcp register             # 把 lightroom_mcp 登记到 Claude Code / Codex（调用它们自己的命令）
   ```
2. **插件**：把 `plugin/lrmcp.lrdevplugin` 复制到 `%APPDATA%\Adobe\Lightroom\Modules\LrMcp.lrplugin`，
   删掉副本里的 `DevRun.lua` 和 `devscripts` 文件夹（开发用的辅助），重启 Lightroom。在
   *文件 → 增效工具管理器* 里应能看到 LrMCP 已启用。`powershell -ExecutionPolicy Bypass -File build.ps1`
   可以生成干净的插件副本和安装包（需要 conda 环境和 Inno Setup，见 [CONTRIBUTING](CONTRIBUTING.md)）。
3. 重启你的 AI 助手，让它调用 `lr_status`。出问题就让它调用 `lr_doctor`。

> **Windows 的坑**：*Claude 桌面版是 MSIX 应用*，Windows 会悄悄重定向它启动的程序对 `AppData` 的写入。
> 请在你自己的 PowerShell 里运行安装和复制步骤，不要通过 Claude 桌面版启动的 shell。
> 所以本项目的共享文件放在 `C:\Users\<你>\LrMcp`，而不是 `AppData`。

Claude 桌面版暂不自动登记（配置文件位置取决于安装方式），请在 *设置 → 开发者 → 编辑配置* 里手动添加命令。

## 💬 反馈

遇到问题，或者在别的 AI 应用里跑通了？欢迎 **[提交 issue](../../issues/new/choose)**（问题反馈、兼容性报告、功能建议）
或到 **[讨论区](../../discussions)** 聊。表单会让你贴上 `lr_doctor` 的输出，大多数问题一看就明白。
请不要上传照片或 Lightroom 目录。本软件不收集任何使用数据。

## 🗺️ 路线图

| 版本 | 计划 |
|---|---|
| 0.1 | 只读：看照片、调整参数、预览、直方图数字、自检 |
| 0.2–0.11.1 | 受保护的单张修改、目录信息、批量预演、预设与还原；详见上面的工具列表 |
| 当前开发分支 | 冲突保护、修图前后对比、多图挑片预览、从已验证修改生成批量预演 |
| 后续 | RAW 与全新 Windows 环境的安装包验收；经验证后再考虑批量预设和导出 |

蒙版、AI 功能取决于 Adobe SDK 能提供什么，暂不承诺。

## 📚 文档

- [`docs/方案.md`](docs/方案.md)：方案与设计
- [`docs/实验记录.md`](docs/实验记录.md)：在 Lightroom Classic 15.2 上的实测（例如：写入必须放进 `withWriteAccessDo`，快照和撤销不可靠）
- [`docs/实现记录.md`](docs/实现记录.md)：实现中的发现与验证结果

## 🙏 另见

其他 Lightroom Classic MCP 项目，参考过思路（没有复制代码）：
[Automaat/lightroom-mcp](https://github.com/Automaat/lightroom-mcp)（TypeScript，MIT，工具很多，支持 Windows 和 macOS）、
[noopz/lightroom_mcp](https://github.com/noopz/lightroom_mcp)（Python，Apache-2.0）。
本项目的目标是：小、对修改很谨慎、用 Python、方便不懂编程的人安装。

## ⚖️ 许可证与声明

[MIT](LICENSE)。第三方依赖见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
Adobe、Lightroom 是 Adobe Inc. 的商标；本项目与 Adobe 无关联，未获其认可或赞助。
Adobe Lightroom Classic SDK 不在本仓库内，也不要提交。
