# MinerU Skill 移植与使用说明

> 适用版本：mineru skill（Claude Code Skills）  
> 文档生成时间：2026-05-17

---

## 一、目录结构

```text
mineru/
├── SKILL.md              # Claude Code 读取的 skill 定义（含硬编码路径，移植后需修改）
└── scripts/
    ├── mineru_parse.py   # 核心解析脚本（含超限文档拆分/合并编排逻辑）
    └── doc_splitter.py   # 超限文档拆分/Word转PDF/结果合并模块（按需惰性加载第三方依赖）
```

---

## 二、移植步骤

### 第 1 步：拷贝文件

将整个 `mineru/` 目录复制到目标机器的 Claude Code skills 目录：

```bash
# Linux / macOS
cp -r mineru/ ~/.claude/skills/mineru/

# Windows（PowerShell）
Copy-Item -Recurse mineru "C:\Users\<用户名>\.claude\skills\mineru"
```

> **注意**：目标路径必须是 `~/.claude/skills/mineru/`，否则需执行第 2 步修改路径。

### 第 2 步：修改 SKILL.md 中的硬编码路径（若目标路径不同）

`SKILL.md` 中的示例命令写死了原始路径，Claude 会照搬这些路径调用脚本。  
如果目标机器的路径不是 `~/.claude/skills/mineru/`，需将 SKILL.md 中所有出现的：

```text
~/.claude/skills/mineru/scripts/mineru_parse.py
```

替换为实际路径，例如：

```bash
# Linux/macOS 快速替换
sed -i 's|~/.claude/skills/mineru|/your/actual/path/mineru|g' ~/.claude/skills/mineru/SKILL.md

# Windows PowerShell
(Get-Content .claude\skills\mineru\SKILL.md) `
  -replace '~/.claude/skills/mineru', 'C:\your\actual\path\mineru' |
  Set-Content .claude\skills\mineru\SKILL.md
```

> **脚本本身（`mineru_parse.py`）无硬编码路径**，只有 SKILL.md 的示例说明需要修改。

### 第 3 步：配置 API Token 环境变量

在 [https://mineru.net/apiManage](https://mineru.net/apiManage) 申请 Bearer Token 后，设置环境变量：

```bash
# Linux / macOS（写入 ~/.bashrc 或 ~/.zshrc 永久生效）
export MINERU_TOKEN="your-token-here"
export MINERU_TOKEN_1="your-backup-token"   # 可选，主 token 配额耗尽时自动切换

# Windows（PowerShell，永久写入用户环境变量）
[System.Environment]::SetEnvironmentVariable("MINERU_TOKEN", "your-token-here", "User")
[System.Environment]::SetEnvironmentVariable("MINERU_TOKEN_1", "your-backup-token", "User")
```

> Windows 设置系统环境变量后需重启终端才生效。

### 第 4 步：确认运行时依赖

| 依赖 | 说明 |
| ------ | ------ |
| Python 3.8+ | 常规调用仅需标准库，无需额外 pip 安装 |
| `curl` | 用于上传文件到 OSS 预签名 URL，需在 PATH 中可访问 |
| 网络访问 `mineru.net` | 脚本调用公网 API，需能访问 `https://mineru.net` |
| `pypdf`（按需） | 仅当文档超过 200 页或 200MB 触发自动拆分时才需要：`pip install pypdf` |
| LibreOffice（按需） | 仅当超限 Word 文档需要转换为 PDF 再拆分时才需要，需要 `soffice` 在 PATH 中：Windows `winget install --id LibreOffice.LibreOffice -e`；macOS `brew install --cask libreoffice`；Linux `apt/dnf install libreoffice` |

验证：

```bash
python --version    # >= 3.8
curl --version      # 任意版本即可
pip show pypdf      # 仅需要自动拆分功能时验证
soffice --version   # 仅需要 Word 超限转换功能时验证
```

> **Windows 中文控制台编码说明**：Windows 默认中文控制台使用 GBK 编码。脚本在 `main()` 入口已对 `stdout`/`stderr` 做了 `reconfigure(errors="replace")` 防御性处理，遇到 GBK 无法编码的字符（如某些特殊符号）会替换为占位符而不是让脚本崩溃，无需额外配置即可正常使用。

---

## 三、使用方法

所有命令中的脚本路径请替换为实际安装路径。

### 基本用法

```bash
# 单文件解析（输出 Markdown + JSON）
python ~/.claude/skills/mineru/scripts/mineru_parse.py document.pdf

# 指定输出目录
python ~/.claude/skills/mineru/scripts/mineru_parse.py document.pdf --output-dir ./output

# 批量：整个目录
python ~/.claude/skills/mineru/scripts/mineru_parse.py ./input-dir/ --output-dir ./output

# 批量：多个文件
python ~/.claude/skills/mineru/scripts/mineru_parse.py a.pdf b.docx c.png --output-dir ./output
```

### 指定额外输出格式

默认只输出 Markdown + JSON，可按需追加：

```bash
# 同时导出 Word 文档
python mineru_parse.py file.pdf --extra-formats docx

# 同时导出 Word + HTML + LaTeX
python mineru_parse.py file.pdf --extra-formats docx html latex
```

### 常用参数说明

| 参数 | 默认值 | 说明 |
| ------ | -------- | ------ |
| `--output-dir DIR` | `mineru_output` | 输出根目录，每个文件建一个子目录 |
| `--extra-formats` | 空（仅 md+json） | 可选 `docx` `html` `latex`，可组合；触发自动拆分的文件会强制降级为仅 md+json |
| `--language` | `ch` | 文档主要语言，如 `en`、`ch` |
| `--no-ocr` | OCR 开启 | 关闭 OCR |
| `--no-formula` | 公式识别开启 | 关闭公式识别 |
| `--no-table` | 表格识别开启 | 关闭表格识别 |
| `--page-ranges "1-10,20"` | 全部页 | 仅解析指定页码范围；与自动拆分互斥 |
| `--model-version` | `vlm` | 精准解析模式；解析 HTML 文件须改为 `MinerU-HTML` |
| `--token-env NAME` | 见下方 | 指定读取 Token 的环境变量名，可重复传入 |
| `--no-auto-split` | 关闭（默认自动拆分） | 恢复超限文档（>200页或>200MB）直接失败的旧行为 |
| `--max-pages-per-chunk N` | `200` | 每个分片最大页数（1-200），主要用于调试拆分逻辑 |

### Token 切换与回退

```bash
# 默认自动回退：先用 MINERU_TOKEN，失败后用 MINERU_TOKEN_1
python mineru_parse.py file.pdf

# 强制只用备份 Token
python mineru_parse.py file.pdf --token-env MINERU_TOKEN_1

# 自定义多 Token 链式回退
python mineru_parse.py file.pdf \
    --token-env MINERU_TOKEN_A \
    --token-env MINERU_TOKEN_B
```

### 解析 HTML 文件

HTML 文件须使用专用模式，且不支持 `--extra-formats`：

```bash
python mineru_parse.py page.html --model-version MinerU-HTML
```

### 超限文档（>200页 或 >200MB）自动拆分与合并

默认开启，无需额外参数。PDF 提交前主动探测并本地拆分；Word (.doc/.docx) 体积超限提前拆分，页数超限则先提交失败后自动转换（需 LibreOffice）再拆分重试。所有分片解析完成后自动合并为完整的 `full.md`/`full_content_list.json`/`images/`：

```bash
# 无需特殊参数，超限自动处理
python mineru_parse.py huge_report.pdf

# 用 --no-auto-split 恢复旧行为（超限直接失败，需手动 --page-ranges 分段）
python mineru_parse.py huge_report.pdf --no-auto-split

# 调试：用较小的分片页数验证拆分/合并逻辑
python mineru_parse.py sample.pdf --max-pages-per-chunk 5
```

触发拆分的文件会强制降级为仅 md+json 输出（不支持 `--extra-formats`），且与 `--page-ranges` 互斥。

**已知局限**：合并逻辑只支持 `full_content_list.json`（v1 格式，含扁平 `page_idx` 字段）。`full_content_list_v2.json` 的合并**尚不支持**——真实调用 MinerU API 验证发现，v2 版本的实际结构是嵌套的 `content.paragraph_content`/`content.image_source.path`，与 v1 的扁平 `page_idx` 字段完全不同。触发拆分时，合并逻辑遇到 v2 文件会打印警告并跳过，不影响 `full.md`/`full_content_list.json` 主产出。

**真实验证状态**：已用真实 MinerU API + 真实构造的 205 页 PDF 与 205 页 docx 完整跑通：PDF 主动拆分（提交前探测触发）、docx 反应式补救（原始提交真实失败 → LibreOffice 转换 → 拆分 → 重新提交成功）、`page_idx` 跨分片偏移校正、图片按分片隔离合并（重命名避免同名冲突）、`--no-auto-split` 回退（复现真实 `-60006` 失败）、`--extra-formats` 降级标注，205 页内容合并后逐页核对零缺失零重复。

详见 [SKILL.md](SKILL.md) 「超限文档自动拆分与合并」章节。

---

## 四、输出结构

解析完成后，`<output-dir>/<文件名>/` 目录下包含：

```text
output/
├── summary.json                    # 本批次总结：batch_id、每文件状态与路径（含 auto_split 等新字段）
└── <文件名>/
    ├── full.md                     # Markdown 主结果（始终生成；触发拆分时为合并结果）
    ├── full_content_list.json      # 触发拆分时：合并后的结构化内容列表
    ├── full_content_list_v2.json
    ├── *_content_list.json         # 未拆分时：单次解析的结构化内容列表
    ├── *_content_list_v2.json
    ├── *_model.json                # 模型推理中间结果（未拆分场景）
    ├── layout.json                 # 版面信息（未拆分场景）
    ├── *_origin.pdf / full_origin.pdf  # 解析时使用的 PDF 副本
    ├── images/                     # 抽取的图片（触发拆分时已合并重命名）
    ├── _chunks/part01/, part02/... # 仅触发拆分时：各分片原始产物归档
    ├── full.docx                   # 仅当 --extra-formats docx 时生成（且未触发拆分）
    ├── full.html                   # 仅当 --extra-formats html 时生成（且未触发拆分）
    └── full.tex                    # 仅当 --extra-formats latex 时生成（且未触发拆分）
```

---

## 五、支持的输入格式

| 类型 | 扩展名 |
| --- | --- |
| 文档 | `.pdf` `.doc` `.docx` `.ppt` `.pptx` `.xls` `.xlsx` |
| 图片 | `.png` `.jpg` `.jpeg` `.jp2` `.webp` `.gif` `.bmp` |
| 网页 | `.html`（须用 `--model-version MinerU-HTML`） |

**限制**：单文件 ≤ 200 MB 且 ≤ 200 页；单次批量 ≤ 50 个文件。**PDF/Word 超过页数或体积限制时默认自动本地拆分、分别解析、再合并**，图片/PPT/XLS/HTML 超限仍需人工处理（不在自动拆分范围内）。

---

## 六、常见错误排查

| 错误码 / 现象 | 原因 | 解决方法 |
| -------------- | ------ | --------- |
| `A0202 / A0211` | Token 格式错误或无效 | 检查 `MINERU_TOKEN` 环境变量是否正确设置 |
| `-60002` | 文件后缀不支持或缺失 | 确认文件扩展名在支持列表中 |
| `-60005` | 文件超过 200 MB | 默认自动拆分已处理该场景；若仍报错，检查是否加了 `--no-auto-split` 或未安装 `pypdf` |
| `-60006` | 文件超过 200 页 | 默认自动拆分已处理该场景；`--page-ranges` **无法**绕过此限制（校验物理文件页数，非 page_ranges 跨度）；仍报错同上排查 |
| `soffice` 不在 PATH | 超限 Word 文档需要转换但 LibreOffice 未安装 | 按平台安装 LibreOffice（见「第 4 步：确认运行时依赖」），装完重启终端 |
| LibreOffice 转换超时/产物缺失 | 转换失败（超时600秒或产物未生成） | 手动打开该文件确认是否损坏，或手动转换为 PDF 后单独提交 |
| `curl not found` | curl 未安装或不在 PATH | 安装 curl 并确认 `curl --version` 可执行 |
| `none of these env vars are set` | Token 环境变量未设置 | 按第 3 步配置环境变量 |
| Windows schannel 警告（exit 56） | curl schannel 后端兼容问题 | 脚本已自动识别为成功，可忽略 |
| `state=failed` | 服务端解析失败 | 查看 `summary.json` 中对应条目的 `err_msg` |
| `state=partial` | 触发拆分后部分分片解析失败 | 查看 `summary.json` 中 `missing_parts` 字段，了解缺失的页码范围，必要时用 `--page-ranges` 单独补齐 |

---

## 七、在 Claude Code 中通过 Skill 调用

完成移植后，在 Claude Code 对话中直接用自然语言触发，无需手动运行脚本：

- `用 MinerU 解析这个 PDF`
- `把 report.pdf 转成 Markdown`
- `解析 ./docs/ 目录下所有文件，输出 docx 格式`
- `OCR 扫描版 PDF 并提取表格`

Claude 会自动调用 `mineru_parse.py` 并处理结果。
