"""接收浏览器上传的截图：校验、落盘、入库（docs/interfaces.md §5.3、docs/architecture.md §5.3）。

* 图片用 Pillow 解码校验并取宽高——浏览器报的类型和尺寸都不可信。解码放线程里做，不卡事件循环。
* 会话时间 = ``captured_at − sessions.started_at``（``captured_at`` 是浏览器已经换算成服务端时钟的采集时刻）。
* 文件名用截图编号（递增）：``<data_dir>/sessions/<会话>/frames/<编号>.webp``。
* **画面有没有变**由服务端自己再判一次（和这场会议里上一张收到的图比缩略图）：浏览器每隔一段时间会上传一张
  「兜底」截图，画面其实没变；这种图照常进时间线，但不必再生成摘要、不必再往实时模型的上下文里追加一行
  （``IngestedFrame.changed`` 为假，由画面摘要那一侧沿用上一张的摘要）。

HTTP 状态码由 ``IngestError.status`` 给出，接口层只管转成 ``{"error": ...}``。
"""

from __future__ import annotations

import asyncio
import io
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from loguru import logger
from PIL import Image, UnidentifiedImageError

from agentic_meeting.store.db import Store
from agentic_meeting.types import ScreenFrame, Session

MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_CLOCK_SKEW_SECS = 60.0
THUMB_SIZE = (64, 36)  # 与浏览器端判断画面变化用的缩略图一致
DIFF_BLOCK = (8, 6)  # 比较时分成的小块（宽、高，像素）：8 列 × 6 行

# Pillow 的格式名 → (文件后缀, 响应里的媒体类型)
FORMATS: dict[str, tuple[str, str]] = {
    "WEBP": (".webp", "image/webp"),
    "JPEG": (".jpg", "image/jpeg"),
}


class IngestError(Exception):
    """截图被拒收。``status`` 是 HTTP 状态码，消息直接面向用户。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True, slots=True)
class DecodedImage:
    width: int
    height: int
    suffix: str
    thumb: bytes  # 64×36 灰度缩略图的像素


@dataclass(frozen=True, slots=True)
class IngestedFrame:
    frame: ScreenFrame
    # 与这场会议里上一张截图相比画面是否变了；第一张恒为真
    changed: bool
    # 画面没变时，上一张截图的编号（摘要沿用它的）
    same_as: int | None = None


def decode_image(data: bytes, *, max_side_px: int) -> DecodedImage:
    """解码校验一张截图。不是图片、格式不对、尺寸超限都抛 ``IngestError(400)``。"""
    try:
        with Image.open(io.BytesIO(data)) as image:
            fmt = (image.format or "").upper()
            if fmt not in FORMATS:
                raise IngestError(400, "只接受 WebP 或 JPEG 格式的截图")
            width, height = image.size
            # 先看尺寸再解码：声称很大的图不去解它
            if width < 1 or height < 1 or max(width, height) > max_side_px:
                raise IngestError(400, f"截图的长边不能超过 {max_side_px} 像素")
            image.load()
            thumb = image.convert("L").resize(THUMB_SIZE, Image.Resampling.BILINEAR).tobytes()
    except IngestError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as e:
        raise IngestError(400, "上传的文件不是有效的图片") from e
    except Image.DecompressionBombError as e:
        raise IngestError(400, f"截图的长边不能超过 {max_side_px} 像素") from e
    return DecodedImage(width, height, FORMATS[fmt][0], thumb)


def thumb_difference(a: bytes, b: bytes) -> float:
    """两张缩略图差多少（0 = 完全一样，1 = 有一块黑白相反）：分成小块，取**变化最大的那一块**的平均差 / 255。

    不用整张图的平均差：两页都是白底、只是文字不同的幻灯片，整张平均下来只差 1% 左右，会被当成没变
    （实测；换成按块取最大之后是 9%–14%，同一张图重新压缩是 0.1%）。和浏览器端的 ``frameDifference`` 是同一个算法。
    尺寸不是标准缩略图时退回整张的平均差。
    """
    if len(a) != len(b) or not a:
        return 1.0
    x = np.frombuffer(a, dtype=np.uint8).astype(np.int16)
    y = np.frombuffer(b, dtype=np.uint8).astype(np.int16)
    diff = np.abs(x - y)
    width, height = THUMB_SIZE
    if diff.size != width * height:
        return float(diff.mean() / 255.0)
    blocks = diff.reshape(
        height // DIFF_BLOCK[1], DIFF_BLOCK[1], width // DIFF_BLOCK[0], DIFF_BLOCK[0]
    )
    return float(blocks.mean(axis=(1, 3)).max() / 255.0)


def media_type_of(path: str) -> str:
    suffix = Path(path).suffix.lower()
    for known, media_type in FORMATS.values():
        if known == suffix:
            return media_type
    return "application/octet-stream"


class FrameIngestor:
    def __init__(
        self,
        store: Store,
        data_dir: Path,
        *,
        max_side_px: int,
        change_threshold: float,
        now: Callable[[], float] = time.time,
        max_bytes: int = MAX_IMAGE_BYTES,
        max_skew_secs: float = MAX_CLOCK_SKEW_SECS,
    ) -> None:
        self._store = store
        self._data_dir = Path(data_dir)
        self._max_side = max_side_px
        self._threshold = change_threshold
        self._now = now
        self._max_bytes = max_bytes
        self._max_skew = max_skew_secs
        # 每场会议上一张「画面变了」的截图：(截图编号, 缩略图)。只在内存里，重启后第一张按「变了」处理。
        self._last: dict[str, tuple[int, bytes]] = {}

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    def path_of(self, frame: ScreenFrame) -> Path | None:
        """截图文件的绝对路径；数据库里的路径跑出了数据目录（不该发生）时返回 ``None``。"""
        root = self._data_dir.resolve()
        full = (root / frame.path).resolve()
        return full if full.is_relative_to(root) else None

    def forget(self, session_id: str) -> None:
        self._last.pop(session_id, None)

    async def ingest(self, session: Session, data: bytes, captured_at: float) -> IngestedFrame:
        if len(data) > self._max_bytes:
            raise IngestError(413, f"截图太大（最多 {self._max_bytes // (1024 * 1024)} MB）")
        if not data:
            raise IngestError(400, "上传的文件是空的")
        now = self._now()
        if not (abs(captured_at - now) <= self._max_skew):  # NaN 也在这里被拒
            raise IngestError(
                400,
                f"截图的采集时间与服务端时间相差超过 {self._max_skew:.0f} 秒，请刷新页面重新对时",
            )
        decoded = await asyncio.to_thread(decode_image, data, max_side_px=self._max_side)

        last = self._last.get(session.id)
        changed = last is None or thumb_difference(last[1], decoded.thumb) > self._threshold
        frame = await self._store.add_frame(
            session.id,
            t=max(0.0, captured_at - session.started_at),
            width=decoded.width,
            height=decoded.height,
            suffix=decoded.suffix,
            now=now,
        )
        assert frame.id is not None
        try:
            await asyncio.to_thread(self._write, self._data_dir / frame.path, data)
        except OSError as e:
            logger.exception("截图落盘失败")
            await self._store.delete_frame(frame.id)
            raise IngestError(500, "截图保存失败（磁盘不可写？）") from e
        if changed:
            # 只和上一张「变了」的图比：画面缓慢漂移时，累计的变化迟早会超过阈值
            self._last[session.id] = (frame.id, decoded.thumb)
            return IngestedFrame(frame, True)
        assert last is not None
        return IngestedFrame(frame, False, same_as=last[0])

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
