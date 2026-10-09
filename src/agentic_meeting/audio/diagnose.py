"""离线诊断：电平与语音检测占比。

给 ``scripts/mic_check.py`` 和测试用；只依赖 Pipecat 自带的语音检测模型，不联网、不需要权重。
所有音频都是 16 kHz 单声道 s16le 字节。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams, VADState

from agentic_meeting.asr.base import ASR_SAMPLE_RATE
from agentic_meeting.audio.gain import InputGainFilter
from agentic_meeting.config import AudioConfig

CHUNK_SAMPLES = ASR_SAMPLE_RATE // 50  # 20 毫秒，与传输输入给出的帧长同量级


def _samples(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0


def rms_dbfs(pcm: bytes) -> float:
    x = _samples(pcm)
    if len(x) == 0:
        return -120.0
    return 20.0 * math.log10(max(float(np.sqrt(np.mean(x * x))), 1e-9))


def peak_dbfs(pcm: bytes) -> float:
    x = _samples(pcm)
    if len(x) == 0:
        return -120.0
    return 20.0 * math.log10(max(float(np.max(np.abs(x))), 1e-9))


def scale_db(pcm: bytes, gain_db: float) -> bytes:
    """固定增益（可为负），带削波保护。0 dB 原样返回。"""
    if gain_db == 0.0:
        return pcm
    y = _samples(pcm) * 10 ** (gain_db / 20)
    return np.clip(np.rint(y * 32768.0), -32768, 32767).astype("<i2").tobytes()


def load_pcm16k(path: str | Path) -> bytes:
    """读一个音频文件（WAV / FLAC / OGG…），混成单声道、线性重采样到 16 kHz。"""
    import soundfile as sf

    data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    if rate != ASR_SAMPLE_RATE and len(mono):
        n_out = round(len(mono) * ASR_SAMPLE_RATE / rate)
        mono = np.interp(np.linspace(0, len(mono) - 1, n_out), np.arange(len(mono)), mono)
    return np.clip(np.rint(mono * 32768.0), -32768, 32767).astype("<i2").tobytes()


async def run_filter(flt: InputGainFilter, pcm: bytes, chunk_samples: int = CHUNK_SAMPLES) -> bytes:
    """把音频按帧喂给滤波器，拼回输出。"""
    step = chunk_samples * 2
    return b"".join([await flt.filter(pcm[i : i + step]) for i in range(0, len(pcm), step)])


@dataclass(frozen=True)
class VadStats:
    speech_ratio: float  # 被判为「说话中」的步数占比
    segments: int  # 「开始说话」的次数；越碎越多
    max_volume: float  # 平滑后的音量最大值（0…1，0.6 ≈ −50 LUFS）


async def vad_stats(pcm: bytes, *, min_volume: float = 0.6) -> VadStats:
    """用 Pipecat 的 Silero 语音检测跑一遍，统计占比和碎片数（参数除 ``min_volume`` 外取默认值）。"""
    vad = SileroVADAnalyzer(params=VADParams(min_volume=min_volume))
    vad.set_sample_rate(ASR_SAMPLE_RATE)
    step = CHUNK_SAMPLES * 2
    total = speaking = segments = 0
    previous = VADState.QUIET
    max_volume = 0.0
    try:
        for i in range(0, len(pcm) - step + 1, step):
            state = await vad.analyze_audio(pcm[i : i + step])
            total += 1
            if state == VADState.SPEAKING:
                speaking += 1
                if previous != VADState.SPEAKING:
                    segments += 1
            previous = state
            max_volume = max(max_volume, vad._prev_volume)
    finally:
        await vad.cleanup()
    return VadStats(speaking / total if total else 0.0, segments, max_volume)


DEFAULT_GAINS_DB = (0.0, 6.0, 12.0, 18.0, 24.0, 30.0, 36.0)


async def gain_variants(
    pcm: bytes,
    gains_db: tuple[float, ...] = DEFAULT_GAINS_DB,
    audio_cfg: AudioConfig | None = None,
) -> list[tuple[str, bytes]]:
    """同一段音频的各档增益版本：每个固定增益一份；给了 ``audio_cfg`` 时再加一份「自动增益」（按配置跑一遍滤波器）。"""
    variants = [(f"{g:+.0f} dB", scale_db(pcm, g)) for g in gains_db]
    if audio_cfg is not None:
        flt = InputGainFilter.from_config(audio_cfg.model_copy(update={"level_log_secs": 0.0}))
        variants.append(("自动增益", await run_filter(flt, pcm)))
    return variants


@dataclass(frozen=True)
class SweepRow:
    label: str
    rms_dbfs: float
    peak_dbfs: float
    min_volume: float
    stats: VadStats


async def sweep(
    pcm: bytes,
    *,
    gains_db: tuple[float, ...] = DEFAULT_GAINS_DB,
    min_volumes: tuple[float, ...] = (0.6, 0.0),
    audio_cfg: AudioConfig | None = None,
) -> list[SweepRow]:
    """各档增益 × 各个音量门限，统计语音检测的结果（``scripts/mic_check.py`` 的主体）。"""
    rows = []
    for label, variant in await gain_variants(pcm, gains_db, audio_cfg):
        for min_volume in min_volumes:
            rows.append(
                SweepRow(
                    label,
                    rms_dbfs(variant),
                    peak_dbfs(variant),
                    min_volume,
                    await vad_stats(variant, min_volume=min_volume),
                )
            )
    return rows


def format_sweep(rows: list[SweepRow]) -> str:
    header = f"{'增益':<10}{'RMS dBFS':>10}{'峰值 dBFS':>11}{'音量门限':>9}{'说话占比':>9}{'开始说话次数':>13}{'最大音量':>9}"
    lines = [header, "-" * len(header)]
    for r in rows:
        lines.append(
            f"{r.label:<10}{r.rms_dbfs:>10.1f}{r.peak_dbfs:>11.1f}{r.min_volume:>9.2f}"
            f"{r.stats.speech_ratio:>9.2f}{r.stats.segments:>13d}{r.stats.max_volume:>9.2f}"
        )
    return "\n".join(lines)
