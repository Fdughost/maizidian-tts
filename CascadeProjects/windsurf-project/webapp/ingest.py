"""上传/粘贴 → segments，含字节、字符、段数三重校验。"""
from __future__ import annotations

import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, UploadFile

import tts_core

logger = logging.getLogger(__name__)

ALLOWED_EXT = {".docx", ".txt"}

MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 5 * 1024 * 1024))
MAX_TOTAL_CHARS = int(os.environ.get("MAX_TOTAL_CHARS", 20000))
MAX_TEXT_CHARS = int(os.environ.get("MAX_TEXT_CHARS", 300))

# 防御性上限，应对极端短句拆分场景。
# 正常用例：20000 字 / 300 字每段 ≈ 67 段，远低于该值。
# MAX_SEGMENTS **不**是容量预估，而是阻断异常输入（大量超短句、特殊字符堆叠等）的兜底。
MAX_SEGMENTS = int(os.environ.get("MAX_SEGMENTS", 200))


def _save_upload_with_limit(file: UploadFile, max_bytes: int) -> Path:
    """流式落盘到临时文件；累计字节超 max_bytes 立即抛 413。文件名不来自 UploadFile。"""
    suffix = Path(file.filename or "").suffix.lower()
    fd, tmp_name = tempfile.mkstemp(prefix=f"upload-{uuid.uuid4().hex}-", suffix=suffix)
    os.close(fd)
    tmp_path = Path(tmp_name)
    total = 0
    try:
        with tmp_path.open("wb") as out:
            while True:
                chunk = file.file.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Upload exceeds {max_bytes} bytes",
                    )
                out.write(chunk)
    except HTTPException:
        tmp_path.unlink(missing_ok=True)
        raise
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


def ingest(
    source_type: str,
    file: Optional[UploadFile],
    text: Optional[str],
) -> list[str]:
    """读取输入返回段落列表，做基础校验（非空 + 扩展名）。后续切分/字数/段数校验由调用方做。"""
    if source_type == "text":
        if text is None:
            raise HTTPException(status_code=400, detail="Missing text")
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        if not paragraphs:
            paragraphs = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not paragraphs:
            raise HTTPException(status_code=400, detail="Empty input")
        return paragraphs

    if source_type != "file" or file is None:
        raise HTTPException(status_code=400, detail="Missing file")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported extension; allowed: {sorted(ALLOWED_EXT)}",
        )

    tmp_path = _save_upload_with_limit(file, MAX_UPLOAD_BYTES)
    try:
        try:
            if ext == ".docx":
                paragraphs = tts_core.read_docx(tmp_path)
            else:
                paragraphs = tts_core.read_txt(tmp_path)
        except ValueError as e:
            # 编码不支持
            raise HTTPException(status_code=400, detail=str(e))
    finally:
        tmp_path.unlink(missing_ok=True)

    if not paragraphs:
        raise HTTPException(status_code=400, detail="Empty input")
    return paragraphs


def split_and_validate(paragraphs: list[str]) -> list[str]:
    """切分为合成片段，并校验段数 / 总字符数上限。"""
    segments: list[str] = []
    for para in paragraphs:
        segments.extend(tts_core.split_long_text(para, MAX_TEXT_CHARS))

    if len(segments) > MAX_SEGMENTS:
        raise HTTPException(
            status_code=413,
            detail=f"Too many segments after split: {len(segments)} > {MAX_SEGMENTS}",
        )
    total_chars = sum(len(s) for s in segments)
    if total_chars > MAX_TOTAL_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"Total chars {total_chars} exceeds {MAX_TOTAL_CHARS}",
        )
    return segments
