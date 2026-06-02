#!/usr/bin/env python3
"""火山引擎 豆包语音合成 批量 Word 文件合成脚本（CLI 入口）。

使用方法：
  python3 custom_tts.py                     # 默认读取 inputs/ 下第一个 .docx
  python3 custom_tts.py inputs/0308.docx    # 指定 Word 文件

凭证配置：
  在项目根目录创建 .env 或设置环境变量：
  VOLCENGINE_APPID / VOLCENGINE_ACCESS_TOKEN / VOLCENGINE_CLUSTER / VOLCENGINE_VOICE_TYPE

核心合成逻辑在 `tts_core.py`；本文件负责 CLI 参数、.env 加载、文件 IO、进度打印。
文档参考：https://www.volcengine.com/docs/6561/1257584
"""
from __future__ import annotations

import glob
import logging
import os
import sys
import time
from datetime import datetime

import tts_core


# ======================== 加载 .env ========================
try:
    from dotenv import load_dotenv
    _d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(5):
        env_path = os.path.join(_d, ".env")
        if os.path.exists(env_path):
            load_dotenv(env_path)
            break
        parent = os.path.dirname(_d)
        if parent == _d:
            break
        _d = parent
except ImportError:
    pass


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUTS_BASE = os.path.join(SCRIPT_DIR, "outputs")

MAX_TEXT_CHARS = 300
SILENCE_BETWEEN_MS = 500


def _find_inputs_dir():
    env_val = os.environ.get("INPUTS_DIR")
    if env_val:
        return env_val
    d = SCRIPT_DIR
    for _ in range(5):
        candidate = os.path.join(d, "inputs")
        if os.path.isdir(candidate):
            return candidate
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return os.path.join(os.getcwd(), "inputs")


INPUTS_DIR = _find_inputs_dir()


def _build_config() -> tts_core.TTSConfig:
    appid = os.environ.get("VOLCENGINE_APPID", "")
    access_token = os.environ.get("VOLCENGINE_ACCESS_TOKEN", "")
    cluster = os.environ.get("VOLCENGINE_CLUSTER", "volcano_icl")
    voice_type = os.environ.get("VOLCENGINE_VOICE_TYPE", "")
    missing = [
        name for name, value in [
            ("VOLCENGINE_APPID", appid),
            ("VOLCENGINE_ACCESS_TOKEN", access_token),
            ("VOLCENGINE_VOICE_TYPE", voice_type),
        ] if not value
    ]
    if missing:
        print("错误：缺少必要配置，请在 .env 文件或环境变量中设置：")
        for key in missing:
            print(f"  {key}=your_value_here")
        sys.exit(1)
    return tts_core.TTSConfig(
        appid=appid,
        access_token=access_token,
        cluster=cluster,
        voice_type=voice_type,
    )


def find_docx_file(arg=None):
    if arg:
        path = arg if os.path.isabs(arg) else os.path.join(SCRIPT_DIR, arg)
        if not os.path.exists(path):
            alt = os.path.join(INPUTS_DIR, os.path.basename(arg))
            if os.path.exists(alt):
                path = alt
        if not os.path.exists(path):
            print(f"错误：找不到文件 {arg}")
            sys.exit(1)
        return path

    if not os.path.isdir(INPUTS_DIR):
        print(f"错误：找不到 inputs 目录：{INPUTS_DIR}")
        sys.exit(1)

    docx_files = sorted([
        f for f in glob.glob(os.path.join(INPUTS_DIR, "*.docx"))
        if not os.path.basename(f).startswith("~$")
    ])
    if not docx_files:
        print("错误：inputs 目录下没有 .docx 文件")
        sys.exit(1)
    return docx_files[0]


def main():
    logging.basicConfig(level=logging.INFO, format="           %(message)s")
    cfg = _build_config()

    arg = sys.argv[1] if len(sys.argv) > 1 else None
    docx_path = find_docx_file(arg)
    docx_name = os.path.splitext(os.path.basename(docx_path))[0]

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    outputs_dir = os.path.join(OUTPUTS_BASE, run_id)

    print("========== 批量TTS合成 ==========")
    print(f"Word 文件：{docx_path}")
    print(f"音色：{cfg.voice_type}")
    print(f"输出目录：{outputs_dir}")
    print()

    paragraphs = tts_core.read_docx(docx_path)
    print(f"读取到 {len(paragraphs)} 个段落")

    segments: list[str] = []
    for para in paragraphs:
        segments.extend(tts_core.split_long_text(para, MAX_TEXT_CHARS))
    print(f"切分后共 {len(segments)} 个合成片段")
    print()

    os.makedirs(outputs_dir, exist_ok=True)

    segment_files: list[str] = []
    total_duration = 0

    for i, text in enumerate(segments):
        idx = i + 1
        filename = f"segment_{idx:03d}.mp3"
        filepath = os.path.join(outputs_dir, filename)
        preview = text[:40] + ("..." if len(text) > 40 else "")
        print(f"[{idx}/{len(segments)}] 合成中：{preview}")

        try:
            audio_bytes, duration_ms = tts_core.synthesize_text(text, cfg)
            with open(filepath, "wb") as f:
                f.write(audio_bytes)
            total_duration += duration_ms
            segment_files.append(filepath)
            print(f"         ✓ {filename} ({duration_ms}ms, {len(audio_bytes)}字节)")
        except RuntimeError as e:
            print(f"         ✗ 失败：{e}")
            print("           跳过该片段，继续处理...")
            continue

        time.sleep(0.2)

    print()
    print("========== 合成完成 ==========")
    print(f"成功：{len(segment_files)}/{len(segments)} 个片段")
    print(f"总时长：{total_duration / 1000:.1f} 秒")

    if segment_files:
        merged_path = os.path.join(outputs_dir, f"{docx_name}_合并.mp3")
        print(f"\n正在合并音频 -> {merged_path}")
        if tts_core.merge_audio_files(segment_files, merged_path, SILENCE_BETWEEN_MS):
            print(f"✓ 合并成功！文件大小：{os.path.getsize(merged_path)} 字节")

    print(f"\n所有文件已保存至：{outputs_dir}")


if __name__ == "__main__":
    main()
