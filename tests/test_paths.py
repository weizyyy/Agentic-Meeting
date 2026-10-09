"""services/paths.py：定位推理程序与动态库。

全部用 tmp_path 造假目录结构，不依赖真实的 runtimes/ 或 third_party/。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_meeting.services import paths
from agentic_meeting.services.paths import default_roots, find_executable, find_library


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


# ---- find_executable ----


def test_override_is_used_directly(tmp_path: Path):
    custom = touch(tmp_path / "custom" / "my-llama")
    other = touch(tmp_path / "runtimes" / "llama-server")  # 有 override 时不应再去查找
    assert find_executable("llama-server", str(custom), roots=[tmp_path / "runtimes"]) == custom
    assert other.exists()


def test_override_that_does_not_exist_raises(tmp_path: Path):
    missing = tmp_path / "nope" / "llama-server"
    with pytest.raises(FileNotFoundError, match="nope"):
        find_executable("llama-server", str(missing), roots=[tmp_path])


def test_relative_override_resolves_against_repo_root(tmp_path: Path, monkeypatch):
    """与 AppConfig.resolve 一致：配置里的相对路径相对仓库根目录，而不是当前目录。"""
    monkeypatch.setattr(paths, "REPO_ROOT", tmp_path)
    exe = touch(tmp_path / "bin" / "tool")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert find_executable("tool", "bin/tool", roots=[]) == exe


def test_finds_plain_name_and_exe_suffix(tmp_path: Path):
    assert (
        find_executable("a", roots=[touch(tmp_path / "r1" / "a").parent]) == tmp_path / "r1" / "a"
    )
    root = tmp_path / "r2"
    exe = touch(root / "sub" / "b.exe")
    assert find_executable("b", roots=[root]) == exe


def test_shallowest_match_wins_within_a_root(tmp_path: Path):
    root = tmp_path / "runtimes"
    touch(root / "llama_cpp" / "deep" / "er" / "llama-server")
    shallow = touch(root / "llama_cpp" / "llama-server.exe")
    touch(root / "z" / "y" / "llama-server")
    assert find_executable("llama-server", roots=[root]) == shallow


def test_equal_depth_ties_break_deterministically(tmp_path: Path):
    root = tmp_path / "runtimes"
    first = touch(root / "a" / "tool")
    touch(root / "b" / "tool")
    assert find_executable("tool", roots=[root]) == first


def test_roots_are_searched_in_order(tmp_path: Path):
    """先找到的根目录胜出，哪怕后面的根目录里有更浅的同名文件。"""
    runtimes = tmp_path / "runtimes"
    build = tmp_path / "third_party" / "x" / "build"
    in_runtimes = touch(runtimes / "deep" / "er" / "tts-server")
    touch(build / "tts-server")
    assert find_executable("tts-server", roots=[runtimes, build]) == in_runtimes
    # 第一个根目录里没有时才轮到下一个。
    assert find_executable("tts-server", roots=[tmp_path / "empty", build]) == build / "tts-server"


def test_name_must_match_exactly_and_be_a_file(tmp_path: Path):
    root = tmp_path / "runtimes"
    touch(root / "llama-server-impl.dll")  # 同目录下名字相近的动态库不能误中
    touch(root / "llama-server.txt")
    (root / "llama-server").mkdir()  # 同名目录不算
    (root / "tts-server.dir").mkdir()
    with pytest.raises(FileNotFoundError):
        find_executable("llama-server", roots=[root])


def test_missing_roots_are_skipped(tmp_path: Path):
    real = tmp_path / "real"
    exe = touch(real / "tool")
    assert find_executable("tool", roots=[tmp_path / "does-not-exist", real]) == exe


def test_not_found_error_tells_where_to_look(tmp_path: Path):
    with pytest.raises(FileNotFoundError) as exc:
        find_executable("llama-server", roots=[tmp_path / "runtimes"])
    message = str(exc.value)
    assert "llama-server" in message
    assert "python scripts/runtimes.py status" in message
    assert "launch.executable" in message  # 也提示可以在配置里直接指定


def test_default_roots_are_runtimes_then_third_party_builds(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(paths, "REPO_ROOT", tmp_path)
    (tmp_path / "third_party" / "b-lib" / "build").mkdir(parents=True)
    (tmp_path / "third_party" / "a-lib" / "build").mkdir(parents=True)
    (tmp_path / "third_party" / "c-lib" / "src").mkdir(parents=True)  # 没有 build/ 的不算
    assert default_roots() == [
        tmp_path / "runtimes",
        tmp_path / "third_party" / "a-lib" / "build",
        tmp_path / "third_party" / "b-lib" / "build",
    ]


def test_default_roots_are_used_when_roots_omitted(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(paths, "REPO_ROOT", tmp_path)
    exe = touch(tmp_path / "third_party" / "qwentts" / "build" / "tts-server.exe")
    assert find_executable("tts-server") == exe


# ---- find_library ----


def test_library_filename_rules_per_platform(tmp_path: Path):
    root = tmp_path / "runtimes"
    dll = touch(root / "bin" / "nemo_speech_asr_c.dll")
    so = touch(root / "lib" / "libnemo_speech_asr_c.so")
    dylib = touch(root / "lib" / "libnemo_speech_asr_c.1.dylib")
    assert find_library("nemo_speech_asr_c", roots=[root], platform="win32") == dll
    assert find_library("nemo_speech_asr_c", roots=[root], platform="linux") == so
    assert find_library("nemo_speech_asr_c", roots=[root], platform="darwin") == dylib


def test_library_ignores_other_platform_names_and_lookalikes(tmp_path: Path):
    root = tmp_path / "runtimes"
    touch(root / "libnemo_speech_asr_c.so")  # Windows 上不认
    touch(root / "nemo_speech_asr_c_extra.dll")  # 名字只是前缀相同
    touch(root / "nemo_speech_asr_c.lib")  # 导入库不是动态库
    with pytest.raises(FileNotFoundError):
        find_library("nemo_speech_asr_c", roots=[root], platform="win32")

    other = tmp_path / "other"
    touch(other / "nemo_speech_asr_c.dll")  # Linux 上不认
    touch(other / "xnemo_speech_asr_c.so")  # Linux 要求 lib 前缀
    touch(other / "libnemo_speech_asr_c.a")  # 静态库不算
    with pytest.raises(FileNotFoundError):
        find_library("nemo_speech_asr_c", roots=[other], platform="linux")


def test_library_prefers_unversioned_name_at_same_depth(tmp_path: Path):
    """Linux 的 lib<stem>.so 通常是指向 .so.1 的符号链接；同一目录下取最短的名字。"""
    root = tmp_path / "lib"
    touch(root / "libx.so.1.2.3")
    touch(root / "libx.so.1")
    plain = touch(root / "libx.so")
    assert find_library("x", roots=[root], platform="linux") == plain


def test_library_shallowest_and_root_order(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    deep = touch(first / "a" / "b" / "x.dll")
    touch(second / "x.dll")
    assert find_library("x", roots=[first, second], platform="win32") == deep
    shallow = touch(first / "x.dll")
    assert find_library("x", roots=[first, second], platform="win32") == shallow


def test_library_override_and_not_found(tmp_path: Path):
    lib = touch(tmp_path / "mine" / "whatever.dll")
    assert find_library("x", str(lib), roots=[], platform="win32") == lib
    with pytest.raises(FileNotFoundError, match="missing"):
        find_library("x", str(tmp_path / "missing.dll"), roots=[], platform="win32")
    with pytest.raises(FileNotFoundError) as exc:
        find_library("x", roots=[tmp_path / "empty"], platform="win32")
    assert "python scripts/runtimes.py status" in str(exc.value)
    assert "library_path" in str(exc.value)
