#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Minecraft 完整下载器（命令行，无 GUI）

基于 minecraft-launcher-lib（https://github.com/JakobDev/minecraft-launcher-lib）：
  * 原版：version json / client jar / libraries / natives / assets / logging
  * Forge：自动挑选版本、下载 installer 并执行其中的 processors
  * NeoForge / Fabric / Quilt：同样走官方安装流程
  * OptiFine：由库调用官方安装器
所以下载结果是完整的版本文件，而不是只凑齐文件。本工具只下载，不负责启动游戏。

用法（先 pip install -r requirements.txt）：
    python mc_download.py                                    # 交互向导
    python mc_download.py --list
    python mc_download.py --version 1.20.4                   # 装到 ./.minecraft
    python mc_download.py --version 1.20.1 --loader forge
    python mc_download.py --version 1.20.4 --loader fabric
    python mc_download.py --version 1.20.1 --loader forge --dir "D:\\MC\\.minecraft"

默认目录是当前目录下的 .minecraft，可用 --dir 指定（支持相对路径和 ~）。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import platform
import queue
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

try:
    import minecraft_launcher_lib as mll
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "缺少依赖 minecraft-launcher-lib。\n"
        "请先运行：pip install -r requirements.txt\n"
    )
    sys.exit(2)

# 库的子模块默认不一定被导入，这里显式加载，保证 getattr 能拿到
# 注意：不加载 command / runtime，本工具没有任何启动游戏的代码路径
for _name in ("install", "forge", "fabric", "quilt", "neo_forge", "neoforge",
              "optifine", "utils"):
    try:
        importlib.import_module("minecraft_launcher_lib." + _name)
    except Exception:
        pass
del _name


# ----------------------------------------------------------------------------
# 下载源：支持多个源按优先级回退（镜像规则取自 PCL2 的 DlSource）
# ----------------------------------------------------------------------------

BUILTIN_SOURCES = [
    ("official", None),
    ("bmclapi", "https://bmclapi2.bangbang93.com"),
]

# 兼容旧写法
SOURCE_ALIASES = {
    "auto": "bmclapi,official",
    "mirror": "bmclapi",
}

_ORIGINAL_REQUEST = None


def build_source_registry(custom_mirrors):
    """内置源 + 通过 --mirror NAME=URL 追加的自定义源。"""
    registry = {}
    for name, base in BUILTIN_SOURCES:
        registry[name] = base

    for item in custom_mirrors or []:
        if "=" not in item:
            raise SystemExit("--mirror 需要 NAME=URL 格式，例如 --mirror my=https://mc.example.com")
        name, base = item.split("=", 1)
        name = name.strip()
        base = base.strip().rstrip("/")
        if not name or not base.startswith(("http://", "https://")):
            raise SystemExit(f"--mirror 格式有误：{item}")
        registry[name] = base

    return registry


def parse_source_chain(spec, registry):
    """把 'bmclapi,official' 这样的字符串解析成 [(名称, 基础地址), ...]。"""
    text = (spec or "auto").strip()
    text = SOURCE_ALIASES.get(text, text)
    names = [part.strip() for part in text.replace("，", ",").split(",") if part.strip()]
    if not names:
        names = ["bmclapi", "official"]

    chain = []
    for name in names:
        if name not in registry:
            raise SystemExit(f"未知的下载源：{name}（可用：{', '.join(registry)}）")
        chain.append((name, registry[name]))
    return chain


def mirror_url(url, base):
    """把官方地址换成镜像地址（BMCLAPI 路径约定）；该镜像不覆盖这个地址时返回 None。"""
    if not base or not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    if url.startswith(base):
        return None

    # 版本清单 / 版本 Json / 主 Jar / 资源索引：整段路径照搬
    for host in (
        "https://piston-data.mojang.com",
        "https://piston-meta.mojang.com",
        "https://launcher.mojang.com",
        "https://launchermeta.mojang.com",
    ):
        if url.startswith(host):
            return base + url[len(host):]

    # 资源文件
    if url.startswith("https://resources.download.minecraft.net"):
        return base + "/assets" + url[len("https://resources.download.minecraft.net"):]

    # 支持库 maven（原版 / Forge / Fabric / NeoForge）
    for host in (
        "https://libraries.minecraft.net",
        "https://maven.minecraftforge.net",
        "https://maven.fabricmc.net",
    ):
        if url.startswith(host):
            return base + "/maven" + url[len(host):]
    if url.startswith("https://maven.neoforged.net/releases"):
        return base + "/maven" + url[len("https://maven.neoforged.net/releases"):]

    # Forge 版本清单
    if url == "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json":
        return base + "/forge/promotions_slim.json"
    if url.startswith("https://files.minecraftforge.net"):
        return base + "/forge" + url[len("https://files.minecraftforge.net"):]

    # Fabric meta
    if url.startswith("https://meta.fabricmc.net"):
        return base + "/fabric-meta" + url[len("https://meta.fabricmc.net"):]

    return None


def enable_sources(chain):
    """
    把 minecraft-launcher-lib 内部的 HTTP GET 按 chain 顺序重定向：
    靠前的源优先，命中 4xx / 网络错误就自动试下一个源。
    返回用于显示的源名称；无法启用时返回 None。
    """
    global _ORIGINAL_REQUEST

    if all(base is None for _, base in chain):
        return "官方源"

    try:
        import requests
    except ImportError:
        return None

    if _ORIGINAL_REQUEST is None:
        _ORIGINAL_REQUEST = requests.sessions.Session.request
    original_request = _ORIGINAL_REQUEST

    def patched(self, method, url, *args, **kwargs):
        if method.upper() != "GET":
            return original_request(self, method, url, *args, **kwargs)

        candidates = []
        for _, base in chain:
            candidate = url if base is None else (mirror_url(url, base) or url)
            if candidate not in candidates:
                candidates.append(candidate)

        if len(candidates) == 1 and candidates[0] == url:
            return original_request(self, method, url, *args, **kwargs)

        last_error = None
        last_response = None
        for candidate in candidates:
            try:
                response = original_request(self, method, candidate, *args, **kwargs)
            except Exception as exc:  # 网络错误，换下一个源
                last_error = exc
                continue
            if response.status_code < 400:
                return response
            last_response = response

        if last_response is not None:
            return last_response
        if last_error is not None:
            raise last_error
        return original_request(self, method, url, *args, **kwargs)

    requests.sessions.Session.request = patched
    return " → ".join(name for name, _ in chain)


# ----------------------------------------------------------------------------
# 兼容不同版本的 minecraft-launcher-lib：函数名有小幅变化，这里做一层探测
# ----------------------------------------------------------------------------

def submodule(*names):
    """返回第一个存在的子模块（例如 fabric / quilt / neo_forge）。"""
    for name in names:
        module = getattr(mll, name, None)
        if module is not None:
            return module
    return None


def function(module, *names):
    """返回模块里第一个可调用的函数。"""
    if module is None:
        return None
    for name in names:
        fn = getattr(module, name, None)
        if callable(fn):
            return fn
    return None


# ----------------------------------------------------------------------------
# 进度显示
# ----------------------------------------------------------------------------

class Progress:
    """把库的 callback 字典转成一行进度条。"""

    def __init__(self):
        self.status = ""
        self.current = 0
        self.max = 0
        self._last = 0.0

    def callbacks(self):
        return {
            "setStatus": self.set_status,
            "setProgress": self.set_progress,
            "setMax": self.set_max,
        }

    def set_status(self, text):
        self.status = text or ""
        self._render(force=True)

    def set_progress(self, value):
        try:
            self.current = int(value)
        except (TypeError, ValueError):
            self.current = 0
        self._render()

    def set_max(self, value):
        try:
            self.max = int(value)
        except (TypeError, ValueError):
            self.max = 0
        self._render(force=True)

    def _render(self, force=False):
        now = time.time()
        if not force and now - self._last < 0.15:
            return
        self._last = now
        if self.max > 0:
            percent = min(100.0, self.current * 100.0 / self.max)
            length = 28
            filled = int(length * percent / 100)
            bar = "#" * filled + "-" * (length - filled)
            line = f"\r[{bar}] {percent:5.1f}%  {self.status}"
        else:
            line = f"\r{self.status}"
        sys.stdout.write(line[:110].ljust(110))
        sys.stdout.flush()

    def done(self):
        sys.stdout.write("\r" + " " * 110 + "\r")
        sys.stdout.flush()


# ----------------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------------

DEFAULT_DIR_NAME = ".minecraft"


def default_directory():
    """默认放在「当前目录/.minecraft」，与 PCL 的便携习惯一致；可用 --dir 覆盖。"""
    return os.path.abspath(DEFAULT_DIR_NAME)


def resolve_directory(path):
    """把 --dir / 向导输入解析成绝对路径，支持相对路径和 ~。"""
    text = (path or "").strip().strip('"').strip("'")
    if not text:
        return default_directory()
    return os.path.abspath(os.path.expanduser(text))


def find_version_dir(directory, prefix):
    """在 versions/ 里找以 prefix 开头、最近修改的版本文件夹（用于推断安装后的版本 ID）。"""
    versions_dir = os.path.join(directory, "versions")
    if not os.path.isdir(versions_dir):
        return None
    matches = []
    for name in os.listdir(versions_dir):
        if name.lower().startswith(prefix.lower()):
            path = os.path.join(versions_dir, name)
            if os.path.isdir(path):
                matches.append((os.path.getmtime(path), name))
    if not matches:
        return None
    matches.sort()
    return matches[-1][1]


def list_versions(keyword):
    utils = submodule("utils")
    fn = function(utils, "get_version_list")
    if not fn:
        print("当前 minecraft-launcher-lib 版本不支持列出版本，请升级：pip install -U minecraft-launcher-lib")
        return
    try:
        versions = fn()
    except Exception as exc:
        print(f"获取版本列表失败：{exc}")
        return

    keyword = (keyword or "").lower()
    count = 0
    for item in versions:
        version_id = str(item.get("id", ""))
        version_type = str(item.get("type", ""))
        if keyword and keyword not in version_id.lower() and keyword not in version_type.lower():
            continue
        print(f"{version_id:30} {version_type:10} {item.get('releaseTime', '')}")
        count += 1
    print(f"\n共 {count} 个版本")


# ----------------------------------------------------------------------------
# 各加载器
# ----------------------------------------------------------------------------

def install_vanilla(directory, version, cb):
    install = submodule("install")
    fn = function(install, "install_minecraft_version")
    if not fn:
        raise RuntimeError("minecraft-launcher-lib 缺少 install.install_minecraft_version")
    fn(version, directory, callback=cb)
    return version


def forge_version_list():
    module = submodule("forge")
    fn = function(module, "get_forge_versions", "get_forge_version_list")
    if not fn:
        return []
    try:
        return list(fn())
    except Exception:
        return []


def pick_forge_version(mc_version, wanted):
    if wanted:
        return wanted

    module = submodule("forge")
    finder = function(module, "find_forge_version")
    if finder:
        try:
            found = finder(mc_version)
            if found:
                return found
        except Exception:
            pass

    candidates = [v for v in forge_version_list() if v.startswith(mc_version + "-")]
    # 跳过 beta 之类，优先纯数字版本
    stable = [v for v in candidates if all(part.isdigit() for part in v.split("-")[-1].split("."))]
    pool = stable or candidates
    if not pool:
        raise RuntimeError(f"没有找到 Minecraft {mc_version} 可用的 Forge 版本，请用 --loader-version 指定")
    return pool[-1]


def install_forge(directory, mc_version, forge_version, cb, java=None):
    module = submodule("forge")
    fn = function(module, "install_forge_version")
    if not fn:
        raise RuntimeError("minecraft-launcher-lib 缺少 forge.install_forge_version")

    kwargs = {"callback": cb}
    if java:
        kwargs["java"] = java
    try:
        result = fn(forge_version, directory, **kwargs)
    except TypeError:
        # 老版本没有 java 参数
        kwargs.pop("java", None)
        result = fn(forge_version, directory, **kwargs)

    if isinstance(result, str) and result:
        return result
    return f"{mc_version}-forge-{forge_version}"


def neoforge_prefix(mc_version):
    """NeoForge 版本号与 MC 版本的对应：1.20.1 → 47.x，其余 1.20.2+ → <minor>.<patch>.x。"""
    try:
        parts = mc_version.split(".")
        minor = int(parts[1])
        patch = int(parts[2]) if len(parts) > 2 else 0
    except (IndexError, ValueError):
        return None
    if mc_version == "1.20.1":
        return "47."
    if (minor, patch) >= (20, 2):
        return f"{minor}.{patch}."
    return None


def neoforge_versions():
    module = submodule("neo_forge", "neoforge")
    fn = function(module, "get_neo_forge_versions", "get_neoforge_versions", "get_versions")
    if not fn:
        return []
    try:
        return list(fn())
    except Exception:
        return []


def pick_neoforge_version(mc_version, wanted):
    if wanted:
        return wanted
    prefix = neoforge_prefix(mc_version)
    if not prefix:
        raise RuntimeError(f"无法推断 Minecraft {mc_version} 对应的 NeoForge 版本号，请用 --loader-version 指定")
    candidates = [v for v in neoforge_versions() if v.startswith(prefix)]
    stable = [v for v in candidates if "-" not in v]
    pool = stable or candidates
    if not pool:
        raise RuntimeError(f"没有找到 Minecraft {mc_version} 可用的 NeoForge 版本，请用 --loader-version 指定")
    return pool[-1]


def install_neoforge(directory, neo_version, cb):
    module = submodule("neo_forge", "neoforge")
    fn = function(module, "install_neo_forge", "install_neoforge")
    if not fn:
        raise RuntimeError("当前 minecraft-launcher-lib 版本不支持 NeoForge，请升级：pip install -U minecraft-launcher-lib")
    result = fn(neo_version, directory, callback=cb)
    if isinstance(result, str) and result:
        return result
    return f"neoforge-{neo_version}"


def install_fabric(directory, mc_version, loader_version, cb):
    module = submodule("fabric")
    fn = function(module, "install_fabric")
    if not fn:
        raise RuntimeError("当前 minecraft-launcher-lib 版本不支持 Fabric")

    kwargs = {"callback": cb}
    if loader_version:
        kwargs["loader_version"] = loader_version
    result = fn(mc_version, directory, **kwargs)
    if isinstance(result, str) and result:
        return result
    if loader_version:
        return f"fabric-loader-{loader_version}-{mc_version}"
    found = find_version_dir(directory, "fabric-loader-")
    if found:
        return found
    raise RuntimeError("Fabric 安装完成但无法确定版本 ID，请用 --loader-version 明确指定")


def install_quilt(directory, mc_version, loader_version, cb):
    module = submodule("quilt")
    fn = function(module, "install_quilt")
    if not fn:
        raise RuntimeError("当前 minecraft-launcher-lib 版本不支持 Quilt")

    kwargs = {"callback": cb}
    if loader_version:
        kwargs["loader_version"] = loader_version
    result = fn(mc_version, directory, **kwargs)
    if isinstance(result, str) and result:
        return result
    if loader_version:
        return f"quilt-loader-{loader_version}-{mc_version}"
    found = find_version_dir(directory, "quilt-loader-")
    if found:
        return found
    raise RuntimeError("Quilt 安装完成但无法确定版本 ID，请用 --loader-version 明确指定")


def install_optifine(directory, optifine_version, cb):
    module = submodule("optifine")
    fn = function(module, "install_optifine")
    if not fn:
        raise RuntimeError("当前 minecraft-launcher-lib 版本不支持 OptiFine")
    result = fn(optifine_version, directory, callback=cb)
    if isinstance(result, str) and result:
        return result
    return f"OptiFine_{optifine_version}"


# ----------------------------------------------------------------------------
# 启动
# ----------------------------------------------------------------------------

NETWORK_ERROR_HINTS = (
    "timeout", "timed out", "connection", "max retries", "ssl", "proxy",
    "getaddrinfo", "temporary failure", "network", "eof", "reset",
)


def is_network_error(message):
    text = (message or "").lower()
    return any(hint in text for hint in NETWORK_ERROR_HINTS)


def loader_supports(loader, mc_version):
    """Fabric / Quilt 是否支持该 MC 版本；查不到时返回 None（不阻塞安装）。"""
    if loader not in ("fabric", "quilt"):
        return None

    url = ("https://meta.fabricmc.net/v2/versions/game"
           if loader == "fabric"
           else "https://meta.quiltmc.org/v3/versions/game")
    try:
        import requests
        response = requests.get(url, timeout=20)
        if response.status_code >= 400:
            return None
        data = response.json()
        versions = set()
        for item in data:
            if isinstance(item, dict):
                versions.add(str(item.get("version")))
        return mc_version in versions
    except Exception:
        return None


# ----------------------------------------------------------------------------
# 多线程下载引擎（自实现，用于原版 / Fabric / Quilt）
# ----------------------------------------------------------------------------

MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest.json"
OS_NAME = {"Windows": "windows", "Darwin": "osx"}.get(platform.system(), "linux")


def sha1_of(path):
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rules_allow(rules):
    """按版本 Json 的 rules 判断当前系统是否需要这个文件（等价 PCL2 的 McJsonRuleCheck）。"""
    if not rules:
        return True
    allowed = False
    for rule in rules:
        action = str(rule.get("action", "allow"))
        matched = True
        os_rule = rule.get("os") or {}
        if "name" in os_rule and os_rule["name"] != OS_NAME:
            matched = False
        if "arch" in os_rule:
            arch = str(os_rule["arch"])
            if arch == "x86" or "arm" in arch:
                matched = False
        features = rule.get("features") or {}
        if features.get("is_demo_user"):
            matched = False
        if any("quick_play" in key for key in features):
            matched = False
        if matched:
            allowed = action == "allow"
    return allowed


def maven_rel(name):
    """group:artifact:version[:classifier] → maven 相对路径。"""
    parts = name.split(":")
    group, artifact, version = parts[0], parts[1], parts[2]
    classifier = parts[3] if len(parts) > 3 else None
    filename = f"{artifact}-{version}" + (f"-{classifier}" if classifier else "") + ".jar"
    return "/".join(group.split(".") + [artifact, version, filename])


class DownloadTask:
    __slots__ = ("url", "path", "sha1", "size")

    def __init__(self, url, path, sha1=None, size=None):
        self.url = url
        self.path = path
        self.sha1 = (sha1 or "").lower() or None
        self.size = int(size) if size else None


def abort_now():
    """Ctrl+C 时立刻退出，不等线程池里的连接超时。"""
    try:
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(130)


def _human_size(value):
    size = float(value or 0)
    if size < 1024:
        return f"{int(size)}B"
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024.0
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}"
    return f"{size:.1f}TB"


def _shorten(text, width):
    text = str(text)
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return "..." + text[-(width - 3):]


def _enable_ansi():
    """终端是否支持光标移动（Windows 上顺便打开 VT 处理）。"""
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            return False
    return True


class DownloadBoard:
    """多行进度面板：每个下载线程占一行，显示当前文件自己的进度条。"""

    def __init__(self, slots, root=""):
        self.slots = max(1, int(slots))
        self.root = root or ""
        self.active = _enable_ansi()
        # 终端放不下就不显示那么多行，避免把屏幕刷爆
        terminal_lines = shutil.get_terminal_size((100, 24)).lines
        self.display_slots = min(self.slots, max(1, terminal_lines - 3))
        self._lock = threading.Lock()
        self._render_lock = threading.Lock()
        self._rows = [None] * self.display_slots
        self._total = 0
        self._done = 0
        self._started = False
        self._last_render = 0.0

    def start(self, total):
        self._total = total
        self._done = 0
        if not self.active:
            return
        with self._render_lock:
            sys.stdout.write("\n" * (self.display_slots + 1))
            sys.stdout.flush()
            self._started = True
        self.render(force=True)

    def set_row(self, slot, name, done, total):
        if slot >= self.display_slots:
            return
        with self._lock:
            self._rows[slot] = (name, done, total)
        self.render()

    def clear_row(self, slot):
        if slot >= self.display_slots:
            return
        with self._lock:
            self._rows[slot] = None
        self.render(force=True)

    def file_done(self):
        with self._lock:
            self._done += 1
        self.render(force=True)

    def stop(self):
        self._started = False

    def _format_row(self, name, done, total, width):
        bar_width = 18
        if total and total > 0:
            percent = min(100.0, done * 100.0 / total)
            filled = int(bar_width * percent / 100)
            bar = "#" * filled + "-" * (bar_width - filled)
            size_text = f"{_human_size(done)}/{_human_size(total)}"
        else:
            percent = 0.0
            bar = "-" * bar_width
            size_text = _human_size(done)
        prefix = f"[{bar}] {percent:5.1f}% {size_text:>17} "
        room = max(8, width - len(prefix) - 1)
        return prefix + _shorten(name, room)

    def render(self, force=False):
        if not self.active or not self._started:
            return
        now = time.time()
        if not force and now - self._last_render < 0.1:
            return
        if not self._render_lock.acquire(blocking=False):
            return
        try:
            self._last_render = now
            width = shutil.get_terminal_size((100, 24)).columns
            with self._lock:
                rows = list(self._rows)
                done = self._done
            lines = ["" if row is None else self._format_row(row[0], row[1], row[2], width) for row in rows]
            lines.append(_shorten(f"总进度 {done}/{self._total} 个文件", width))
            output = ["\033[%dA" % (self.display_slots + 1)]
            for line in lines:
                output.append("\033[2K" + line + "\n")
            sys.stdout.write("".join(output))
            sys.stdout.flush()
        finally:
            self._render_lock.release()


class ParallelDownloader:
    """固定线程池下载；已存在且校验通过的文件直接跳过。"""

    def __init__(self, threads=8, root=""):
        self.threads = max(1, int(threads))
        self.root = root or ""
        self._lock = threading.Lock()
        self._done = 0
        self._total = 0
        self._failed = []
        self._board = DownloadBoard(1, self.root)
        self._slot_queue = queue.Queue()
        self._cancelled = threading.Event()

    def _valid(self, task):
        """已有文件是否可信：先比大小，再比 SHA1；空文件视为无效。"""
        if not os.path.isfile(task.path):
            return False
        try:
            file_size = os.path.getsize(task.path)
        except OSError:
            return False
        if file_size <= 0:
            return False
        if task.size and file_size != task.size:
            return False
        if task.sha1:
            try:
                return sha1_of(task.path) == task.sha1
            except OSError:
                return False
        return True

    def _display_name(self, path):
        if self.root:
            try:
                return os.path.relpath(path, self.root)
            except ValueError:
                pass
        return os.path.basename(path)

    def _fetch(self, task, slot=0):
        import requests
        if self._cancelled.is_set():
            return False
        temp = task.path + ".tmp"
        name = self._display_name(task.path)
        try:
            folder = os.path.dirname(task.path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            with requests.get(task.url, timeout=30, stream=True) as response:
                response.raise_for_status()
                total = task.size or int(response.headers.get("Content-Length") or 0)
                self._board.set_row(slot, name, 0, total)
                written = 0
                with open(temp, "wb") as handle:
                    for chunk in response.iter_content(1024 * 128):
                        if self._cancelled.is_set():
                            raise RuntimeError("canceled")
                        if not chunk:
                            continue
                        handle.write(chunk)
                        written += len(chunk)
                        self._board.set_row(slot, name, written, total)
            if task.sha1 and sha1_of(temp) != task.sha1:
                os.remove(temp)
                return False
            if task.size and os.path.getsize(temp) != task.size:
                os.remove(temp)
                return False
            os.replace(temp, task.path)
            return True
        except Exception:
            try:
                if os.path.exists(temp):
                    os.remove(temp)
            except OSError:
                pass
            return False

    def _worker(self, task):
        slot = self._slot_queue.get()
        ok = False
        try:
            ok = self._fetch(task, slot)
        except Exception:
            ok = False
        finally:
            self._board.clear_row(slot)
            self._board.file_done()
            self._slot_queue.put(slot)
            with self._lock:
                self._done += 1
                if not ok:
                    self._failed.append(task.path)
                if not self._board.active and (self._done % 25 == 0 or self._done == self._total):
                    print(f"\r  下载中 {self._done}/{self._total}", end="", flush=True)
        return ok

    def run(self, tasks, title):
        # 先逐个校验已存在的文件：通过就跳过，不通过才重新下载
        total = len(tasks)
        print(f"{title}：正在校验已有文件（{total} 个）……")
        pending = []
        skipped = 0
        for index, task in enumerate(tasks, 1):
            if self._valid(task):
                skipped += 1
            else:
                pending.append(task)
            if total > 200 and index % 200 == 0:
                print(f"\r  校验中 {index}/{total}", end="", flush=True)
        if total > 200:
            print()

        print(f"{title}：{skipped} 个已存在且校验通过（跳过），{len(pending)} 个需要下载，线程 {self.threads}")
        if not pending:
            return []

        slots = min(self.threads, len(pending))
        self._slot_queue = queue.Queue()
        for index in range(slots):
            self._slot_queue.put(index)

        self._board = DownloadBoard(slots, self.root)
        self._total = len(pending)
        self._done = 0
        self._failed = []
        self._cancelled.clear()
        self._board.start(len(pending))

        pool = ThreadPoolExecutor(max_workers=slots)
        try:
            futures = [pool.submit(self._worker, task) for task in pending]
            for future in futures:
                future.result()
        except KeyboardInterrupt:
            # Ctrl+C：通知所有线程停手，不再等待剩余任务
            self._cancelled.set()
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                pool.shutdown(wait=False)
            self._board.stop()
            raise
        else:
            pool.shutdown(wait=True)

        self._board.stop()
        if not self._board.active:
            print()
        return self._failed


def _get_json(url):
    import requests
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


def install_vanilla_fast(directory, version, threads=8, no_assets=False):
    """自实现的原版安装：版本 Json → 主 Jar → 支持库 → 资源索引 → 资源文件（多线程）。"""
    manifest = _get_json(MANIFEST_URL)
    entry = next((item for item in manifest.get("versions", []) if item.get("id") == version), None)
    if not entry:
        raise RuntimeError(f"版本清单里没有 {version}")

    version_folder = os.path.join(directory, "versions", version)
    json_path = os.path.join(version_folder, version + ".json")
    downloader = ParallelDownloader(threads, directory)
    # 版本清单里带了这个 Json 的 sha1 / size，用来校验已有的版本 Json
    if downloader.run([DownloadTask(entry["url"], json_path, entry.get("sha1"), entry.get("size"))], "版本 Json"):
        raise RuntimeError("版本 Json 下载失败")

    with open(json_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    tasks = []
    client = (data.get("downloads") or {}).get("client")
    if not client or not client.get("url"):
        raise RuntimeError(f"版本 {version} 没有提供客户端 Jar 下载地址（可能是远古版本）")
    tasks.append(DownloadTask(
        client["url"],
        os.path.join(version_folder, version + ".jar"),
        client.get("sha1"),
        client.get("size"),
    ))

    # 日志配置（1.7+ 的版本 Json 里有 logging.client.file）
    logging_info = (data.get("logging") or {}).get("client") or {}
    log_file = logging_info.get("file") or {}
    if log_file.get("url"):
        log_name = str(log_file.get("id") or "client-log4j.xml")
        tasks.append(DownloadTask(
            log_file["url"],
            os.path.join(directory, "assets", "log_configs", log_name),
            log_file.get("sha1"),
            log_file.get("size"),
        ))

    for library in data.get("libraries", []):
        if not rules_allow(library.get("rules")):
            continue
        downloads = library.get("downloads") or {}
        natives = library.get("natives") or {}
        if natives:
            classifier = natives.get(OS_NAME)
            if not classifier:
                continue
            classifier = classifier.replace("${arch}", "64" if sys.maxsize > 2 ** 32 else "32")
            info = (downloads.get("classifiers") or {}).get(classifier)
            if info and info.get("url"):
                rel = info.get("path") or maven_rel(library["name"]).replace(".jar", f"-{classifier}.jar")
                tasks.append(DownloadTask(info["url"], os.path.join(directory, "libraries", rel.replace("/", os.sep)), info.get("sha1"), info.get("size")))
            else:
                rel = maven_rel(library["name"]).replace(".jar", f"-{classifier}.jar")
                base = (library.get("url") or "https://libraries.minecraft.net/").rstrip("/")
                tasks.append(DownloadTask(base + "/" + rel, os.path.join(directory, "libraries", rel.replace("/", os.sep))))
        else:
            artifact = downloads.get("artifact")
            if artifact and artifact.get("url"):
                rel = artifact.get("path") or maven_rel(library["name"])
                tasks.append(DownloadTask(artifact["url"], os.path.join(directory, "libraries", rel.replace("/", os.sep)), artifact.get("sha1"), artifact.get("size")))
            else:
                rel = maven_rel(library["name"])
                base = (library.get("url") or "https://libraries.minecraft.net/").rstrip("/")
                tasks.append(DownloadTask(base + "/" + rel, os.path.join(directory, "libraries", rel.replace("/", os.sep))))

    index = data.get("assetIndex")
    if index and not no_assets:
        index_id = str(index.get("id") or "legacy")
        index_path = os.path.join(directory, "assets", "indexes", index_id + ".json")
        if index.get("url"):
            if downloader.run([DownloadTask(index["url"], index_path, index.get("sha1"), index.get("size"))], "资源索引"):
                raise RuntimeError("资源索引下载失败")

        if os.path.isfile(index_path):
            with open(index_path, "r", encoding="utf-8") as handle:
                index_data = json.load(handle)
            virtual = bool(index_data.get("virtual"))
            map_to_resources = bool(index_data.get("map_to_resources"))
            for name, info in (index_data.get("objects") or {}).items():
                digest = str(info.get("hash", ""))
                if len(digest) < 2:
                    continue
                if map_to_resources:
                    path = os.path.join(directory, "resources", name.replace("/", os.sep))
                elif virtual:
                    path = os.path.join(directory, "assets", "virtual", "legacy", name.replace("/", os.sep))
                else:
                    path = os.path.join(directory, "assets", "objects", digest[:2], digest)
                tasks.append(DownloadTask(
                    f"https://resources.download.minecraft.net/{digest[:2]}/{digest}",
                    path, digest, info.get("size"),
                ))
    elif index and no_assets:
        print("已跳过资源文件（assets）。")

    failed = downloader.run(tasks, "主 Jar / 支持库 / 资源文件")
    if failed:
        raise RuntimeError(f"{len(failed)} 个文件下载失败，例如：{failed[0]}")
    return version


def _download_profile(directory, version_id, profile, threads, title):
    """下载 Fabric / Quilt profile 里的支持库，并写入版本 Json。"""
    folder = os.path.join(directory, "versions", version_id)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, version_id + ".json"), "w", encoding="utf-8") as handle:
        json.dump(profile, handle, ensure_ascii=False, indent=2)

    tasks = []
    for library in profile.get("libraries", []):
        name = library.get("name")
        if not name:
            continue
        rel = maven_rel(name)
        base = (library.get("url") or "https://libraries.minecraft.net/").rstrip("/")
        tasks.append(DownloadTask(base + "/" + rel, os.path.join(directory, "libraries", rel.replace("/", os.sep))))

    failed = ParallelDownloader(threads, directory).run(tasks, title)
    if failed:
        raise RuntimeError(f"{title} 有文件下载失败，例如：{failed[0]}")


def install_fabric_fast(directory, mc_version, loader_version, threads=8, no_assets=False):
    meta = _get_json(f"https://meta.fabricmc.net/v2/versions/loader/{mc_version}")
    if not isinstance(meta, list) or not meta:
        raise RuntimeError(f"Version {mc_version} is not supported")

    if loader_version:
        match = next((item for item in meta if str((item.get("loader") or {}).get("version")) == loader_version), None)
        if not match:
            raise RuntimeError(f"Fabric {loader_version} 不适用于 Minecraft {mc_version}")
    else:
        stable = [item for item in meta if (item.get("loader") or {}).get("stable")]
        match = (stable or meta)[0]

    selected = str((match.get("loader") or {}).get("version"))
    profile = _get_json(f"https://meta.fabricmc.net/v2/versions/loader/{mc_version}/{selected}/profile/json")
    version_id = str(profile.get("id") or f"fabric-loader-{selected}-{mc_version}")

    install_vanilla_fast(directory, mc_version, threads, no_assets)
    _download_profile(directory, version_id, profile, threads, "Fabric 支持库")
    return version_id


def install_quilt_fast(directory, mc_version, loader_version, threads=8, no_assets=False):
    meta = _get_json(f"https://meta.quiltmc.org/v3/versions/loader/{mc_version}")
    if not isinstance(meta, list) or not meta:
        raise RuntimeError(f"Version {mc_version} is not supported")

    if loader_version:
        match = next((item for item in meta if str((item.get("loader") or {}).get("version")) == loader_version), None)
        if not match:
            raise RuntimeError(f"Quilt {loader_version} 不适用于 Minecraft {mc_version}")
    else:
        match = meta[0]

    selected = str((match.get("loader") or {}).get("version"))
    profile = _get_json(f"https://meta.quiltmc.org/v3/versions/loader/{mc_version}/{selected}/profile/json")
    version_id = str(profile.get("id") or f"quilt-loader-{selected}-{mc_version}")

    install_vanilla_fast(directory, mc_version, threads, no_assets)
    _download_profile(directory, version_id, profile, threads, "Quilt 支持库")
    return version_id


# ----------------------------------------------------------------------------
# 交互向导（分类型选版本 → 选加载器 → 选下载内容 → 选目录）
# ----------------------------------------------------------------------------

VERSION_GROUPS = [
    ("release", "正式版（Release）"),
    ("snapshot", "快照（Snapshot）"),
    ("ancient", "远古版本（Alpha / Beta）"),
    ("manual", "手动输入版本号"),
]

LOADERS = [
    ("vanilla", "原版（Vanilla）"),
    ("forge", "Forge"),
    ("neoforge", "NeoForge"),
    ("fabric", "Fabric"),
    ("quilt", "Quilt"),
    ("optifine", "OptiFine"),
]

CONTENT_OPTIONS = [
    ("full", "完整安装（游戏 + 支持库 + 资源文件）"),
    ("no_assets", "游戏 + 支持库（跳过 assets，可能缺声音/语言）"),
]

_VERSION_CACHE = None


def ask(prompt, default=None):
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}：").strip()
    except EOFError:
        print()
        return default or ""
    return answer or (default or "")


def ask_index(prompt, count, default=1):
    answer = ask(prompt, str(default))
    if answer.isdigit():
        index = int(answer)
        if 1 <= index <= count:
            return index - 1
    return default - 1


def load_version_list():
    global _VERSION_CACHE
    if _VERSION_CACHE is not None:
        return _VERSION_CACHE
    utils = submodule("utils")
    fn = function(utils, "get_version_list")
    if not fn:
        _VERSION_CACHE = []
        return _VERSION_CACHE
    try:
        print("正在获取版本列表……")
        _VERSION_CACHE = list(fn())
    except Exception as exc:
        print(f"获取版本列表失败：{exc}")
        _VERSION_CACHE = []
    return _VERSION_CACHE


def wizard_sources(registry):
    names = list(registry.keys())
    print()
    print("【1/6】选择下载源（可多选，逗号分隔；靠前的优先，失败自动换下一个）")
    for index, name in enumerate(names, 1):
        base = registry[name]
        print(f"  {index}. {name:<10} {'官方（Mojang）' if base is None else base}")
    print("  直接回车 = 镜像优先（bmclapi → official）")
    answer = ask("输入序号")
    if not answer:
        return ["bmclapi", "official"] if "bmclapi" in registry else names

    chosen = []
    for part in answer.replace("，", ",").split(","):
        part = part.strip()
        if part.isdigit():
            index = int(part) - 1
            if 0 <= index < len(names):
                chosen.append(names[index])
    if not chosen:
        print("没有识别到有效序号，使用默认：bmclapi → official")
        return ["bmclapi", "official"] if "bmclapi" in registry else names
    return chosen


def wizard_version():
    print()
    print("【2/6】选择版本类型")
    for index, (_, label) in enumerate(VERSION_GROUPS, 1):
        print(f"  {index}. {label}")
    kind = VERSION_GROUPS[ask_index("输入序号", len(VERSION_GROUPS), 1)][0]

    if kind == "manual":
        return ask("输入 Minecraft 版本号", "1.20.4")

    versions = load_version_list()
    if kind == "release":
        pool = [v for v in versions if str(v.get("type", "")) == "release"]
    elif kind == "snapshot":
        pool = [v for v in versions if str(v.get("type", "")) == "snapshot"]
    else:
        pool = [v for v in versions if str(v.get("type", "")) in ("old_beta", "old_alpha")]

    if not pool:
        print("没有取到该类型的版本，改为手动输入。")
        return ask("输入 Minecraft 版本号", "1.20.4")

    print()
    print("【3/6】选择具体版本（最新在前）")
    limit = 100
    ##输出的版本个数限制
    for index, item in enumerate(pool[:limit], 1):
        print(f"  {index:2}. {item.get('id')}")
    print(f"  列表只显示前 {limit} 个，也可以直接输入版本号")
    answer = ask("输入序号或版本号", "1")
    if answer.isdigit():
        index = int(answer) - 1
        if 0 <= index < min(len(pool), limit):
            return str(pool[index].get("id"))
        return str(pool[0].get("id"))
    return answer


def wizard_loader():
    print()
    print("【4/6】选择加载器")
    for index, (_, label) in enumerate(LOADERS, 1):
        print(f"  {index}. {label}")
    loader = LOADERS[ask_index("输入序号", len(LOADERS), 1)][0]

    loader_version = None
    if loader == "optifine":
        loader_version = ask("OptiFine 版本号（例如 1.20.1_HD_U_I6）")
        if not loader_version:
            raise SystemExit("OptiFine 必须指定版本号，已取消。")
    elif loader != "vanilla":
        loader_version = ask("加载器版本（直接回车 = 自动选择）") or None
    return loader, loader_version


def wizard_content():
    print()
    print("【5/6】选择下载内容")
    for index, (_, label) in enumerate(CONTENT_OPTIONS, 1):
        print(f"  {index}. {label}")
    return CONTENT_OPTIONS[ask_index("输入序号", len(CONTENT_OPTIONS), 1)][0]


def wizard_directory():
    print()
    print("【6/6】选择目标目录")
    return ask("目标 .minecraft 目录", default_directory())


def disable_assets_download():
    """把库里的 install_assets 换成空实现，用于「跳过资源文件」。"""
    install = submodule("install")
    fn = getattr(install, "install_assets", None) if install else None
    if callable(fn):
        setattr(install, "install_assets", lambda *args, **kwargs: None)
        return True
    return False


# ----------------------------------------------------------------------------
# 命令行
# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# 首次运行免责声明
# ----------------------------------------------------------------------------

DISCLAIMER_TEXT = """
==================================================================
                     免责声明 / Disclaimer
==================================================================
  本工具为纯技术下载工具。
  用户需自行确保其下载和使用行为，符合所在国家/地区的法律法规以及游戏发行商的区域政策。
  请确保您已拥有该游戏的正版授权。
==================================================================
"""


def disclaimer_marker():
    return os.path.join(os.path.expanduser("~"), ".mc-downloader-accepted")


def check_disclaimer(args):
    """首次运行必须确认免责声明；确认后写入标记文件，之后不再询问。"""
    marker = disclaimer_marker()
    if os.path.isfile(marker):
        return True

    print(DISCLAIMER_TEXT)

    accepted = False
    if getattr(args, "accept_disclaimer", False) or os.environ.get("MC_DOWNLOAD_ACCEPT_DISCLAIMER") == "1":
        accepted = True
    elif sys.stdin.isatty():
        try:
            answer = input("输入 Y 继续，其他任意键退出：").strip()
        except EOFError:
            answer = ""
        accepted = answer.lower() in ("y", "yes")
    else:
        print("当前不是交互终端。请加 --accept-disclaimer（或设置环境变量 MC_DOWNLOAD_ACCEPT_DISCLAIMER=1）后重试。")
        return False

    if not accepted:
        print("已退出。")
        return False

    try:
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
    except OSError:
        pass
    print("已记录确认，继续。\n")
    return True


def build_parser():
    parser = argparse.ArgumentParser(
        description="Minecraft 完整下载器（原版 / Forge / NeoForge / Fabric / Quilt / OptiFine）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python mc_download.py                        # 交互向导\n"
            "  python mc_download.py --list 1.20\n"
            "  python mc_download.py --version 1.20.4\n"
            "  python mc_download.py --version 1.20.1 --loader forge\n"
            "  python mc_download.py --version 1.20.4 --loader fabric --loader-version 0.15.7\n"
            "  python mc_download.py --version 1.20.4 --source bmclapi,official\n"
            "  python mc_download.py --version 1.20.4 --source bmclapi,mymirror --mirror mymirror=https://mc.example.com\n"
        ),
    )
    parser.add_argument("--version", "-v", help="Minecraft 版本，例如 1.20.4；不填则进入交互向导")
    parser.add_argument(
        "--loader", "-l",
        default=None,
        choices=["vanilla", "forge", "neoforge", "fabric", "quilt", "optifine"],
        help="加载器类型，默认 vanilla",
    )
    parser.add_argument("--loader-version", help="加载器版本；Forge 不填会用推荐版，其余不填用最新版")
    parser.add_argument("--dir", "-d", help=".minecraft 目录；默认当前目录下的 .minecraft，支持相对路径和 ~")
    parser.add_argument("--list", nargs="?", const="", metavar="关键字", help="列出版本后退出，可带关键字过滤")
    parser.add_argument(
        "--source",
        default="auto",
        help="下载源，逗号分隔按优先级排列：auto / official / bmclapi / 自定义名称，例如 bmclapi,official",
    )
    parser.add_argument(
        "--mirror",
        action="append",
        metavar="NAME=URL",
        help="追加自定义镜像源（BMCLAPI 路径约定），可重复，例如 --mirror my=https://mc.example.com",
    )
    parser.add_argument("--interactive", "-i", action="store_true", help="强制进入交互向导")
    parser.add_argument("--no-assets", action="store_true", help="跳过资源文件（assets）下载")
    parser.add_argument("--threads", type=int, default=48, help="并行下载线程数，默认 48")
    parser.add_argument(
        "--engine",
        choices=["auto", "fast", "legacy"],
        default="auto",
        help="下载引擎：auto=原版/Fabric/Quilt 用多线程引擎，Forge 等用库；fast=强制多线程；legacy=全部用库",
    )
    parser.add_argument("--java", help="java.exe 的完整路径（Forge / NeoForge 安装时使用）")
    parser.add_argument("--accept-disclaimer", action="store_true", help="非交互环境（CI）下确认免责声明并继续")
    return parser


def install(args, directory, cb):
    loader = args.loader.lower()
    mc_version = args.version
    engine = getattr(args, "engine", "auto")
    threads = getattr(args, "threads", 8)
    use_fast = engine in ("auto", "fast")

    if loader == "vanilla":
        if use_fast:
            return install_vanilla_fast(directory, mc_version, threads, args.no_assets)
        return install_vanilla(directory, mc_version, cb)

    if loader == "forge":
        forge_version = pick_forge_version(mc_version, args.loader_version)
        print(f"Forge 版本：{forge_version}")
        return install_forge(directory, mc_version, forge_version, cb, args.java)

    if loader == "neoforge":
        neo_version = pick_neoforge_version(mc_version, args.loader_version)
        print(f"NeoForge 版本：{neo_version}")
        return install_neoforge(directory, neo_version, cb)

    if loader == "fabric":
        if use_fast:
            return install_fabric_fast(directory, mc_version, args.loader_version, threads, args.no_assets)
        return install_fabric(directory, mc_version, args.loader_version, cb)

    if loader == "quilt":
        if use_fast:
            return install_quilt_fast(directory, mc_version, args.loader_version, threads, args.no_assets)
        return install_quilt(directory, mc_version, args.loader_version, cb)

    if loader == "optifine":
        if not args.loader_version:
            raise RuntimeError("OptiFine 必须用 --loader-version 指定版本，例如 --loader-version 1.20.4_HD_U_I6")
        return install_optifine(directory, args.loader_version, cb)

    raise RuntimeError(f"未知的加载器：{loader}")


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    args = build_parser().parse_args(argv)

    # 首次运行先过免责声明，未确认就直接退出（不下载、不联网）
    if not check_disclaimer(args):
        return 2

    registry = build_source_registry(args.mirror)

    # 没有 --version 且是交互终端 → 自动进入向导
    interactive = args.interactive or (not args.version and args.list is None and sys.stdin.isatty())

    # 【1/6】下载源
    if interactive:
        chain = [(name, registry[name]) for name in wizard_sources(registry)]
    else:
        chain = parse_source_chain(args.source, registry)

    source_label = enable_sources(chain)
    if not source_label:
        source_label = "官方源（未找到 requests，镜像加速不可用）"

    if args.list is not None:
        print(f"下载源：{source_label}")
        list_versions(args.list)
        return 0

    if interactive:
        # 【2/6】【3/6】版本类型 + 具体版本
        if not args.version:
            args.version = wizard_version()

        # 【4/6】加载器
        if args.loader is None:
            loader, loader_version = wizard_loader()
            args.loader = loader
            if loader_version and not args.loader_version:
                args.loader_version = loader_version

        # 【5/6】下载内容
        content = wizard_content()
        if content == "no_assets":
            args.no_assets = True

        # 【6/6】目录
        if not args.dir:
            args.dir = wizard_directory()

    args.loader = args.loader or "vanilla"

    if not args.version:
        print("请指定 --version，例如：--version 1.20.4（可用 --list 查看全部版本）")
        return 2

    # 加载器与 MC 版本的兼容性预检（主要针对 Fabric / Quilt）
    if loader_supports(args.loader, args.version) is False:
        print()
        print(f"{args.loader} 不支持 Minecraft {args.version}。")
        if args.loader == "fabric":
            print("Fabric 从 Minecraft 1.14 开始才支持，1.13 及更早只能用原版（vanilla）或 Forge。")
        elif args.loader == "quilt":
            print("Quilt 支持范围与 Fabric 接近，1.14 之前的版本请用原版或 Forge。")
        if interactive and args.loader != "vanilla":
            if ask("改用原版（vanilla）安装？(Y/n)", "Y").lower() in ("y", "yes"):
                args.loader = "vanilla"
                args.loader_version = None
            else:
                return 1
        else:
            return 1

    directory = resolve_directory(args.dir)
    os.makedirs(directory, exist_ok=True)

    if args.no_assets:
        if disable_assets_download():
            print("已选择跳过资源文件（assets）：游戏可以启动，但可能没有声音/语言文件。")
        else:
            print("警告：当前库版本不支持跳过资源文件，本次仍会下载 assets。")

    if interactive:
        print()
        print("即将开始：")
        print(f"  下载源：{source_label}")
        print(f"  版本：{args.version}")
        print(f"  加载器：{args.loader}" + (f" {args.loader_version}" if args.loader_version else ""))
        print(f"  资源文件：{'跳过' if args.no_assets else '下载'}")
        print(f"  线程数：{max(1, args.threads)}")
        print(f"  目录：{directory}")
        if ask("确认下载？(Y/n)", "Y").lower() in ("n", "no"):
            print("已取消。")
            return 0

    print(f"下载源：{source_label}")
    print(f"目标目录：{directory}")
    print(f"目标版本：{args.version}    加载器：{args.loader}")
    if args.engine != "legacy" and args.loader in ("vanilla", "fabric", "quilt"):
        print(f"下载引擎：多线程（{max(1, args.threads)} 线程）")
    print()

    progress = Progress()
    attempt = 0
    while True:
        try:
            version_id = install(args, directory, progress.callbacks())
            break
        except KeyboardInterrupt:
            progress.done()
            print("已取消。")
            abort_now()
        except Exception as exc:
            progress.done()
            message = str(exc)
            print(f"下载失败：{message}")

            # 版本/加载器不兼容：换源没有意义，直接给出建议
            if "not supported" in message.lower() or "unsupported" in message.lower():
                print()
                print(f"{args.loader} 不支持 Minecraft {args.version}。")
                if args.loader == "fabric":
                    print("Fabric 从 Minecraft 1.14 开始才支持，1.13 及更早只能用原版或 Forge。")
                if interactive and args.loader != "vanilla":
                    if ask("改用原版（vanilla）重新下载？(Y/n)", "Y").lower() in ("y", "yes"):
                        args.loader = "vanilla"
                        args.loader_version = None
                        progress = Progress()
                        continue
                return 1

            if not interactive or len(chain) <= 1 or attempt >= len(chain) - 1:
                return 1

            if not is_network_error(message):
                print("这看起来不是网络问题，换源不一定有用；可以用 --source official 进一步排查。")
            if ask("换个下载源重试？(y/N)", "N").lower() not in ("y", "yes"):
                return 1

            attempt += 1
            chain = chain[1:] + chain[:1]
            source_label = enable_sources(chain) or source_label
            progress = Progress()
            print(f"改用下载源：{source_label}")

    progress.done()
    print(f"完成，版本 ID：{version_id}")
    print(f"版本目录：{os.path.join(directory, 'versions', version_id)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # 任何阶段（向导、校验、下载）按 Ctrl+C 都直接退出
        sys.stdout.write("\n已取消。\n")
        abort_now()
