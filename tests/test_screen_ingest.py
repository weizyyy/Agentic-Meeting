"""截图接收：校验、落盘、入库、时间线消息（interfaces.md §5.3）。"""

from __future__ import annotations

import asyncio
import io
import threading
import time
from pathlib import Path

import httpx
import pytest
from PIL import Image
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame

from agentic_meeting.screen import ingest as ingest_module
from agentic_meeting.screen.ingest import (
    FrameIngestor,
    IngestError,
    decode_image,
    media_type_of,
    thumb_difference,
)
from agentic_meeting.store.db import Store
from agentic_meeting.types import ScreenFrame
from agentic_meeting.web.app import create_app


def image_bytes(color=(200, 30, 30), size=(320, 180), fmt="WEBP") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, fmt)
    return buf.getvalue()


class FakeWorker:
    def __init__(self):
        self.frames = []

    async def queue_frame(self, frame):
        self.frames.append(frame)

    async def cancel(self):
        pass

    def messages(self, kind):
        return [
            f.data
            for f in self.frames
            if isinstance(f, RTVIServerMessageFrame) and f.data.get("type") == kind
        ]


class FakeRecorder:
    elapsed_secs = 1.0


class FakeCaptions:
    def __init__(self, fail=False):
        self.items = []
        self.fail = fail

    async def submit(self, ingested):
        if self.fail:
            raise RuntimeError("boom")
        self.items.append(ingested)


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #


def test_decode_reads_real_size_and_format():
    d = decode_image(image_bytes(size=(640, 360)), max_side_px=1920)
    assert (d.width, d.height, d.suffix) == (640, 360, ".webp")
    assert len(d.thumb) == 64 * 36
    assert decode_image(image_bytes(fmt="JPEG"), max_side_px=1920).suffix == ".jpg"


@pytest.mark.parametrize(
    "data", [b"not an image", b"RIFF\x00\x00\x00\x00WEBPVP8 ", b"\xff\xd8\xff"]
)
def test_decode_rejects_garbage(data):
    with pytest.raises(IngestError) as e:
        decode_image(data, max_side_px=1920)
    assert e.value.status == 400


def test_decode_rejects_other_formats_and_oversize():
    with pytest.raises(IngestError) as e:
        decode_image(image_bytes(fmt="PNG"), max_side_px=1920)
    assert e.value.status == 400 and "WebP" in e.value.message
    assert decode_image(image_bytes(size=(1920, 100)), max_side_px=1920).width == 1920
    for size in [(1921, 100), (100, 1921)]:
        with pytest.raises(IngestError) as e:
            decode_image(image_bytes(size=size), max_side_px=1920)
        assert e.value.status == 400 and "1920" in e.value.message


def test_thumb_difference():
    black, white = bytes(64 * 36), bytes([255]) * (64 * 36)
    assert thumb_difference(black, black) == 0.0
    assert thumb_difference(black, white) == pytest.approx(1.0)
    assert thumb_difference(white, black) == pytest.approx(1.0)  # 不因无符号减法回绕
    half = bytes([255]) * (32 * 36) + bytes(32 * 36)
    assert thumb_difference(black, half) == pytest.approx(1.0)  # 有整块变了就算变了
    # 白底幻灯片上只改了几行字：整张平均只差一点点，但有字的那一块差得多
    page = bytearray(white)
    for row in range(12, 15):
        page[row * 64 + 8 : row * 64 + 24] = bytes(16)
    difference = thumb_difference(white, bytes(page))
    assert difference == pytest.approx(0.5) and difference > 48 / (64 * 36)
    assert thumb_difference(bytes(8), bytes([255]) * 4 + bytes(4)) == pytest.approx(
        0.5
    )  # 非标准尺寸
    assert thumb_difference(black, b"") == 1.0  # 长度对不上按「变了」算
    assert thumb_difference(b"", b"") == 1.0


def test_media_type_of():
    assert media_type_of("a/b/000001.webp") == "image/webp"
    assert media_type_of("x.JPG") == "image/jpeg"
    assert media_type_of("x.bin") == "application/octet-stream"


# --------------------------------------------------------------------------- #
# FrameIngestor
# --------------------------------------------------------------------------- #


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "meetings.db", 4, assistant_name="Nova")
    yield s
    await s.close()


def ingestor(store, tmp_path, **kw):
    kw.setdefault("now", lambda: 1100.0)
    return FrameIngestor(store, tmp_path / "data", max_side_px=1920, change_threshold=0.04, **kw)


async def test_ingest_writes_file_row_and_session_time(store, tmp_path):
    session = await store.create_session(now=1000.0)
    ing = ingestor(store, tmp_path)
    data = image_bytes()
    got = await ing.ingest(session, data, 1090.5)
    f = got.frame
    assert (f.t, f.width, f.height, f.caption_status) == (90.5, 320, 180, "pending")
    assert f.path == f"sessions/{session.id}/frames/{f.id:06d}.webp"
    assert (tmp_path / "data" / f.path).read_bytes() == data
    assert ing.path_of(f) == (tmp_path / "data" / f.path).resolve()
    assert await store.get_frame(f.id) == f
    assert (await store.get_session(session.id)).last_active_at == 1100.0


async def test_ingest_clamps_negative_session_time(store, tmp_path):
    session = await store.create_session(now=1095.0)
    got = await ingestor(store, tmp_path).ingest(session, image_bytes(), 1094.0)
    assert got.frame.t == 0.0


async def test_ingest_marks_unchanged_frames(store, tmp_path):
    session = await store.create_session(now=1000.0)
    ing = ingestor(store, tmp_path)
    first = await ing.ingest(session, image_bytes((10, 10, 10)), 1100.0)
    same = await ing.ingest(session, image_bytes((12, 12, 12)), 1100.0)
    again = await ing.ingest(session, image_bytes((10, 10, 10)), 1100.0)
    different = await ing.ingest(session, image_bytes((250, 250, 250)), 1100.0)
    after = await ing.ingest(session, image_bytes((250, 250, 250)), 1100.0)
    assert (first.changed, first.same_as) == (True, None)
    assert (same.changed, same.same_as) == (False, first.frame.id)
    assert (again.changed, again.same_as) == (False, first.frame.id)  # 指向最初那张，不是上一张
    assert (different.changed, different.same_as) == (True, None)
    assert (after.changed, after.same_as) == (False, different.frame.id)
    # 每一张都进了时间线
    assert len(await store.list_frames(session.id)) == 5


async def test_ingest_compares_against_last_changed_frame_so_drift_accumulates(store, tmp_path):
    session = await store.create_session(now=1000.0)
    ing = ingestor(store, tmp_path)
    results = [
        (await ing.ingest(session, image_bytes((v, v, v)), 1100.0)).changed
        for v in (0, 6, 12, 18)  # 每步约 2.4%，都不到 4%，但累计会超过
    ]
    assert results == [True, False, True, False]


async def test_unchanged_is_per_session(store, tmp_path):
    a = await store.create_session(now=1000.0)
    b = await store.create_session(now=1000.0)
    ing = ingestor(store, tmp_path)
    await ing.ingest(a, image_bytes(), 1100.0)
    assert (await ing.ingest(b, image_bytes(), 1100.0)).changed is True
    ing.forget(a.id)
    assert (await ing.ingest(a, image_bytes(), 1100.0)).changed is True


@pytest.mark.parametrize("captured_at", [1039.9, 1160.1, float("nan"), float("inf")])
async def test_ingest_rejects_clock_skew(store, tmp_path, captured_at):
    session = await store.create_session(now=1000.0)
    with pytest.raises(IngestError) as e:
        await ingestor(store, tmp_path).ingest(session, image_bytes(), captured_at)
    assert e.value.status == 400 and "对时" in e.value.message
    assert await store.list_frames(session.id) == []


@pytest.mark.parametrize("captured_at", [1040.0, 1160.0])
async def test_ingest_accepts_skew_at_the_limit(store, tmp_path, captured_at):
    session = await store.create_session(now=1000.0)
    await ingestor(store, tmp_path).ingest(session, image_bytes(), captured_at)


async def test_ingest_rejects_too_large_and_empty(store, tmp_path):
    session = await store.create_session(now=1000.0)
    ing = ingestor(store, tmp_path, max_bytes=100)
    with pytest.raises(IngestError) as e:
        await ing.ingest(session, b"x" * 101, 1100.0)
    assert e.value.status == 413
    with pytest.raises(IngestError) as e:
        await ing.ingest(session, b"", 1100.0)
    assert e.value.status == 400
    with pytest.raises(IngestError) as e:  # 正好到上限的不算超大（这里因为不是图片而被拒）
        await ing.ingest(session, b"x" * 100, 1100.0)
    assert e.value.status == 400


async def test_ingest_removes_row_when_file_cannot_be_written(store, tmp_path):
    session = await store.create_session(now=1000.0)
    blocker = tmp_path / "data"
    blocker.write_text("这是个文件，不是目录", encoding="utf-8")
    ing = ingestor(store, tmp_path)
    with pytest.raises(IngestError) as e:
        await ing.ingest(session, image_bytes(), 1100.0)
    assert e.value.status == 500
    assert await store.list_frames(session.id) == []
    # 失败的那张不算「上一张」
    blocker.unlink()
    assert (await ing.ingest(session, image_bytes(), 1100.0)).changed is True


async def test_decoding_does_not_block_the_event_loop(store, tmp_path, monkeypatch):
    seen = {}
    real = ingest_module.decode_image

    def spy(data, *, max_side_px):
        seen["thread"] = threading.current_thread()
        return real(data, max_side_px=max_side_px)

    monkeypatch.setattr(ingest_module, "decode_image", spy)
    session = await store.create_session(now=1000.0)
    await ingestor(store, tmp_path).ingest(session, image_bytes(), 1100.0)
    assert seen["thread"] is not threading.main_thread()


async def test_path_of_refuses_paths_outside_data_dir(store, tmp_path):
    ing = ingestor(store, tmp_path)
    evil = ScreenFrame(session_id="s", t=0, path="../outside.webp", width=1, height=1)
    assert ing.path_of(evil) is None


# --------------------------------------------------------------------------- #
# 存储层
# --------------------------------------------------------------------------- #


async def test_store_frames_listing_latest_and_caption(store):
    s = await store.create_session(now=1000.0)
    other = await store.create_session(now=1000.0)
    a = await store.add_frame(s.id, t=30.0, width=10, height=5, suffix=".webp")
    b = await store.add_frame(s.id, t=10.0, width=10, height=5, suffix=".jpg")
    c = await store.add_frame(other.id, t=99.0, width=10, height=5, suffix=".webp")
    assert [f.id for f in await store.list_frames(s.id)] == [b.id, a.id]  # 按时间
    assert [f.id for f in await store.list_frames(s.id, t_from=10.0, t_to=30.0)] == [b.id, a.id]
    assert [f.id for f in await store.list_frames(s.id, t_from=10.1)] == [a.id]
    assert [f.id for f in await store.list_frames(s.id, t_to=29.9)] == [b.id]
    assert (await store.latest_frame(s.id)).id == a.id
    assert (await store.latest_frame(other.id)).id == c.id
    assert await store.latest_frame("nope") is None
    assert b.path.endswith(f"{b.id:06d}.jpg")

    assert await store.set_frame_caption(a.id, status="done", caption="一张表")
    got = await store.get_frame(a.id)
    assert (got.caption, got.caption_status) == ("一张表", "done")
    assert await store.set_frame_caption(a.id, status="failed")  # 不给文字就保留原来的
    got = await store.get_frame(a.id)
    assert (got.caption, got.caption_status) == ("一张表", "failed")
    assert not await store.set_frame_caption(9999, status="done", caption="x")

    await store.delete_frame(b.id)
    assert await store.get_frame(b.id) is None
    await store.delete_session(s.id)
    assert await store.get_frame(a.id) is None  # 级联
    assert await store.get_frame(c.id) is not None


# --------------------------------------------------------------------------- #
# HTTP 接口
# --------------------------------------------------------------------------- #


class Env:
    def __init__(self, cfg, app, client, store):
        self.cfg, self.app, self.client, self.store = cfg, app, client, store
        self.resources = app.state.resources
        self.manager = self.resources.sessions
        self.data_dir = Path(cfg.resolve(cfg.session.data_dir))

    async def go_live(self):
        live = await self.manager.begin()
        worker = FakeWorker()
        await self.manager.register(live, worker, FakeRecorder())
        return live, worker

    async def upload(self, data=None, *, captured_at=None, content_type="image/webp"):
        at = time.time() if captured_at is None else captured_at
        return await self.client.post(
            "/api/frames",
            data={"captured_at": str(at)},
            files={
                "image": ("shot.webp", data if data is not None else image_bytes(), content_type)
            },
        )


async def open_env(cfg, tmp_path):
    store = await Store.open(tmp_path / "meetings.db", 4, assistant_name="Nova")
    app = create_app(cfg, store=store, static_dir=tmp_path / "nope")
    return store, app


@pytest.fixture
async def env(make_cfg, tmp_path):
    cfg = make_cfg()
    store, app = await open_env(cfg, tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield Env(cfg, app, client, store)
            live = app.state.resources.sessions.live
            if live is not None:  # 不留活动连接，否则应用关闭时要等它收尾
                await app.state.resources.sessions.finish(live)
    await store.close()


async def test_upload_stores_frame_and_notifies_page(env):
    live, worker = await env.go_live()
    data = image_bytes(size=(400, 200))
    r = await env.upload(data)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"id", "t"} and body["t"] >= 0
    frame = await env.store.get_frame(body["id"])
    assert (frame.session_id, frame.width, frame.height) == (live.session.id, 400, 200)
    assert worker.messages("frame") == [
        {"type": "frame", "id": body["id"], "t": body["t"], "width": 400, "height": 200}
    ]
    assert (env.data_dir / frame.path).read_bytes() == data

    img = await env.client.get(f"/api/frames/{body['id']}/image")
    assert img.status_code == 200 and img.content == data
    assert img.headers["content-type"] == "image/webp"
    # 编号可能在删除会议后被复用，不能让浏览器把图片当成永久不变的
    assert img.headers["cache-control"] == "private, no-cache"

    listing = await env.client.get("/api/frames")
    assert listing.json() == {
        "items": [
            {
                "id": body["id"],
                "t": body["t"],
                "width": 400,
                "height": 200,
                "caption": None,
                "caption_status": "skipped",  # 测试里没有后台模型
            }
        ]
    }
    by_id = await env.client.get("/api/frames", params={"session_id": live.session.id})
    assert by_id.json() == listing.json()


async def test_upload_uses_real_content_not_declared_type(env):
    await env.go_live()
    r = await env.upload(image_bytes(fmt="JPEG"), content_type="image/webp")
    assert r.status_code == 200
    frame = await env.store.get_frame(r.json()["id"])
    assert frame.path.endswith(".jpg")
    img = await env.client.get(f"/api/frames/{frame.id}/image")
    assert img.headers["content-type"] == "image/jpeg"


async def test_upload_without_live_session_is_404(env):
    r = await env.upload()
    assert r.status_code == 404 and "没有进行中的会议" in r.json()["error"]
    # 已中断的会议也不收
    live, _ = await env.go_live()
    await env.manager.finish(live)
    assert (await env.upload()).status_code == 404
    assert await env.store.list_frames(live.session.id) == []


async def test_upload_rejections(env):
    live, worker = await env.go_live()
    not_image = await env.upload(b"hello")
    assert not_image.status_code == 400 and "图片" in not_image.json()["error"]
    assert (await env.upload(image_bytes(fmt="PNG"))).status_code == 400
    skew = await env.upload(captured_at=time.time() - 120)
    assert skew.status_code == 400 and "对时" in skew.json()["error"]
    bad_time = await env.upload(captured_at="yesterday")
    assert bad_time.status_code == 400 and "captured_at" in bad_time.json()["error"]
    big = await env.upload(b"\x00" * (4 * 1024 * 1024 + 1))
    assert big.status_code == 413
    missing = await env.client.post("/api/frames", data={"captured_at": "1"})
    assert missing.status_code == 422 and "error" in missing.json()
    assert await env.store.list_frames(live.session.id) == []
    assert worker.messages("frame") == []


async def test_upload_refused_when_screen_disabled(make_cfg, tmp_path):
    cfg = make_cfg()
    cfg.screen.enabled = False
    store, app = await open_env(cfg, tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            e = Env(cfg, app, client, store)
            await e.go_live()
            assert (await e.upload()).status_code == 403
            await e.manager.finish(e.manager.live)
    await store.close()


async def test_upload_hands_frame_to_captions_and_survives_their_failure(env):
    await env.go_live()
    captions = FakeCaptions()
    env.resources.captions = captions
    first = await env.upload(image_bytes((0, 0, 0)))
    second = await env.upload(image_bytes((0, 0, 0)))
    assert [i.frame.id for i in captions.items] == [first.json()["id"], second.json()["id"]]
    assert [i.changed for i in captions.items] == [True, False]
    assert captions.items[1].same_as == first.json()["id"]

    env.resources.captions = FakeCaptions(fail=True)
    assert (await env.upload(image_bytes((255, 255, 255)))).status_code == 200


async def test_frame_image_and_list_not_found(env):
    assert (await env.client.get("/api/frames/999/image")).status_code == 404
    assert (await env.client.get("/api/frames")).status_code == 404  # 没有任何会议
    r = await env.client.get("/api/frames", params={"session_id": "nope"})
    assert r.status_code == 404 and r.json()["error"] == "找不到这场会议"
    # 行还在但文件没了
    await env.go_live()
    body = (await env.upload()).json()
    frame = await env.store.get_frame(body["id"])
    (env.data_dir / frame.path).unlink()
    assert (await env.client.get(f"/api/frames/{body['id']}/image")).status_code == 404


async def test_deleting_session_removes_frame_files(env):
    live, _ = await env.go_live()
    body = (await env.upload()).json()
    frame = await env.store.get_frame(body["id"])
    saved = env.data_dir / frame.path
    assert saved.is_file()
    await env.manager.finish(live)
    assert (await env.client.delete(f"/api/sessions/{live.session.id}")).status_code == 200
    assert not saved.exists()
    assert await env.store.get_frame(body["id"]) is None


async def test_concurrent_uploads_get_distinct_files(env):
    await env.go_live()
    rs = await asyncio.gather(*[env.upload(image_bytes((i * 40, 0, 0))) for i in range(5)])
    ids = [r.json()["id"] for r in rs]
    assert len(set(ids)) == 5
    paths = {(await env.store.get_frame(i)).path for i in ids}
    assert len(paths) == 5


# --------------------------------------------------------------------------- #
# screen_state 消息
# --------------------------------------------------------------------------- #


class Emitter:
    def __init__(self):
        self.handlers = []

    def event_handler(self, name):
        def register(fn):
            self.handlers.append((name, fn))
            return fn

        return register

    async def fire(self, name, *args):
        for registered, fn in self.handlers:
            if registered == name:
                await fn(self, *args)


class FakeMessage:
    def __init__(self, type_, data):
        self.type, self.data = type_, data


async def test_screen_state_messages_are_logged_and_never_raise():
    from loguru import logger

    from agentic_meeting.pipeline.bot import wire_screen_state

    lines = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="INFO")
    try:
        rtvi = Emitter()
        wire_screen_state(rtvi)
        await rtvi.fire("on_client_message", FakeMessage("screen_state", {"sharing": True}))
        await rtvi.fire("on_client_message", FakeMessage("screen_state", {"sharing": False}))
        await rtvi.fire("on_client_message", FakeMessage("screen_state", "坏的"))
        await rtvi.fire("on_client_message", FakeMessage("text_input", {"text": "别的消息"}))
    finally:
        logger.remove(sink)
    assert lines == ["屏幕共享已开始", "屏幕共享已停止", "屏幕共享已停止"]
