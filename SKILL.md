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
3. 限制：单文件 ≤ 200 MB 且 ≤ 200 页；单次批量 ≤ 50 个文件。

## 工作流程

脚本位置：`scripts/mineru_parse.py`（本 skill 目录下的 scripts/ 子目录，由当前 runtime 解析路径）。

### Step 1: 收集输入文件

```
输入：用户指定的文件或目录路径
输出：文件列表（自动过滤支持的类型）
```

- 支持类型：`.pdf .doc .docx .ppt .pptx .xls .xlsx .png .jpg .jpeg .jp2 .webp .gif .bmp .html`
- 目录输入时自动收集目录下所有支持类型的文件
- 数量超过 50 个时拒绝并提示分批

### Step 2: 确认参数

```
输入：用户需求（语言/OCR/公式/表格/输出格式/页码范围）
输出：完整的命令行参数
```

按用户需求组合参数，默认值见下方参数速查表。用户未明确指定时使用默认值。

### Step 3: 调用脚本

```bash
python scripts/mineru_parse.py <文件或目录> [参数...]
```

脚本内部执行四阶段：
1. `POST /api/v4/file-urls/batch` — 创建批次，获取 OSS 预签名上传 URL
2. `PUT` (curl) — 逐个上传文件到 OSS
3. `GET /api/v4/extract-results/batch/{batch_id}` — 轮询直到全部 `done` 或 `failed`
4. 下载 `full_zip_url` 并解压到输出目录

### Step 4: 处理结果

```
输入：<output-dir>/<文件名>/full.md
输出：向用户展示解析后的 Markdown 内容
```

读取 `full.md` 返回给用户。如有 `state=failed` 的文件，查看 `summary.json` 中的 `err_msg`。

## 参数速查

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--output-dir DIR` | `mineru_output` | 输出根目录，每个文件建一个子目录 |
| `--extra-formats {docx,html,latex} ...` | 空（仅 md+json） | 额外导出格式 |
| `--language ch\|en\|...` | `ch` | 文档主要语言 |
| `--no-ocr` | OCR 启用 | 关闭 OCR |
| `--no-formula` | 公式启用 | 关闭公式识别 |
| `--no-table` | 表格启用 | 关闭表格识别 |
| `--page-ranges "1-10,20"` | 全部 | 仅解析指定页码 |
| `--model-version vlm\|pipeline\|MinerU-HTML` | `vlm` | HTML 文件须改为 `MinerU-HTML` |
| `--token-env NAME`（可重复） | `MINERU_TOKEN` → `MINERU_TOKEN_1` | 指定 Token 环境变量，按顺序尝试 |

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
|------|------|
| 默认（主 → 备用自动回退） | 无需额外参数 |
| 强制只用备用 Token | `--token-env MINERU_TOKEN_1` |
| 自定义多 Token 链 | `--token-env TOKEN_A --token-env TOKEN_B` |

回退仅在 `POST /file-urls/batch` 阶段触发（配额耗尽 / 鉴权失败）。批次创建成功后，后续步骤使用同一 Token。

## 输出结构

```
<output-dir>/
├── summary.json                    # 批次总结：batch_id、每文件状态
└── <文件名>/
    ├── full.md                     # Markdown 主结果（始终生成）
    ├── *_content_list.json         # 结构化内容列表
    ├── *_content_list_v2.json
    ├── *_model.json                # 模型推理中间结果
    ├── layout.json                 # 版面信息
    ├── *_origin.pdf                # 解析使用的 PDF 副本
    ├── images/                     # 抽取的图片
    ├── full.docx                   # 仅 --extra-formats docx
    ├── full.html                   # 仅 --extra-formats html
    └── full.tex                    # 仅 --extra-formats latex
```

## 注意事项

- Windows 下系统环境变量设置后需重启终端才生效。
- Windows curl 的 schannel 后端上传完毕时可能输出 "server closed abruptly"（exit 56），脚本已自动识别为成功。
- HTML 源文件不支持 `extra_formats`，且必须使用 `--model-version MinerU-HTML`。
- 脚本仅走"本地文件 → OSS 预签名 URL"路径，不使用 URL 提交模式（境外 URL 会超时）。

## 🚫 不要做

| 反模式 | 为什么不行 | 正确做法 |
|--------|-----------|---------|
| 对 HTML 文件使用 `--extra-formats` | API 不支持，请求会失败 | HTML 只用默认 md+json 输出 |
| 对 HTML 文件使用默认 `--model-version vlm` | HTML 需要专用解析模式 | 必须用 `--model-version MinerU-HTML` |
| 用 `--token-env MINERU_TOKEN_1` 但未设置该环境变量 | 脚本直接报错退出 | 先用 `export` 或 `SetEnvironmentVariable` 设置 |
| 批量超过 50 个文件 | API 限制，脚本拒绝执行 | 分多批处理，每批 ≤ 50 |
| 提交 GitHub/AWS 等境外 URL | 网络限制导致超时 | 先下载到本地，再提交本地文件 |
| 未设任何 Token 环境变量就调用脚本 | 脚本报错 `none of these env vars are set` | 先在 mineru.net 申请 Token 并设置环境变量 |

## 失败排查

遇到错误时按以下步骤处理：

### Token 相关

| 触发条件 | 一线修复 | 仍失败兜底 |
|----------|---------|-----------|
| 错误码 `A0202` 或 `A0211` | 检查 Token 是否复制完整，重新设置环境变量 | 到 [mineru.net/apiManage](https://mineru.net/apiManage) 重新申请 Token |
| `none of these env vars are set` | 确认环境变量已设置且当前 shell 可读取（`echo $MINERU_TOKEN`） | Windows 用户重启终端；Linux/macOS 执行 `source ~/.bashrc` |
| 主 Token 创建批次失败 | 自动切换到 `MINERU_TOKEN_1`（已设置时） | 手动指定 `--token-env CUSTOM_TOKEN_NAME` |

### 文件相关

| 触发条件 | 一线修复 | 仍失败兜底 |
|----------|---------|-----------|
| 错误码 `-60002` | 确认文件扩展名在支持列表中 | 将文件转为 PDF 后重试 |
| 错误码 `-60005` | 文件超过 200 MB，压缩或拆分 | 降低分辨率（图片）或提取部分页面 |
| 错误码 `-60006` | 用 `--page-ranges "1-100"` 分批处理前半 | 继续 `--page-ranges "101-200"` 处理后半 |

### 环境相关

| 触发条件 | 一线修复 | 仍失败兜底 |
|----------|---------|-----------|
| `curl not found` | 安装 curl：`apt install curl` / `brew install curl` / [curl.se](https://curl.se) | 确认 curl 在 PATH 中：`curl --version` |
| Windows schannel exit 56 | 无需处理，脚本已自动识别为上传成功 | — |
| `state=failed`（不含上述错误码） | 查看 `<output-dir>/summary.json` 中对应条目的 `err_msg` | 检查文件是否损坏、是否加密、是否为纯图片扫描件 |
| 轮询超时（30 分钟未完成） | 检查网络连接，确认可访问 `mineru.net` | 重新提交，`--page-ranges` 缩小范围 |
