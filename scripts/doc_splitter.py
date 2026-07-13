"""超限文档（PDF/Word）本地拆分、转换与合并工具模块。

服务于 mineru_parse.py 的"超长/超大文档自动拆分"功能：MinerU API 单文件限制为
页数 <=200 页、体积 <=200MB（错误码分别为 -60006 / -60005）。本模块提供：

  - PDF 页数/体积探测与按限制切分（split_pdf_by_limits）
  - Word(.doc/.docx) 转 PDF（仅 LibreOffice headless，跨平台一致）
  - 多分片解析结果的合并（markdown / images / content_list.json）

设计要点：
  - 第三方依赖（pypdf）一律在函数体内惰性 import，不在模块顶层引入，
    保证不触发拆分的调用路径完全不需要安装 pypdf。
  - 本模块不涉及任何 HTTP/API 调用，只做本地文件处理，便于独立测试。

用法示例：
    from doc_splitter import split_pdf_by_limits, convert_office_to_pdf
    chunks = split_pdf_by_limits(Path("big.pdf"), Path("./tmp"), max_pages=200)
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

MINERU_MAX_PAGES = 200
MINERU_MAX_SIZE_BYTES = 200 * 1024 * 1024  # 200MB：MinerU 硬限制，用于判定是否需要拆分
# 拆分时按更小的目标体积留安全余量，减少"分片写出后仍踩线超限"需要二次拆分的概率
SPLIT_TARGET_SIZE_BYTES = 190 * 1024 * 1024

MAX_RECURSIVE_SPLIT_DEPTH = 4  # 单个分片体积仍超限时，递归二分重切的最大层数


class MissingDependencyError(RuntimeError):
    """需要的第三方依赖（pypdf / soffice）未安装或不可用。"""


class UnsplittableFileError(RuntimeError):
    """文件无法被安全拆分（加密且无法解密、已损坏，或单页体积本身已超限）。"""


class OfficeConversionError(RuntimeError):
    """Word/Office 文档转 PDF 失败（soffice 不可用、转换超时或转换后产物缺失）。"""


@dataclass
class ChunkInfo:
    """一个"实际要提交给 MinerU"的物理文件单元。"""

    original_path: Path  # 原始输入文件（用户指定的那份，未拆分前）
    chunk_path: Path      # 实际提交的物理文件（未拆分时等于 original_path 或转换后的PDF）
    part_index: int       # 1-based；未拆分时恒为 1
    part_count: int       # 该原始文件的总分片数；未拆分时恒为 1
    start_page: int       # 1-based，相对"被拆分的那份物理PDF"
    end_page: int
    is_split: bool = False
    split_reason: str | None = None  # "pages" | "size" | "pages+size" | None，仅信息用途


# ---------------------------------------------------------------------------
# PDF 页数 / 体积探测
# ---------------------------------------------------------------------------

def _import_pypdf():
    try:
        import pypdf  # noqa: PLC0415
    except ImportError as e:
        raise MissingDependencyError(
            "检测到文档超过 MinerU 限制（200页或200MB）需要自动拆分，但未安装 pypdf。"
            "请运行 `pip install pypdf` 后重试，或加 --no-auto-split 后手动用 "
            "--page-ranges 分段处理。"
        ) from e
    return pypdf


def get_pdf_page_count(path: Path) -> int:
    """读取 PDF 的物理页数。加密 PDF 会先尝试空密码解密。"""
    pypdf = _import_pypdf()
    try:
        reader = pypdf.PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as e:
                raise UnsplittableFileError(
                    f"{path.name} 已加密且无法用空密码解密，请先移除密码保护后重试: {e}"
                ) from e
        return len(reader.pages)
    except UnsplittableFileError:
        raise
    except Exception as e:
        raise UnsplittableFileError(f"{path.name} 无法读取页数（可能已损坏）: {e}") from e


def needs_split(path: Path, max_pages: int) -> tuple[bool, str | None]:
    """本地零成本判定文件是否需要拆分。

    体积检查对 .pdf / .doc / .docx 通用。页数检查仅对 .pdf 生效——
    .doc/.docx 的渲染后页数本地无法零成本获知，调用方需自行跳过。
    返回 (是否需要拆分, split_reason)。
    """
    size = path.stat().st_size
    size_over = size > MINERU_MAX_SIZE_BYTES
    pages_over = False
    if path.suffix.lower() == ".pdf":
        pages_over = get_pdf_page_count(path) > max_pages

    if pages_over and size_over:
        return True, "pages+size"
    if pages_over:
        return True, "pages"
    if size_over:
        return True, "size"
    return False, None


# ---------------------------------------------------------------------------
# PDF 拆分（核心统一函数：同时满足页数与体积两条限制）
# ---------------------------------------------------------------------------

def _write_pdf_slice(reader, start0: int, end0: int, out_path: Path) -> None:
    """写出 reader 的 [start0, end0) 页（0-based，半开区间）到 out_path。"""
    pypdf = _import_pypdf()
    writer = pypdf.PdfWriter()
    for i in range(start0, end0):
        writer.add_page(reader.pages[i])
    with open(out_path, "wb") as f:
        writer.write(f)


def split_pdf_by_limits(
    src: Path, out_dir: Path, *, max_pages: int = MINERU_MAX_PAGES,
    max_size_bytes: int = SPLIT_TARGET_SIZE_BYTES,
) -> list[ChunkInfo]:
    """按页数与体积双重限制拆分 PDF，保证每个产出分片都满足两条限制。

    不超限时直接返回单元素 list（chunk_path=src，不拷贝，零额外 IO）。
    超限时分两步：
      1) 按"平均单页体积"估算的安全页数切出初步边界；
      2) 对每个初步边界递归二分（写出探测体积，超限则再二分，最多
         MAX_RECURSIVE_SPLIT_DEPTH 层），把所有边界解析为"叶子边界"列表。
    所有叶子边界解析完毕后统一重新编号 part_index=1..N（N=叶子总数）、
    part_count=N，保证编号连续唯一——不会出现两个分片共享同一个
    part_index 的情况（这会导致合并阶段按 part_index 索引图片重命名映射
    表时发生错位或冲突）。
    """
    pypdf = _import_pypdf()
    total_size = src.stat().st_size
    total_pages = get_pdf_page_count(src)

    pages_over = total_pages > max_pages
    size_over = total_size > MINERU_MAX_SIZE_BYTES
    if not pages_over and not size_over:
        return [ChunkInfo(
            original_path=src, chunk_path=src, part_index=1, part_count=1,
            start_page=1, end_page=total_pages, is_split=False, split_reason=None,
        )]

    split_reason = "pages+size" if (pages_over and size_over) else ("pages" if pages_over else "size")

    avg_bytes_per_page = total_size / max(total_pages, 1)
    if avg_bytes_per_page > 0:
        size_derived_max_pages = max(1, int(max_size_bytes // avg_bytes_per_page))
    else:
        size_derived_max_pages = max_pages
    effective_max_pages = max(1, min(max_pages, size_derived_max_pages))

    out_dir.mkdir(parents=True, exist_ok=True)
    reader = pypdf.PdfReader(str(src))
    if reader.is_encrypted:
        reader.decrypt("")

    # 第一步：按 effective_max_pages 切出初步边界（0-based 半开区间）
    boundaries: list[tuple[int, int]] = []
    p = 0
    while p < total_pages:
        boundaries.append((p, min(p + effective_max_pages, total_pages)))
        p += effective_max_pages

    # 第二步：对每个初步边界递归解析为满足体积限制的叶子边界
    def resolve_leaves(start0: int, end0: int, depth: int) -> list[tuple[int, int, Path]]:
        tmp_path = out_dir / f"{src.stem}__probe_{start0}_{end0}.pdf"
        _write_pdf_slice(reader, start0, end0, tmp_path)
        size = tmp_path.stat().st_size
        if size <= MINERU_MAX_SIZE_BYTES:
            return [(start0, end0, tmp_path)]
        tmp_path.unlink(missing_ok=True)
        if end0 - start0 <= 1:
            raise UnsplittableFileError(
                f"{src.name} 第 {start0 + 1} 页单页体积约 {size / 1024 / 1024:.1f}MB，"
                f"已超过 200MB 限制，无法通过再拆分解决，请人工压缩该页内容后处理。"
            )
        if depth >= MAX_RECURSIVE_SPLIT_DEPTH:
            raise UnsplittableFileError(
                f"{src.name} 第 {start0 + 1}-{end0} 页递归拆分 {MAX_RECURSIVE_SPLIT_DEPTH} 层后"
                f"体积仍超过 200MB，无法通过再拆分解决，请人工压缩该范围内容后处理。"
            )
        mid0 = start0 + (end0 - start0) // 2
        return (resolve_leaves(start0, mid0, depth + 1)
                + resolve_leaves(mid0, end0, depth + 1))

    leaves: list[tuple[int, int, Path]] = []
    for start0, end0 in boundaries:
        leaves.extend(resolve_leaves(start0, end0, depth=1))

    # 第三步：统一按叶子总数重新连续编号，重命名为最终 part 文件
    part_count = len(leaves)
    digits = max(2, len(str(part_count)))
    chunks: list[ChunkInfo] = []
    for idx, (start0, end0, tmp_path) in enumerate(leaves, start=1):
        final_name = (
            f"{src.stem}__part{idx:0{digits}d}of{part_count:0{digits}d}"
            f"_p{start0 + 1}-{end0}.pdf"
        )
        final_path = out_dir / final_name
        tmp_path.rename(final_path)
        chunks.append(ChunkInfo(
            original_path=src, chunk_path=final_path, part_index=idx, part_count=part_count,
            start_page=start0 + 1, end_page=end0, is_split=True, split_reason=split_reason,
        ))
    return chunks


# ---------------------------------------------------------------------------
# Word/Office -> PDF 转换（仅 LibreOffice，跨平台一致）
# ---------------------------------------------------------------------------

_SOFFICE_INSTALL_HINT = (
    "需要 LibreOffice（soffice）将超限 Word 文档转换为 PDF 后再拆分，但未在 PATH 中找到。\n"
    "  Windows: winget install --id LibreOffice.LibreOffice -e "
    "（或 https://www.libreoffice.org/download/ 手动下载安装）\n"
    "  macOS:   brew install --cask libreoffice\n"
    "  Linux:   apt install libreoffice   /   dnf install libreoffice\n"
    "安装后需重新打开终端使 PATH 生效。若不便安装，请手动将该文件转换为 PDF 后单独提交。"
)


def convert_office_to_pdf(src: Path, out_dir: Path, *, timeout_sec: int = 600) -> Path:
    """用 LibreOffice headless 把 Word 文档转换为 PDF。

    返回码为 0 不代表转换真的成功，必须额外校验产物文件确实存在且非空。
    """
    soffice = shutil.which("soffice") or shutil.which("soffice.exe")
    if not soffice:
        raise MissingDependencyError(_SOFFICE_INSTALL_HINT)

    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(src)],
            timeout=timeout_sec, capture_output=True,
        )
    except subprocess.TimeoutExpired as e:
        raise OfficeConversionError(
            f"LibreOffice 转换 {src.name} 超过 {timeout_sec} 秒未完成，判定为失败。"
            f"请手动将该文件转换为 PDF 后单独提交。"
        ) from e

    out_pdf = out_dir / f"{src.stem}.pdf"
    if not out_pdf.exists() or out_pdf.stat().st_size == 0:
        stderr_tail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-300:]
        raise OfficeConversionError(
            f"LibreOffice 转换 {src.name} 未产出有效 PDF（returncode={proc.returncode}）。"
            f"stderr尾部: {stderr_tail!r}。请手动用 LibreOffice/Word 打开该文件确认是否已损坏，"
            f"或手动转换为 PDF 后单独提交。"
        )
    return out_pdf


# ---------------------------------------------------------------------------
# 错误特征识别（用于反应式补救的触发判断）
# ---------------------------------------------------------------------------

PAGE_LIMIT_ERROR_PATTERNS = [
    re.compile(r"-?60006"),
    re.compile(r"页[数码]?.{0,6}(超过|超限|超出|限制)"),
    re.compile(r"(超过|超出).{0,6}200.{0,4}页"),
    re.compile(r"page[s]?.{0,10}(limit|exceed)", re.I),
    re.compile(r"too many pages", re.I),
]

SIZE_LIMIT_ERROR_PATTERNS = [
    re.compile(r"-?60005"),
    re.compile(r"(体积|大小|文件).{0,6}(超过|超限|超出|限制)"),
    re.compile(r"(超过|超出).{0,4}200.{0,4}(MB|M)\b"),
    re.compile(r"size.{0,10}(limit|exceed)", re.I),
    re.compile(r"file.{0,10}too large", re.I),
]


def is_page_limit_error(err_msg: str | None) -> bool:
    if not err_msg:
        return False
    return any(p.search(err_msg) for p in PAGE_LIMIT_ERROR_PATTERNS)


def is_size_limit_error(err_msg: str | None) -> bool:
    if not err_msg:
        return False
    return any(p.search(err_msg) for p in SIZE_LIMIT_ERROR_PATTERNS)


def is_limit_error(err_msg: str | None) -> bool:
    """页数超限或体积超限任一命中即可，供反应式补救统一判断触发条件。
    误判代价仅是多花一次转换+提交时间，不影响正确性。"""
    return is_page_limit_error(err_msg) or is_size_limit_error(err_msg)


# ---------------------------------------------------------------------------
# 结果合并：markdown / images / content_list.json
# ---------------------------------------------------------------------------

def merge_images(
    chunk_dirs: list[Path], chunk_infos: list[ChunkInfo], dest_images_dir: Path,
) -> list[dict[str, str]]:
    """把每个 chunk 的 images/* 拷贝到 dest_images_dir 并按分片命名空间隔离。

    返回与 chunk_infos 等长的 list[dict]：每个 dict 是
    {旧相对路径(如 'images/abcd1234.jpg') -> 新相对路径
     (如 'images/part02_abcd1234.jpg')}，供 merge_markdown / merge_content_list
    重写引用路径时使用。若某 chunk 没有 images/ 目录，对应位置为空 dict。
    """
    dest_images_dir.mkdir(parents=True, exist_ok=True)
    rename_maps: list[dict[str, str]] = []
    for chunk_dir, info in zip(chunk_dirs, chunk_infos):
        src_images_dir = chunk_dir / "images"
        mapping: dict[str, str] = {}
        if src_images_dir.is_dir():
            for img in sorted(src_images_dir.iterdir()):
                if not img.is_file():
                    continue
                new_name = f"part{info.part_index:02d}_{img.name}"
                shutil.copy2(img, dest_images_dir / new_name)
                mapping[f"images/{img.name}"] = f"images/{new_name}"
        rename_maps.append(mapping)
    return rename_maps


def _rewrite_image_refs(text: str, rename_map: dict[str, str]) -> str:
    for old, new in rename_map.items():
        text = text.replace(old, new)
    return text


def merge_markdown(
    chunk_dirs: list[Path], chunk_infos: list[ChunkInfo],
    image_rename_maps: list[dict[str, str]], dest_md: Path,
    failed_parts: dict[int, str] | None = None,
) -> None:
    """按 part_index 顺序拼接各 chunk 的 full.md，重写图片引用，插入拼接点标记。

    failed_parts: {part_index: err_msg}，标记哪些分片解析失败，在对应位置
    插入警告占位符而不是尝试读取不存在的 full.md。
    """
    failed_parts = failed_parts or {}
    pieces: list[str] = []
    for chunk_dir, info in zip(chunk_dirs, chunk_infos):
        if info.part_index in failed_parts:
            pieces.append(
                f"\n\n<!-- ⚠ 第{info.part_index}部分（原文档第{info.start_page}-"
                f"{info.end_page}页）解析失败：{failed_parts[info.part_index]}；"
                f"对应内容缺失 -->\n\n"
            )
            continue
        md_path = chunk_dir / "full.md"
        if not md_path.exists():
            pieces.append(
                f"\n\n<!-- ⚠ 第{info.part_index}部分（原文档第{info.start_page}-"
                f"{info.end_page}页）未找到 full.md，内容缺失 -->\n\n"
            )
            continue
        text = md_path.read_text(encoding="utf-8")
        mapping = image_rename_maps[info.part_index - 1] if image_rename_maps else {}
        text = _rewrite_image_refs(text, mapping)
        if pieces:
            pieces.append(
                f"\n\n<!-- ⟪拼接点：第{info.part_index}部分开始，对应原文档第"
                f"{info.start_page}-{info.end_page}页；跨此边界的表格/段落可能被"
                f"截断，请人工核对⟫ -->\n\n"
            )
        pieces.append(text)
    dest_md.parent.mkdir(parents=True, exist_ok=True)
    dest_md.write_text("".join(pieces), encoding="utf-8")


def merge_content_list(
    chunk_dirs: list[Path], chunk_infos: list[ChunkInfo],
    image_rename_maps: list[dict[str, str]], dest_path: Path, *,
    filename_glob: str, failed_part_indices: set[int] | None = None,
) -> bool:
    """合并各 chunk 的 content_list(.v2).json，校正 page_idx 为原文档绝对页码。

    ⚠️ 依赖假设：MinerU 输出的 content_list.json 每条记录含 'page_idx' 字段
    （从 0 开始、按当次提交批次内部计数）。这一假设在正式启用前必须用真实
    API 响应验证；若字段不存在，本函数会显式抛错而不是静默产出错误页码。

    返回 True 表示确实合并出了内容；False 表示所有 chunk 均无此文件
    （调用方据此决定是否生成该输出文件，不视为错误）。
    """
    failed_part_indices = failed_part_indices or set()
    merged: list[dict] = []
    any_found = False
    for chunk_dir, info in zip(chunk_dirs, chunk_infos):
        if info.part_index in failed_part_indices:
            continue
        matches = sorted(chunk_dir.glob(filename_glob))
        if not matches:
            continue
        any_found = True
        mapping = image_rename_maps[info.part_index - 1] if image_rename_maps else {}
        data = json.loads(matches[0].read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise RuntimeError(
                f"{matches[0]} 顶层不是数组，与预期的 content_list 结构不符，无法安全合并"
            )
        for item in data:
            if "page_idx" not in item:
                raise RuntimeError(
                    f"MinerU 输出的 {matches[0].name} 不含预期的 page_idx 字段，"
                    f"无法安全合并页码，请检查 API 响应结构是否变化后再启用自动拆分。"
                )
            item["page_idx"] = item["page_idx"] + (info.start_page - 1)
            for key in ("img_path", "image_path"):
                if key in item and isinstance(item[key], str) and item[key] in mapping:
                    item[key] = mapping[item[key]]
            merged.append(item)
    if not any_found:
        return False
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return True
