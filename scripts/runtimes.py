#!/usr/bin/env python3
"""按需获取/构建推理运行时（只处理程序，绝不下载模型权重）。

只依赖标准库，可在建好虚拟环境之前直接运行：

    python scripts/runtimes.py status
    python scripts/runtimes.py variants llama_cpp
    python scripts/runtimes.py fetch llama_cpp            # 自动判断平台/后端
    python scripts/runtimes.py fetch nemo_speech --variant windows-x64-cuda
    python scripts/runtimes.py fetch llama_cpp --with-extras   # 连同 CUDA 运行库
    python scripts/runtimes.py build qwentts --backend cuda
    python scripts/runtimes.py build qwentts --backend cuda --cmake-arg=-DCMAKE_CUDA_ARCHITECTURES=native
    python scripts/runtimes.py source qwentts             # 只初始化子模块源码

版本与资产名来自仓库根目录的 runtimes.lock.toml；预编译包解压到 runtimes/<name>/。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK_FILE = ROOT / "runtimes.lock.toml"
RUNTIMES_DIR = ROOT / "runtimes"
DOWNLOAD_DIR = RUNTIMES_DIR / "_downloads"

# 这些后缀一律拒绝：本脚本不负责权重。
WEIGHT_SUFFIXES = (".gguf", ".safetensors", ".onnx", ".nemo", ".pt", ".pth", ".bin")


def load_lock() -> dict:
    with LOCK_FILE.open("rb") as f:
        return tomllib.load(f)


def get_entry(lock: dict, name: str) -> dict:
    if name not in lock:
        sys.exit(f"未知运行时 {name!r}，可选：{', '.join(lock)}")
    return lock[name]


# --------------------------------------------------------------------------- #
# 平台探测
# --------------------------------------------------------------------------- #


def detect_os_arch() -> tuple[str, str]:
    system = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(platform.system())
    if system is None:
        sys.exit(f"不支持的操作系统：{platform.system()}")
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return system, arch


def detect_cuda_major() -> int | None:
    """返回本机可用的 CUDA Toolkit 大版本；只装了驱动、没装 Toolkit 时返回 None。"""
    candidates: list[int] = []
    cuda_path = os.environ.get("CUDA_PATH")
    roots = [Path(cuda_path).parent] if cuda_path else []
    roots += [Path(r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA"), Path("/usr/local")]
    for root in roots:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            name = child.name.lower()
            for prefix in ("v", "cuda-"):
                if name.startswith(prefix):
                    try:
                        candidates.append(int(name[len(prefix) :].split(".")[0]))
                    except ValueError:
                        pass
    return max(candidates) if candidates else None


def has_nvidia_gpu() -> bool:
    return shutil.which("nvidia-smi") is not None


def default_variant(entry: dict) -> str | None:
    """按 平台 → 后端 的优先级，从锁文件里挑一个存在的 variant。"""
    variants = entry.get("variants", {})
    system, arch = detect_os_arch()
    prefix = f"{system}-{arch}"
    order: list[str] = []
    if system == "macos":
        order = ["metal", "cpu"]
    elif has_nvidia_gpu():
        major = detect_cuda_major()
        # 优先选与本机 Toolkit 大版本一致的；没装 Toolkit 时选 cuda12 并提示 --with-extras。
        order = [f"cuda{major}"] if major else []
        order += ["cuda", "cuda12", "cuda13", "vulkan", "cpu"]
    else:
        order = ["vulkan", "cpu"]
    for backend in order:
        key = f"{prefix}-{backend}"
        if key in variants:
            return key
    return None


# --------------------------------------------------------------------------- #
# 下载与解压
# --------------------------------------------------------------------------- #


def release_url(repo: str, tag: str, asset: str) -> str:
    return f"https://github.com/{repo}/releases/download/{tag}/{asset}"


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  下载 {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "agentic-meeting-runtimes"})
    with urllib.request.urlopen(req) as resp, tmp.open("wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        last_pct = -1
        while chunk := resp.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 // total
                if pct // 10 != last_pct // 10:
                    print(f"    {pct:3d}%  {done >> 20} / {total >> 20} MB", flush=True)
                    last_pct = pct
    tmp.replace(dest)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def verify_sidecar(repo: str, tag: str, asset: str, archive: Path) -> None:
    sidecar = DOWNLOAD_DIR / f"{asset}.sha256"
    download(release_url(repo, tag, f"{asset}.sha256"), sidecar)
    expected = sidecar.read_text(encoding="utf-8").split()[0].strip().lower()
    actual = sha256_of(archive)
    if expected != actual:
        archive.unlink(missing_ok=True)
        sys.exit(f"SHA-256 校验失败：{asset}\n  期望 {expected}\n  实际 {actual}")
    print(f"  SHA-256 校验通过：{actual[:16]}…")


def _safe_target(dest: Path, member_name: str) -> Path:
    target = (dest / member_name).resolve()
    if not target.is_relative_to(dest.resolve()):
        sys.exit(f"压缩包内含越界路径，已中止：{member_name}")
    return target


def extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    print(f"  解压到 {dest.relative_to(ROOT)}")
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                _safe_target(dest, info.filename)
                if info.filename.lower().endswith(WEIGHT_SUFFIXES):
                    sys.exit(f"压缩包里出现疑似权重文件，已中止：{info.filename}")
            zf.extractall(dest)
    else:
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                _safe_target(dest, member.name)
            tf.extractall(dest, filter="data")


def find_executable(base: Path, exe: str) -> Path | None:
    if not base.is_dir():
        return None
    names = {exe, f"{exe}.exe"}
    hits = [p for p in base.rglob("*") if p.is_file() and p.name in names]
    # 路径最浅的优先，避免捡到示例/测试目录里的同名文件。
    return min(hits, key=lambda p: len(p.parts)) if hits else None


def runtime_dir(name: str) -> Path:
    return RUNTIMES_DIR / name


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #


def git(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_LFS_SKIP_SMUDGE="1")  # 不拉 LFS 大文件
    return subprocess.run(["git", *args], cwd=cwd, env=env, text=True, capture_output=True)


def cmd_status(lock: dict, _args: argparse.Namespace) -> None:
    system, arch = detect_os_arch()
    print(
        f"平台：{system}-{arch}   NVIDIA GPU：{'有' if has_nvidia_gpu() else '无'}"
        f"   CUDA Toolkit 大版本：{detect_cuda_major() or '未检测到'}\n"
    )
    for name, entry in lock.items():
        print(f"[{name}] {entry.get('description', '')}")
        src = ROOT / entry["source"]
        head = (
            git("rev-parse", "--short", "HEAD", cwd=src).stdout.strip()
            if (src / ".git").exists()
            else ""
        )
        print(
            f"  源码   {entry['source']}  {'@ ' + head if head else '（子模块未初始化，运行 source 子命令）'}"
            f"  锁定版本 {entry['version']}"
        )
        if entry.get("reference_only"):
            print("  用途   只读参考，无需二进制\n")
            continue
        for exe in entry.get("executables", []):
            hit = find_executable(runtime_dir(name), exe) or find_executable(src / "build", exe)
            where = hit.relative_to(ROOT) if hit else "未安装"
            print(f"  程序   {exe:<14} {where}")
        if entry.get("build_only"):
            print("  获取   仅源码构建：python scripts/runtimes.py build", name)
        else:
            print(
                f"  获取   python scripts/runtimes.py fetch {name}   （默认 variant：{default_variant(entry)}）"
            )
        print()


def cmd_variants(lock: dict, args: argparse.Namespace) -> None:
    entry = get_entry(lock, args.name)
    if entry.get("build_only"):
        print("仅源码构建，可选 backend：", ", ".join(entry.get("build", {})))
        return
    chosen = default_variant(entry)
    for key, assets in entry.get("variants", {}).items():
        mark = "  ← 本机默认" if key == chosen else ""
        print(f"{key:<24} {', '.join(assets)}{mark}")


def cmd_fetch(lock: dict, args: argparse.Namespace) -> None:
    entry = get_entry(lock, args.name)
    if entry.get("build_only") or entry.get("reference_only"):
        sys.exit(f"{args.name} 没有预编译包；请用 build 或 source 子命令。")
    variant = args.variant or default_variant(entry)
    if variant not in entry.get("variants", {}):
        sys.exit(
            f"没有匹配本机的预编译包（{variant}）。可选：{', '.join(entry.get('variants', {}))}"
        )
    assets = list(entry["variants"][variant])
    if args.with_extras:
        assets += entry.get("extras", {}).get(variant, [])
    dest = runtime_dir(args.name)
    if dest.exists() and any(dest.iterdir()):
        if not args.force:
            sys.exit(f"{dest.relative_to(ROOT)} 已存在；加 --force 覆盖。")
        shutil.rmtree(dest)
    print(f"[{args.name}] variant = {variant}")
    for asset in assets:
        archive = DOWNLOAD_DIR / asset
        if not archive.exists():
            download(release_url(entry["repo"], entry["release_tag"], asset), archive)
        else:
            print(f"  已有缓存 {archive.relative_to(ROOT)}")
        if entry.get("sha256_sidecar"):
            verify_sidecar(entry["repo"], entry["release_tag"], asset, archive)
        extract(archive, dest)
    (dest / "VARIANT").write_text(f"{variant}\n{entry['release_tag']}\n", encoding="utf-8")
    for exe in entry.get("executables", []):
        hit = find_executable(dest, exe)
        print(f"  {'✔' if hit else '✘'} {exe}: {hit.relative_to(ROOT) if hit else '未在包内找到'}")
    if (
        variant.split("-")[-1].startswith("cuda")
        and not detect_cuda_major()
        and not args.with_extras
    ):
        print(
            "  提示：未检测到 CUDA Toolkit；若启动时报缺少 cublas/cudart 动态库，"
            "请加 --with-extras --force 重新获取。"
        )


def cmd_source(lock: dict, args: argparse.Namespace) -> None:
    entry = get_entry(lock, args.name)
    res = git("submodule", "update", "--init", "--depth", "1", entry["source"])
    print(res.stdout or res.stderr or f"{entry['source']} 已就绪")
    if res.returncode:
        sys.exit(res.returncode)


def msvc_environment(vcvars_ver: str = "") -> dict[str, str]:
    """Windows：返回带 MSVC 工具链的环境变量。

    当前环境里已经有 cl.exe（例如在 Native Tools 命令提示符里）就直接沿用；否则用 vswhere
    找到最新安装的 vcvars64.bat，执行它并把得到的环境变量读回来。这样普通终端里也能构建。
    ``vcvars_ver`` 用于选择并排安装的旧版工具集（如 "14.44"），对应 vcvars 的 -vcvars_ver 参数。
    """
    if shutil.which("cl") and not vcvars_ver:
        return dict(os.environ)
    program_files = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    vswhere = Path(program_files) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    hint = "请先安装 Visual Studio（或 Build Tools）并勾选「使用 C++ 的桌面开发」。"
    if not vswhere.is_file():
        sys.exit(f"找不到 cl.exe，也找不到 vswhere.exe。{hint}")
    found = subprocess.run(
        [
            str(vswhere),
            "-latest",
            "-products",
            "*",
            "-requires",
            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-find",
            r"VC\Auxiliary\Build\vcvars64.bat",
        ],
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    if not found:
        sys.exit(f"已安装的 Visual Studio 里没有 C++ 工具链。{hint}")
    vcvars = found[0].strip()
    ver_arg = f" -vcvars_ver={vcvars_ver}" if vcvars_ver else ""
    print(f"  导入 MSVC 环境：{vcvars}{ver_arg}", flush=True)
    # /s 让 cmd 去掉最外层那对引号；chcp 65001 让 set 的输出是 UTF-8。
    dump = subprocess.run(
        f'cmd /d /s /c "chcp 65001>nul && "{vcvars}"{ver_arg} >nul 2>&1 && set"',
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout
    env = dict(line.split("=", 1) for line in dump.splitlines() if "=" in line)
    # Windows 的环境变量名不区分大小写，但 Python 的 dict 区分；统一成 PATH 供后面查找。
    if "PATH" not in env and "Path" in env:
        env["PATH"] = env.pop("Path")
    if not shutil.which("cl", path=env.get("PATH")):
        if vcvars_ver:
            sys.exit(
                f"没有找到 {vcvars_ver} 版的 MSVC 工具集。请在 Visual Studio Installer 的"
                "「单个组件」里加装对应版本的「MSVC … C++ x64/x86 生成工具」。"
            )
        sys.exit(f"执行 {vcvars} 后仍找不到 cl.exe。{hint}")
    return env


def probe_nvcc(env: dict[str, str]) -> list[str]:
    """Windows：在跑 cmake 之前先确认 nvcc 能和当前的 MSVC 配合。

    返回需要追加给 cmake 的参数。CUDA Toolkit 只认它发布时已有的 MSVC 版本：
    稍新一点的往往加 -allow-unsupported-compiler 就能用，差得多的会让 nvcc 的前端直接崩溃
    （实测：CUDA 13.0.48 + MSVC 14.51 即 Visual Studio 18）。提前探测能给出明确的结论，
    而不是让用户去读几百行的 CMake 报错。
    """
    cuda_path = env.get("CUDA_PATH", "")
    nvcc = shutil.which("nvcc", path=env.get("PATH")) or (
        str(Path(cuda_path) / "bin" / "nvcc.exe") if cuda_path else None
    )
    if not nvcc or not Path(nvcc).is_file():
        sys.exit(
            "找不到 nvcc。请安装完整的 CUDA Toolkit（只装显卡驱动不够），或改用 --backend cpu。"
        )
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "probe.cu").write_text(
            "__global__ void k(float *x) { x[0] = 1.0f; }\nint main() { return 0; }\n",
            encoding="ascii",
        )

        def compile_with(flags: list[str]) -> subprocess.CompletedProcess:
            return subprocess.run(
                [nvcc, *flags, "-c", "probe.cu", "-o", "probe.obj"],
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
                errors="replace",
            )

        if compile_with([]).returncode == 0:
            return []
        override = "-allow-unsupported-compiler"
        second = compile_with([override])
        if second.returncode == 0:
            print(
                f"  注意：nvcc 不认识当前的 MSVC 版本，已加 {override} 跳过它的版本检查", flush=True
            )
            return [f"-DCMAKE_CUDA_FLAGS={override}"]
        tail = (second.stdout + second.stderr).strip().splitlines()[-1:]
    version = subprocess.run([nvcc, "--version"], capture_output=True, text=True).stdout
    release = next((ln.strip() for ln in version.splitlines() if "release" in ln), "版本未知")
    sys.exit(
        "nvcc 无法与当前的 MSVC 配合，CUDA 版无法构建。\n"
        f"  CUDA：{release}\n"
        f"  MSVC：{env.get('VCToolsVersion', '版本未知')}\n"
        f"  nvcc 的报错：{tail[0] if tail else '（无输出）'}\n"
        "可选的解决办法：\n"
        "  1. 安装支持当前 Visual Studio 的更新版 CUDA Toolkit；\n"
        "  2. 在 Visual Studio Installer 的「单个组件」里加装旧一代的 MSVC 工具集，\n"
        "     然后加 --vcvars-ver <版本号，如 14.44> 重新构建；\n"
        "  3. 先用 --backend cpu 构建一个不依赖 CUDA 的版本。"
    )


def cmd_build(lock: dict, args: argparse.Namespace) -> None:
    entry = get_entry(lock, args.name)
    backends = entry.get("build")
    if not backends:
        sys.exit(
            f"{args.name} 未在 runtimes.lock.toml 里声明构建方式；参见 docs/runtimes.md 手工构建。"
        )
    if args.backend not in backends:
        sys.exit(f"backend 可选：{', '.join(backends)}")
    src = ROOT / entry["source"]
    cmd_source(lock, args)
    # 该运行时自己的子模块（如 qwentts.cpp 的 ggml）。
    res = git("submodule", "update", "--init", "--depth", "1", cwd=src)
    if res.returncode:
        sys.exit(res.stderr)
    on_windows = platform.system() == "Windows"
    env = msvc_environment(args.vcvars_ver) if on_windows else dict(os.environ)
    cmake = shutil.which("cmake", path=env.get("PATH"))
    if cmake is None:
        sys.exit("找不到 cmake，请先安装（3.26 及以上）。")
    extra = probe_nvcc(env) if on_windows and args.backend == "cuda" else []
    build_dir = src / "build"
    if args.clean and build_dir.exists():
        print(f"  清理 {build_dir.relative_to(ROOT)}", flush=True)
        shutil.rmtree(build_dir)
    configure = [
        cmake,
        "-S",
        str(src),
        "-B",
        str(build_dir),
        "-DCMAKE_BUILD_TYPE=Release",
        *backends[args.backend],
        *extra,
        *args.cmake_arg,
    ]
    if shutil.which("ninja", path=env.get("PATH")):
        configure += ["-G", "Ninja"]
    for step in (configure, [cmake, "--build", str(build_dir), "--config", "Release", "-j"]):
        print("$", " ".join(step), flush=True)
        subprocess.run(step, check=True, env=env)
    for exe in entry.get("executables", []):
        hit = find_executable(build_dir, exe)
        print(f"  {'✔' if hit else '✘'} {exe}: {hit.relative_to(ROOT) if hit else '未生成'}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="列出各运行时的源码/二进制状态").set_defaults(func=cmd_status)

    p = sub.add_parser("variants", help="列出某运行时可下载的预编译包")
    p.add_argument("name")
    p.set_defaults(func=cmd_variants)

    p = sub.add_parser("fetch", help="下载并解压预编译包到 runtimes/<name>/")
    p.add_argument("name")
    p.add_argument("--variant", help="不填则按本机平台自动选择")
    p.add_argument("--with-extras", action="store_true", help="同时获取 CUDA 运行库等附加包")
    p.add_argument("--force", action="store_true", help="覆盖已有目录")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("source", help="初始化某运行时的源码子模块（浅克隆）")
    p.add_argument("name")
    p.set_defaults(func=cmd_source)

    p = sub.add_parser("build", help="从源码构建（仅对锁文件里声明了 build 的运行时）")
    p.add_argument("name")
    p.add_argument("--backend", required=True, help="cuda / vulkan / metal / cpu")
    p.add_argument(
        "--cmake-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="追加给 cmake 配置步骤的参数，可重复；例如 --cmake-arg=-DCMAKE_CUDA_ARCHITECTURES=native",
    )
    p.add_argument("--clean", action="store_true", help="先删除已有的 build 目录再构建")
    p.add_argument(
        "--vcvars-ver",
        default="",
        metavar="VER",
        help="仅 Windows：改用并排安装的旧版 MSVC 工具集，例如 14.44（CUDA 不认新版 MSVC 时用）",
    )
    p.set_defaults(func=cmd_build)

    args = parser.parse_args()
    args.func(load_lock(), args)


if __name__ == "__main__":
    # 输出被重定向到文件或管道时，Windows 默认用本地代码页；两个流统一成 UTF-8，避免中文乱码。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    main()
