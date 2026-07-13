# MinerU-Skill

[![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

通过 [MinerU 精准解析（VLM）API](https://mineru.net) 将 PDF、Office 文档、图片、HTML 转换为高质量结构化 Markdown / JSON / DOCX / HTML / LaTeX。作为 Claude Code 的 Skill 运行，也支持命令行独立调用。

## 目录

- [功能亮点](#功能亮点)
- [支持的输入格式](#支持的输入格式)
- [快速开始](#快速开始)
  - [1. 安装 Skill](#1-安装-skill)
  - [2. 配置 Token](#2-配置-token)
  - [3. 验证环境](#3-验证环境)
- [使用方式](#使用方式)
  - [Claude Code 对话中调用](#claude-code-对话中调用)
  - [命令行直接调用](#命令行直接调用)
- [常用参数](#常用参数)
- [超长/超大文档自动拆分](#超长超大文档自动拆分)
- [输出结构](#输出结构)
- [Token 管理与回退](#token-管理与回退)
- [常见问题排查](#常见问题排查)
- [项目结构](#项目结构)
- [参考链接](#参考链接)

## 功能亮点

- 🎯 **精准解析（VLM）**：基于视觉语言模型，高保真还原文档版面、表格、公式
- 📦 **默认路径零依赖**：不触发拆分的常规调用仅使用 Python 标准库 + 系统自带的 `curl`
- ✂️ **超限文档自动拆分/合并**：PDF/Word 超过 200 页或 200MB 时自动本地拆分、分别解析、再合并为完整文档，用户无感（需要 `pypdf`；Word 超限转换需要 LibreOffice）
- 🔄 **批量处理**：支持目录/多文件一次提交，最多 50 个文件
- 🔁 **Token 自动回退**：多 Token 链式尝试，配额耗尽时自动切换备用 Token
- 📄 **多格式输出**：除默认 Markdown + JSON 外，可选输出 DOCX / HTML / LaTeX
- 🖥️ **跨平台**：Windows / Linux / macOS 均可使用

## 支持的输入格式

| 类型 | 扩展名 | 说明 |
| --- | --- | --- |
| 文档 | `.pdf` `.doc` `.docx` `.ppt` `.pptx` `.xls` `.xlsx` | — |
| 图片 | `.png` `.jpg` `.jpeg` `.jp2` `.webp` `.gif` `.bmp` | OCR 自动开启（可关闭） |
| 网页 | `.html` | 需指定 `--model-version MinerU-HTML` |

**限制**：单文件 ≤ 200 MB 且 ≤ 200 页；单次批量 ≤ 50 个文件。**PDF/Word 超过这两条限制时默认自动本地拆分、分别解析、再合并为完整文档**，无需手动分批（详见 [超长/超大文档自动拆分](#超长超大文档自动拆分)）。

## 快速开始

### 1. 安装 Skill

将本仓库拷贝到 Claude Code 的 skills 目录：

```bash
# Linux / macOS
cp -r mineru/ ~/.claude/skills/mineru/

# Windows（PowerShell）
Copy-Item -Recurse mineru "$env:USERPROFILE\.claude\skills\mineru"
```

> **注意**：目标路径必须是 `~/.claude/skills/mineru/`。如果放到其他路径，需修改 `SKILL.md` 中的路径引用，详见 [mineru移植与使用说明.md](mineru移植与使用说明.md)。

### 2. 配置 Token

在 [mineru.net/apiManage](https://mineru.net/apiManage) 申请 Bearer Token，然后设置环境变量：

**Linux / macOS**（写入 `~/.bashrc` 或 `~/.zshrc`）：

```bash
export MINERU_TOKEN="your-token-here"
export MINERU_TOKEN_1="your-backup-token"   # 可选，备用
```

**Windows**（PowerShell，永久写入用户环境变量）：

```powershell
[System.Environment]::SetEnvironmentVariable("MINERU_TOKEN", "your-token-here", "User")
[System.Environment]::SetEnvironmentVariable("MINERU_TOKEN_1", "your-backup-token", "User")
```

> Windows 设置后需重启终端才生效。

### 3. 验证环境

```bash
python --version    # 需 ≥ 3.8
curl --version      # 任意版本即可
```

超限文档自动拆分为可选能力，仅在真正触发时才需要：

```bash
pip install pypdf   # PDF 本地拆分需要
# Word 超限转换需要 LibreOffice（soffice 在 PATH 中）：
#   Windows: winget install --id LibreOffice.LibreOffice -e
#   macOS:   brew install --cask libreoffice
#   Linux:   apt install libreoffice   /   dnf install libreoffice
```

## 使用方式

### Claude Code 对话中调用

安装 Skill 后，在 Claude Code 对话中用自然语言即可触发：

- `用 MinerU 解析这个 PDF`
- `把 report.pdf 转成 Markdown`
- `解析 ./docs/ 目录下所有文件，输出 docx 格式`
- `OCR 扫描版 PDF 并提取表格`

Claude 会自动调用 `mineru_parse.py` 并处理结果。

### 命令行直接调用

```bash
# 单文件解析
python ~/.claude/skills/mineru/scripts/mineru_parse.py document.pdf

# 指定输出目录
python ~/.claude/skills/mineru/scripts/mineru_parse.py document.pdf --output-dir ./output

# 批量：整个目录
python ~/.claude/skills/mineru/scripts/mineru_parse.py ./input-dir/ --output-dir ./output

# 批量：多个文件
python ~/.claude/skills/mineru/scripts/mineru_parse.py a.pdf b.docx c.png --output-dir ./output

# 导出额外格式
python ~/.claude/skills/mineru/scripts/mineru_parse.py file.pdf --extra-formats docx html latex

# 解析 HTML 文件（须指定专用模式）
python ~/.claude/skills/mineru/scripts/mineru_parse.py page.html --model-version MinerU-HTML
```

## 常用参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--output-dir DIR` | `mineru_output` | 输出根目录，每个文件建一个子目录 |
| `--extra-formats` | 空（仅 md + json） | 可选 `docx` `html` `latex`，可组合；触发自动拆分的文件会强制降级为仅 md+json |
| `--language` | `ch` | 文档主要语言，如 `en`、`ch` |
| `--no-ocr` | OCR 开启 | 关闭 OCR |
| `--no-formula` | 公式识别开启 | 关闭公式识别 |
| `--no-table` | 表格识别开启 | 关闭表格识别 |
| `--page-ranges "1-10,20"` | 全部页 | 仅解析指定页码范围；与自动拆分互斥 |
| `--model-version` | `vlm` | 精准解析模式；HTML 文件须改为 `MinerU-HTML` |
| `--token-env NAME` | 见下方 | 指定读取 Token 的环境变量名，可重复传入指定回退链 |
| `--no-auto-split` | 关闭（默认自动拆分） | 恢复超限文档直接失败的旧行为 |
| `--max-pages-per-chunk N` | `200` | 每个分片最大页数（1-200），调试用 |

## 超长/超大文档自动拆分

MinerU 单文件限制为 **≤200 页且 ≤200MB**，这两条限制校验的都是物理文件本身，无法用 `--page-ranges` 绕过。默认开启（`--no-auto-split` 关闭）：

- **PDF**：提交前主动探测页数/体积，超限立即本地用 `pypdf` 精确拆分为多个达标分片。
- **Word（.doc/.docx）**：体积超限可提前探测转换拆分；页数超限无法本地预判，先按原文件提交，失败后自动转换（LibreOffice `soffice --headless`）为 PDF 再拆分重试。
- **合并**：所有分片解析完成后自动合并为完整的 `full.md` / `full_content_list.json` / `images/`，跨分片边界插入拼接点注释标记（已知局限：跨分片的表格/段落可能被截断，不自动修复）。`full_content_list_v2.json` 的合并**尚不支持**——真实 API 验证发现其结构（嵌套的 `content.paragraph_content`）与 v1 版本（扁平 `page_idx` 字段）完全不同，合并逻辑遇到该文件会跳过并打印警告，不影响 `full.md`/`full_content_list.json` 主产出。
- **降级**：触发拆分的文件本次调用强制仅输出 md+json，不支持 `--extra-formats`。
- **依赖**：PDF 拆分需要 `pip install pypdf`；Word 转换需要 LibreOffice（`soffice` 在 PATH 中）。均为按需惰性加载，不触发拆分的调用不受影响。

已用真实 MinerU API + 真实构造的 205 页 PDF/docx 完整验证：主动拆分（PDF）、反应式补救（docx 提交失败 → LibreOffice 转换 → 拆分 → 重新提交）、`page_idx` 跨分片偏移校正、图片隔离合并、`--no-auto-split` 回退、`--extra-formats` 降级标注均按预期工作，205 页内容合并后零缺失零重复。

详见 [SKILL.md](SKILL.md) 「超限文档自动拆分与合并」章节。

## 输出结构

```text
<output-dir>/
├── summary.json                    # 本批次总结：batch_id、每文件状态与路径（含 auto_split 等新字段）
└── <文件名>/
    ├── full.md                     # ⭐ Markdown 主结果（始终生成；触发拆分时为合并结果）
    ├── full_content_list.json      # 触发拆分时：合并后的结构化内容列表
    ├── full_content_list_v2.json
    ├── *_content_list.json         # 未拆分时：单次解析的结构化内容列表
    ├── *_content_list_v2.json
    ├── *_model.json                # 模型推理中间结果（未拆分场景）
    ├── layout.json                 # 版面信息（未拆分场景）
    ├── *_origin.pdf / full_origin.pdf  # 解析时使用的 PDF 副本
    ├── images/                     # 抽取的图片（触发拆分时已合并重命名）
    ├── _chunks/part01/, part02/... # 仅触发拆分时：各分片原始产物归档
    ├── full.docx                   # 仅当 --extra-formats docx（且未触发拆分）
    ├── full.html                   # 仅当 --extra-formats html（且未触发拆分）
    └── full.tex                    # 仅当 --extra-formats latex（且未触发拆分）
```

## Token 管理与回退

| 场景 | 命令 |
| --- | --- |
| 默认（主 Token → 备用 Token 自动回退） | 无需额外参数 |
| 强制只用备用 Token | `--token-env MINERU_TOKEN_1` |
| 自定义多 Token 链 | `--token-env TOKEN_A --token-env TOKEN_B` |

回退逻辑仅在"创建批次"阶段触发（配额耗尽 / 鉴权失败时）。一旦批次创建成功，后续的上传与轮询使用同一个 Token。

## 常见问题排查

| 错误码 / 现象 | 原因 | 解决方法 |
| --- | --- | --- |
| `A0202 / A0211` | Token 格式错误或无效 | 检查环境变量是否正确设置 |
| `-60002` | 文件后缀不支持或缺失 | 确认文件扩展名在支持列表中 |
| `-60005` | 文件超过 200 MB | 默认自动拆分已处理，若仍报错检查是否加了 `--no-auto-split` 或未安装 `pypdf` |
| `-60006` | 文件超过 200 页 | 默认自动拆分已处理，`--page-ranges` 无法绕过此限制；若仍报错同上排查 |
| `soffice` 不在 PATH | 超限 Word 转换需要 LibreOffice | 按平台安装：Windows `winget install --id LibreOffice.LibreOffice -e`；macOS `brew install --cask libreoffice`；Linux `apt/dnf install libreoffice` |
| `curl not found` | curl 未安装或不在 PATH | 安装 curl 并确认 `curl --version` 可执行 |
| `none of these env vars are set` | Token 环境变量未设置 | 按[快速开始](#2-配置-token)配置环境变量 |
| Windows schannel 警告（exit 56） | curl schannel 后端兼容问题 | 脚本已自动识别为成功，可忽略 |
| `state=failed` | 服务端解析失败 | 查看 `summary.json` 中对应条目的 `err_msg` |
| `state=partial` | 触发拆分后部分分片解析失败 | 查看 `summary.json` 中 `missing_parts` 字段，了解缺失的页码范围 |

## 项目结构

```text
mineru/
├── README.md                       # 项目入口文档（本文件）
├── SKILL.md                        # Claude Code Skill 定义文件
├── mineru移植与使用说明.md           # 详细的移植与使用手册
└── scripts/
    ├── mineru_parse.py             # 核心解析脚本（含拆分/合并编排逻辑）
    └── doc_splitter.py             # 超限文档拆分/转换/合并模块
```

- [SKILL.md](SKILL.md) 是 Claude Code 读取的 Skill 元数据，包含所有命令模板和工作流程说明
- [mineru_parse.py](scripts/mineru_parse.py) 是独立的 CLI 工具，不依赖 Skill 框架也可直接运行
- [doc_splitter.py](scripts/doc_splitter.py) 提供 PDF 拆分、Word 转 PDF、结果合并的纯文件级处理逻辑，被 `mineru_parse.py` 按需惰性调用
- [mineru移植与使用说明.md](mineru移植与使用说明.md) 提供了更详细的移植步骤、参数说明和排查指南

## 参考链接

- [MinerU 官网](https://mineru.net)
- [API Token 申请](https://mineru.net/apiManage)
- [MinerU API 文档](https://mineru.net/docs/api)
