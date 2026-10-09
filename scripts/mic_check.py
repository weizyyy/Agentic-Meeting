"""麦克风电平诊断：同一段录音在各档增益下，语音检测还能留住多少。

    uv run python scripts/mic_check.py --wav 我的录音.wav

识别断断续续、字幕缺字时先跑这个。用任何录音工具在**平时开会的姿势和音量**下录一小段话（十几秒就够），存成
WAV / FLAC / OGG。脚本把它依次放大 0、+6 … +36 dB 以及「自动增益」（按 config.toml 的 ``[audio]``），
用 Pipecat 自带的语音检测各跑一遍，两个音量门限各一次：配置里的门限（``turn.vad_min_volume``）和 0（关闭这道门限）。

怎么看：「说话占比」低、「开始说话次数」多，就是语音被切碎了——识别拿到的只是碎片。「0 dB」那一行明显差于加了增益的行，
说明麦克风偏轻：调高系统的输入音量，或者调 ``[audio]`` 里的 ``target_dbfs`` / ``max_gain_db``。

只需要 Pipecat 自带的模型：不联网、不需要权重、不需要任何服务。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from agentic_meeting.audio.diagnose import (
    DEFAULT_GAINS_DB,
    format_sweep,
    load_pcm16k,
    peak_dbfs,
    rms_dbfs,
    sweep,
)
from agentic_meeting.config import load_config


def parse_floats(text: str) -> tuple[float, ...]:
    return tuple(float(x) for x in text.split(",") if x.strip())


async def run(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    pcm = load_pcm16k(args.wav)
    print(
        f"音频 {len(pcm) / 2 / 16000:.1f} 秒，原始 RMS {rms_dbfs(pcm):.1f} dBFS，"
        f"峰值 {peak_dbfs(pcm):.1f} dBFS；"
        f"当前配置：auto_gain={cfg.audio.auto_gain}，max_gain_db={cfg.audio.max_gain_db:g}，"
        f"target_dbfs={cfg.audio.target_dbfs:g}，vad_min_volume={cfg.turn.vad_min_volume:g}"
    )
    min_volumes = tuple(dict.fromkeys((cfg.turn.vad_min_volume, 0.0)))
    rows = await sweep(
        pcm, gains_db=parse_floats(args.gains), min_volumes=min_volumes, audio_cfg=cfg.audio
    )
    print("\n===== 语音检测 =====")
    print(format_sweep(rows))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--wav", required=True, help="一段录音（WAV / FLAC / OGG），任意采样率")
    parser.add_argument("--config", default=None, help="配置文件，默认 config/config.toml")
    parser.add_argument(
        "--gains",
        default=",".join(f"{g:g}" for g in DEFAULT_GAINS_DB),
        help="要试的固定增益（dB，逗号分隔）",
    )
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
