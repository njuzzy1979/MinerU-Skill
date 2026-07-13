---
name: mineru
description: Use this skill to parse PDFs, Office files (doc/docx/ppt/pptx/xls/xlsx), images, or HTML into structured Markdown / JSON / DOCX / HTML / LaTeX via the MinerU 精准解析（VLM）API. Trigger when the user asks to "parse / 解析 / 转换 / extract" a document with MinerU, mentions MinerU, supplies a PDF and asks for high-quality conversion to markdown/docx/html/latex, or wants to OCR scanned documents and tables/formulas with high fidelity.
---

# MinerU 精准解析 Skill

通过 MinerU 公网 API 的"精准解析（model_version=vlm）"模式，将 PDF/Office/图片/HTML 转换为高质量 Markdown 与可选的 DOCX / HTML / LaTeX。

## 前置条件

1. 至少设置一个 MinerU API Token 环境变量（Bearer Token，在 https://mineru.net/apiManage 申请）：
   - `MINERU_TOKEN` —— 主 Token（首选）
   - `MINERU_TOKEN_1` —— 备份 Token（可选）
2. 系统中存在 `python`（≥ 3.8，仅需标准库）与 `curl`。
3. 限制：单文件 ≤ 200 MB 且 ≤ 200 页；单次批量 ≤ 50 个文件。**超过页数或体积限制的 PDF/Word 文档默认会被自动本地拆分、分别解析、再自动合并，用户无需手动分批**（见下方「超限文档自动拆分与合并」）。
4. （仅超限文档场景需要）本地拆分 PDF 需要 `pypdf`：`pip install pypdf`。超限 Word 文档转换为 PDF 需要 LibreOffice（`soffice` 在 PATH 中）。不触发拆分的常规调用不需要这两项依赖。

## 工作流程

脚本位置：`scripts/mineru_parse.py`（本 skill 目录下的 scripts/ 子目录，由当前 runtime 解析路径）。

### 🛑 STOP: 检查 Token

在执行任何操作之前，先验证环境变量：

```bash
# 检查是否有任一 Token 可用
echo $MINERU_TOKEN || echo $MINERU_TOKEN_1
```

如果两个都为空 → **停止执行**，引导用户：
> "MinerU API Token 未设置。请先到 https://mineru.net/apiManage 申请 Token，然后设置环境变量：
>
> - Linux/macOS: `export MINERU_TOKEN='your-token'`
> - Windows: `[System.Environment]::SetEnvironmentVariable('MINERU_TOKEN', 'your-token', 'User')`（需重启终端）"

Token 确认存在后继续。

### Step 1: 收集输入文件

```text
输入：用户指定的文件或目录路径
输出：文件列表（自动过滤支持的类型）
```

- 支持类型：`.pdf .doc .docx .ppt .pptx .xls .xlsx .png .jpg .jpeg .jp2 .webp .gif .bmp .html`
- 目录输入时自动收集目录下所有支持类型的文件
- 数量超过 50 个时拒绝并提示分批

🔴 **CHECKPOINT** — 文件数 > 10 时，列出完整文件清单并确认用户是否继续。文件数 > 50 时，🛑 STOP 并提示用户分批。

### Step 2: 确认参数

```text
输入：用户需求（语言/OCR/公式/表格/输出格式/页码范围）
输出：完整的命令行参数
```

按用户需求组合参数，默认值见下方参数速查表。用户未明确指定时使用默认值。

🔴 **CHECKPOINT** — 如果输入文件包含 `.html` 文件，必须确认 `--model-version MinerU-HTML` 已设置，且**不传** `--extra-formats`。HTML 用默认 vlm 模式或在 HTML 文件上启用 extra_formats 都会导致解析失败。

### Step 3: 调用脚本

```bash
python scripts/mineru_parse.py <文件或目录> [参数...]
```

脚本内部最多执行五阶段（第2、3阶段仅在存在超限文档时才实际触发）：

1. `[1/5]` 收集文件，对 PDF 主动探测页数/体积、对 .doc/.docx 探测体积，判定是否需要本地拆分
2. `[2/5]` 提交（最多两路：常规文件一路，本地预拆分出的分片一路），每路各自：
   `POST /api/v4/file-urls/batch` 创建批次 → `PUT`(curl) 上传 → `GET /api/v4/extract-results/batch/{batch_id}` 轮询
3. `[3/5]` 反应式补救：.doc/.docx 若因页数或体积超限失败，自动转换为 PDF 后拆分并重新提交
4. `[4/5]` 按原始文件分组，多分片结果自动合并为完整的 full.md / content_list.json / images
5. `[5/5]` 下载解压、写 summary.json、清理本地拆分产生的临时文件

无超限文档的常规调用，行为与拆分功能引入前完全一致（仅一路提交，无需第2/3阶段的额外分支）。

### Step 4: 处理结果

```text
输入：<output-dir>/<文件名>/full.md
输出：向用户展示解析后的 Markdown 内容
```

读取 `full.md` 返回给用户。检查 `summary.json` 中每个文件的 `state`：

- `state=done`：正常，直接展示 `full.md`
- `state=failed`：查看 `err_msg` 排查（见下方失败排查）
- `state=partial`：**文档触发了自动拆分，其中部分分片解析失败**。`full.md` 仍会生成，但缺失部分会以 `⚠ 解析失败` 占位标记呈现；需要读取 `missing_parts` 字段（含每个失败分片的页码范围与 `err_msg`），**明确告知用户哪些页码范围内容缺失**，不能当作完全成功处理

## 参数速查

| 参数 | 默认值 | 说明 |
| ------ | -------- | ------ |
| `--output-dir DIR` | `mineru_output` | 输出根目录，每个文件建一个子目录 |
| `--extra-formats {docx,html,latex} ...` | 空（仅 md+json） | 额外导出格式；**触发自动拆分的文件会强制降级为仅 md+json**（见下方说明） |
| `--language ch\|en\|...` | `ch` | 文档主要语言 |
| `--no-ocr` | OCR 启用 | 关闭 OCR |
| `--no-formula` | 公式启用 | 关闭公式识别 |
| `--no-table` | 表格启用 | 关闭表格识别 |
| `--page-ranges "1-10,20"` | 全部 | 仅解析指定页码；**与自动拆分互斥**，若目标文件超限会报错，需二选一 |
| `--model-version vlm\|pipeline\|MinerU-HTML` | `vlm` | HTML 文件须改为 `MinerU-HTML` |
| `--token-env NAME`（可重复） | `MINERU_TOKEN` → `MINERU_TOKEN_1` | 指定 Token 环境变量，按顺序尝试 |
| `--no-auto-split` | 关闭（默认开启自动拆分） | 逃生舱：完全恢复超限文档直接失败的旧行为（`-60005`/`-60006`），不做任何拆分/合并 |
| `--max-pages-per-chunk N` | `200` | 每个分片最大页数（`1≤N≤200`）；体积限制固定 200MB，不可调；主要用于小样本调试拆分逻辑 |

### 常用命令示例

```bash
# 单文件
python scripts/mineru_parse.py document.pdf --output-dir ./output

# 批量：目录
python scripts/mineru_parse.py ./input-dir/ --output-dir ./output

# 批量：多个文件
python scripts/mineru_parse.py a.pdf b.docx c.png --output-dir ./output

# 额外格式
python scripts/mineru_parse.py file.pdf --extra-formats docx html latex

# HTML 文件（必须用专用模式，不支持 extra_formats）
python scripts/mineru_parse.py page.html --model-version MinerU-HTML
```

## Token 回退

| 场景 | 命令 |
| ------ | ------ |
| 默认（主 → 备用自动回退） | 无需额外参数 |
| 强制只用备用 Token | `--token-env MINERU_TOKEN_1` |
| 自定义多 Token 链 | `--token-env TOKEN_A --token-env TOKEN_B` |

回退仅在 `POST /file-urls/batch` 阶段触发（配额耗尽 / 鉴权失败）。批次创建成功后，后续步骤使用同一 Token。

## 超限文档自动拆分与合并

MinerU 对单文件有两条独立硬限制：**页数 ≤200 页**（错误码 `-60006`）与**体积 ≤200MB**（错误码 `-60005`）。两者校验的都是物理文件本身，**无法用 `--page-ranges` 参数绕过**（-60006 官方修复建议就是"拆分文件后重试"）。默认开启自动拆分（`--no-auto-split` 可关闭），全程用户无感：

- **PDF**：提交前主动探测页数与体积，任一超限立即本地按 `pypdf` 精确切分为多个 ≤200页 且 ≤200MB 的分片。
- **Word (.doc/.docx)**：体积可提前探测，超限则先转换再拆分；页数无法在本地零成本预判，采用反应式策略——先按原文件正常提交，若失败且错误特征匹配页数/体积超限，才用 LibreOffice（`soffice --headless`）转换为 PDF 后按同一套逻辑拆分，重新提交一次。
- **合并**：所有分片解析完成后，按原始文件自动合并为完整的 `full.md`（图片路径按分片命名空间隔离重写，避免同名冲突）、`full_content_list.json` / `full_content_list_v2.json`（`page_idx` 按分片偏移量校正为原文档绝对页码）、`images/`。跨分片边界处插入 `<!-- 拼接点 -->` 注释标记，提示该处的表格/段落可能被物理截断（已知局限，不自动修复）。
- **`--extra-formats` 降级**：触发拆分的文件本次调用会强制仅输出 md+json，不支持 docx/html/latex（避免合并二进制格式或拼接质量存疑的输出）。控制台会打印一次性 warning，`summary.json` 对应条目标注 `extra_formats_degraded: true`。
- **依赖**：PDF 拆分需要 `pypdf`（`pip install pypdf`，仅在真正触发拆分时才会被 import，不影响不含超限文档的常规调用）；Word 转换需要 LibreOffice 的 `soffice` 在 PATH 中。

## 输出结构

```text
<output-dir>/
├── summary.json                    # 批次总结：batch_id、每文件状态（含 auto_split 等新字段）
└── <文件名>/
    ├── full.md                     # Markdown 主结果（始终生成；触发拆分时为合并结果）
    ├── full_content_list.json      # 触发拆分时：合并后的结构化内容列表（page_idx 已校正）
    ├── full_content_list_v2.json
    ├── *_content_list.json         # 未拆分时：单次解析的结构化内容列表
    ├── *_content_list_v2.json
    ├── *_model.json                # 模型推理中间结果（未拆分场景）
    ├── layout.json                 # 版面信息（未拆分场景）
    ├── *_origin.pdf / full_origin.pdf  # 解析使用的 PDF 副本（触发拆分时为原始文件归档）
    ├── images/                     # 抽取的图片（触发拆分时已按分片重命名合并）
    ├── _chunks/part01/, part02/... # 仅触发拆分时：各分片的原始解析产物归档
    ├── full.docx                   # 仅 --extra-formats docx（且未触发拆分）
    ├── full.html                   # 仅 --extra-formats html（且未触发拆分）
    └── full.tex                    # 仅 --extra-formats latex（且未触发拆分）
```

## 注意事项

- Windows 下系统环境变量设置后需重启终端才生效。
- Windows curl 的 schannel 后端上传完毕时可能输出 "server closed abruptly"（exit 56），脚本已自动识别为成功。
- HTML 源文件不支持 `extra_formats`，且必须使用 `--model-version MinerU-HTML`。
- 脚本仅走"本地文件 → OSS 预签名 URL"路径，不使用 URL 提交模式（境外 URL 会超时）。
- 触发自动拆分的文档，跨分片边界的表格/段落可能被物理截断，脚本只插入注释标记供人工核对，不自动修复。
- 自动拆分仅覆盖 PDF 与 Word（.doc/.docx）；图片/PPT/XLS/HTML 的超限仍需人工处理。

## 🚫 不要做

| 反模式 | 为什么不行 | 正确做法 |
| -------- | ----------- | --------- |
| 对 HTML 文件使用 `--extra-formats` | API 不支持，请求会失败 | HTML 只用默认 md+json 输出 |
| 对 HTML 文件使用默认 `--model-version vlm` | HTML 需要专用解析模式 | 必须用 `--model-version MinerU-HTML` |
| 用 `--token-env MINERU_TOKEN_1` 但未设置该环境变量 | 脚本直接报错退出 | 先用 `export` 或 `SetEnvironmentVariable` 设置 |
| 批量超过 50 个文件 | API 限制，脚本拒绝执行 | 分多批处理，每批 ≤ 50 |
| 提交 GitHub/AWS 等境外 URL | 网络限制导致超时 | 先下载到本地，再提交本地文件 |
| 未设任何 Token 环境变量就调用脚本 | 脚本报错 `none of these env vars are set` | 先在 mineru.net 申请 Token 并设置环境变量 |
| 对超限文档用 `--page-ranges` 试图分批绕过限制 | `page_ranges` 校验的是原文档页码，无法绕过物理文件层面的 200 页/200MB 限制，且与自动拆分互斥 | 让脚本自动拆分整份文档（默认行为），或自行裁剪出目标页码范围为独立文件后单独提交 |
| 对触发了自动拆分的文件强行要求完整 `--extra-formats`（docx/html/latex） | 拆分场景下会被自动降级为仅 md+json，指定了也不会生效 | 需要完整格式的话，先手动把文档裁剪到限制内再单独提交 |

## 失败排查

遇到错误时按以下步骤处理：

### Token 相关

| 触发条件 | 一线修复 | 仍失败兜底 |
| ---------- | --------- | ----------- |
| 错误码 `A0202` 或 `A0211` | 检查 Token 是否复制完整，重新设置环境变量 | 到 [mineru.net/apiManage](https://mineru.net/apiManage) 重新申请 Token |
| `none of these env vars are set` | 确认环境变量已设置且当前 shell 可读取（`echo $MINERU_TOKEN`） | Windows 用户重启终端；Linux/macOS 执行 `source ~/.bashrc` |
| 主 Token 创建批次失败 | 自动切换到 `MINERU_TOKEN_1`（已设置时） | 手动指定 `--token-env CUSTOM_TOKEN_NAME` |

### 文件相关

| 触发条件 | 一线修复 | 仍失败兜底 |
| ---------- | --------- | ----------- |
| 错误码 `-60002` | 确认文件扩展名在支持列表中 | 将文件转为 PDF 后重试 |
| 错误码 `-60005`（文件超过 200MB） | 默认自动拆分已处理该场景，无需手动干预；若看到该错误码说明自动拆分未生效（如加了 `--no-auto-split`） | 去掉 `--no-auto-split`；仍失败则确认 `pypdf` 已安装 |
| 错误码 `-60006`（文件超过 200 页） | 默认自动拆分已处理该场景，无需手动干预；`--page-ranges` **无法**绕过此限制（校验的是物理文件页数，不是 page_ranges 跨度） | 去掉 `--no-auto-split`（若加了的话）；仍失败则确认 `pypdf` 已安装 |

### 超限拆分/合并相关

| 触发条件 | 一线修复 | 仍失败兜底 |
| ---------- | --------- | ----------- |
| 检测到需要拆分但未安装 `pypdf` | 提示运行 `pip install pypdf` 后重试 | 加 `--no-auto-split`，手动用 `--page-ranges` 分段处理限制内的部分 |
| 需要转换 Word 文档但 `soffice` 不在 PATH | 按平台安装 LibreOffice：Windows `winget install --id LibreOffice.LibreOffice -e`；macOS `brew install --cask libreoffice`；Linux `apt/dnf install libreoffice`，装完重启终端 | 手动把该 docx 转换为 PDF 后单独提交 |
| LibreOffice 转换超时（600秒）或转换后产物缺失 | 视为转换失败并报错，无需人工判断 | 手动用 LibreOffice/Word 打开该文件确认是否损坏，或手动转换为 PDF 后单独提交 |
| PDF 加密/损坏，无法读取页数或拆分 | 脚本自动尝试空密码解密；仍失败提示先移除密码保护 | 用其他工具解密/修复后重试 |
| 分片递归拆分 4 层后单页体积仍超 200MB（极端高分辨率扫描件） | 该分片单独失败报错，不影响其他分片 | 用 PDF 工具压缩该页内容后单独处理 |
| 拆分/补救后提交单元总数超过 50 | 减少本次一起提交的原始文件数量，分批调用 | — |
| `summary.json` 中出现 `state=partial` | 读取 `missing_parts` 字段，告知用户哪些页码范围内容缺失 | 用 `--page-ranges` 单独重新提交缺失范围，手动拼接进已生成的 `full.md` |

### 环境相关

| 触发条件 | 一线修复 | 仍失败兜底 |
| ---------- | --------- | ----------- |
| `curl not found` | 安装 curl：`apt install curl` / `brew install curl` / [curl.se](https://curl.se) | 确认 curl 在 PATH 中：`curl --version` |
| Windows schannel exit 56 | 无需处理，脚本已自动识别为上传成功 | — |
| `state=failed`（不含上述错误码） | 查看 `<output-dir>/summary.json` 中对应条目的 `err_msg` | 检查文件是否损坏、是否加密、是否为纯图片扫描件 |
| 轮询超时（30 分钟未完成） | 检查网络连接，确认可访问 `mineru.net` | 重新提交，`--page-ranges` 缩小范围（未触发自动拆分的场景） |

## 参考资源

| 资源 | 位置 |
| ------ | ------ |
| 核心脚本 | `scripts/mineru_parse.py`（本 skill 目录下） |
| 拆分/合并模块 | `scripts/doc_splitter.py`（本 skill 目录下） |
| MinerU 官网 | https://mineru.net |
| API Token 申请 | https://mineru.net/apiManage |
| API 文档 | https://mineru.net/docs/api |
| 详细移植手册 | 本仓库 `mineru移植与使用说明.md` |
