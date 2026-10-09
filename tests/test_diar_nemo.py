"""说话人区分的正式实现（diar/nemo_ctypes.py）与工厂（diar/__init__.py）。

不需要权重：异步外壳用一个假的同步绑定来测（线程、顺序、换算、过滤、错误）；ctypes 绑定本身只测
「函数签名都设置了」和「模型路径不存在时报出库返回的错误文字」，动态库找不到就跳过。
真实模型的测试标记为 ``gpu``。
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from loguru import logger

from agentic_meeting.config import AppConfig
from agentic_meeting.diar import build_diarizer
from agentic_meeting.diar.base import NullDiarizer
from agentic_meeting.diar.nemo_ctypes import NemoDiarizer, bind_library, load_library
from agentic_meeting.services.paths import find_library
from agentic_meeting.types import SpeakerSegment


class FakeBinding:
    """同步绑定的替身：记录每次调用发生在哪个线程、收到了什么。"""

    def __init__(self, *, segments=None, open_error: Exception | None = None, speakers: int = 4):
        self.thread_ids: set[int] = set()
        self.calls: list[str] = []
        self.pushed: list[np.ndarray] = []
        self.max_speakers = 0
        self._speakers = speakers
        self._segments = segments or []
        self._open_error = open_error
        self.closed = 0
        self._active = 0
        self.max_active = 0  # 同一时刻有几个调用在里面：同一条流不允许并发访问

    def _enter(self, name: str) -> None:
        self.thread_ids.add(threading.get_ident())
        self.calls.append(name)

    def open(self) -> None:
        self._enter("open")
        if self._open_error:
            raise self._open_error
        self.max_speakers = self._speakers

    def push(self, audio: np.ndarray) -> None:
        self._enter("push")
        self._active += 1
        self.max_active = max(self.max_active, self._active)
        time.sleep(0.002)  # 给并发留出重叠的机会
        self.pushed.append(audio)
        self._active -= 1

    def segments(self) -> list[SpeakerSegment]:
        self._enter("segments")
        return list(self._segments)

    def labeled_secs(self) -> float:
        self._enter("labeled_secs")
        return 1.5

    def finish(self) -> None:
        self._enter("finish")

    def close(self) -> None:
        self._enter("close")
        self.closed += 1


def pcm(*samples: int) -> bytes:
    return np.array(samples, dtype="<i2").tobytes()


@pytest.fixture
def cfg(make_cfg) -> AppConfig:
    return make_cfg()


async def started(cfg, binding: FakeBinding) -> NemoDiarizer:
    d = NemoDiarizer(cfg, binding_factory=lambda: binding)
    await d.start()
    return d


# --------------------------------------------------------------------------- #
# 异步外壳
# --------------------------------------------------------------------------- #


async def test_all_native_calls_run_on_one_worker_thread_off_the_event_loop(cfg):
    binding = FakeBinding()
    d = await started(cfg, binding)
    await d.push_audio(pcm(1, 2, 3))
    await d.segments()
    await d.labeled_secs()
    await d.finish()
    await d.close()
    assert binding.calls == ["open", "push", "segments", "labeled_secs", "finish", "close"]
    assert len(binding.thread_ids) == 1  # 底层的错误信息是线程局部的，必须始终同一个线程
    assert binding.thread_ids != {threading.get_ident()}  # 阻塞调用不在事件循环线程里


async def test_pcm16_is_converted_to_float32_in_range(cfg):
    binding = FakeBinding()
    d = await started(cfg, binding)
    await d.push_audio(pcm(0, 16384, -16384, 32767, -32768))
    (pushed,) = binding.pushed
    assert pushed.dtype == np.float32
    assert pushed.tolist() == pytest.approx([0.0, 0.5, -0.5, 32767 / 32768, -1.0])
    await d.close()


async def test_empty_audio_is_not_forwarded(cfg):
    binding = FakeBinding()
    d = await started(cfg, binding)
    await d.push_audio(b"")
    assert binding.pushed == []
    await d.close()


async def test_audio_reaches_the_model_in_order_even_when_pushed_concurrently(cfg):
    binding = FakeBinding()
    d = await started(cfg, binding)
    await asyncio.gather(*(d.push_audio(pcm(i + 1)) for i in range(30)))
    assert [int(round(p[0] * 32768)) for p in binding.pushed] == list(range(1, 31))
    assert binding.max_active == 1 and len(binding.thread_ids) == 1  # 串行执行，不并发
    await d.close()


async def test_segments_are_filtered_by_end_time_and_sorted_by_start(cfg):
    unsorted = [
        SpeakerSegment(9.0, 12.0, 2),
        SpeakerSegment(0.0, 4.0, 1),
        SpeakerSegment(5.0, 8.0, 1),
        SpeakerSegment(8.0, 10.0, 3),
    ]
    d = await started(cfg, FakeBinding(segments=unsorted))
    assert [(s.start_secs, s.speaker) for s in await d.segments()] == [
        (0.0, 1),
        (5.0, 1),
        (8.0, 3),
        (9.0, 2),
    ]
    # 只要结束时间晚于 since_secs 的：恰好等于的不算
    assert [(s.start_secs, s.speaker) for s in await d.segments(since_secs=8.0)] == [
        (8.0, 3),
        (9.0, 2),
    ]
    assert await d.segments(since_secs=12.0) == []
    await d.close()


async def test_max_speakers_comes_from_the_model(cfg):
    d = NemoDiarizer(cfg, binding_factory=lambda: FakeBinding(speakers=4))
    assert d.max_speakers == 0
    await d.start()
    assert d.max_speakers == 4
    await d.close()


async def test_start_failure_propagates_and_the_worker_is_released(cfg):
    binding = FakeBinding(open_error=RuntimeError("create 失败（状态码 2）：模型文件不存在"))
    d = NemoDiarizer(cfg, binding_factory=lambda: binding)
    with pytest.raises(RuntimeError, match="模型文件不存在"):
        await d.start()
    await d.close()  # 启动失败后关闭也不能抛


async def test_use_before_start_or_after_close_is_an_error(cfg):
    d = NemoDiarizer(cfg, binding_factory=FakeBinding)
    with pytest.raises(RuntimeError, match="尚未启动"):
        await d.push_audio(pcm(1))
    await d.start()
    await d.close()
    with pytest.raises(RuntimeError, match="已关闭"):
        await d.push_audio(pcm(1))
    await d.close()  # 重复关闭无害


async def test_start_twice_is_rejected(cfg):
    d = await started(cfg, FakeBinding())
    with pytest.raises(RuntimeError):
        await d.start()
    await d.close()


async def test_close_releases_the_native_resources_once(cfg):
    binding = FakeBinding()
    d = await started(cfg, binding)
    await d.close()
    await d.close()
    assert binding.closed == 1


# --------------------------------------------------------------------------- #
# 工厂：失败时降级
# --------------------------------------------------------------------------- #


async def test_build_diarizer_none_backend_gives_the_null_diarizer(cfg):
    cfg.diarization.backend = "none"
    d = await build_diarizer(cfg, binding_factory=lambda: pytest.fail("不该加载任何东西"))
    assert isinstance(d, NullDiarizer)


async def test_build_diarizer_returns_a_started_diarizer(cfg):
    binding = FakeBinding()
    d = await build_diarizer(cfg, binding_factory=lambda: binding)
    assert isinstance(d, NemoDiarizer) and d.max_speakers == 4
    assert binding.calls == ["open"]
    await d.close()


async def test_build_diarizer_degrades_to_null_and_tells_the_user(cfg):
    notices: list[tuple[str, str]] = []

    async def notice(level: str, text: str) -> None:
        notices.append((level, text))

    logged: list[str] = []
    sink = logger.add(lambda m: logged.append(str(m)), level="ERROR")
    try:
        binding = FakeBinding(open_error=RuntimeError("create 失败（状态码 2）：打不开模型"))
        d = await build_diarizer(cfg, binding_factory=lambda: binding, on_notice=notice)
    finally:
        logger.remove(sink)
    assert isinstance(d, NullDiarizer)
    assert binding.closed == 1  # 已经创建了一半的资源要释放
    assert len(notices) == 1 and notices[0][0] == "warn"
    assert "不分说话人" in notices[0][1] and "打不开模型" in notices[0][1]
    assert any("打不开模型" in line for line in logged)


async def test_build_diarizer_survives_a_failing_notice_callback(cfg):
    async def notice(level: str, text: str) -> None:
        raise RuntimeError("发不出去")

    d = await build_diarizer(
        cfg, binding_factory=lambda: FakeBinding(open_error=RuntimeError("坏了")), on_notice=notice
    )
    assert isinstance(d, NullDiarizer)


async def test_missing_library_also_degrades(cfg, tmp_path):
    cfg.diarization.library_path = str(tmp_path / "不存在.dll")
    d = await build_diarizer(cfg)  # 默认工厂：真的去找动态库
    assert isinstance(d, NullDiarizer)


# --------------------------------------------------------------------------- #
# ctypes 绑定（需要动态库本身，不需要权重）
# --------------------------------------------------------------------------- #


def _library() -> Path | None:
    try:
        return find_library("nemo_speech_asr_c")
    except FileNotFoundError:
        return None


LIBRARY = _library()
needs_library = pytest.mark.skipif(LIBRARY is None, reason="runtimes/ 下没有 nemo_speech 动态库")

EXPORTED = [
    "nemo_speech_asr_last_error",
    "nemo_speech_diar_create",
    "nemo_speech_diar_destroy",
    "nemo_speech_diar_num_speakers",
    "nemo_speech_diar_seconds_per_frame",
    "nemo_speech_diar_stream_open",
    "nemo_speech_diar_stream_push_f32",
    "nemo_speech_diar_stream_finish",
    "nemo_speech_diar_stream_close",
    "nemo_speech_diar_frame_count",
    "nemo_speech_diar_segments",
]


@needs_library
def test_every_function_has_argtypes_and_restype_set():
    lib = load_library(str(LIBRARY))
    bind_library(lib)
    for name in EXPORTED:
        fn = getattr(lib, name)
        assert fn.argtypes is not None, f"{name} 没有设置 argtypes（64 位指针会被截断）"
    assert lib.nemo_speech_diar_destroy.restype is None
    assert lib.nemo_speech_diar_stream_close.restype is None


@needs_library
async def test_a_missing_model_path_reports_the_librarys_own_error(cfg, tmp_path):
    cfg.diarization.model_path = str(tmp_path / "不存在的模型.gguf")
    d = NemoDiarizer(cfg)
    with pytest.raises(RuntimeError) as error:
        await d.start()
    message = str(error.value)
    assert "create" in message and "状态码" in message
    assert message.split("：", 1)[1].strip(), "库返回的错误文字不应为空"
    await d.close()


# --------------------------------------------------------------------------- #
# 真实模型（pytest -m gpu 运行；用环境变量 AGENTIC_MEETING_TEST_WAV 指定一段多人对话的录音）
# --------------------------------------------------------------------------- #

MEETING_WAV = Path(os.environ.get("AGENTIC_MEETING_TEST_WAV", ""))
HAS_MEETING_WAV = MEETING_WAV.is_file()


@pytest.mark.gpu
async def test_real_model_finds_at_least_two_speakers(tmp_path):
    from agentic_meeting.audio.diagnose import load_pcm16k
    from agentic_meeting.config import load_config

    cfg = load_config()
    if cfg.diarization.backend == "none" or not cfg.diarization.model_path:
        pytest.skip("config.toml 里没有配置说话人区分模型")
    if not HAS_MEETING_WAV:
        pytest.skip("没有用 AGENTIC_MEETING_TEST_WAV 指定录音")
    audio = load_pcm16k(MEETING_WAV)
    d = NemoDiarizer(cfg)
    await d.start()
    try:
        step = 16000 * 2 // 3  # 约 0.3 秒一块
        for i in range(0, len(audio), step):
            await d.push_audio(audio[i : i + step])
        await d.finish()
        segments = await d.segments()
    finally:
        await d.close()
    assert len({s.speaker for s in segments}) >= 2
