"""假推理服务：在测试进程里起几个本机 HTTP 服务，代替识别、实时模型、语音合成、嵌入和后台 agent。

应用照常按配置去连它们，走的是和真实部署一样的 HTTP 协议；只是回答由测试安排：

* 识别：音频静音时什么也不认出来；有声音时按排队的「台词」逐字吐出，段落收尾时吐完整句。
* 模型（实时、后台 agent 共用一套）：按规则匹配最后一条消息，回一段文字或一次工具调用。
* 语音合成：回一段非静音的 PCM。
* 嵌入：按字符散列成固定维度的向量，字面相近的文字向量也相近。

每个服务可以单独「停掉」（返回 503），用来验证降级。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import json
import math
import socket
import threading
import time
import uuid
import wave
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

VOICED_RMS = 300  # 识别窗口的 RMS 超过它才算有人说话（16 位采样）
CHARS_PER_SEC = 6.0  # 未收尾时每秒音频认出几个字
EMBEDDING_DIMENSIONS = 64


@dataclass
class Reply:
    """模型的一次回答：一段文字，或一次工具调用。"""

    text: str = ""
    tool: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class Rule:
    """最后一条消息里含 ``needle`` 时用 ``reply`` 回答（只用一次，除非 ``sticky``）。"""

    needle: str
    reply: Reply
    sticky: bool = False


class FakeService:
    """一个假服务：独占一个端口；``down`` 为真时所有请求返回 503。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.app = FastAPI()
        self.port = 0
        self.down = False
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()

        @self.app.middleware("http")
        async def gate(request: Request, call_next: Callable) -> Response:
            if self.down:
                return JSONResponse({"error": f"{self.name} 已停止"}, status_code=503)
            return await call_next(request)

        @self.app.get("/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def record(self, body: dict[str, Any]) -> None:
        with self._lock:
            self.requests.append(body)

    def count(self) -> int:
        with self._lock:
            return len(self.requests)


class FakeASR(FakeService):
    """识别服务（llama-server 的 ``/v1/chat/completions`` 带音频输入，加 ``/tokenize``）。"""

    def __init__(self) -> None:
        super().__init__("识别服务")
        self.lines: deque[str] = deque()
        self.current: str | None = None
        self.heard_voice = threading.Event()

        @self.app.post("/v1/chat/completions")
        async def complete(request: Request) -> dict[str, Any]:
            body = await request.json()
            self.record(body)
            return self._answer(body)

        @self.app.post("/tokenize")
        async def tokenize(request: Request) -> dict[str, Any]:
            text = (await request.json())["content"]
            return {"tokens": [{"id": i, "piece": ch} for i, ch in enumerate(text)]}

    def say(self, text: str) -> None:
        """下一段有声音的音频认作 ``text``。"""
        with self._lock:
            self.lines.append(text)

    def _answer(self, body: dict[str, Any]) -> dict[str, Any]:
        messages = body["messages"]
        assistant = messages[-1]["content"]
        prefix = assistant.split("<asr_text>", 1)[1] if "<asr_text>" in assistant else ""
        audio = next(
            part["input_audio"]["data"]
            for part in messages[1]["content"]
            if part.get("type") == "input_audio"
        )
        samples = _wav_samples(base64.b64decode(audio))
        rms = math.sqrt(float(np.mean(samples.astype(np.float64) ** 2))) if len(samples) else 0.0
        final = body.get("max_tokens") == 32  # 收尾那一步放开到配置的上限（asr.max_new_tokens）
        with self._lock:
            if rms >= VOICED_RMS:
                self.heard_voice.set()
                if self.current is None and self.lines:
                    self.current = self.lines.popleft()
            line = self.current or ""
            if final:
                self.current = None
        if final:
            known = line
        else:
            known = line[: int(len(samples) / 16000 * CHARS_PER_SEC)]
        continuation = known[len(prefix) :] if known.startswith(prefix) else ""
        return _completion(assistant + continuation)


class FakeModel(FakeService):
    """OpenAI 兼容的对话模型：按规则回答，流式与非流式都支持。"""

    def __init__(self, name: str, default: str) -> None:
        super().__init__(name)
        self.rules: list[Rule] = []
        self.default = default

        @self.app.post("/v1/chat/completions")
        async def complete(request: Request) -> Any:
            body = await request.json()
            self.record(body)
            reply = self._reply(body)
            if body.get("stream"):
                return StreamingResponse(_stream(reply), media_type="text/event-stream")
            return _completion(reply.text, reply)

        @self.app.get("/v1/models")
        async def models() -> dict[str, Any]:
            return {"object": "list", "data": [{"id": "fake-model", "object": "model"}]}

    def when(self, needle: str, reply: Reply, *, sticky: bool = False) -> None:
        with self._lock:
            self.rules.append(Rule(needle, reply, sticky))

    def _reply(self, body: dict[str, Any]) -> Reply:
        if body.get("max_tokens") == 1:
            return Reply(text="好")  # 预热前缀缓存的请求，回答没人看，不消耗规则
        messages = body.get("messages") or []
        last = messages[-1] if messages else {}
        if last.get("role") in ("tool", "developer") or "[任务 " in _text_of(last):
            return Reply(text="好的，已经处理。")
        text = _text_of(last)
        with self._lock:
            for rule in self.rules:
                if rule.needle in text:
                    if not rule.sticky:
                        self.rules.remove(rule)
                    return rule.reply
        return Reply(text=self.default)

    def texts(self) -> list[str]:
        """收到的每个请求里最后一条消息的文字。"""
        with self._lock:
            return [_text_of((r.get("messages") or [{}])[-1]) for r in self.requests]


class FakeTTS(FakeService):
    """语音合成（``/v1/audio/speech``，回 24 kHz 单声道 PCM）。"""

    def __init__(self) -> None:
        super().__init__("语音合成")

        @self.app.post("/v1/audio/speech")
        async def speech(request: Request) -> Response:
            self.record(await request.json())
            t = np.arange(int(24000 * 0.6)) / 24000
            pcm = (np.sin(2 * np.pi * 440 * t) * 8000).astype("<i2").tobytes()
            return Response(pcm, media_type="audio/pcm")


class FakeEmbedding(FakeService):
    """嵌入服务（``/v1/embeddings``）。"""

    def __init__(self) -> None:
        super().__init__("嵌入服务")

        @self.app.post("/v1/embeddings")
        async def embeddings(request: Request) -> dict[str, Any]:
            body = await request.json()
            self.record(body)
            return {
                "object": "list",
                "data": [
                    {"object": "embedding", "index": i, "embedding": _embed(text)}
                    for i, text in enumerate(body["input"])
                ],
            }


class Inference:
    """全部假服务，跑在后台线程自己的事件循环里，不受测试的事件循环阻塞影响。"""

    def __init__(self) -> None:
        self.asr = FakeASR()
        self.llm = FakeModel("实时模型", "好的。")
        self.agent = FakeModel(
            "后台模型",
            json.dumps({"brief": "已经整理好了", "detail_md": "## 结论\n- 虚构的结论"}),
        )
        self.tts = FakeTTS()
        self.embedding = FakeEmbedding()
        self._servers: list[uvicorn.Server] = []
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)

    @property
    def services(self) -> list[FakeService]:
        return [self.asr, self.llm, self.agent, self.tts, self.embedding]

    def start(self) -> None:
        self._thread.start()
        for service in self.services:
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            service.port = sock.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(service.app, log_level="warning"))
            self._servers.append(server)
            asyncio.run_coroutine_threadsafe(server.serve(sockets=[sock]), self._loop)
        deadline = time.monotonic() + 10
        while not all(server.started for server in self._servers):
            if time.monotonic() > deadline:
                raise RuntimeError("假推理服务没有启动")
            time.sleep(0.01)

    def stop(self) -> None:
        for server in self._servers:
            server.should_exit = True
        deadline = time.monotonic() + 5
        while any(server.started for server in self._servers) and time.monotonic() < deadline:
            time.sleep(0.01)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def reset(self) -> None:
        """每个测试开始前：清掉规则、台词、请求记录，全部恢复在线。"""
        for service in self.services:
            service.down = False
            with service._lock:
                service.requests.clear()
        with self.asr._lock:
            self.asr.lines.clear()
            self.asr.current = None
        self.asr.heard_voice.clear()
        for model in (self.llm, self.agent):
            with model._lock:
                model.rules.clear()


# --------------------------------------------------------------------------- #
# 协议细节
# --------------------------------------------------------------------------- #


def _wav_samples(data: bytes) -> np.ndarray:
    with contextlib.closing(wave.open(io.BytesIO(data))) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")


def _text_of(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _completion(text: str, reply: Reply | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": text}
    finish = "stop"
    if reply is not None and reply.tool:
        message = {"role": "assistant", "content": None, "tool_calls": [_tool_call(reply)]}
        finish = "tool_calls"
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "fake-model",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _tool_call(reply: Reply) -> dict[str, Any]:
    return {
        "id": f"call_{uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {"name": reply.tool, "arguments": json.dumps(reply.arguments)},
    }


async def _stream(reply: Reply):
    base = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "fake-model",
    }

    def chunk(delta: dict[str, Any], finish: str | None = None) -> str:
        data = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    yield chunk({"role": "assistant", "content": ""})
    if reply.tool:
        call = _tool_call(reply)
        yield chunk({"tool_calls": [{"index": 0, **call}]})
        yield chunk({}, "tool_calls")
    else:
        for i in range(0, len(reply.text), 4):
            yield chunk({"content": reply.text[i : i + 4]})
        yield chunk({}, "stop")
    usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
    yield f"data: {json.dumps({**base, 'choices': [], 'usage': usage})}\n\n"
    yield "data: [DONE]\n\n"


def _embed(text: str) -> list[float]:
    vector = [0.0] * EMBEDDING_DIMENSIONS
    for ch in text:
        digest = hashlib.blake2b(ch.encode("utf-8"), digest_size=2).digest()
        vector[int.from_bytes(digest, "little") % EMBEDDING_DIMENSIONS] += 1.0
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]
