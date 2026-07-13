"""MinerU 精准解析（vlm）API 客户端：单文件 / 批量本地文件上传。

用法示例：
    python mineru_parse.py path/to/file.pdf
    python mineru_parse.py input-files/ --extra-formats docx html
    python mineru_parse.py a.pdf b.pdf --extra-formats latex --output-dir ./out
    python mineru_parse.py file.pdf --no-ocr --language en

超限文档（>200页 或 >200MB）自动拆分/合并：
    默认开启，对 PDF/.doc/.docx 超限文件本地拆分为多个分片分别提交解析，
    完成后自动合并为完整的 full.md / content_list.json / images。
    PDF 拆分需要 pypdf（`pip install pypdf`）；超限 Word 转换需要 LibreOffice
    （soffice 在 PATH 中）。用 --no-auto-split 恢复到旧行为（超限直接失败）。
    详见 doc_splitter.py 与 SKILL.md「超限文档自动拆分与合并」章节。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import doc_splitter
from doc_splitter import ChunkInfo

API_BASE = "https://mineru.net/api/v4"
SUPPORTED_EXTRA_FORMATS = {"docx", "html", "latex"}
SUPPORTED_INPUT_EXT = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".png", ".jpg", ".jpeg", ".jp2", ".webp", ".gif", ".bmp", ".html",
}
MAX_BATCH = 50
POLL_INTERVAL_SEC = 8
POLL_TIMEOUT_SEC = 60 * 30


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _http_json(method: str, url: str, *, headers: dict, body: bytes | None = None,
               retries: int = 3, timeout: int = 60) -> dict:
    last_err: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                status = resp.status
                break
        except urllib.error.HTTPError as e:
            raw = e.read()
            status = e.code
            break
        except Exception as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    else:
        raise RuntimeError(f"{method} {url} failed after {retries} retries: {last_err!r}")

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise RuntimeError(f"{method} {url} returned non-JSON: HTTP {status}, body={raw[:300]!r}")
    if data.get("code") not in (0, 200):
        raise RuntimeError(f"{method} {url} failed: HTTP {status} body={data}")
    return data


def collect_files(inputs: list[str]) -> list[Path]:
    files: list[Path] = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            for sub in sorted(p.iterdir()):
                if sub.is_file() and sub.suffix.lower() in SUPPORTED_INPUT_EXT:
                    files.append(sub)
        elif p.is_file():
            if p.suffix.lower() not in SUPPORTED_INPUT_EXT:
                raise ValueError(f"unsupported file type: {p}")
            files.append(p)
        else:
            raise FileNotFoundError(item)
    if not files:
        raise ValueError("no input files found")
    if len(files) > MAX_BATCH:
        raise ValueError(f"batch size {len(files)} exceeds MinerU limit {MAX_BATCH}")
    return files


def request_upload_urls(token: str, files: list[Path], *, language: str, is_ocr: bool,
                        enable_formula: bool, enable_table: bool,
                        extra_formats: list[str], model_version: str,
                        page_ranges: str | None) -> tuple[str, list[str]]:
    payload: dict = {
        "enable_formula": enable_formula,
        "enable_table": enable_table,
        "language": language,
        "model_version": model_version,
        "files": [
            {"name": f.name, "is_ocr": is_ocr}
            | ({"page_ranges": page_ranges} if page_ranges else {})
            for f in files
        ],
    }
    if extra_formats:
        payload["extra_formats"] = extra_formats
    body = _http_json("POST", f"{API_BASE}/file-urls/batch",
                      headers=_auth_headers(token),
                      body=json.dumps(payload).encode("utf-8"),
                      timeout=60)
    data = body["data"]
    if len(data.get("file_urls", [])) != len(files):
        raise RuntimeError(f"unexpected file_urls response: {data}")
    return data["batch_id"], data["file_urls"]


def upload_files(file_urls: list[str], files: list[Path]) -> None:
    """Upload files to OSS pre-signed URLs.

    Uses curl with HTTP/1.1 and an empty Content-Type header (the URL was
    signed with empty Content-Type). On Windows the schannel backend may
    report exit code 56 ("server closed abruptly") even after a full
    upload — we treat that as success when curl confirms it sent all bytes.
    File contents are piped via stdin (`--upload-file -`) to avoid issues
    with non-ASCII file paths on Windows curl.
    """
    curl = shutil.which("curl")
    if not curl:
        raise RuntimeError("curl not found on PATH; install curl to enable uploads")
    for url, path in zip(file_urls, files):
        size = path.stat().st_size
        data = path.read_bytes()
        cmd = [
            curl, "-sS", "--http1.1", "-H", "Content-Type:",
            "-o", os.devnull,
            "-w", "%{http_code} %{size_upload}",
            "-X", "PUT", "--upload-file", "-", url,
        ]
        last_err = ""
        for attempt in range(3):
            proc = subprocess.run(cmd, input=data, capture_output=True, timeout=900)
            out = (proc.stdout or b"").decode("ascii", "replace").strip().split()
            http_code = int(out[0]) if out and out[0].isdigit() else 0
            sent = int(out[1]) if len(out) > 1 and out[1].isdigit() else 0
            if http_code in (200, 201):
                print(f"  uploaded: {path.name} ({size} bytes)")
                break
            if proc.returncode == 56 and sent >= size:
                print(f"  uploaded: {path.name} ({size} bytes) [schannel close warning ignored]")
                break
            last_err = (
                f"rc={proc.returncode} http={http_code} sent={sent}/{size} "
                f"stderr={proc.stderr.decode('utf-8','replace').strip()[:300]!r}"
            )
            print(f"  retry {attempt + 1}/3 for {path.name}: {last_err}")
            time.sleep(3 * (attempt + 1))
        else:
            raise RuntimeError(f"upload {path.name} failed after retries: {last_err}")


def poll_batch(token: str, batch_id: str) -> list[dict]:
    deadline = time.time() + POLL_TIMEOUT_SEC
    last_status = ""
    while time.time() < deadline:
        body = _http_json("GET", f"{API_BASE}/extract-results/batch/{batch_id}",
                          headers={"Authorization": f"Bearer {token}"}, timeout=60)
        results = body["data"]["extract_result"]
        states = [r.get("state") for r in results]
        status = ", ".join(f"{r.get('file_name')}={r.get('state')}" for r in results)
        if status != last_status:
            print(f"  status: {status}")
            last_status = status
        if all(s in ("done", "failed") for s in states):
            return results
        time.sleep(POLL_INTERVAL_SEC)
    raise TimeoutError(f"batch {batch_id} did not finish within {POLL_TIMEOUT_SEC}s")


def download_and_extract(zip_url: str, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(zip_url, timeout=600) as resp:
        content = resp.read()
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        zf.extractall(dest_dir)


# ---------------------------------------------------------------------------
# 超限文档自动拆分/合并 —— 编排层
#
# 以下函数负责把"用户原始输入文件"展开为"实际提交给 MinerU 的物理文件
# 列表"，并在解析完成后把多个分片的结果合并回一份完整文档。核心的文件级
# 处理（页数/体积探测、PDF 拆分、docx 转换、结果合并）都委托给
# doc_splitter 模块；本文件只负责编排与结果落盘，尽量复用上面已有的
# request_upload_urls / upload_files / poll_batch / download_and_extract。
# ---------------------------------------------------------------------------

def expand_for_split(
    files: list[Path], *, max_pages: int, work_dir: Path,
) -> tuple[list[ChunkInfo], list[ChunkInfo]]:
    """把原始输入文件展开为 (normal_chunks, presplit_chunks)。

    normal_chunks: 按原样直接提交（可能后续因超限失败，docx 会走反应式补救）。
    presplit_chunks: 提交前已在本地完成拆分的分片（PDF 主动拆分，或体积超限
    的 docx 提前转换+拆分产出）。
    """
    normal_chunks: list[ChunkInfo] = []
    presplit_chunks: list[ChunkInfo] = []
    for f in files:
        suffix = f.suffix.lower()
        if suffix == ".pdf":
            over, reason = doc_splitter.needs_split(f, max_pages)
            if over:
                print(f"  {f.name}: 超限（{reason}），本地拆分中 ...")
                chunks = doc_splitter.split_pdf_by_limits(f, work_dir / f.stem, max_pages=max_pages)
                print(f"    -> {len(chunks)} 个分片")
                presplit_chunks.extend(chunks)
            else:
                # 未超限，直通提交；end_page 对未拆分单元不参与任何合并逻辑，
                # 固定填 0 即可（仅 is_split=True 的分片才会用到 start/end_page）。
                normal_chunks.append(ChunkInfo(
                    original_path=f, chunk_path=f, part_index=1, part_count=1,
                    start_page=1, end_page=0, is_split=False, split_reason=None,
                ))
        elif suffix in (".doc", ".docx"):
            # 体积可本地零成本探测；页数需要渲染后才知道，无法提前判断，
            # 这里只检查体积，页数留给提交后的反应式补救处理。
            size_over = f.stat().st_size > doc_splitter.MINERU_MAX_SIZE_BYTES
            if size_over:
                print(f"  {f.name}: 体积超限，转换为 PDF 后本地拆分中 ...")
                pdf_path = doc_splitter.convert_office_to_pdf(f, work_dir / f.stem)
                chunks = doc_splitter.split_pdf_by_limits(pdf_path, work_dir / f.stem, max_pages=max_pages)
                for c in chunks:
                    c.original_path = f  # 合并阶段仍按最初的 .docx 分组
                print(f"    -> {len(chunks)} 个分片")
                presplit_chunks.extend(chunks)
            else:
                normal_chunks.append(ChunkInfo(
                    original_path=f, chunk_path=f, part_index=1, part_count=1,
                    start_page=1, end_page=0, is_split=False, split_reason=None,
                ))
        else:
            normal_chunks.append(ChunkInfo(
                original_path=f, chunk_path=f, part_index=1, part_count=1,
                start_page=1, end_page=0, is_split=False, split_reason=None,
            ))
    return normal_chunks, presplit_chunks


def submit_chunks(
    candidates: list[tuple[str, str]], chunks: list[ChunkInfo], *,
    language: str, is_ocr: bool, enable_formula: bool, enable_table: bool,
    extra_formats: list[str], model_version: str, page_ranges: str | None,
) -> list[tuple[ChunkInfo, dict]]:
    """提交一组 chunk（一次 POST /file-urls/batch + 上传 + 轮询），
    走完整的 token 回退链。返回 [(ChunkInfo, api_result_dict), ...]，
    顺序与 chunks 一致。原样复用 request_upload_urls/upload_files/poll_batch，
    不重复造轮子。"""
    if not chunks:
        return []
    chunk_paths = [c.chunk_path for c in chunks]
    token: str | None = None
    batch_id: str | None = None
    file_urls: list[str] = []
    last_err: Exception | None = None
    for name, candidate in candidates:
        print(f"  trying token from ${name} ...")
        try:
            batch_id, file_urls = request_upload_urls(
                candidate, chunk_paths, language=language, is_ocr=is_ocr,
                enable_formula=enable_formula, enable_table=enable_table,
                extra_formats=extra_formats, model_version=model_version,
                page_ranges=page_ranges,
            )
            token = candidate
            print(f"  using ${name}; batch_id={batch_id}")
            break
        except Exception as e:
            last_err = e
            print(f"  ${name} failed: {e}")
    if token is None or batch_id is None:
        raise RuntimeError(f"all tokens failed; last error: {last_err}")

    upload_files(file_urls, chunk_paths)
    results = poll_batch(token, batch_id)
    return list(zip(chunks, results))


def reactive_retry(
    failed_items: list[tuple[ChunkInfo, dict]], *, work_dir: Path, max_pages: int,
) -> list[ChunkInfo]:
    """对疑似"页数或体积超限"失败的 .doc/.docx 原始提交做补救：转换为 PDF
    后按限制拆分，产出新的 ChunkInfo 供重新提交。只处理 docx，只尝试一次。
    单个文件的补救失败不影响其他文件，异常信息会追加进该文件的 err_msg
    （由调用方处理，本函数只负责产出能补救成功的部分）。"""
    retry_chunks: list[ChunkInfo] = []
    for chunk, result in failed_items:
        if chunk.original_path.suffix.lower() not in (".doc", ".docx"):
            continue
        if not doc_splitter.is_limit_error(result.get("err_msg")):
            continue
        try:
            print(f"  {chunk.original_path.name}: 疑似超限失败，转换为 PDF 后拆分重试 ...")
            pdf_path = doc_splitter.convert_office_to_pdf(
                chunk.original_path, work_dir / chunk.original_path.stem)
            new_chunks = doc_splitter.split_pdf_by_limits(
                pdf_path, work_dir / chunk.original_path.stem, max_pages=max_pages)
            for c in new_chunks:
                c.original_path = chunk.original_path
            print(f"    -> {len(new_chunks)} 个分片，重新提交")
            retry_chunks.extend(new_chunks)
        except (doc_splitter.MissingDependencyError, doc_splitter.OfficeConversionError,
                doc_splitter.UnsplittableFileError) as e:
            result["err_msg"] = f"{result.get('err_msg')} | 反应式补救失败: {e}"
            print(f"    补救失败: {e}")
    return retry_chunks


def merge_original_file(
    original: Path, chunks_and_results: list[tuple[ChunkInfo, dict]], *, output_dir: Path,
    extra_formats_degraded: bool, requested_extra_formats: list[str],
) -> dict:
    """把属于同一个原始文件的所有分片结果合并/落盘，产出 summary.json 的
    对应条目。chunks_and_results 需已按 part_index 排序。"""
    chunks_and_results = sorted(chunks_and_results, key=lambda pr: pr[0].part_index)
    target = output_dir / original.stem

    if len(chunks_and_results) == 1 and not chunks_and_results[0][0].is_split:
        # legacy 直通路径：与改造前完全一致的行为
        chunk, r = chunks_and_results[0]
        item = {
            "file": original.name, "state": r.get("state"),
            "full_zip_url": r.get("full_zip_url"), "err_msg": r.get("err_msg"),
            "output_dir": None, "split": False, "part_count": 1,
        }
        if r.get("state") == "done" and r.get("full_zip_url"):
            print(f"  downloading -> {target}")
            download_and_extract(r["full_zip_url"], target)
            item["output_dir"] = str(target)
        else:
            print(f"  skip download for {original.name}: state={r.get('state')} err={r.get('err_msg')}")
        return item

    # 多分片合并路径
    chunk_result_dirs: list[Path] = []
    chunk_infos: list[ChunkInfo] = []
    failed_parts: dict[int, str] = {}
    for chunk, r in chunks_and_results:
        chunk_infos.append(chunk)
        part_dir = target / "_chunks" / f"part{chunk.part_index:02d}"
        if r.get("state") == "done" and r.get("full_zip_url"):
            print(f"  downloading part {chunk.part_index}/{chunk.part_count} -> {part_dir}")
            download_and_extract(r["full_zip_url"], part_dir)
            chunk_result_dirs.append(part_dir)
        else:
            print(f"  part {chunk.part_index}/{chunk.part_count} failed: {r.get('err_msg')}")
            chunk_result_dirs.append(part_dir)  # 目录可能不存在，merge_* 会跳过
            failed_parts[chunk.part_index] = r.get("err_msg") or "unknown error"

    item = {
        "file": original.name, "err_msg": None, "output_dir": str(target),
        "split": True, "part_count": chunks_and_results[0][0].part_count,
        "extra_formats_degraded": extra_formats_degraded,
        "extra_formats_requested": requested_extra_formats,
    }

    if len(failed_parts) == len(chunks_and_results):
        item["state"] = "failed"
        item["err_msg"] = "; ".join(f"part{p}: {m}" for p, m in failed_parts.items())
        item["output_dir"] = None
        return item

    image_rename_maps = doc_splitter.merge_images(chunk_result_dirs, chunk_infos, target / "images")
    doc_splitter.merge_markdown(
        chunk_result_dirs, chunk_infos, image_rename_maps, target / "full.md",
        failed_parts=failed_parts,
    )
    for glob_pat, out_name in (
        ("*_content_list.json", "full_content_list.json"),
        ("*_content_list_v2.json", "full_content_list_v2.json"),
    ):
        try:
            doc_splitter.merge_content_list(
                chunk_result_dirs, chunk_infos, image_rename_maps, target / out_name,
                filename_glob=glob_pat, failed_part_indices=set(failed_parts),
            )
        except RuntimeError as e:
            print(f"  警告：{out_name} 合并失败，跳过（不影响 full.md）：{e}")

    # 归档用户原始输入文件（不是转换出的中间 PDF）供追溯：拆分不改变内容，
    # 原始文件本身就是权威、完整的版本。PDF 归档为 full_origin.pdf（与未拆分
    # 场景的 *_origin.pdf 语义对齐）；docx 等其他类型按原始扩展名归档，
    # 避免"触发拆分的 docx 完全没有原始文件副本"这个空白。
    try:
        origin_ext = original.suffix.lower()
        archive_name = "full_origin.pdf" if origin_ext == ".pdf" else f"full_origin{origin_ext}"
        shutil.copy2(original, target / archive_name)
    except OSError as e:
        print(f"  警告：归档原始文件失败（不影响主结果）：{e}")

    if failed_parts:
        item["state"] = "partial"
        item["missing_parts"] = [
            {"part_index": p, "start_page": next(c.start_page for c in chunk_infos if c.part_index == p),
             "end_page": next(c.end_page for c in chunk_infos if c.part_index == p), "err_msg": m}
            for p, m in sorted(failed_parts.items())
        ]
    else:
        item["state"] = "done"
    return item


def parse(inputs: list[str], *, output_dir: Path, language: str, is_ocr: bool,
          enable_formula: bool, enable_table: bool, extra_formats: list[str],
          model_version: str, page_ranges: str | None,
          token_env_names: list[str], auto_split: bool = True,
          max_pages_per_chunk: int = doc_splitter.MINERU_MAX_PAGES) -> dict:
    candidates: list[tuple[str, str]] = []
    for name in token_env_names:
        val = os.environ.get(name)
        if val:
            candidates.append((name, val))
    if not candidates:
        raise RuntimeError(
            f"none of these env vars are set: {token_env_names}. "
            f"Set MINERU_TOKEN (and optionally MINERU_TOKEN_1 as backup)."
        )

    files = collect_files(inputs)
    print(f"[1/5] collected {len(files)} file(s); model_version={model_version}, "
          f"extra_formats={extra_formats or 'default(md+json)'}, auto_split={auto_split}")

    work_dir = output_dir / ".mineru_split_tmp"

    if auto_split:
        normal_chunks, presplit_chunks = expand_for_split(
            files, max_pages=max_pages_per_chunk, work_dir=work_dir)
    else:
        normal_chunks = [
            ChunkInfo(original_path=f, chunk_path=f, part_index=1, part_count=1,
                      start_page=1, end_page=0, is_split=False, split_reason=None)
            for f in files
        ]
        presplit_chunks = []

    if page_ranges and presplit_chunks:
        raise RuntimeError(
            "--page-ranges 与自动拆分不兼容：检测到有文件超限已被本地拆分。"
            "请二选一：①去掉 --page-ranges 让脚本自动拆分整份文档；"
            "②自行裁剪出所需页码范围为独立 ≤200 页/≤200MB 文件后单独提交。"
        )

    total_units = len(normal_chunks) + len(presplit_chunks)
    if total_units > MAX_BATCH:
        raise RuntimeError(
            f"本次输入拆分后共产生 {total_units} 个提交单元，超过 MinerU 单批次上限 "
            f"{MAX_BATCH}。请减少本次一起提交的原始文件数量，分批调用。"
        )

    print(f"[2/5] 展开为 {len(normal_chunks)} 个常规提交单元 + "
          f"{len(presplit_chunks)} 个预拆分单元 ...")

    paired: list[tuple[ChunkInfo, dict]] = []
    if normal_chunks:
        print(f"  提交路 A（原始 extra_formats={extra_formats or 'default'}）...")
        paired.extend(submit_chunks(
            candidates, normal_chunks, language=language, is_ocr=is_ocr,
            enable_formula=enable_formula, enable_table=enable_table,
            extra_formats=extra_formats, model_version=model_version,
            page_ranges=page_ranges,
        ))
    if presplit_chunks:
        if extra_formats:
            print(f"  [WARN] 以下文件因超限被本地拆分，--extra-formats 自动降级为仅 md+json: "
                  f"{sorted({c.original_path.name for c in presplit_chunks})}")
        print(f"  提交路 B（预拆分单元，extra_formats=[]）...")
        paired.extend(submit_chunks(
            candidates, presplit_chunks, language=language, is_ocr=is_ocr,
            enable_formula=enable_formula, enable_table=enable_table,
            extra_formats=[], model_version=model_version,
            page_ranges=None,
        ))

    print(f"[3/5] 反应式补救（针对疑似超限失败的 .doc/.docx 原始提交）...")
    if auto_split and not page_ranges:
        failed_docx = [
            (c, r) for c, r in paired
            if r.get("state") == "failed" and c.original_path.suffix.lower() in (".doc", ".docx")
        ]
        retry_chunks = reactive_retry(failed_docx, work_dir=work_dir, max_pages=max_pages_per_chunk)
        if retry_chunks:
            retry_originals = {c.original_path for c in retry_chunks}
            paired = [pr for pr in paired if pr[0].original_path not in retry_originals]
            print(f"  提交路 C（反应式补救分片，extra_formats=[]）...")
            paired.extend(submit_chunks(
                candidates, retry_chunks, language=language, is_ocr=is_ocr,
                enable_formula=enable_formula, enable_table=enable_table,
                extra_formats=[], model_version=model_version, page_ranges=None,
            ))
    else:
        print("  跳过（未开启自动拆分，或指定了 --page-ranges）")

    print(f"[4/5] 按原始文件分组合并结果 ...")
    output_dir.mkdir(parents=True, exist_ok=True)
    grouped: dict[Path, list[tuple[ChunkInfo, dict]]] = {}
    for pr in paired:
        grouped.setdefault(pr[0].original_path, []).append(pr)

    summary = {"auto_split": auto_split, "max_pages_per_chunk": max_pages_per_chunk, "results": []}
    for f in files:
        group = grouped.get(f, [])
        if not group:
            summary["results"].append({
                "file": f.name, "state": "failed", "err_msg": "no submission result (internal error)",
                "output_dir": None, "split": False, "part_count": 1,
            })
            continue
        was_degraded = any(c.is_split for c, _ in group) and bool(extra_formats)
        summary["results"].append(merge_original_file(
            f, group, output_dir=output_dir,
            extra_formats_degraded=was_degraded, requested_extra_formats=extra_formats,
        ))

    print(f"[5/5] 写 summary.json 并清理临时文件 ...")
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if work_dir.exists():
        try:
            shutil.rmtree(work_dir)
        except OSError as e:
            print(f"  [WARN] 临时文件清理失败（不影响结果）: {e}；可手动删除 {work_dir}")
    print(f"\nDONE. summary -> {summary_path}")
    return summary


def main() -> int:
    # Windows 中文控制台默认用 GBK 编码，遇到 GBK 无法表示的字符（如某些
    # 特殊符号、MinerU 返回的 err_msg 里可能出现的字符）会导致 print()
    # 直接抛 UnicodeEncodeError 崩溃。这里做防御性 reconfigure：无法编码的
    # 字符替换为 '?' 而不是让整个脚本崩溃（仅在 stdout 支持 reconfigure 时生效，
    # 如被重定向到文件等场景不受影响）。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="replace")

    ap = argparse.ArgumentParser(description="MinerU precise (vlm) parsing client")
    ap.add_argument("inputs", nargs="+", help="file(s) or directory")
    ap.add_argument("--output-dir", default="mineru_output", help="local output directory")
    ap.add_argument("--extra-formats", nargs="*", default=[],
                    choices=sorted(SUPPORTED_EXTRA_FORMATS),
                    help="extra export formats; default exports markdown + json only")
    ap.add_argument("--language", default="ch", help='document language, e.g. "ch", "en"')
    ap.add_argument("--no-ocr", action="store_true", help="disable OCR (default: enabled)")
    ap.add_argument("--no-formula", action="store_true", help="disable formula recognition")
    ap.add_argument("--no-table", action="store_true", help="disable table recognition")
    ap.add_argument("--page-ranges", default=None, help='page ranges, e.g. "1-10,20"')
    ap.add_argument("--model-version", default="vlm",
                    choices=["vlm", "pipeline", "MinerU-HTML"],
                    help='default "vlm" = precise parsing')
    ap.add_argument("--token-env", action="append", default=None,
                    metavar="NAME",
                    help="env var name(s) holding MinerU tokens, tried in order. "
                         "Default: MINERU_TOKEN then MINERU_TOKEN_1. "
                         "Pass multiple times to chain custom names.")
    ap.add_argument("--no-auto-split", action="store_true",
                    help="disable automatic split/merge for oversized documents "
                         "(>200 pages or >200MB); restores legacy behavior of "
                         "failing with -60005/-60006 on such files")
    ap.add_argument("--max-pages-per-chunk", type=int, default=doc_splitter.MINERU_MAX_PAGES,
                    help="max pages per split chunk (1-200, default 200); mainly for "
                         "testing the split/merge logic with small sample files")
    args = ap.parse_args()

    if not (1 <= args.max_pages_per_chunk <= doc_splitter.MINERU_MAX_PAGES):
        print(f"ERROR: --max-pages-per-chunk must be between 1 and "
              f"{doc_splitter.MINERU_MAX_PAGES}", file=sys.stderr)
        return 1

    token_env_names = args.token_env or ["MINERU_TOKEN", "MINERU_TOKEN_1"]

    try:
        parse(
            args.inputs,
            output_dir=Path(args.output_dir),
            language=args.language,
            is_ocr=not args.no_ocr,
            enable_formula=not args.no_formula,
            enable_table=not args.no_table,
            extra_formats=list(args.extra_formats),
            model_version=args.model_version,
            page_ranges=args.page_ranges,
            token_env_names=token_env_names,
            auto_split=not args.no_auto_split,
            max_pages_per_chunk=args.max_pages_per_chunk,
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
