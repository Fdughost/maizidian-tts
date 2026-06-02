"""火山引擎豆包 TTS 核心库（纯函数 + TTSConfig）。

被 CLI（custom_tts.py）和 webapp 共同使用。
本模块**不**读取环境变量、**不**加载 .env、**不**解析命令行参数。
所有诊断信息通过 stdlib logging 发出，由调用方决定如何展示。
"""
from __future__ import annotations

import base64
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests
from docx import Document

logger = logging.getLogger(__name__)

# 仅这些错误码触发指数退避重试；其他业务错误（如文本违规）直接失败
RATE_LIMIT_CODES = {429, 4029}


@dataclass
class TTSConfig:
    appid: str
    cluster: str
    voice_type: str
    access_token: str = field(repr=False)         # 防止 repr / 日志泄漏
    encoding: str = "mp3"
    speed_ratio: float = 1.0
    loudness_ratio: float = 1.0
    api_url: str = "https://openspeech.bytedance.com/api/v1/tts"
    max_retries: int = 3
    user_uid: str = "batch_tts_user"


def read_docx(filepath) -> list[str]:
    """读取 .docx 段落，过滤空段。"""
    doc = Document(str(filepath))
    return [p.text.strip() for p in doc.paragraphs if p.text.strip()]


def read_txt(filepath) -> list[str]:
    """读取 .txt 文件，按空行分段；编码不支持时抛 ValueError。

    优先尝试 utf-8-sig（兼容 BOM），其次 utf-8；都失败则要求重存为 UTF-8。
    """
    raw = Path(filepath).read_bytes()
    text: Optional[str] = None
    for enc in ("utf-8-sig", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("Unsupported text encoding (please save as UTF-8)")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        paragraphs = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return paragraphs


def split_long_text(text: str, max_chars: int = 300) -> list[str]:
    """按句末标点切分超长文本；无句末标点时按字符硬切。"""
    if len(text) <= max_chars:
        return [text]
    sentences = re.split(r"(?<=[。！？!?.;\n])", text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if not sentence:
            continue
        if len(current) + len(sentence) <= max_chars:
            current += sentence
        else:
            if current:
                chunks.append(current)
            while len(sentence) > max_chars:
                chunks.append(sentence[:max_chars])
                sentence = sentence[max_chars:]
            current = sentence
    if current:
        chunks.append(current)
    return chunks


def synthesize_text(text: str, cfg: TTSConfig) -> tuple[bytes, int]:
    """合成单段文本，返回 (mp3_bytes, duration_ms)。

    协议铁规（火山引擎 HTTP API 要求，请勿"修正"）：
      1. 请求体 `app.token` 必须是字面量字符串 `"access_token"`，**不是** cfg.access_token。
      2. Header 形如 `Authorization: Bearer;{cfg.access_token}`（**分号** + token，不是空格）。
    任一修改都会让接口立即 401 / 业务错误码。
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer;{cfg.access_token}",
    }
    body = {
        "app": {
            "appid": cfg.appid,
            "token": "access_token",       # 字面量；勿替换
            "cluster": cfg.cluster,
        },
        "user": {"uid": cfg.user_uid},
        "audio": {
            "voice_type": cfg.voice_type,
            "encoding": cfg.encoding,
            "speed_ratio": cfg.speed_ratio,
            "loudness_ratio": cfg.loudness_ratio,
        },
        "request": {
            "reqid": str(uuid.uuid4()),
            "text": text,
            "operation": "query",
        },
    }

    last_exc: Optional[Exception] = None
    for attempt in range(cfg.max_retries + 1):
        try:
            resp = requests.post(cfg.api_url, headers=headers, json=body, timeout=60)
            result = resp.json()
            code = result.get("code")
            message = result.get("message", "unknown error")

            if code == 3000:
                audio_bytes = base64.b64decode(result.get("data", ""))
                duration = int(result.get("addition", {}).get("duration", "0"))
                return audio_bytes, duration

            if code in RATE_LIMIT_CODES:
                wait = 2 ** attempt
                if attempt < cfg.max_retries:
                    logger.warning("限流（错误码 %s），%ss 后重试", code, wait)
                    time.sleep(wait)
                    continue
                raise RuntimeError(
                    f"限流，重试 {cfg.max_retries} 次后仍失败 [错误码 {code}]：{message}"
                )

            raise RuntimeError(f"合成失败 [错误码 {code}]：{message}")

        except RuntimeError:
            raise
        except Exception as e:
            last_exc = e
            wait = 2 ** attempt
            if attempt < cfg.max_retries:
                logger.warning("网络异常（%s），%ss 后重试", e, wait)
                time.sleep(wait)
                continue

    raise RuntimeError(f"网络请求失败，重试 {cfg.max_retries} 次后仍失败：{last_exc}")


def merge_audio_files(segment_files: list, output_path, silence_ms: int = 500) -> bool:
    """合并 MP3 片段。优先 pydub + ffmpeg（含静音间隔），失败回退二进制拼接。"""
    try:
        from pydub import AudioSegment
        combined = AudioSegment.empty()
        silence = AudioSegment.silent(duration=silence_ms)
        for i, fp in enumerate(segment_files):
            seg = AudioSegment.from_mp3(str(fp))
            if i > 0:
                combined += silence
            combined += seg
        combined.export(str(output_path), format="mp3")
        logger.info("merge: pydub+ffmpeg with %sms silence", silence_ms)
        return True
    except Exception:
        pass

    try:
        with open(output_path, "wb") as outfile:
            for fp in segment_files:
                with open(fp, "rb") as infile:
                    outfile.write(infile.read())
        logger.info("merge: binary concat (no silence; install ffmpeg to enable)")
        return True
    except Exception as e:
        logger.error("merge failed: %s", e)
        return False
