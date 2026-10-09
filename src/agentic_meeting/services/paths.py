"""定位推理程序与动态库。

程序和库的位置来自两类地方：`scripts/runtimes.py fetch` 解压到 ``runtimes/`` 的预编译包，
以及源码构建出来的 ``third_party/*/build/``。配置里的 ``executable`` / ``library_path``
非空时直接用它，不再查找。

这里是平台差异允许出现的地方（动态库的文件名规则），业务代码不要再自己判断平台。
"""

from __future__ import annotations

import fnmatch
import glob
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from agentic_meeting.config import REPO_ROOT


def default_roots() -> list[Path]:
    """默认的搜索根目录，按优先级排列：预编译包在前，源码构建产物在后。"""
    return [REPO_ROOT / "runtimes", *sorted((REPO_ROOT / "third_party").glob("*/build"))]


def _use_override(override: str, what: str) -> Path:
    # 与 AppConfig.resolve 一致：相对路径相对仓库根目录，而不是进程的当前目录。
    path = Path(override).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    path = Path(os.path.normpath(path))
    if not path.is_file():
        raise FileNotFoundError(f"配置里指定的{what}不存在：{path}")
    return path


def _search(roots: Sequence[Path], accept: Callable[[str], bool]) -> Path | None:
    """按 ``roots`` 的顺序查找：第一个有命中的根目录胜出，其内取路径最浅的文件。

    深度相同时取文件名最短的（Linux 的 ``libx.so`` 排在 ``libx.so.1`` 前面），
    再相同则按路径字符串排序，保证结果与目录遍历顺序无关。
    """
    for root in roots:
        if not root.is_dir():
            continue
        hits: list[tuple[int, int, str]] = []
        for dirpath, _dirs, files in os.walk(root):
            depth = len(Path(dirpath).relative_to(root).parts)
            for filename in files:
                if accept(filename) and (Path(dirpath) / filename).is_file():
                    hits.append((depth, len(filename), str(Path(dirpath) / filename)))
        if hits:
            return Path(min(hits)[2])
    return None


def _not_found(what: str, searched: Sequence[Path], config_hint: str) -> FileNotFoundError:
    where = "、".join(str(root) for root in searched) or "（没有可查找的目录）"
    return FileNotFoundError(
        f"找不到{what}（已查找：{where}）。"
        f"先运行 python scripts/runtimes.py status 查看推理程序是否已获取，"
        f"或在配置里用 {config_hint} 直接指定路径。"
    )


def find_executable(name: str, override: str = "", *, roots: Sequence[Path] | None = None) -> Path:
    """找名为 ``name`` 或 ``name.exe`` 的可执行文件。

    ``override`` 非空时直接用它（不存在则报错）。``roots`` 供测试替换搜索目录。
    找不到时抛 ``FileNotFoundError``。
    """
    if override:
        return _use_override(override, f"程序 {name}")
    searched = default_roots() if roots is None else list(roots)
    names = {name, f"{name}.exe"}
    found = _search(searched, lambda filename: filename in names)
    if found is None:
        raise _not_found(f"程序 {name}", searched, "launch.executable")
    return found


def _library_pattern(stem: str, platform: str) -> str:
    stem = glob.escape(stem)
    if platform == "win32":
        return f"{stem}.dll"
    if platform == "darwin":
        return f"lib{stem}*.dylib"
    return f"lib{stem}.so*"


def find_library(
    stem: str,
    override: str = "",
    *,
    roots: Sequence[Path] | None = None,
    platform: str | None = None,
) -> Path:
    """找动态库。文件名按平台匹配：Windows ``<stem>.dll``、Linux ``lib<stem>.so*``、
    macOS ``lib<stem>*.dylib``。``platform`` 默认取 ``sys.platform``，供测试覆盖各平台的规则。
    """
    if override:
        return _use_override(override, f"动态库 {stem}")
    searched = default_roots() if roots is None else list(roots)
    pattern = _library_pattern(stem, platform or sys.platform)
    found = _search(searched, lambda filename: fnmatch.fnmatchcase(filename, pattern))
    if found is None:
        raise _not_found(f"动态库 {stem}", searched, "library_path")
    return found
