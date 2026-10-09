"""说话人区分的正式实现：用 ctypes 调 NeMo-Speech.cpp 动态库的流式 C 接口（docs/interfaces.md §4.2）。

分两层：

* ``CtypesBinding``：同步、薄薄一层 ctypes；
* ``NemoDiarizer``：实现 ``diar/base.py`` 的异步 ``Diarizer`` 协议。**所有底层调用都放进同一个单线程的
  ``ThreadPoolExecutor``**——同一条流不允许并发访问，库的 ``last_error`` 又是线程局部的，必须在出错的同一线程里读；
  同时也不阻塞事件循环。

注意：只用动态库，不要运行 ``nemo-speech`` 命令行（它会自动联网下载权重）。
"""

from __future__ import annotations

import asyncio
import ctypes as C
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

import numpy as np
from loguru import logger

from agentic_meeting.asr.base import ASR_SAMPLE_RATE
from agentic_meeting.config import AppConfig
from agentic_meeting.services.paths import find_library
from agentic_meeting.types import SpeakerSegment

LIBRARY_STEM = "nemo_speech_asr_c"


# --------------------------------------------------------------------------- #
# C 结构体（字段顺序与类型必须与头文件一致，interfaces.md §4.2）
# --------------------------------------------------------------------------- #


class DiarModelConfig(C.Structure):
    _fields_ = [
        ("size", C.c_size_t),
        ("model_path", C.c_char_p),
        ("gpu", C.c_int32),
        ("preset", C.c_char_p),
        ("chunk_frames", C.c_int32),
        ("right_context_frames", C.c_int32),
        ("left_context_frames", C.c_int32),
        ("fifo_frames", C.c_int32),
        ("spkcache_frames", C.c_int32),
        ("update_period_frames", C.c_int32),
    ]


class DiarSegmentationConfig(C.Structure):
    _fields_ = [
        ("size", C.c_size_t),
        ("onset", C.c_float),
        ("offset", C.c_float),
        ("pad_onset_sec", C.c_double),
        ("pad_offset_sec", C.c_double),
        ("min_gap_sec", C.c_double),
        ("min_duration_sec", C.c_double),
    ]


class DiarSegment(C.Structure):
    _fields_ = [("start_time", C.c_double), ("end_time", C.c_double), ("speaker", C.c_int32)]


def load_library(path: str) -> C.CDLL:
    """加载动态库。Windows 上它依赖同目录下的其他动态库，必须先登记目录。"""
    if os.name == "nt":
        os.add_dll_directory(os.path.dirname(os.path.abspath(path)))
    return C.CDLL(path)


def bind_library(lib: C.CDLL) -> None:
    """给用到的每个函数设置参数与返回类型。不设的话 64 位指针会被截断。"""
    ptr = C.c_void_p
    lib.nemo_speech_asr_last_error.restype = C.c_char_p
    lib.nemo_speech_asr_last_error.argtypes = []
    lib.nemo_speech_diar_create.restype = C.c_int
    lib.nemo_speech_diar_create.argtypes = [C.POINTER(DiarModelConfig), C.POINTER(ptr)]
    lib.nemo_speech_diar_destroy.restype = None
    lib.nemo_speech_diar_destroy.argtypes = [ptr]
    lib.nemo_speech_diar_num_speakers.restype = C.c_int32
    lib.nemo_speech_diar_num_speakers.argtypes = [ptr]
    lib.nemo_speech_diar_seconds_per_frame.restype = C.c_double
    lib.nemo_speech_diar_seconds_per_frame.argtypes = [ptr]
    lib.nemo_speech_diar_stream_open.restype = C.c_int
    lib.nemo_speech_diar_stream_open.argtypes = [ptr, C.POINTER(ptr)]
    lib.nemo_speech_diar_stream_push_f32.restype = C.c_int
    lib.nemo_speech_diar_stream_push_f32.argtypes = [
        ptr,
        C.POINTER(C.c_float),
        C.c_size_t,
        C.c_int32,
    ]
    lib.nemo_speech_diar_stream_finish.restype = C.c_int
    lib.nemo_speech_diar_stream_finish.argtypes = [ptr]
    lib.nemo_speech_diar_stream_close.restype = None
    lib.nemo_speech_diar_stream_close.argtypes = [ptr]
    lib.nemo_speech_diar_frame_count.restype = C.c_int64
    lib.nemo_speech_diar_frame_count.argtypes = [ptr]
    lib.nemo_speech_diar_segments.restype = C.c_int
    lib.nemo_speech_diar_segments.argtypes = [
        ptr,
        C.POINTER(DiarSegmentationConfig),
        C.POINTER(DiarSegment),
        C.c_size_t,
        C.POINTER(C.c_size_t),
    ]


# --------------------------------------------------------------------------- #
# 同步绑定
# --------------------------------------------------------------------------- #


class DiarBinding(Protocol):
    """同步绑定的接口（``NemoDiarizer`` 只依赖它，测试里换成假的）。所有方法必须在同一个线程里调用。"""

    max_speakers: int

    def open(self) -> None: ...
    def push(self, audio: np.ndarray) -> None: ...
    def segments(self) -> list[SpeakerSegment]: ...
    def labeled_secs(self) -> float: ...
    def finish(self) -> None: ...
    def close(self) -> None: ...


class CtypesBinding:
    """真正调动态库的同步实现。"""

    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._lib: C.CDLL | None = None
        self._model = C.c_void_p()
        self._stream = C.c_void_p()
        self._seconds_per_frame = 0.0
        self.max_speakers = 0
        # 字符串要保存在实例上，保证调用期间不被回收。
        self._model_path = str(cfg.resolve(cfg.diarization.model_path)).encode("utf-8")
        preset = cfg.diarization.preset
        self._preset = preset.encode("utf-8") if preset else None
        seg = cfg.diarization.segmentation
        self._segmentation = DiarSegmentationConfig(
            C.sizeof(DiarSegmentationConfig),
            seg.onset,
            seg.offset,
            seg.pad_onset_secs,
            seg.pad_offset_secs,
            seg.min_gap_secs,
            seg.min_duration_secs,
        )

    def _check(self, status: int, what: str) -> None:
        if status != 0:
            assert self._lib is not None
            message = self._lib.nemo_speech_asr_last_error() or b""
            raise RuntimeError(
                f"{what} 失败（状态码 {status}）：{message.decode('utf-8', 'replace')}"
            )

    def open(self) -> None:
        path = find_library(LIBRARY_STEM, self._cfg.diarization.library_path)
        self._lib = load_library(str(path))
        bind_library(self._lib)
        lib = self._lib

        conf = DiarModelConfig()
        conf.size = C.sizeof(DiarModelConfig)
        conf.model_path = self._model_path
        conf.gpu = self._cfg.diarization.gpu
        conf.preset = self._preset
        conf.left_context_frames = -1  # 这个字段 0 是有效取值，-1 才表示沿用预设

        self._check(lib.nemo_speech_diar_create(C.byref(conf), C.byref(self._model)), "create")
        self._check(lib.nemo_speech_diar_stream_open(self._model, C.byref(self._stream)), "open")
        self.max_speakers = int(lib.nemo_speech_diar_num_speakers(self._model))
        self._seconds_per_frame = float(lib.nemo_speech_diar_seconds_per_frame(self._model))

    def push(self, audio: np.ndarray) -> None:
        """送入 16 kHz 单声道 float32。必须按顺序送入全部音频（包括静音）。"""
        assert self._lib is not None
        data = np.ascontiguousarray(audio, dtype=np.float32)
        pointer = data.ctypes.data_as(C.POINTER(C.c_float))
        self._check(
            self._lib.nemo_speech_diar_stream_push_f32(
                self._stream, pointer, len(data), ASR_SAMPLE_RATE
            ),
            "push",
        )

    def finish(self) -> None:
        assert self._lib is not None
        self._check(self._lib.nemo_speech_diar_stream_finish(self._stream), "finish")

    def labeled_secs(self) -> float:
        """已经标注到的时刻（比音频晚 0.6–1.1 秒）。"""
        assert self._lib is not None
        return self._lib.nemo_speech_diar_frame_count(self._stream) * self._seconds_per_frame

    def segments(self) -> list[SpeakerSegment]:
        """两段式调用：先问条数，再按条数取。"""
        assert self._lib is not None
        count = C.c_size_t(0)
        conf = C.byref(self._segmentation)
        self._check(
            self._lib.nemo_speech_diar_segments(self._stream, conf, None, 0, C.byref(count)),
            "segments",
        )
        if count.value == 0:
            return []
        buf = (DiarSegment * count.value)()
        self._check(
            self._lib.nemo_speech_diar_segments(
                self._stream, conf, buf, count.value, C.byref(count)
            ),
            "segments",
        )
        return [SpeakerSegment(s.start_time, s.end_time, s.speaker) for s in buf[: count.value]]

    def close(self) -> None:
        if self._lib is None:
            return
        if self._stream:
            self._lib.nemo_speech_diar_stream_close(self._stream)
            self._stream = C.c_void_p()
        if self._model:
            self._lib.nemo_speech_diar_destroy(self._model)
            self._model = C.c_void_p()


# --------------------------------------------------------------------------- #
# 异步外壳
# --------------------------------------------------------------------------- #


class NemoDiarizer:
    """实现 ``diar/base.py`` 的 ``Diarizer`` 协议。一路音频对应一个实例。"""

    def __init__(
        self, cfg: AppConfig, *, binding_factory: Callable[[], DiarBinding] | None = None
    ) -> None:
        self._cfg = cfg
        self._binding_factory = binding_factory or (lambda: CtypesBinding(cfg))
        self._binding: DiarBinding | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="diarizer")
        self._started = False
        self._closed = False
        self._max_speakers = 0

    @property
    def max_speakers(self) -> int:
        return self._max_speakers

    async def _run(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._executor, fn, *args)

    def _ready(self) -> DiarBinding:
        if self._closed:
            raise RuntimeError("说话人区分已关闭")
        if not self._started or self._binding is None:
            raise RuntimeError("说话人区分尚未启动")
        return self._binding

    async def start(self) -> None:
        if self._started or self._closed:
            raise RuntimeError("说话人区分已经启动过或已关闭，不能再次启动")
        binding = self._binding_factory()
        self._binding = binding  # 先登记，open 失败时 close 才能释放创建了一半的资源
        await self._run(binding.open)
        self._max_speakers = binding.max_speakers
        self._started = True

    async def push_audio(self, pcm16: bytes) -> None:
        binding = self._ready()
        if not pcm16:
            return
        audio = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        await self._run(binding.push, audio)

    async def segments(self, since_secs: float = 0.0) -> list[SpeakerSegment]:
        binding = self._ready()
        found: list[SpeakerSegment] = await self._run(binding.segments)
        return sorted((s for s in found if s.end_secs > since_secs), key=lambda s: s.start_secs)

    async def labeled_secs(self) -> float:
        return float(await self._run(self._ready().labeled_secs))

    async def finish(self) -> None:
        """流结束时调用，让模型把最后几帧也标注出来。"""
        await self._run(self._ready().finish)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        binding, self._binding = self._binding, None
        try:
            if binding is not None:
                await self._run(binding.close)
        except Exception:
            logger.exception("释放说话人区分资源时出错")
        finally:
            self._executor.shutdown(wait=False)
