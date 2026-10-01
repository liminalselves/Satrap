"""构建内置 Python, 全部依赖和 WebUI 的 Windows x64 便携发行包"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = "3.13.15"


def run(command: list[str], cwd: Path = ROOT) -> None:
    """使用 UTF-8 执行构建工具, 失败立即停止"""
    print("执行: " + subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True, encoding="utf-8")


def digest(path: Path) -> str:
    """流式计算文件 SHA-256"""
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def download_runtime(cache: Path) -> Path:
    """从 Python 官方下载固定版本运行时并验证发布清单哈希"""
    filename = f"python-{PYTHON_VERSION}-embeddable-amd64.zip"
    base = f"https://www.python.org/ftp/python/{PYTHON_VERSION}"
    with urllib.request.urlopen(f"{base}/windows-{PYTHON_VERSION}.json", timeout=60) as response:
        manifest = json.loads(response.read().decode("utf-8"))
    expected = None
    for version in manifest["versions"]:
        if version.get("url", "").endswith("/" + filename):
            expected = version.get("hash", {}).get("sha256")
    if not expected:
        raise RuntimeError("Python 官方清单中未找到嵌入式运行时 SHA-256")
    expected = expected.removeprefix("sha256:")
    target = cache / filename
    if not target.is_file() or digest(target) != expected:
        print(f"下载 Python {PYTHON_VERSION} 嵌入式运行时...", flush=True)
        with urllib.request.urlopen(f"{base}/{filename}", timeout=120) as response:
            with target.open("wb") as output:
                shutil.copyfileobj(response, output)
    if digest(target) != expected:
        target.unlink()
        raise RuntimeError("Python 运行时 SHA-256 校验失败")
    return target


def copy_sources(stage: Path) -> None:
    """只复制 Git 跟踪的应用文件, 避免混入本地配置与运行数据"""
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z", "--", "satrap"], cwd=ROOT,
    ).decode("utf-8").split("\0")
    if not any(name.endswith("satrap/__main__.py") for name in tracked):
        raise RuntimeError("未找到 Git 跟踪的 Satrap 源码, 请在项目仓库中构建")
    for name in tracked:
        if not name:
            continue
        source = ROOT / name
        target = stage / name
        if source.is_symlink() or not source.resolve().is_relative_to(ROOT):
            raise RuntimeError(f"拒绝复制源码目录之外的文件: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def archive(stage: Path, target: Path) -> None:
    """压缩可移动发行目录, 排除首次验证产生的缓存和运行数据"""
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for file in sorted(stage.rglob("*")):
            if not file.is_file():
                continue
            relative = file.relative_to(stage)
            if ".satrap" in relative.parts or "__pycache__" in relative.parts or file.suffix == ".pyc":
                continue
            output.write(file, Path(stage.name) / relative)


def build(skip_frontend: bool, output_dir: Path, constraints: Path | None) -> Path:
    """组装运行时, 依赖, 源码, 前端与用户入口并输出 ZIP"""
    if sys.platform != "win32" or sys.maxsize <= 2**32 or sys.version_info[:2] != (3, 13):
        raise RuntimeError("请使用 Windows x64 Python 3.13 执行构建")
    os.environ.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PIP_DISABLE_PIP_VERSION_CHECK="1")
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")   # type: ignore
    if not skip_frontend:
        npm = shutil.which("npm.cmd")
        if npm is None:
            raise RuntimeError("构建前端需要 Node.js 与 npm")
        run([npm, "ci"], ROOT / "satrap-ui")
        run([npm, "run", "build"], ROOT / "satrap-ui")
    if not (ROOT / "satrap-ui/dist/index.html").is_file():
        raise RuntimeError("前端未构建, 不能创建开箱即用的发行包")

    cache = ROOT / "build/release-cache"
    cache.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    runtime_zip = download_runtime(cache)
    with tempfile.TemporaryDirectory(prefix="release-", dir=ROOT / "build") as workspace:
        work = Path(workspace)
        wheel_dir = work / "wheels"
        run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheel_dir), str(ROOT)])
        run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheel_dir), "aiocqhttp==1.4.4"])
        wheel = next(wheel_dir.glob("satrap-*.whl"))
        with zipfile.ZipFile(wheel) as package:
            metadata_name = next(name for name in package.namelist() if name.endswith(".dist-info/METADATA"))
            metadata = BytesParser().parsebytes(package.read(metadata_name))
        version = metadata["Version"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]*", version):
            raise RuntimeError("不支持的发行版本号")
        name = f"Satrap-v{version}-windows-x64"
        stage = work / name
        runtime = stage / "runtime"
        runtime.mkdir(parents=True)
        with zipfile.ZipFile(runtime_zip) as package:
            package.extractall(runtime)
        (runtime / "python313._pth").write_text(
            "python313.zip\n.\nLib/site-packages\n..\nimport site\n", encoding="utf-8",
        )
        site = runtime / "Lib/site-packages"
        command = [
            sys.executable, "-m", "pip", "install", "--only-binary=:all:",
            "--no-compile", "--target", str(site), str(wheel) + "[all]",
            "-r", str(ROOT / "scripts/release/requirements.in"),
            "--find-links", str(wheel_dir),
        ]
        if constraints:
            command.extend(["--constraint", str(constraints.resolve())])
        run(command)
        installed_app = site / "satrap"
        if not installed_app.resolve().is_relative_to(work):
            raise RuntimeError("依赖安装目录越界")
        shutil.rmtree(installed_app)
        copy_sources(stage)
        shutil.copytree(ROOT / "satrap-ui/dist", stage / "satrap-ui/dist")
        for filename in ("LICENSE", "config.example.yaml"):
            shutil.copy2(ROOT / filename, stage / filename)
        for filename in ("start.bat", "stop.bat", "satrap.bat"):
            shutil.copy2(ROOT / "scripts/release" / filename, stage / filename)
        release = stage / "release"
        release.mkdir()
        shutil.copy2(ROOT / "scripts/release/launcher.py", release / "launcher.py")
        shutil.copy2(ROOT / "scripts/release/smoke_release.py", release / "smoke_release.py")
        shutil.copy2(ROOT / "docs/getting-started/release.md", stage / "使用说明.md")

        packages = json.loads(subprocess.check_output([
            sys.executable, "-m", "pip", "list", "--path", str(site), "--format=json",
        ], encoding="utf-8"))
        requirements = "\n".join(sorted(f"{item['name']}=={item['version']}" for item in packages if item['name'] != "satrap")) + "\n"
        (stage / "requirements.lock.txt").write_text(requirements, encoding="utf-8")
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, encoding="utf-8").strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT))
        (stage / "release.json").write_text(json.dumps({
            "version": version, "platform": "windows-x64", "python": PYTHON_VERSION,
            "python_sha256": digest(runtime_zip), "commit": revision, "dirty": dirty,
            "built_at": datetime.now(timezone.utc).isoformat(), "packages": packages,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        run([str(runtime / "python.exe"), "-X", "utf8", str(release / "smoke_release.py")], stage)
        temporary_zip = work / f"{name}.zip"
        archive(stage, temporary_zip)
        target = output_dir / temporary_zip.name
        shutil.move(str(temporary_zip), str(target))
        (output_dir / f"{name}.sha256").write_text(f"{digest(target)}  {target.name}\n", encoding="utf-8")
        shutil.copy2(stage / "requirements.lock.txt", output_dir / f"{name}.requirements.txt")
        print(f"发行包已生成: {target} ({target.stat().st_size / 1024**2:.1f} MiB)", flush=True)
        return target


def main() -> None:
    """读取构建参数, 默认重新构建前端"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-frontend", action="store_true", help="复用已构建的 satrap-ui/dist")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--constraints", type=Path, default=ROOT / "scripts/release/constraints-windows-x64.txt", help="指定精确依赖版本, 默认使用仓库发行锁定文件")
    args = parser.parse_args()
    build(args.skip_frontend, args.output_dir.resolve(), args.constraints)


if __name__ == "__main__":
    main()
