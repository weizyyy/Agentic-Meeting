"""声纹比对门限的标定：用几个人的录音，看同一个人和不同人的相似度各落在哪里，给 match_threshold 一个依据。

用法::

    uv run python scripts/calibrate_speaker_threshold.py data/voices
    uv run python scripts/calibrate_speaker_threshold.py data/voices --lengths 10,30,0 --out data/eval/speaker.json

录音按人放，每人一个子目录，每个文件当作这个人在一场会议里说的话（16 kHz、单声道、16 位的 WAV）::

    data/voices/林晓/周会-1.wav
    data/voices/林晓/周会-2.wav
    data/voices/周明/周会-1.wav

声纹服务要先跑起来，地址取配置里的 ``speaker_memory.base_url``（``services up``，或者
``uv run python -m agentic_meeting.voice.service --model <模型> --port 8084``）。这个脚本不启动服务、
不下载任何东西，录音也不离开本机。

做法和应用里一样：每个文件去掉静音，切成不超过 6 秒的窗口送去算声纹，按时长加权平均成这场会议的声纹。
档案是这个人其他文件的声纹（每个文件一个样本，最多 8 个）；拿每个文件只取前 N 秒语音的声纹（``--lengths``，
0 表示整个文件）去和所有档案比：

- 同一人的分数：和自己的档案（不含这个文件本身）的相似度；不同人的分数：和别人的档案的相似度；
- 用配置里的 ``match_threshold`` 和 ``match_margin`` 跑一遍应用里同样的提示规则，数提示对、提示错、没有提示；
  只有一个文件的人，拿他的文件去比时没有他的档案，正好看「陌生人」会不会被认成别人；
- 推荐门限：不出错的前提下提示对的最多的门限（同样多时取高的）。

输出一张 Markdown 表，可以直接贴进 docs/benchmarks.md。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from agentic_meeting.config import load_config
from agentic_meeting.voice.client import SpeakerClient, SpeakerServiceError
from agentic_meeting.voice.clips import MAX_CLIP_SECS, MIN_CLIP_SECS
from agentic_meeting.voice.matching import MatchInputs, ProfileSamples, suggest
from agentic_meeting.voice.profiles import MAX_SAMPLES, voiceprint

SAMPLE_RATE = 16000
FRAME = SAMPLE_RATE // 50  # 20 毫秒一帧，用来去掉静音


@dataclass
class Recording:
    person: str
    path: Path
    clips: list[tuple[bytes, float, str]]  # (vector, secs, model_sha256)，按时间顺序


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(
                f"{path}：要 16 kHz、单声道、16 位的 WAV，可以用 "
                "ffmpeg -i 原文件 -ar 16000 -ac 1 -sample_fmt s16 新文件.wav 转换"
            )
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")


def speech_windows(pcm: np.ndarray, silence_dbfs: float) -> list[np.ndarray]:
    """去掉低于 ``silence_dbfs`` 的 20 毫秒帧，剩下的按顺序切成不超过 6 秒的窗口，不足 1 秒的丢掉。"""
    gate = 32768.0 * 10 ** (silence_dbfs / 20)
    frames = [pcm[i : i + FRAME] for i in range(0, len(pcm) - FRAME + 1, FRAME)]
    kept = [f for f in frames if np.sqrt(np.mean(f.astype(np.float64) ** 2)) >= gate]
    if not kept:
        return []
    speech = np.concatenate(kept)
    size = int(MAX_CLIP_SECS * SAMPLE_RATE)
    windows = [speech[i : i + size] for i in range(0, len(speech), size)]
    return [w for w in windows if len(w) >= MIN_CLIP_SECS * SAMPLE_RATE]


def load_windows(root: Path, silence_dbfs: float) -> list[tuple[str, Path, list[np.ndarray]]]:
    """每个文件 → (人, 文件, 语音窗口)。"""
    found = []
    for person_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for path in sorted(person_dir.glob("*.wav")):
            windows = speech_windows(read_wav(path), silence_dbfs)
            if windows:
                found.append((person_dir.name, path, windows))
            else:
                print(f"跳过 {path}：去掉静音后不到 {MIN_CLIP_SECS:g} 秒", file=sys.stderr)
    return found


async def embed_all(
    found: list[tuple[str, Path, list[np.ndarray]]], client: SpeakerClient
) -> list[Recording]:
    model = (await client.info()).model_sha256
    recordings: list[Recording] = []
    try:
        for person, path, windows in found:
            clips = []
            for window in windows:
                vector = np.asarray(await client.embed(window.tobytes()), dtype="<f4")
                clips.append((vector.tobytes(), len(window) / SAMPLE_RATE, model))
            recordings.append(Recording(person, path, clips))
    finally:
        await client.close()
    return recordings


def first_secs(
    clips: list[tuple[bytes, float, str]], secs: float
) -> list[tuple[bytes, float, str]]:
    """前 ``secs`` 秒语音的片段（0 = 全部），像会议刚开始一会儿时那样。"""
    if secs <= 0:
        return clips
    taken, total = [], 0.0
    for clip in clips:
        if total >= secs:
            break
        taken.append(clip)
        total += clip[1]
    return taken


def profiles_without(recordings: list[Recording], skip: Recording) -> list[ProfileSamples]:
    """每人一份档案，样本是他其他文件的整段声纹；``skip`` 自己不算。"""
    profiles = []
    people = sorted({r.person for r in recordings})
    for pid, person in enumerate(people, start=1):
        samples = []
        for r in recordings:
            if r.person == person and r is not skip:
                voice = voiceprint(r.clips)
                if voice is not None:
                    samples.append(np.asarray(voice.vector, dtype="<f4").tobytes())
        if samples:
            profiles.append(ProfileSamples(pid, person, skip.clips[0][2], samples[-MAX_SAMPLES:]))
    return profiles


def score(clips: list[tuple[bytes, float, str]], profile: ProfileSamples) -> float:
    voice = voiceprint(clips)
    assert voice is not None
    mine = np.asarray(voice.vector, dtype=np.float64)
    return max(float(np.dot(np.frombuffer(v, dtype="<f4"), mine)) for v in profile.vectors)


def evaluate(
    recordings: list[Recording], secs: float, threshold: float, margin: float
) -> dict[str, object]:
    same: list[float] = []
    other: list[float] = []
    trials: list[tuple[MatchInputs, str]] = []
    for r in recordings:
        clips = first_secs(r.clips, secs)
        profiles = profiles_without(recordings, r)
        for profile in profiles:
            (same if profile.name == r.person else other).append(score(clips, profile))
        trials.append((MatchInputs({1: clips}, set(), set(), profiles), r.person))

    def outcome(t: float) -> tuple[int, int, int]:
        right = wrong = none = 0
        for inputs, person in trials:
            hits = suggest(inputs, min_secs=0.0, threshold=t, margin=margin)
            if not hits:
                none += 1
            elif hits[0].name == person:
                right += 1
            else:
                wrong += 1
        return right, wrong, none

    right, wrong, none = outcome(threshold)
    grid = [round(0.30 + 0.01 * i, 2) for i in range(61)]
    safe = [(r, t) for t in grid for r, w, _ in [outcome(t)] if w == 0]
    best = max(safe)[1] if safe else None
    return {
        "secs": secs,
        "files": len(trials),
        "same": summary(same),
        "other": summary(other),
        "right": right,
        "wrong": wrong,
        "none": none,
        "recommended": best,
    }


def summary(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    a = np.asarray(values)
    return {
        "n": len(values),
        "min": float(a.min()),
        "median": float(np.median(a)),
        "max": float(a.max()),
    }


def describe(stats: dict[str, float] | None) -> str:
    if stats is None:
        return "—"
    return f"{stats['min']:.2f} / {stats['median']:.2f} / {stats['max']:.2f}（{stats['n']:.0f}）"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("voices", type=Path, help="录音目录，每人一个子目录")
    parser.add_argument("--config", help="配置文件路径（默认 config/config.toml）")
    parser.add_argument(
        "--lengths", default="10,30,0", help="每个文件只取前多少秒语音来比，逗号分隔；0 = 整个文件"
    )
    parser.add_argument("--silence-dbfs", type=float, default=-45.0, help="低于这个电平的帧当静音")
    parser.add_argument("--out", type=Path, help="把结果另存为 JSON")
    args = parser.parse_args()

    cfg = load_config(args.config)
    client = SpeakerClient.from_config(cfg.speaker_memory)
    if client is None:
        print('speaker_memory.backend 是 "none"：先在配置里开启说话人记忆。', file=sys.stderr)
        return 2
    try:
        found = load_windows(args.voices, args.silence_dbfs)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    try:
        recordings = asyncio.run(embed_all(found, client))
    except SpeakerServiceError as e:
        print(f"声纹服务不可用（{cfg.speaker_memory.base_url}）：{e}", file=sys.stderr)
        return 1
    people = {r.person for r in recordings}
    if len(people) < 2 or len(recordings) == len(people):
        print("至少要两个人，并且至少有一个人有两个以上的文件。", file=sys.stderr)
        return 2

    sm = cfg.speaker_memory
    results = [
        evaluate(recordings, float(x), sm.match_threshold, sm.match_margin)
        for x in args.lengths.split(",")
    ]
    print(
        f"{len(people)} 人、{len(recordings)} 个文件；门限 {sm.match_threshold}，差距 {sm.match_margin}\n"
    )
    print(
        "| 每个文件用的语音 | 同一人（最低 / 中位 / 最高，次数） | 不同人（最低 / 中位 / 最高，次数） "
        "| 提示对 / 错 / 没提示 | 推荐门限 |"
    )
    print("| --- | --- | --- | --- | --- |")
    for r in results:
        length = "整个文件" if r["secs"] == 0 else f"前 {r['secs']:g} 秒"
        recommended = "—" if r["recommended"] is None else f"{r['recommended']:.2f}"
        print(
            f"| {length} | {describe(r['same'])} | {describe(r['other'])} "
            f"| {r['right']} / {r['wrong']} / {r['none']} | {recommended} |"
        )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
