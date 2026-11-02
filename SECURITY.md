# 安全说明

（English version below / 英文版在下方）

## 这个软件能做什么、不能做什么

- **目录信息的修改：** 星级、旗标、颜色标签、标题、说明、关键词（一次一张照片，限制长度，旧值先存盘，可还原；不能删除关键词本身，也不碰其他元数据）。
- **批量修改的限制：** 一次最多 10 张；必须先预演（预演什么都不改），再用预演返回的一次性编号执行（15 分钟有效）；只做滑块的相对改动，不做裁剪/曲线；遇到第一个失败就停止；每张照片各自存旧值，可单张或整批还原。
- **预设的还原机制：** 套预设时，整张照片的设置先存盘；还原时插件只接受“照片当前已有、类型相同、在已知范围内”的项，永远不接受蒙版。Lightroom 自己生成的内部值（如 GrainSeed）还原不掉，不影响画面。
- **受保护的修改：**可修改白名单内的调整参数、目录信息，并套用不带蒙版的开发预设；不碰蒙版，不删除或导入导出照片。调整参数有范围和单次限幅，修改前把旧值存到 `C:\Users\<你>\LrMcp\edits`。**只读开关**（文件 `C:\Users\<你>\LrMcp\readonly`）会让服务和插件拒绝一切修改。0.1 版是纯只读。
- 它还会写的文件：预览 JPEG（`LrMcp\previews`，不会加入你的 Lightroom 目录）、口令、端口/日志文件和修改记录，都在 `C:\Users\<你>\LrMcp`。
- **在真实作品上用修改功能前，请先备份目录。** 开发分支会在还原前检查后续改动，发现冲突时默认拒绝覆盖；用户明确确认后可用 `force=true` 覆盖。
- Lightroom 会自行规范化部分预设参数（如 `LensBlur`）；这些参数的后续手动改动无法可靠识别冲突。还原预设前请先查看照片及预演结果。
- 所有东西都在你自己的电脑上运行。Python 服务只监听 `127.0.0.1`，且独占端口。除下面的更新提醒外，本项目不会通过网络发送任何东西。**注意：** 工具返回的预览图和照片信息会发给你连接的 AI 服务（Claude、Codex 等），这是它的工作方式，所以只对愿意分享给该服务的照片使用。
- 插件和服务之间的请求需要随机口令（每次服务启动重新生成）、正确的 `Host` 头，并且带 `Origin` 头（浏览器会加）的请求一律拒绝，所以网页无法和服务通信。
- 插件只运行固定白名单里的命令，从不接收请求里的文件路径；修改时插件自己再检查滑块白名单、范围、单次限幅和只读开关，不信任服务。
- 开发版包含**开发通道**（`DevRun.lua`、`devscripts/`、`server/dev_bridge.py`），只在 `~/LrMcp/dev_enabled` 存在时才启用，发布版不含它。手动复制插件文件夹的话，请删掉 `DevRun.lua` 和 `devscripts/`。

## 更新提醒（唯一的联网行为）

服务每天最多向 GitHub 公开接口询问一次本项目的最新版本；如果比已安装的新，`lr_status` 会多一行提示。这是匿名的 HTTPS 请求，超时 5 秒，不发送任何关于你、你的照片或 Lightroom 的信息（GitHub 能看到你的 IP 和 User-Agent `lrmcp/<版本>`）。不会自动下载或安装。关闭方法：设环境变量 `LRMCP_NO_UPDATE_CHECK=1`，或在 `C:\Users\<你>\LrMcp\` 下建一个空文件 `no_update_check`。

## 已知局限

- 以你的 Windows 用户运行的任何程序都能读口令文件并和服务通信（本机工具的通病）。口令文件的权限用 `icacls` 尽力收紧，尚未验证。
- 可同时运行多个服务（每个 AI 会话一个），端口 58770–58789；插件只轮询这个范围，每个服务有自己的随机口令。

## 报告漏洞

请用 GitHub 的**私密漏洞报告**（"Security" 标签 → "Report a vulnerability"）。用不了的话，请开一个 issue，只说明"我有安全问题要报告"，**不要写细节**，我会安排私下渠道。修复发布前请不要公开细节。

---

# Security

## What this software can and cannot do

- **Guarded changes:** it can change whitelisted develop settings and catalog metadata, and apply presets without masks.
  It cannot edit masks, delete photos, or import/export. Develop changes have range and step limits, and old values are
  saved before a write (`C:\Users\<you>\LrMcp\edits`) for restoration. A **read-only switch** (file
  `C:\Users\<you>\LrMcp\readonly`) makes the server and plug-in refuse every change. Version 0.1 was read-only.
- Other files it writes: preview JPEGs in `C:\Users\<you>\LrMcp\previews` (never added to your catalog), a token, a
  port/log file and the saved change records, all in `C:\Users\<you>\LrMcp`.
- **Back up your catalog before using the change tools on real work.** The development branch checks for later changes
  before restoration and refuses conflicting writes by default; `force=true` overwrites only after explicit review.
- Lightroom normalizes some preset settings, such as `LensBlur`. Later manual changes to these settings cannot be
  detected reliably, so inspect the photo and restoration plan before restoring a preset.
- Everything runs on your own computer. The Python server listens on `127.0.0.1` only, with an exclusive bind
  (no other process can share the port). Nothing is sent over the internet by this project. **Note:** the preview
  images and photo information that the tools return go to whichever AI service you connect (Claude, Codex, ...);
  that is how it works, so only use it with photos you are happy to share with that service.
- Requests between the Lightroom plug-in and the server need a random token (regenerated on every server start,
  stored in a file that only your Windows user should read), a correct `Host` header, and are refused if they carry
  an `Origin` header (which browsers add), so a web page cannot talk to the server.
- The plug-in runs a fixed whitelist of commands. It never takes a file path from a request, and for changes it checks the
  slider whitelist, the range, the step size and the read-only switch itself, without trusting the server.
- Development builds contain a **development bridge** (`DevRun.lua`, `devscripts/`, `server/dev_bridge.py`) that can run
  Lua scripts from the plug-in's own folder. It is active only while `~/LrMcp/dev_enabled` exists, which only the
  development server creates. Release builds do not contain it. If you copy the plug-in folder by hand, delete
  `DevRun.lua` and `devscripts/`.

## Update notice (the only internet access)

Once a day the server asks GitHub's public API for the latest release of this project and, if it is newer than the
installed version, `lr_status` shows a one-line notice. It is an anonymous HTTPS GET with a 5-second timeout; nothing
about you, your photos or Lightroom is sent (GitHub sees your IP address and the User-Agent `lrmcp/<version>`).
Nothing is downloaded or installed automatically. Switch it off by setting `LRMCP_NO_UPDATE_CHECK=1` or by creating an
empty file `C:\Users\<you>\LrMcp\no_update_check`.

## Known limits

- Any program running as your Windows user can read the token file and talk to the server (the usual limit of local
  tools). The token file's permissions are tightened on a best-effort basis (`icacls`) and not yet verified.
- Several servers (one per AI session) can run at once, on ports 58770–58789; the plug-in only polls that range, and each server has its own random token.

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** ("Security" tab → "Report a vulnerability"). If that is not
available to you, open an issue that says you have a security report **without details**, and I will arrange a private
channel. Please do not publish details before a fix is available.
