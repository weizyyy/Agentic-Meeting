"""入口收音增强（audio/gain.py）与输入电平诊断（audio/diagnose.py）。

信号用合成的「像说话」的波形（带音节起伏的两个正弦叠加），不依赖任何模型；
最后一条回归测试用仓库里自带的真人语音样本复现真实麦克风上遇到的现象（麦克风电平偏低 → 语音检测被切成碎片）。
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from loguru import logger

from agentic_meeting.audio.diagnose import (
    format_sweep,
    gain_variants,
    load_pcm16k,
    rms_dbfs,
    run_filter,
    scale_db,
    sweep,
    vad_stats,
)
from agentic_meeting.audio.gain import InputGainFilter
from agentic_meeting.config import REPO_ROOT, AudioConfig

SR = 16000
CHUNK = 320  # 20 毫秒，传输输入给出的典型帧长


def speechlike(secs: float, dbfs: float, *, seed: int = 0, modulation: float = 0.5) -> bytes:
    """4 Hz 音节起伏的两个正弦叠加加一点噪声，整体 RMS 为 ``dbfs``。"""
    t = np.arange(int(secs * SR)) / SR
    rng = np.random.default_rng(seed)
    carrier = np.sin(2 * math.pi * 220 * t) + 0.6 * np.sin(2 * math.pi * 710 * t)
    carrier += 0.05 * rng.standard_normal(len(t))
    envelope = 1.0 - modulation * (0.5 + 0.5 * np.sin(2 * math.pi * 4 * t))
    x = carrier * envelope
    x *= 10 ** (dbfs / 20) / np.sqrt(np.mean(x**2))
    return np.clip(np.rint(x * 32768), -32768, 32767).astype("<i2").tobytes()


def frame_levels(pcm: bytes) -> np.ndarray:
    """每 20 毫秒一帧的 RMS（dBFS）。"""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768
    n = len(x) // CHUNK
    frames = x[: n * CHUNK].reshape(n, CHUNK)
    return 20 * np.log10(np.sqrt((frames**2).mean(axis=1)) + 1e-9)


def loud_syllable_level(pcm: bytes) -> float:
    """较响音节的电平：帧 RMS 的第 90 百分位（target_dbfs 的含义）。"""
    return float(np.percentile(frame_levels(pcm), 90))


@pytest.fixture
def make_filter():
    def make(**kwargs) -> InputGainFilter:
        return InputGainFilter(**{"level_log_secs": 0.0, **kwargs})

    return make


# --------------------------------------------------------------------------- #
# 自动增益
# --------------------------------------------------------------------------- #


async def test_quiet_speech_is_brought_up_to_the_target(make_filter):
    flt = make_filter(target_dbfs=-22.0, max_gain_db=40.0)
    out = await run_filter(flt, speechlike(4.0, -50.0), CHUNK)
    last_second = out[-SR * 2 :]
    assert loud_syllable_level(last_second) == pytest.approx(-22.0, abs=3.0)
    assert flt.gain_db > 20


async def test_gain_never_exceeds_the_cap(make_filter):
    flt = make_filter(max_gain_db=20.0)
    out = await run_filter(flt, speechlike(4.0, -60.0), CHUNK)
    assert flt.gain_db <= 20.0 + 1e-6
    assert flt.gain_db == pytest.approx(20.0, abs=0.5)
    # 增益只有 20 dB，所以输出仍然比目标轻得多
    assert loud_syllable_level(out[-SR * 2 :]) < -35


async def test_loud_input_is_left_alone(make_filter):
    flt = make_filter()
    pcm = speechlike(2.0, -12.0)
    out = await run_filter(flt, pcm, CHUNK)
    assert flt.gain_db == pytest.approx(0.0, abs=1e-6)
    assert out == pcm  # 增益为 0 dB 时逐字节原样输出，连限幅器都不碰


async def test_signal_below_the_noise_floor_does_not_move_the_gain(make_filter):
    flt = make_filter(gain_db=6.0, noise_floor_dbfs=-70.0)
    await run_filter(flt, speechlike(3.0, -85.0), CHUNK)
    assert flt.gain_db == pytest.approx(6.0, abs=1e-6)


async def test_silence_keeps_the_gain_reached_during_speech(make_filter):
    flt = make_filter(max_gain_db=40.0)
    await run_filter(flt, speechlike(3.0, -50.0), CHUNK)
    reached = flt.gain_db
    assert reached > 15
    await run_filter(flt, speechlike(4.0, -88.0, seed=1), CHUNK)
    assert flt.gain_db == pytest.approx(reached, abs=0.5)  # 下一句开头已经在合适的增益上


async def test_a_sudden_loud_voice_pulls_the_gain_down_quickly(make_filter):
    flt = make_filter(max_gain_db=40.0)
    await run_filter(flt, speechlike(3.0, -50.0), CHUNK)
    assert flt.gain_db > 15
    await run_filter(flt, speechlike(0.3, -15.0, seed=2), CHUNK)
    assert flt.gain_db < 6


async def test_initial_gain_is_used_until_speech_is_heard(make_filter):
    flt = make_filter(gain_db=10.0)
    assert flt.gain_db == 10.0
    out = await run_filter(flt, speechlike(0.5, -88.0), CHUNK)
    assert rms_dbfs(out) == pytest.approx(rms_dbfs(speechlike(0.5, -88.0)) + 10.0, abs=1.0)


# --------------------------------------------------------------------------- #
# 固定增益、关闭、限幅
# --------------------------------------------------------------------------- #


async def test_disabled_filter_returns_the_same_bytes(make_filter):
    flt = make_filter(auto_gain=False, gain_db=0.0)
    pcm = speechlike(1.0, -40.0)
    assert await flt.filter(pcm) is pcm


async def test_fixed_gain_scales_by_the_configured_decibels(make_filter):
    flt = make_filter(auto_gain=False, gain_db=12.0)
    pcm = speechlike(2.0, -50.0)
    out = await run_filter(flt, pcm, CHUNK)
    assert rms_dbfs(out) - rms_dbfs(pcm) == pytest.approx(12.0, abs=0.1)


async def test_limiter_prevents_wrap_around_near_full_scale(make_filter):
    flt = make_filter(auto_gain=False, gain_db=12.0)
    t = np.arange(SR) / SR
    pcm = np.rint(31000 * np.sin(2 * math.pi * 300 * t)).astype("<i2").tobytes()
    out = np.frombuffer(await run_filter(flt, pcm, CHUNK), dtype="<i2")
    x = np.frombuffer(pcm, dtype="<i2")
    assert out.max() <= 32767 and out.min() >= -32768
    big = np.abs(x) > 1000
    assert (np.sign(out[big]) == np.sign(x[big])).all()  # 回绕会让符号翻转
    assert np.abs(out).max() > 30000  # 压住了，但没有被压扁到很小


@pytest.mark.parametrize("chunk", [1, 3, 7, 160, 320, 1000, 16000])
async def test_any_chunking_keeps_the_length(make_filter, chunk):
    flt = make_filter(max_gain_db=30.0)
    pcm = speechlike(1.0, -45.0)
    out = await run_filter(flt, pcm, chunk)
    assert len(out) == len(pcm)  # 会话时间轴按采样数走：不能丢帧、不能改长度


async def test_empty_chunk_is_returned_as_is(make_filter):
    assert await make_filter().filter(b"") == b""


# --------------------------------------------------------------------------- #
# 容错
# --------------------------------------------------------------------------- #


async def test_an_internal_error_passes_the_original_audio_through(make_filter, monkeypatch):
    flt = make_filter()

    def boom(self, audio):
        raise RuntimeError("增益算法出错")

    monkeypatch.setattr(InputGainFilter, "_process", boom)
    pcm = speechlike(0.1, -40.0)
    assert await flt.filter(pcm) == pcm  # 转录链路优先存活：原样放行，不抛出


async def test_the_error_is_logged_but_not_on_every_frame(make_filter, monkeypatch):
    flt = make_filter()
    monkeypatch.setattr(
        InputGainFilter, "_process", lambda self, audio: (_ for _ in ()).throw(ValueError("x"))
    )
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(str(m)), level="ERROR")
    try:
        for _ in range(50):
            await flt.filter(speechlike(0.02, -40.0))
    finally:
        logger.remove(sink)
    assert 1 <= len(messages) <= 3  # 限频


# --------------------------------------------------------------------------- #
# 电平过低的提示与日志
# --------------------------------------------------------------------------- #


async def test_persistent_very_low_level_calls_back_exactly_once(make_filter):
    calls: list[int] = []

    async def on_low() -> None:
        calls.append(1)

    flt = make_filter(on_low_level=on_low)
    await run_filter(flt, speechlike(9.0, -62.0), CHUNK)
    assert calls == [1]


@pytest.mark.parametrize("dbfs", [-30.0, -46.0])
async def test_ordinary_or_merely_quiet_level_never_calls_back(make_filter, dbfs):
    """−46 dBFS 的说话声虽然轻，自动增益补得上，不该吓用户。"""
    calls: list[int] = []

    async def on_low() -> None:
        calls.append(1)

    flt = make_filter(on_low_level=on_low)
    await run_filter(flt, speechlike(9.0, dbfs), CHUNK)
    assert calls == []


async def test_short_quiet_stretch_does_not_call_back(make_filter):
    calls: list[int] = []

    async def on_low() -> None:
        calls.append(1)

    flt = make_filter(on_low_level=on_low)
    await run_filter(flt, speechlike(3.0, -62.0), CHUNK)  # 不到约 5 秒
    await run_filter(flt, speechlike(3.0, -30.0), CHUNK)
    await run_filter(flt, speechlike(3.0, -62.0), CHUNK)  # 中间恢复过，计时清零
    assert calls == []


async def test_a_failing_callback_does_not_break_the_audio(make_filter):
    async def on_low() -> None:
        raise RuntimeError("发提示失败")

    flt = make_filter(on_low_level=on_low)
    pcm = speechlike(9.0, -62.0)
    out = await run_filter(flt, pcm, CHUNK)
    assert len(out) == len(pcm)


async def test_level_is_logged_periodically_by_audio_time(make_filter):
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="INFO")
    try:
        flt = make_filter(level_log_secs=1.0, max_gain_db=30.0)
        await run_filter(flt, speechlike(2.5, -45.0), CHUNK)
    finally:
        logger.remove(sink)
    level_lines = [x for x in lines if "输入电平" in x]
    assert len(level_lines) == 2  # 2.5 秒音频、每 1 秒一行
    assert "dBFS" in level_lines[0] and "增益" in level_lines[0]


async def test_level_log_says_so_when_nothing_is_heard(make_filter):
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="INFO")
    try:
        flt = make_filter(level_log_secs=1.0)
        await run_filter(flt, speechlike(1.2, -90.0), CHUNK)
    finally:
        logger.remove(sink)
    assert any("没有高于" in x for x in lines)


async def test_level_log_can_be_turned_off(make_filter):
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="INFO")
    try:
        flt = make_filter(level_log_secs=0.0)
        await run_filter(flt, speechlike(3.0, -45.0), CHUNK)
    finally:
        logger.remove(sink)
    assert not any("输入电平" in x for x in lines)


# --------------------------------------------------------------------------- #
# 诊断工具
# --------------------------------------------------------------------------- #


def test_scale_db_changes_the_level_by_the_requested_amount():
    pcm = speechlike(1.0, -20.0)
    assert rms_dbfs(scale_db(pcm, -36.0)) == pytest.approx(rms_dbfs(pcm) - 36.0, abs=0.2)
    assert scale_db(pcm, 0.0) == pcm


def test_load_pcm16k_resamples_and_mixes_down(tmp_path):
    import soundfile as sf

    rate = 24000
    t = np.arange(rate) / rate
    stereo = np.stack([0.3 * np.sin(2 * math.pi * 440 * t), 0.3 * np.sin(2 * math.pi * 440 * t)], 1)
    path = tmp_path / "x.wav"
    sf.write(path, stereo, rate, subtype="PCM_16")
    pcm = load_pcm16k(path)
    assert len(pcm) // 2 == pytest.approx(SR, abs=2)  # 1 秒 → 16000 个采样
    assert rms_dbfs(pcm) == pytest.approx(20 * math.log10(0.3 / math.sqrt(2)), abs=0.5)


async def test_gain_variants_include_each_fixed_gain_and_optionally_auto():
    pcm = speechlike(2.0, -55.0)
    variants = await gain_variants(pcm, (0.0, 12.0), AudioConfig(max_gain_db=40.0))
    assert [label for label, _ in variants] == ["+0 dB", "+12 dB", "自动增益"]
    levels = {label: rms_dbfs(v) for label, v in variants}
    assert levels["+12 dB"] - levels["+0 dB"] == pytest.approx(12.0, abs=0.1)
    assert levels["自动增益"] > levels["+0 dB"] + 10  # 自动增益把很轻的输入拉了上来
    assert [label for label, _ in await gain_variants(pcm, (0.0,))] == ["+0 dB"]


async def test_sweep_covers_every_combination_and_formats_a_table():
    pcm = speechlike(2.0, -45.0)
    rows = await sweep(pcm, gains_db=(0.0, 12.0), min_volumes=(0.6, 0.0), audio_cfg=AudioConfig())
    assert [(r.label, r.min_volume) for r in rows] == [
        ("+0 dB", 0.6),
        ("+0 dB", 0.0),
        ("+12 dB", 0.6),
        ("+12 dB", 0.0),
        ("自动增益", 0.6),
        ("自动增益", 0.0),
    ]
    assert all(0.0 <= r.stats.speech_ratio <= 1.0 for r in rows)
    table = format_sweep(rows)
    assert "自动增益" in table and "说话占比" in table
    assert len(table.splitlines()) == 2 + len(rows)


# --------------------------------------------------------------------------- #
# 回归：复现真实麦克风上遇到的现象
# --------------------------------------------------------------------------- #

JFK = REPO_ROOT / "third_party/NeMo-Speech.cpp/test_files/asr/wav/test/jfk.wav"


@pytest.mark.skipif(not JFK.is_file(), reason="第三方样例音频不在（子模块未检出）")
async def test_attenuated_real_speech_survives_vad_only_with_the_gain_stage(make_filter):
    """同一段真人语音衰减 36 dB（RMS 约 −53 dBFS）：不加增益时音量门限把它切光，加了增益后基本恢复。

    只用 Pipecat 自带的 Silero 模型，不联网、不需要权重。数字见 docs/benchmarks.md。
    """
    original = load_pcm16k(JFK)
    quiet = scale_db(original, -36.0)

    baseline = await vad_stats(original, min_volume=0.6)
    without = await vad_stats(quiet, min_volume=0.6)
    boosted = await run_filter(make_filter(), quiet, CHUNK)
    with_gain = await vad_stats(boosted, min_volume=0.6)

    assert baseline.speech_ratio > 0.3  # 样本本身确实有足够的语音
    assert without.speech_ratio < 0.3 * baseline.speech_ratio  # 现象复现
    assert with_gain.speech_ratio >= 0.8 * baseline.speech_ratio  # 对策有效


async def test_the_default_target_is_the_louder_one(make_filter):
    # 目标电平默认 −16 dBFS（−22 时实测声音偏小）
    flt = make_filter(max_gain_db=40.0)
    assert flt.target_dbfs == -16.0
    out = await run_filter(flt, speechlike(4.0, -50.0), CHUNK)
    assert loud_syllable_level(out[-SR * 2 :]) == pytest.approx(-16.0, abs=3.0)
    assert np.abs(np.frombuffer(out, dtype="<i2")).max() < 32767  # 更响也不削波
