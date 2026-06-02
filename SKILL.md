---
name: mineru
description: Use this skill to parse PDFs, Office files (doc/docx/ppt/pptx/xls/xlsx), images, or HTML into structured Markdown / JSON / DOCX / HTML / LaTeX via the MinerU 精准解析（VLM）API. Trigger when the user asks to "parse / 解析 / 转换 / extract" a document with MinerU, mentions MinerU, supplies a PDF and asks for high-quality conversion to markdown/docx/html/latex, or wants to OCR scanned documents and tables/formulas with high fidelity. Supports both single-file and batch (directory or multiple files) processing.
---

# MinerU 精准解析 Skill

通过 MinerU 公网 API 的"精准解析（model_version=vlm）"模式，将 PDF/Office/图片/HTML 转换为高质量 Markdown 与可选的 DOCX / HTML / LaTeX。

## 前置条件

1. 至少设置一个 MinerU API Token 环境变量（Bearer Token，可在 https://mineru.net/apiManage 申请）：
   - `MINERU_TOKEN` —— 主 Token（首选）
   - `MINERU_TOKEN_1` —— 备份 Token（可选）；当主 Token 创建批次失败（例如每日配额耗尽 / 鉴权失败）时自动切换到备份。
2. 系统中存在 `python`（标准库可用即可）与 `curl`。脚本仅依赖 Python 标准库，不需要 `requests`。
3. 单文件 ≤ 200 MB 且 ≤ 200 页；单次批量 ≤ 50 个文件；支持的输入：`pdf / doc / docx / ppt / pptx / xls / xlsx / png / jpg / jpeg / jp2 / webp / gif / bmp / html`。

## 用法

脚本位置（跨平台）：`scripts/mineru_parse.py`（即 `~/.claude/skills/mineru/scripts/mineru_parse.py`）。

### 单文件
```bash
python ~/.claude/skills/mineru/scripts/mineru_parse.py path/to/file.pdf --output-dir ./mineru_output
```

### 批量：目录（自动收集目录下所有支持类型的文件）
```bash
python ~/.claude/skills/mineru/scripts/mineru_parse.py path/to/dir/ --output-dir ./mineru_output
```

### 批量：多个文件
```bash
python ~/.claude/skills/mineru/scripts/mineru_parse.py a.pdf b.docx c.png --output-dir ./out
```

### 指定额外输出格式（除默认 Markdown + JSON 之外）
- `docx`、`html`、`latex` 可任意组合，**必须显式声明**。
```bash
python ~/.claude/skills/mineru/scripts/mineru_parse.py file.pdf --extra-formats docx html latex
```

### 其他常用参数
| 参数 | 默认 | 说明 |
|------|------|------|
| `--output-dir DIR` | `mineru_output` | 解析结果根目录；每个输入文件会建一个以文件名（不含后缀）命名的子目录。|
| `--extra-formats {docx,html,latex} ...` | 空（仅 md + json） | 额外导出格式。|
| `--language ch\|en\|...` | `ch` | 文档主要语言。|
| `--no-ocr` | OCR 启用 | 关闭 OCR（默认开启）。|
| `--no-formula` | 公式启用 | 关闭公式识别。|
| `--no-table` | 表格启用 | 关闭表格识别。|
| `--page-ranges "1-10,20"` | 全部 | 仅解析指定页码。|
| `--model-version vlm\|pipeline\|MinerU-HTML` | `vlm` | **默认 vlm = 精准解析**；解析 HTML 文件时须改为 `MinerU-HTML`。|
| `--token-env NAME` (可重复) | `MINERU_TOKEN` 然后 `MINERU_TOKEN_1` | 指定从哪些环境变量读取 Token，按顺序尝试；首个能成功创建批次的就被采用。|

### Token 回退示例
默认行为已经是先 `MINERU_TOKEN` 再 `MINERU_TOKEN_1`，无需额外参数。需要自定义顺序时：
```bash
# 强制只用备份 Token
python ~/.claude/skills/mineru/scripts/mineru_parse.py file.pdf --token-env MINERU_TOKEN_1

# 自定义多 Token 链式回退
python ~/.claude/skills/mineru/scripts/mineru_parse.py file.pdf \
    --token-env MINERU_TOKEN_A --token-env MINERU_TOKEN_B
```

## 输出结构

解析完成后，`<output-dir>/<file_stem>/` 下包含：
- `full.md` — Markdown 主结果（始终生成）
- `*_content_list.json` / `*_content_list_v2.json` — 结构化内容列表
- `*_model.json` — 模型推理中间结果
- `layout.json` — 版面信息
- `*_origin.pdf` — 解析时使用的 PDF 副本
- `images/` — 抽取的图片
- `full.docx` / `full.html` / `full.tex`（若在 `--extra-formats` 中声明）

`<output-dir>/summary.json` 记录本批 `batch_id` 与每个文件的状态、错误信息和输出目录。

## 工作流程（脚本内部）

1. `POST https://mineru.net/api/v4/file-urls/batch` —— 顶层带 `model_version="vlm"`、`extra_formats`、`enable_formula`、`enable_table`、`language`，files[*] 仅含 `name / is_ocr / page_ranges`。
2. 对每个返回的 OSS 预签名 URL 执行 `PUT`（curl，HTTP/1.1，置空 Content-Type）；上传成功后服务端自动开始解析。
3. 轮询 `GET https://mineru.net/api/v4/extract-results/batch/{batch_id}` 直到所有文件状态为 `done` 或 `failed`。
4. 下载 `full_zip_url` 并解压到 `<output-dir>/<file_stem>/`。

## 注意事项

- 至少有一个 Token 环境变量（`MINERU_TOKEN` 或 `MINERU_TOKEN_1`）必须在调用脚本的 shell 中可见。Windows 下系统环境变量在新开 shell 后才生效。
- Token 回退仅在 `POST /file-urls/batch` 创建批次阶段触发（即"配额耗尽 / 鉴权失败 / 服务端拒绝"等）。一旦批次创建成功，后续上传与轮询使用同一个 Token 进行。
- Windows curl 的 schannel 后端在 OSS 上传完毕时可能输出 "server closed abruptly" 警告（exit code 56），脚本已在 `sent >= size` 时识别为成功并继续。
- HTML 源文件不支持 `extra_formats`，且必须使用 `--model-version MinerU-HTML`。
- MinerU 文档说明 GitHub / AWS 等境外 URL 会因网络限制超时，因此脚本只走"本地文件 → OSS 预签名 URL"路径，不使用 URL 提交模式。

## 失败排查

- `code != 0` 时脚本会原样打印接口返回，常见错误码：
  - `-60002` 文件后缀缺失或不被支持。
  - `-60005` 单文件超过 200MB。
  - `-60006` 文件页数超过 200。
  - `A0202 / A0211` Authorization 头错误。
- 如果 `state=failed`，查看 summary.json 中对应条目的 `err_msg`。
