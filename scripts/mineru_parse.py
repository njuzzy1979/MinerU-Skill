"""MinerU 精准解析（vlm）API 客户端：单文件 / 批量本地文件上传。

用法示例：
    python mineru_parse.py path/to/file.pdf
    python mineru_parse.py input-files/ --extra-formats docx html
    python mineru_parse.py a.pdf b.pdf --extra-formats latex --output-dir ./out
    python mineru_parse.py file.pdf --no-ocr --language en
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


def parse(inputs: list[str], *, output_dir: Path, language: str, is_ocr: bool,
          enable_formula: bool, enable_table: bool, extra_formats: list[str],
          model_version: str, page_ranges: str | None,
          token_env_names: list[str]) -> dict:
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
    print(f"[1/4] collected {len(files)} file(s); model_version={model_version}, "
          f"extra_formats={extra_formats or 'default(md+json)'}")

    print(f"[2/4] requesting upload URLs ...")
    token: str | None = None
    batch_id: str | None = None
    file_urls: list[str] = []
    last_err: Exception | None = None
    for name, candidate in candidates:
        print(f"  trying token from ${name} ...")
        try:
            batch_id, file_urls = request_upload_urls(
                candidate, files, language=language, is_ocr=is_ocr,
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

    print(f"[3/4] uploading files ...")
    upload_files(file_urls, files)

    print(f"[4/4] polling for results ...")
    results = poll_batch(token, batch_id)

    output_dir.mkdir(parents=True, exist_ok=True)
    summary = {"batch_id": batch_id, "results": []}
    for src, r in zip(files, results):
        item = {
            "file": src.name,
            "state": r.get("state"),
            "full_zip_url": r.get("full_zip_url"),
            "err_msg": r.get("err_msg"),
            "output_dir": None,
        }
        if r.get("state") == "done" and r.get("full_zip_url"):
            target = output_dir / src.stem
            print(f"  downloading -> {target}")
            download_and_extract(r["full_zip_url"], target)
            item["output_dir"] = str(target)
        else:
            print(f"  skip download for {src.name}: state={r.get('state')} err={r.get('err_msg')}")
        summary["results"].append(item)

    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDONE. summary -> {summary_path}")
    return summary


def main() -> int:
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
    args = ap.parse_args()

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
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
