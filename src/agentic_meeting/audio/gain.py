"""入口收音增强：自动增益。

麦克风电平偏低时，Pipecat 语音检测的音量门限（``min_volume=0.6``，约 −50 LUFS）会把说话切成碎片，
识别服务只拿到碎片（实测见 docs/benchmarks.md）。这个滤波器挂在传输输入的 ``audio_in_filter`` 上，
先滤波、再把帧推向下游，所以语音检测、识别、会议记录器（说话人区分）看到的是同一份处理后的音频。

约束（docs/pipecat-notes.md §3.1.1）：

* **逐帧等长输出**。会话时间轴按采样数计数（architecture.md §3），不能丢帧、不能改长度、不能返回空字节。
* **转录链路优先存活**：任何异常都原样放行这一帧。
* 只做轻量的 numpy 运算（它跑在事件循环里，一帧 10–20 毫秒）。

算法：

1. 每帧算增益前的 RMS。低于 ``noise_floor_dbfs`` 的帧视为静音，不参与调整。
2. 其余帧更新「说话电平」包络：上升快（30 毫秒）、下降慢（1.5 秒），所以音节之间的短暂停顿不会把增益拉高；
   它跟踪的是**较响音节**的电平，``target_dbfs`` 指的也是它。
3. 期望增益 = ``target_dbfs − 包络``，限制在 0…``max_gain_db``（只增不减），再对增益做 50 毫秒的平滑；
   帧内对增益做线性插值，避免爆音。静音时增益保持不变，下一句开头已经在合适的增益上。
4. 增益之后过软限幅（0.8 满幅以上用 tanh 压向满幅），整型不会回绕。增益为 0 dB 时逐字节原样输出。
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable

import numpy as np
from loguru import logger
from pipecat.audio.filters.base_audio_filter import BaseAudioFilter
from pipecat.frames.frames import FilterControlFrame

from agentic_meeting.asr.base import ASR_SAMPLE_RATE
from agentic_meeting.config import AudioConfig

# 经验值：包络上升要快（跟上句首），下降要慢（句内停顿不把增益拉高）。
ENV_ATTACK_SECS = 0.03  # 说话电平包络上升的时间常数
ENV_RELEASE_SECS = 1.5  # 下降的时间常数
GAIN_SMOOTH_SECS = 0.05  # 增益本身的平滑
LIMITER_KNEE = 0.8  # 满幅的比例，超过这里开始软限幅

# 增益前的说话电平低于它（约在语音检测音量门限附近），并持续 LOW_LEVEL_SECS，就算「麦克风音量过低」。
LOW_INPUT_DBFS = -55.0
LOW_LEVEL_SECS = 5.0

_EPS = 1e-9
_ERROR_LOG_EVERY = 100  # 异常按帧限频：第 1 次和之后每 100 次记一条


def _db(amplitude: float) -> float:
    return 20.0 * math.log10(max(amplitude, _EPS))


def _soft_limit(y: np.ndarray) -> np.ndarray:
    magnitude = np.abs(y)
    over = magnitude > LIMITER_KNEE
    if not over.any():
        return y
    squeezed = LIMITER_KNEE + (1.0 - LIMITER_KNEE) * np.tanh(
        (magnitude - LIMITER_KNEE) / (1.0 - LIMITER_KNEE)
    )
    return np.where(over, np.sign(y) * squeezed, y)


class InputGainFilter(BaseAudioFilter):
    """入口自动增益（也可以只做固定增益）。构造参数对应配置 ``[audio]``，见 ``config.AudioConfig``。"""

    def __init__(
        self,
        *,
        auto_gain: bool = True,
        gain_db: float = 0.0,
        max_gain_db: float = 30.0,
        target_dbfs: float = -16.0,
        noise_floor_dbfs: float = -70.0,
        level_log_secs: float = 10.0,
        on_low_level: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.auto_gain = auto_gain
        self.max_gain_db = max_gain_db
        self.target_dbfs = target_dbfs
        self.noise_floor_dbfs = noise_floor_dbfs
        self.level_log_secs = level_log_secs
        self._on_low_level = on_low_level
        self._rate = ASR_SAMPLE_RATE
        self._gain_db = float(gain_db)
        self._env_db: float | None = None  # 说话电平包络（增益前），还没听到说话时为 None
        # 周期性日志的统计
        self._log_secs = 0.0
        self._active_energy = 0.0
        self._active_samples = 0
        self._total_samples = 0
        self._peak = 0.0
        # 电平过低提示
        self._low_secs = 0.0
        self._low_reported = False
        self._low_pending = False
        self._errors = 0

    @classmethod
    def from_config(
        cls, audio: AudioConfig, on_low_level: Callable[[], Awaitable[None]] | None = None
    ) -> InputGainFilter:
        return cls(
            auto_gain=audio.auto_gain,
            gain_db=audio.gain_db,
            max_gain_db=audio.max_gain_db,
            target_dbfs=audio.target_dbfs,
            noise_floor_dbfs=audio.noise_floor_dbfs,
            level_log_secs=audio.level_log_secs,
            on_low_level=on_low_level,
        )

    # ---- BaseAudioFilter ----

    async def start(self, sample_rate: int) -> None:
        self._rate = sample_rate

    async def stop(self) -> None:
        return None

    async def process_frame(self, frame: FilterControlFrame) -> None:
        return None

    async def filter(self, audio: bytes) -> bytes:
        if not audio:
            return audio
        try:
            out = self._process(audio)
        except Exception:
            if self._errors % _ERROR_LOG_EVERY == 0:
                logger.exception("收音增强出错，这一帧原样放行")
            self._errors += 1
            return audio
        if self._low_pending:
            self._low_pending = False
            await self._notify_low_level()
        return out

    # ---- 状态 ----

    @property
    def gain_db(self) -> float:
        """当前增益（dB）。"""
        return self._gain_db

    @property
    def is_noop(self) -> bool:
        return not self.auto_gain and self._gain_db == 0.0

    # ---- 处理 ----

    def _process(self, audio: bytes) -> bytes:
        samples = np.frombuffer(audio, dtype="<i2", count=len(audio) // 2)
        n = len(samples)
        if n == 0:
            return audio
        x = samples.astype(np.float32) / 32768.0
        seconds = n / self._rate
        energy = float(np.dot(x, x))
        rms_db = _db(math.sqrt(energy / n))

        self._observe(x, energy, rms_db, seconds)
        before = self._gain_db
        after = self._next_gain(seconds)
        self._gain_db = after
        self._maybe_log(seconds)

        if abs(before) < 1e-3 and abs(after) < 1e-3:
            return audio  # 0 dB：逐字节原样，连限幅器都不碰
        ramp = np.linspace(10 ** (before / 20), 10 ** (after / 20), n, dtype=np.float32)
        y = _soft_limit(x * ramp)
        return np.clip(np.rint(y * 32768.0), -32768, 32767).astype("<i2").tobytes()

    def _observe(self, x: np.ndarray, energy: float, rms_db: float, seconds: float) -> None:
        n = len(x)
        self._total_samples += n
        self._peak = max(self._peak, float(np.max(np.abs(x))))
        if rms_db <= self.noise_floor_dbfs:
            return
        self._active_energy += energy
        self._active_samples += n
        if self._env_db is None:
            self._env_db = rms_db
        else:
            tau = ENV_ATTACK_SECS if rms_db > self._env_db else ENV_RELEASE_SECS
            self._env_db += (1.0 - math.exp(-seconds / tau)) * (rms_db - self._env_db)
        if self._env_db < LOW_INPUT_DBFS:
            self._low_secs += seconds
            if self._low_secs >= LOW_LEVEL_SECS and not self._low_reported:
                self._low_reported = True
                self._low_pending = True
        else:
            self._low_secs = 0.0

    def _next_gain(self, seconds: float) -> float:
        if not self.auto_gain or self._env_db is None:
            return self._gain_db
        desired = min(max(self.target_dbfs - self._env_db, 0.0), self.max_gain_db)
        alpha = 1.0 - math.exp(-seconds / GAIN_SMOOTH_SECS)
        return self._gain_db + alpha * (desired - self._gain_db)

    # ---- 日志与提示 ----

    def _maybe_log(self, seconds: float) -> None:
        if self.level_log_secs <= 0:
            return
        self._log_secs += seconds
        if self._log_secs < self.level_log_secs:
            return
        window = self._log_secs
        if self._active_samples == 0:
            logger.info(
                f"输入电平：近 {window:.0f} 秒没有高于 {self.noise_floor_dbfs:.0f} dBFS 的声音"
                "（麦克风静音、没选对设备，或音量过低）"
            )
        else:
            rms = _db(math.sqrt(self._active_energy / self._active_samples))
            ratio = self._active_samples / max(self._total_samples, 1)
            mode = (
                f"当前增益 {self._gain_db:+.1f} dB（上限 {self.max_gain_db:+.0f} dB）"
                if self.auto_gain
                else f"固定增益 {self._gain_db:+.1f} dB"
            )
            logger.info(
                f"输入电平：有声部分 RMS {rms:.1f} dBFS，峰值 {_db(self._peak):.1f} dBFS，"
                f"有声占比 {ratio:.0%}，{mode}"
            )
        self._log_secs = 0.0
        self._active_energy = 0.0
        self._active_samples = 0
        self._total_samples = 0
        self._peak = 0.0

    async def _notify_low_level(self) -> None:
        logger.warning(
            "麦克风音量过低（说话电平低于 −55 dBFS 已持续数秒），请调高系统的输入音量或靠近麦克风"
        )
        if self._on_low_level is None:
            return
        try:
            await self._on_low_level()
        except Exception:
            logger.exception("发送「麦克风音量过低」提示失败")
