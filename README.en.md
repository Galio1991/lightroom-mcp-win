# 📸 Lightroom MCP for Windows

**Let Claude or Codex look at your photos in Adobe Lightroom Classic.**
A Lightroom plug-in plus a small MCP server. It can see the photo you selected, read its develop settings, and show
the assistant a preview with histogram numbers. It can also make guarded develop changes, update catalog information,
and save old values for restoration.

![License: MIT](https://img.shields.io/badge/license-MIT-blue) ![Platform: Windows](https://img.shields.io/badge/platform-Windows-lightgrey) ![Status: early](https://img.shields.io/badge/status-early%200.11.1-orange)

[中文说明](README.md) · [Changelog](CHANGELOG.md) · [Security](SECURITY.md) · [Contributing](CONTRIBUTING.md)

> 🤖 **AI assistant reading this for a user?** Read [`AI_GUIDE.md`](AI_GUIDE.md): it has the whole install and usage procedure.

> ⚠️ **Early version (0.11.1).** Tested only on Windows 11 with Lightroom Classic 15.2. An installer is published, but it has not been install/uninstall-tested on a clean machine yet (待验证).
> Not affiliated with Adobe.

## ✨ What it does

| Tool | What it gives the assistant |
|---|---|
| `lr_status` | Is Lightroom connected, its version, the open catalog, how many photos are selected |
| `lr_list_selected` | Selected photos: name, format, rating, size, ISO / aperture / shutter / focal length / lens / camera |
| `lr_get_develop` | Current develop settings: exposure, contrast, highlights, shadows, white balance, HSL, … |
| `lr_get_preview` | A 1024 px preview (256–2048) **plus numbers**: brightness, clipped highlights/shadows, warm/cool balance |
| `lr_doctor` | Self check in Chinese: what is wrong and how to fix it |
| `lr_set_develop` | **Changes** supported settings of one photo with range and step limits; `preview=true` returns before and after previews with numeric differences |
| `lr_review_selected` (in development) | Show previews and statistics for up to 10 selected photos to help choose picks; it does not change ratings or flags |
| `lr_restore` | Puts the sliders of one earlier change back to their old values |
| `lr_restore_original` | Puts every slider LrMCP changed on a photo back to its value before LrMCP's first change (`dry_run` shows what it would do) |
| `lr_get_metadata` / `lr_set_metadata` | Read and change catalog information: rating, flag (pick / reject), colour label, title, caption, keywords; old values are saved first and can be restored |
| `lr_batch_set_develop` | Batch: add the same relative change to several photos (at most 10). Dry run first (changes nothing, returns each photo's result and a plan id), executed only with that plan id; the whole batch can be undone at once |
| `lr_list_presets` / `lr_apply_preset` | Find and apply presets without masks; save the old settings first and optionally return before and after previews |
| `lr_plan_from_edit` (in development) | Turn a verified single-photo slider edit into a dry-run plan for other photos; use the batch tool to apply after approval |
| `lr_list_edits` | Lists the changes LrMCP made (id, time, photo, sliders, restored or not) |

Example: select a photo and ask *"Look at the selected photo and tell me what is wrong with the exposure."*
The assistant gets the picture and numbers such as `mean brightness 77.6`, `warm/cool index −9.9 %` and can say
*"a bit dark and cool"* with evidence.

### What changing means, and how it is kept safe

- **Only sliders, only a short list**: 89 sliders (exposure, the ten global ones above, 24 HSL values, **temperature / tint of RAW photos**, **colour grading**, **camera calibration**, **parametric curve**, **sharpening / noise reduction / vignettes / grain / defringe**, **crop and straighten**)
  plus **4 point curves** (luminance, red, green, blue). Masks cannot be changed (writing them through the SDK was measured to be unreliable); presets without masks can be applied; JPEGs have no kelvin values, so their white balance cannot be changed.
- **Range limits** (exposure ±5, the rest ±100) and a **step limit per call** (exposure ±1, the rest ±30): a bigger jump
  is refused and the assistant is told to go in several steps and look at the preview each time. Lightroom itself
  does *not* check ranges (measured: it stores out-of-range values as given), so this project does.
- **The old values are saved to disk before anything is written** (`C:\Users\<you>\LrMcp\edits`). Restoring means
  writing them back; Lightroom's snapshots and undo were measured to be unreliable, so they are not used.
- **Read-only switch**: create the file `C:\Users\<you>\LrMcp\readonly` and every change is refused, by the server and
  by the plug-in. Delete the file to allow changes again.
- One photo per call. The plug-in re-checks every rule itself; it does not trust the server.
- Changes appear in Lightroom's History panel as `LrMCP <id>` steps. **Back up your catalog (`.lrcat`) before using this
  on real work, and try it on a test catalog first.** The development branch detects later changes before restoring;
  a conflicting restore requires explicit `force=true` after review.

Files it creates: preview JPEGs (not added to your catalog), a token, a log and the saved change records, all in
`C:\Users\<you>\LrMcp`.

## 🏗️ How it works

```
Claude / Codex ──(MCP, stdio)──▶ lrmcp (Python server, local queue on 127.0.0.1:58770)
                                          ▲
              Lightroom plug-in (Lua) polls the queue, runs a whitelisted command, sends the answer back
```

- Local only: the server binds `127.0.0.1` exclusively, requests need a random token, the `Host` header must match,
  and requests with an `Origin` header (browsers) are refused. Several AI sessions can run at once (one server each, ports 58770–58789).
- The plug-in runs a fixed whitelist of commands and never takes a file path from a request. A write needs a whitelisted
  slider, a value in range, a small enough step, and no read-only switch.
- Note: the images and photo information the tools return go to the AI service you use. See [SECURITY.md](SECURITY.md).

## 🧰 Requirements

- Windows, Adobe Lightroom Classic (developed on 15.2; SDK 15.x), Python 3.10+ (tested with 3.12).
- Claude Code, Codex, or another MCP client (Claude Code was tested; Codex was not).

## 📦 Install with the installer (no Python needed)

1. Download `LrMCP-Setup-*.exe` from [Releases](https://github.com/Denvolatos/lightroom-mcp-win/releases/latest) and compare the SHA-256 on the page.
2. **Double-click it from your own File Explorer** (not from a shell started by Claude Desktop, see the MSIX note below). It is not code-signed, so Windows SmartScreen shows "unknown publisher".
3. Restart Lightroom and your AI assistant, then have it call `lr_status`.

## 🚀 Install from source

1. **Server** (normal PowerShell):
   ```powershell
   pip install ./server
   lrmcp register --dry-run   # shows what it would do
   lrmcp register             # registers "lightroom_mcp" with Claude Code and/or Codex (uses their own CLIs)
   ```
2. **Plug-in**: copy `plugin/lrmcp.lrdevplugin` to `%APPDATA%\Adobe\Lightroom\Modules\LrMcp.lrplugin`, delete
   `DevRun.lua` and the `devscripts` folder from the copy (they are development helpers), and restart Lightroom.
   In *File → Plug-in Manager* LrMCP should be enabled. `powershell -ExecutionPolicy Bypass -File build.ps1`
   builds a clean staged copy plus an installer (needs the conda env and Inno Setup, see [CONTRIBUTING](CONTRIBUTING.md)).
3. Restart your assistant, then ask it to call `lr_status`. If something fails, ask for `lr_doctor`.

> **Windows gotcha:** *Claude Desktop is an MSIX app*, and Windows silently redirects what programs it starts write to
> `AppData`. Run installers and copy steps from your own PowerShell, not from a shell started by Claude Desktop.
> That is also why this project keeps its shared files in `C:\Users\<you>\LrMcp`, not in `AppData`.

Claude Desktop is not registered automatically (its config file location depends on how it was installed); add the
command by hand under *Settings → Developer → Edit Config*.

## 💬 Feedback

Found a problem, or got it working in another AI app? **[Open an issue](../../issues/new/choose)** (bug report,
compatibility report, feature request) or start a **[discussion](../../discussions)**. The forms ask for the output of
`lr_doctor`, which makes most problems easy to find. Please never attach photos or catalogs. No usage data is collected.

## 🗺️ Roadmap

| Version | Plan |
|---|---|
| 0.1 | Read-only: look at photos, develop settings, preview, histogram numbers, self check |
| 0.2–0.11.1 | Guarded single-photo edits, catalog metadata, batch dry runs, presets, and restoration; see the tools above |
| current development branch | Restore conflict detection, before/after previews, selected-photo review, and batch plans from verified edits |
| later | RAW and clean Windows installer acceptance; then consider batch presets and export |

Masks and AI features depend on what Adobe's SDK offers and are not promised.

## 📚 Docs

Design notes are in Chinese for now: [`docs/方案.md`](docs/方案.md) (plan), [`docs/实验记录.md`](docs/实验记录.md) (what was
measured on Lightroom Classic 15.2: e.g. writes need `withWriteAccessDo`, snapshots and undo are unreliable),
[`docs/实现记录.md`](docs/实现记录.md) (implementation notes).

## 🙏 See also

Other Lightroom Classic MCP projects, studied for ideas (no code was copied):
[Automaat/lightroom-mcp](https://github.com/Automaat/lightroom-mcp) (TypeScript, MIT, many tools, Windows and macOS)
and [noopz/lightroom_mcp](https://github.com/noopz/lightroom_mcp) (Python, Apache-2.0).
This project aims to be small, careful about changes, Python-based and easy to install for non-programmers.

## ⚖️ License and disclaimer

[MIT](LICENSE). Third-party packages: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
Adobe and Lightroom are trademarks of Adobe Inc.; this project is not affiliated with, endorsed or sponsored by Adobe.
The Adobe Lightroom Classic SDK is not included and must not be committed.
