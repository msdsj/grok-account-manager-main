"""运行期工具：Python  解释器守卫 + Chromium 浏览器会话封装。"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import psutil
from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import PageDisconnectedError

from . import fingerprint as fingerprint_mod

# 项目根目录（三层上：serve/grok_account_manager/core/browser.py → 项目根）
PROJECT_ROOT = Path(__file__).resolve().parents[3]
TURNSTILE_EXTENSION_PATH = str(PROJECT_ROOT / "extensions" / "turnstile_patch")
_BROWSER_START_LOCK = threading.RLock()
_ACTIVE_PROFILE_PATHS: set[str] = set()
_ACTIVE_CACHE_PATHS: set[str] = set()
_ACTIVE_DEBUG_PORTS: set[int] = set()
_ACTIVE_BROWSER_PIDS: set[int] = set()
_STALE_TEMP_PATHS: set[str] = set()
_BROWSER_START_MAX_ATTEMPTS = 3
_BROWSER_START_RETRY_DELAY_SECONDS = 0.25


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _command_path_argument(arguments: list[str], name: str) -> str:
    prefix = f"{name}="
    for argument in arguments:
        if argument.startswith(prefix):
            return argument[len(prefix):]
    return ""


def _validate_browser_isolation(
    *,
    profile_dir: Path,
    cache_dir: Path,
    debug_port: int,
    browser_address: str,
    browser_user_data_path: str,
    browser_pid: int,
    browser_command: list[str],
) -> None:
    """Fail closed unless Chrome is using the exact isolated resources we assigned."""
    problems: list[str] = []
    expected_address = f"127.0.0.1:{debug_port}"
    if browser_address.replace("localhost", "127.0.0.1") != expected_address:
        problems.append(f"调试地址不匹配（实际 {browser_address}，预期 {expected_address}）")

    expected_profile = _path_key(profile_dir)
    expected_cache = _path_key(cache_dir)
    if expected_profile == expected_cache:
        problems.append("Profile 与缓存目录发生复用")
    if not profile_dir.is_dir() or profile_dir.is_symlink():
        problems.append("Profile 目录不存在或不是独立实体目录")
    if not cache_dir.is_dir() or cache_dir.is_symlink():
        problems.append("缓存目录不存在或不是独立实体目录")

    reported_profile = _path_key(browser_user_data_path) if browser_user_data_path else ""
    command_profile = _command_path_argument(browser_command, "--user-data-dir")
    command_cache = _command_path_argument(browser_command, "--disk-cache-dir")
    command_port = _command_path_argument(browser_command, "--remote-debugging-port")
    if reported_profile != expected_profile:
        problems.append("DrissionPage 连接的 Profile 与本轮分配目录不一致")
    if not command_profile or _path_key(command_profile) != expected_profile:
        problems.append("Chrome 主进程未使用本轮独立 Profile")
    if not command_cache or _path_key(command_cache) != expected_cache:
        problems.append("Chrome 主进程未使用本轮独立缓存目录")
    if command_port != str(debug_port):
        problems.append("Chrome 主进程未使用本轮独立调试端口")
    if "--incognito" not in browser_command:
        problems.append("Chrome 主进程未启用独立无痕上下文")
    if browser_pid <= 0:
        problems.append("无法取得 Chrome 主进程 PID")

    if problems:
        raise RuntimeError("浏览器隔离校验失败：" + "；".join(problems))


def _remove_temp_tree(path: Path) -> None:
    """Remove a Chrome temp tree after the process has released file handles."""
    path_key = _path_key(path)
    for attempt in range(5):
        if not path.exists():
            with _BROWSER_START_LOCK:
                _STALE_TEMP_PATHS.discard(path_key)
            return
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            with _BROWSER_START_LOCK:
                _STALE_TEMP_PATHS.discard(path_key)
            return
        except Exception:
            if attempt == 4:
                break
        time.sleep(0.15 * (attempt + 1))
    if path.exists():
        with _BROWSER_START_LOCK:
            _STALE_TEMP_PATHS.add(path_key)
        print(f"[警告] 浏览器临时目录仍被占用，未能清理: {path}")


def _retry_stale_temp_cleanup() -> None:
    with _BROWSER_START_LOCK:
        stale_paths = tuple(_STALE_TEMP_PATHS)
    temp_root = _path_key(tempfile.gettempdir())
    for raw_path in stale_paths:
        path = Path(raw_path)
        if _path_key(path.parent) != temp_root or not path.name.startswith("grok-chrome-"):
            continue
        _remove_temp_tree(path)


def _find_owned_browser_processes(
    profile_dir: Path,
    cache_dir: Path,
    debug_port: int,
) -> list[tuple[psutil.Process, list[str]]]:
    """Find Chrome processes only when all three assigned resources match exactly."""
    expected_profile = _path_key(profile_dir)
    expected_cache = _path_key(cache_dir)
    matches: list[tuple[psutil.Process, list[str]]] = []
    for process in psutil.process_iter(["pid", "cmdline"]):
        try:
            command = list(process.info.get("cmdline") or [])
            command_profile = _command_path_argument(command, "--user-data-dir")
            command_cache = _command_path_argument(command, "--disk-cache-dir")
            command_port = _command_path_argument(command, "--remote-debugging-port")
            if (
                command_profile
                and command_cache
                and _path_key(command_profile) == expected_profile
                and _path_key(command_cache) == expected_cache
                and command_port == str(debug_port)
            ):
                matches.append((process, command))
        except (psutil.AccessDenied, psutil.NoSuchProcess, ValueError):
            continue
    return matches


def _find_owned_browser_process(
    profile_dir: Path,
    cache_dir: Path,
    debug_port: int,
) -> tuple[psutil.Process, list[str]] | None:
    """Find one unambiguous Chrome main process for isolation verification."""
    matches = _find_owned_browser_processes(profile_dir, cache_dir, debug_port)
    if len(matches) > 1:
        raise RuntimeError("浏览器隔离校验失败：同一环境匹配到多个 Chrome 主进程")
    return matches[0] if matches else None


def _stop_owned_browser_process(process_id: int | None, create_time: float | None) -> None:
    """Ensure only the verified Chrome process tree is gone before deleting its profile."""
    if not process_id:
        return
    try:
        process = psutil.Process(process_id)
        if create_time is not None and abs(process.create_time() - create_time) > 0.01:
            return
        process.wait(timeout=2)
        return
    except psutil.NoSuchProcess:
        return
    except psutil.TimeoutExpired:
        pass
    except Exception as error:
        print(f"[警告] 等待 Chrome 主进程退出失败: PID {process_id} ({error})")
        return

    try:
        processes = [*process.children(recursive=True), process]
        for item in processes:
            try:
                item.terminate()
            except psutil.NoSuchProcess:
                pass
        _gone, alive = psutil.wait_procs(processes, timeout=2)
        for item in alive:
            try:
                item.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(alive, timeout=1)
    except psutil.NoSuchProcess:
        return
    except Exception as error:
        print(f"[警告] 清理 Chrome 进程树失败: PID {process_id} ({error})")


def _find_free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def ensure_stable_python_runtime():
    """优先自动切到更稳定的 3.12 / 3.13，避免 3.14 下 Mail.tm 偶发 TLS/兼容问题。"""
    if sys.version_info < (3, 14) or os.environ.get("GROK_ACCOUNT_MANAGER_REEXEC_DONE") == "1":
        return

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(local_app_data, "Programs", "Python", "Python313", "python.exe"),
    ]

    current_python = os.path.normcase(os.path.abspath(sys.executable))
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        if os.path.normcase(os.path.abspath(candidate)) == current_python:
            return

        print(f"[*] 检测到 Python {sys.version.split()[0]}，自动切换到更稳定的解释器: {candidate}")
        env = os.environ.copy()
        env["GROK_ACCOUNT_MANAGER_REEXEC_DONE"] = "1"
        os.execve(candidate, [candidate, *sys.argv], env)


def warn_runtime_compatibility():
    if sys.version_info >= (3, 14):
        print("[提示] 当前 Python 为 3.14+；若出现 Mail.tm TLS 异常，建议改用 Python 3.12 或 3.13。")


def _find_chrome_path() -> str | None:
    """跨平台检测 Chrome 浏览器路径。"""
    if sys.platform == "darwin":  # macOS
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    elif sys.platform == "win32":  # Windows
        candidates = [
            os.path.join(os.environ.get("PROGRAMFILES", "C:\\Program Files"), "Google\\Chrome\\Application\\chrome.exe"),
            os.path.join(os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)"), "Google\\Chrome\\Application\\chrome.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google\\Chrome\\Application\\chrome.exe"),
        ]
    else:  # Linux
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
        ]

    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _screen_bounds() -> tuple[int, int, int, int]:
    return _SCREEN_BOUNDS_CACHE


def _init_screen_bounds() -> tuple[int, int, int, int]:
    if sys.platform == "darwin":
        try:
            script = (
                'ObjC.import("AppKit"); '
                "const f=$.NSScreen.mainScreen.visibleFrame; "
                "[Number(f.origin.x),Number(f.origin.y),Number(f.size.width),"
                "Number(f.size.height)].join(',');"
            )
            result = subprocess.run(
                ["osascript", "-l", "JavaScript", "-e", script],
                capture_output=True,
                text=True,
                timeout=4,
                check=True,
            )
            x, y, width, height = (
                int(float(part.strip())) for part in result.stdout.strip().split(",")
            )
            if width >= 800 and height >= 600:
                return x, y, width, height
        except Exception:
            pass

    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        w, h = root.winfo_screenwidth(), root.winfo_screenheight()
        root.destroy()
        return 0, 0, w, h
    except Exception:
        return 0, 0, 1440, 900


_SCREEN_BOUNDS_CACHE: tuple[int, int, int, int] = _init_screen_bounds()


def calculate_window_bounds(
    window_index: int,
    window_count: int = 1,
    screen_bounds: tuple[int, int, int, int] | None = None,
) -> tuple[int, int, int, int]:
    """Return a visible slot on the main display for up to six concurrent windows."""
    screen_x, screen_y, screen_width, screen_height = screen_bounds or _screen_bounds()
    visible_count = max(1, min(6, int(window_count or 1)))
    columns = min(3, visible_count)
    rows = 1 if visible_count <= 3 else 2
    slot = max(0, int(window_index)) % (columns * rows)
    column = slot % columns
    row = slot // columns
    slot_width = max(480, screen_width // columns)
    slot_height = max(360, screen_height // rows)
    left = screen_x + column * slot_width
    top = screen_y + row * slot_height
    width = min(slot_width, screen_x + screen_width - left)
    height = min(slot_height, screen_y + screen_height - top)
    return left, top, width, height


def build_chromium_options(
    lang: str = "zh-CN",
    headless: bool = False,
    window_index: int = 0,
    window_count: int = 1,
    start_minimized: bool = False,
) -> ChromiumOptions:
    """构造启动参数：自动端口、Turnstile 扩展、强制语言。

    `lang` 决定 navigator.language / Accept-Language。x.ai 按浏览器语言渲染按钮文案，
    Provider 通过 chrome_lang 字段传它需要的区域（避免按钮匹配字符串错位）。

    `headless` 为 True 时使用 Chrome 新版 headless 模式（--headless=new）。
    注意：Cloudflare Turnstile 可能检测并拒绝 headless 浏览器，导致注册失败。
    """
    co = ChromiumOptions()

    # 跨平台 Chrome 路径检测
    chrome_path = _find_chrome_path()
    if chrome_path:
        co.set_browser_path(chrome_path)
        print(f"[*] 使用 Chrome 路径: {chrome_path}")
    else:
        print("[警告] 未找到 Chrome，将尝试系统默认路径")

    # Headless 模式配置
    if headless:
        co.headless(True)
        print("[警告] 已启用 headless 模式，Turnstile 验证可能失败")
    else:
        print("[*] 使用可见无痕浏览器模式（推荐）")

    # 启动参数先给出位置，启动后 BrowserSession 会再通过 CDP 强制应用一次。
    left, top, window_width, window_height = calculate_window_bounds(
        window_index,
        window_count,
    )
    co.set_argument(f"--window-size={window_width},{window_height}")
    co.set_argument(f"--window-position={left},{top}")
    if start_minimized and not headless:
        co.set_argument("--start-minimized")
    co._grok_window_bounds = (left, top, window_width, window_height)
    co._grok_start_minimized = bool(start_minimized)

    co.set_timeouts(base=1)
    co.set_argument(f"--lang={lang}")
    disabled_features = [
        "Translate",
        "MediaRouter",
        "OptimizationHints",
        "BlockThirdPartyCookies",
        "TrackingProtection3pcd",
        "ThirdPartyStoragePartitioning",
    ]
    for argument in [
        "--incognito",
        "--no-first-run",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-sync",
        "--metrics-recording-only",
        # Blink 在被 CDP 驱动时默认把 navigator.webdriver 置为 true，这是最常见的
        # 反爬信号之一；这个 flag 从根上关掉它，而不是靠页面里再打补丁去掩盖。
        "--disable-blink-features=AutomationControlled",
        f"--disable-features={','.join(disabled_features)}",
    ]:
        co.set_argument(argument)
    # Accept-Language 给一份带后备的列表，避免某些页面对 navigator.language 单值过敏
    accept_languages = f"{lang},{lang.split('-')[0]};q=0.9,en;q=0.8"
    co.set_pref("intl.accept_languages", accept_languages)
    co.set_pref("profile.default_content_setting_values.cookies", 1)
    co.set_pref("profile.default_content_settings.cookies", 1)
    co.set_pref("profile.block_third_party_cookies", False)
    co.set_pref("profile.cookie_controls_mode", 0)
    co.set_pref("profile.content_settings.exceptions.cookies", {
        "https://[*.]grok.com,*": {"setting": 1},
        "https://[*.]x.ai,*": {"setting": 1},
        "https://[*.]accounts.x.ai,*": {"setting": 1},
        "https://[*.]google.com,*": {"setting": 1},
        "https://[*.]accounts.google.com,*": {"setting": 1},
        "https://[*.]x.ai,https://[*.]grok.com": {"setting": 1},
        "https://[*.]accounts.x.ai,https://[*.]grok.com": {"setting": 1},
        "https://[*.]accounts.google.com,https://[*.]x.ai": {"setting": 1},
    })
    co.add_extension(TURNSTILE_EXTENSION_PATH)
    return co


class DrissionBrowserSession:
    """封装 DrissionPage 的 Chromium 实例 + 当前活动 tab。

    Provider 拿到 session 后通过 `.page` 操作页面，通过 `.refresh_page()` 在跳转后
    重新获取活动 tab（旧句柄 PageDisconnectedError 时使用）。
    """

    def __init__(self, options: ChromiumOptions):
        self._options = options
        self._browser: Chromium | None = None
        self._page = None
        self._profile_dir: Path | None = None
        self._cache_dir: Path | None = None
        self._fingerprint_ext_dir: Path | None = None
        self._profile_claimed = False
        self._cache_claimed = False
        self.identity: fingerprint_mod.BrowserIdentity | None = None
        self.debug_port: int | None = None
        self.browser_pid: int | None = None
        self.browser_context_id = ""
        self.environment_id = ""
        self.environment_generation = 0
        self.isolation_verified = False
        self._owns_browser_process = False
        self._browser_process_create_time: float | None = None
        self._lifecycle_lock = threading.RLock()
        self._stop_requested = False
        self._closed_intentionally = False
        self._minimize_on_start = bool(getattr(options, "_grok_start_minimized", False))
        self._window_bounds = getattr(options, "_grok_window_bounds", None)
        self._window_lock = threading.RLock()

    def _prepare_fresh_profile(self) -> None:
        self._cleanup_profile()
        _retry_stale_temp_cleanup()
        try:
            profile_dir = Path(tempfile.mkdtemp(prefix="grok-chrome-profile-"))
            self._profile_dir = profile_dir
            cache_dir = Path(tempfile.mkdtemp(prefix="grok-chrome-cache-"))
            self._cache_dir = cache_dir
            profile_dir.chmod(0o700)
            cache_dir.chmod(0o700)
            profile_key = _path_key(profile_dir)
            cache_key = _path_key(cache_dir)
            if (
                profile_key in _ACTIVE_PROFILE_PATHS
                or cache_key in _ACTIVE_CACHE_PATHS
                or profile_key == cache_key
            ):
                raise RuntimeError("无法分配唯一浏览器 Profile 和缓存目录")
            _ACTIVE_PROFILE_PATHS.add(profile_key)
            _ACTIVE_CACHE_PATHS.add(cache_key)
            self._profile_claimed = True
            self._cache_claimed = True
            self._options.set_user_data_path(str(profile_dir))
            self._options.set_cache_path(str(cache_dir))
            self.environment_generation += 1
            self.environment_id = secrets.token_hex(6)
            print(
                f"[*] 已创建全新浏览器环境: id={self.environment_id}, "
                f"profile={profile_dir}"
            )
        except Exception:
            self._cleanup_profile()
            raise

    def _prepare_fresh_fingerprint(self) -> None:
        """每轮生成一套新的随机浏览器指纹身份，用同名扩展重新加载给 Chromium。"""
        if self._fingerprint_ext_dir is not None:
            shutil.rmtree(self._fingerprint_ext_dir, ignore_errors=True)
            self._fingerprint_ext_dir = None
        self.identity = fingerprint_mod.random_identity()
        try:
            ext_dir = Path(tempfile.mkdtemp(prefix="grok-chrome-fp-"))
            self._fingerprint_ext_dir = ext_dir
            ext_dir.chmod(0o700)
            for filename, content in fingerprint_mod.build_fingerprint_extension_files(self.identity).items():
                (ext_dir / filename).write_text(content, encoding="utf-8")
            self._options.remove_extensions()
            self._options.add_extension(TURNSTILE_EXTENSION_PATH)
            self._options.add_extension(str(ext_dir))
        except Exception:
            if self._fingerprint_ext_dir is not None:
                _remove_temp_tree(self._fingerprint_ext_dir)
                self._fingerprint_ext_dir = None
            raise
        print(
            f"[*] 已生成本轮浏览器指纹: id={self.identity.canvas_seed:08x}, "
            f"gpu={self.identity.gpu_renderer}, "
            f"cpu={self.identity.hardware_concurrency}核, mem={self.identity.device_memory}GB"
        )

    def _cleanup_profile(self) -> None:
        paths = tuple(
            path
            for path in (self._profile_dir, self._cache_dir, self._fingerprint_ext_dir)
            if path is not None
        )
        with _BROWSER_START_LOCK:
            if self._profile_dir is not None and self._profile_claimed:
                _ACTIVE_PROFILE_PATHS.discard(_path_key(self._profile_dir))
            if self._cache_dir is not None and self._cache_claimed:
                _ACTIVE_CACHE_PATHS.discard(_path_key(self._cache_dir))
            if self.debug_port is not None:
                _ACTIVE_DEBUG_PORTS.discard(self.debug_port)
            if self.browser_pid is not None:
                _ACTIVE_BROWSER_PIDS.discard(self.browser_pid)
        self._profile_dir = None
        self._cache_dir = None
        self._fingerprint_ext_dir = None
        self._profile_claimed = False
        self._cache_claimed = False
        self.debug_port = None
        self.browser_pid = None
        self.browser_context_id = ""
        self._owns_browser_process = False
        self._browser_process_create_time = None
        self.isolation_verified = False
        for path in paths:
            _remove_temp_tree(path)

    def _claim_debug_port(self) -> int:
        for _attempt in range(50):
            port = _find_free_local_port()
            if port in _ACTIVE_DEBUG_PORTS:
                continue
            _ACTIVE_DEBUG_PORTS.add(port)
            self.debug_port = port
            return port
        raise RuntimeError("无法分配唯一 Chrome 调试端口")

    def _verify_isolation(self) -> None:
        if (
            self._browser is None
            or self._profile_dir is None
            or self._cache_dir is None
            or self.debug_port is None
        ):
            raise RuntimeError("浏览器隔离校验所需状态不完整")

        process_id = int(getattr(self._browser, "process_id", 0) or 0)
        try:
            process = psutil.Process(process_id)
            browser_command = process.cmdline()
            create_time = process.create_time()
        except Exception as error:
            located = _find_owned_browser_process(
                self._profile_dir,
                self._cache_dir,
                self.debug_port,
            )
            if located is None:
                raise RuntimeError(
                    f"浏览器隔离校验失败：无法读取 Chrome 主进程 {process_id}: {error}"
                ) from error
            process, browser_command = located
            process_id = process.pid
            create_time = process.create_time()

        if process_id in _ACTIVE_BROWSER_PIDS and process_id != self.browser_pid:
            raise RuntimeError(f"浏览器隔离校验失败：PID {process_id} 已被其他 Worker 使用")

        command_profile = _command_path_argument(browser_command, "--user-data-dir")
        command_port = _command_path_argument(browser_command, "--remote-debugging-port")
        if (
            command_profile
            and _path_key(command_profile) == _path_key(self._profile_dir)
            and command_port == str(self.debug_port)
        ):
            self.browser_pid = process_id
            self._browser_process_create_time = create_time
            self._owns_browser_process = True

        _validate_browser_isolation(
            profile_dir=self._profile_dir,
            cache_dir=self._cache_dir,
            debug_port=self.debug_port,
            browser_address=str(getattr(self._browser, "address", "") or ""),
            browser_user_data_path=str(getattr(self._browser, "user_data_path", "") or ""),
            browser_pid=process_id,
            browser_command=browser_command,
        )
        try:
            with socket.create_connection(("127.0.0.1", self.debug_port), timeout=1):
                pass
        except OSError as error:
            raise RuntimeError(
                f"浏览器隔离校验失败：独立调试端口 {self.debug_port} 不可连接"
            ) from error

        _ACTIVE_BROWSER_PIDS.add(process_id)
        self.browser_pid = process_id
        self._browser_process_create_time = create_time
        self._owns_browser_process = True
        self.isolation_verified = True
        print(f"[*] 浏览器隔离校验通过: {self.isolation_summary}")

    def _capture_browser_context(self) -> None:
        try:
            target_info = self._page.run_cdp("Target.getTargetInfo").get("targetInfo", {})
            self.browser_context_id = str(target_info.get("browserContextId") or "")
        except Exception:
            self.browser_context_id = ""

    def _start_locked(self):
        if self._stop_requested:
            raise RuntimeError("任务已停止：浏览器会话禁止重新启动")
        if self._browser is not None:
            raise RuntimeError("浏览器会话已启动，拒绝复用时再次初始化环境")
        self._closed_intentionally = False
        # set_user_data_path() disables DrissionPage's auto_port flag. Allocate
        # and bind each browser serially so concurrent workers cannot all fall
        # back to the default 9222 endpoint and attach to one Chrome instance.
        with _BROWSER_START_LOCK:
            self._prepare_fresh_profile()
            self._prepare_fresh_fingerprint()
            if self._stop_requested:
                raise RuntimeError("任务已停止：取消浏览器启动")
            debug_port = self._claim_debug_port()
            self._options.set_local_port(debug_port)
            print(f"[*] 已为浏览器分配独立调试端口: {debug_port}")
            self._browser = Chromium(self._options)
            self._verify_isolation()
            if self._stop_requested:
                raise RuntimeError("任务已停止：浏览器启动后立即关闭")
            tabs = self._browser.get_tabs()
            # DrissionPage 的 /json tab 列表把最新 tab 放在第 0 位。
            self._page = tabs[0] if tabs else self._browser.new_tab()
            self._capture_browser_context()
        self._apply_window_bounds()
        return self

    def _start_with_retry_locked(self):
        if self._browser is not None:
            raise RuntimeError("浏览器会话已启动，拒绝复用时再次初始化环境")
        for attempt in range(1, _BROWSER_START_MAX_ATTEMPTS + 1):
            if self._stop_requested:
                raise RuntimeError("任务已停止：浏览器会话禁止重新启动")
            try:
                return self._start_locked()
            except Exception as error:
                self._shutdown_browser_locked()
                if self._stop_requested:
                    raise RuntimeError("任务已停止：取消浏览器启动重试") from error
                if attempt >= _BROWSER_START_MAX_ATTEMPTS:
                    raise
                print(
                    f"[警告] 浏览器启动失败（{attempt}/{_BROWSER_START_MAX_ATTEMPTS}）：{error}；"
                    "清理后重试"
                )
                time.sleep(_BROWSER_START_RETRY_DELAY_SECONDS * attempt)

    def start(self):
        with self._lifecycle_lock:
            return self._start_with_retry_locked()

    def _apply_window_bounds(self) -> None:
        if self._page is None:
            return
        try:
            with self._window_lock:
                window_id = self._window_id()
                if self._minimize_on_start:
                    # Chrome is launched with --start-minimized. Do not first
                    # restore and tile it, otherwise macOS briefly shows the
                    # window before this command can minimize it.
                    self._set_window_minimized(window_id)
                    print("[*] 浏览器窗口已最小化")
                else:
                    self._restore_window_bounds(window_id, bring_to_front=True)
        except Exception as error:
            print(f"[警告] 通过 CDP 设置浏览器窗口失败，将保留启动参数状态: {error}")

    def _window_id(self) -> int:
        if self._page is None:
            raise RuntimeError("浏览器窗口尚未创建")
        window_info = self._page.run_cdp("Browser.getWindowForTarget")
        window_id = int(window_info.get("windowId") or 0)
        if window_id <= 0:
            raise RuntimeError("无法取得浏览器窗口 ID")
        return window_id

    def _restore_window_bounds(self, window_id: int, *, bring_to_front: bool) -> None:
        if self._page is None:
            raise RuntimeError("浏览器窗口尚未创建")
        self._page.run_cdp(
            "Browser.setWindowBounds",
            windowId=window_id,
            bounds={"windowState": "normal"},
        )
        if self._window_bounds:
            left, top, width, height = self._window_bounds
            self._page.run_cdp(
                "Browser.setWindowBounds",
                windowId=window_id,
                bounds={
                    "left": left,
                    "top": top,
                    "width": width,
                    "height": height,
                },
            )
            print(f"[*] 浏览器窗口已平铺: x={left}, y={top}, {width}x{height}")
        if bring_to_front:
            self._page.run_cdp("Page.bringToFront")

    def _set_window_minimized(self, window_id: int) -> None:
        if self._page is None:
            raise RuntimeError("浏览器窗口尚未创建")
        self._page.run_cdp(
            "Browser.setWindowBounds",
            windowId=window_id,
            bounds={"windowState": "minimized"},
        )

    def set_window_minimized(self, minimized: bool = True, *, apply_now: bool = True) -> bool:
        """Set this owned window state, or remember it while Chrome is starting."""
        self._minimize_on_start = bool(minimized)
        if not apply_now:
            return False
        with self._window_lock:
            if self._page is None or self._stop_requested:
                return False
            window_id = self._window_id()
            if minimized:
                self._set_window_minimized(window_id)
            else:
                self._restore_window_bounds(window_id, bring_to_front=True)
        return True

    def _shutdown_browser_locked(self) -> None:
        self._closed_intentionally = True
        browser = self._browser
        process_id = self.browser_pid or int(getattr(browser, "process_id", 0) or 0)
        create_time = self._browser_process_create_time
        if browser is not None and self._owns_browser_process:
            try:
                browser.quit(timeout=3, force=False)
            except Exception:
                pass
        if self._owns_browser_process:
            _stop_owned_browser_process(process_id, create_time)
        elif self._profile_dir is not None and self._cache_dir is not None and self.debug_port is not None:
            try:
                matches = _find_owned_browser_processes(
                    self._profile_dir,
                    self._cache_dir,
                    self.debug_port,
                )
                for process, _command in matches:
                    try:
                        process_create_time = process.create_time()
                    except (psutil.AccessDenied, psutil.NoSuchProcess):
                        continue
                    _stop_owned_browser_process(process.pid, process_create_time)
            except Exception as error:
                print(f"[警告] 反查并清理本轮 Chrome 进程失败: {error}")
        self._browser = None
        self._page = None
        self._cleanup_profile()

    def request_stop(self) -> None:
        """Signal the verified Chrome process immediately; cleanup continues asynchronously."""
        self._stop_requested = True
        self._closed_intentionally = True
        if not self._owns_browser_process or not self.browser_pid:
            return
        try:
            process = psutil.Process(self.browser_pid)
            if (
                self._browser_process_create_time is not None
                and abs(process.create_time() - self._browser_process_create_time) > 0.01
            ):
                return
            processes = [*process.children(recursive=True), process]
            for item in processes:
                try:
                    item.kill()
                except psutil.NoSuchProcess:
                    pass
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    def stop(self):
        self.request_stop()
        with self._lifecycle_lock:
            self._shutdown_browser_locked()

    def restart(self):
        """每轮结束都重启整个浏览器实例，避免长时间复用造成的页面/Cookie 污染。"""
        with self._lifecycle_lock:
            if self._stop_requested:
                raise RuntimeError("任务已停止：浏览器会话禁止重新启动")
            self._shutdown_browser_locked()
            return self._start_with_retry_locked()

    @property
    def page(self):
        if self._page is None:
            raise RuntimeError("BrowserSession 未启动；先调用 .start()")
        return self._page

    @property
    def browser(self):
        return self._browser

    @property
    def isolation_summary(self) -> str:
        profile_name = self._profile_dir.name if self._profile_dir is not None else "-"
        status = "已校验" if self.isolation_verified else "未校验"
        return (
            f"环境 {self.environment_id or '-'} · {status} · PID {self.browser_pid or '-'} · "
            f"CDP {self.debug_port or '-'} · Profile {profile_name}"
        )

    def refresh_page(self):
        """验证码确认后页面会跳转，旧 page 句柄可能断开，统一重新获取当前活动 tab。

        `stop()` 会把 `_browser` 置空；若之后仍有循环在停止信号发出后才走到这里，
        不能静默地把浏览器重新拉起来，否则取消操作看起来就像"没生效"。
        """
        if self._browser is None:
            if self._closed_intentionally:
                raise RuntimeError("任务已停止：浏览器已关闭")
            self.start()
            return self._page
        try:
            tabs = self._browser.get_tabs()
            self._page = tabs[0] if tabs else self._browser.new_tab()
        except Exception:
            if self._closed_intentionally:
                raise RuntimeError("任务已停止：浏览器已关闭")
            self.restart()
        return self._page

    def open_url(self, url: str):
        self.refresh_page()
        try:
            self._page.get(url)
        except Exception:
            self.refresh_page()
            self._page = self._browser.new_tab(url)
        return self._page

    def open_new_tab(self, url: str | None = None):
        """在当前无痕上下文中新开 tab，并返回稳定绑定到新 target 的页面对象。"""
        if self._browser is None:
            if self._closed_intentionally or self._stop_requested:
                raise RuntimeError("任务已停止：浏览器已关闭")
            self.start()

        browser = self._browser
        last_error: Exception | None = None
        for _attempt in range(3):
            try:
                tabs = browser.get_tabs()
            except Exception as error:
                last_error = error
                time.sleep(0.4)
                continue

            # Chromium 在 --incognito 下由现有 tab 执行 window.open，才能保证新页与
            # 注册页位于同一个浏览器上下文并共享刚取得的 sso cookie。
            for source in tabs:
                try:
                    before_ids = set(browser.tab_ids)
                    source.run_js("window.open('about:blank', '_blank')")

                    deadline = time.monotonic() + 5
                    target_id = ""
                    while time.monotonic() < deadline:
                        new_ids = [tab_id for tab_id in browser.tab_ids if tab_id not in before_ids]
                        if new_ids:
                            target_id = new_ids[0]
                            break
                        time.sleep(0.1)
                    if not target_id:
                        raise RuntimeError("等待新 tab 超时")

                    self._page = browser.get_tab(target_id)
                    if url:
                        self._page.get(url, retry=0, timeout=8)
                    return self._page
                except Exception as error:
                    last_error = error
                    # source 可能正在完成注册后的最后一次重定向，换另一个稳定 tab。
                    time.sleep(0.3)

        raise RuntimeError(f"无法在当前无痕上下文中新建授权 tab: {last_error}")


def _cookie_item_parts(item) -> tuple[str, str, str]:
    if isinstance(item, dict):
        return (
            str(item.get("name", "")).strip(),
            str(item.get("value", "")).strip(),
            str(item.get("domain", "")).strip(),
        )
    return (
        str(getattr(item, "name", "")).strip(),
        str(getattr(item, "value", "")).strip(),
        str(getattr(item, "domain", "")).strip(),
    )


def _dedupe_cookies(cookies: list) -> list:
    seen: set[tuple[str, str, str]] = set()
    result = []
    for item in cookies:
        name, value, domain = _cookie_item_parts(item)
        key = (name, value, domain)
        if name and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _collect_cookie_candidates(session: DrissionBrowserSession, page) -> tuple[list, list[str]]:
    cookies: list = []
    errors: list[str] = []

    def add(source: str, func):
        try:
            value = func()
            if isinstance(value, dict) and isinstance(value.get("cookies"), list):
                value = value["cookies"]
            if value:
                cookies.extend(value)
        except Exception as error:
            errors.append(f"{source}: {error.__class__.__name__}: {error}")

    current_url = str(getattr(page, "url", "") or "")
    cookie_urls = [
        current_url,
        "https://grok.com/",
        "https://grok.com",
        "https://x.ai/",
        "https://accounts.x.ai/",
        "https://api.x.ai/",
    ]
    cookie_urls = [url for index, url in enumerate(cookie_urls) if url and url not in cookie_urls[:index]]

    add("page.cookies(all_domains=True)", lambda: page.cookies(all_domains=True, all_info=True))
    add("page.cookies(current_domain)", lambda: page.cookies(all_domains=False, all_info=True))
    add("Network.getCookies(urls)", lambda: page.run_cdp("Network.getCookies", urls=cookie_urls))
    add("Network.getAllCookies", lambda: page.run_cdp("Network.getAllCookies"))

    browser = session.browser
    if browser is not None:
        add("browser.cookies", lambda: browser.cookies(all_info=True))
        add("Storage.getCookies", lambda: browser._run_cdp("Storage.getCookies"))
        context_id = ""
        try:
            target_info = page.run_cdp("Target.getTargetInfo").get("targetInfo", {})
            context_id = str(target_info.get("browserContextId") or "")
        except Exception as error:
            errors.append(f"Target.getTargetInfo: {error.__class__.__name__}: {error}")
        if context_id:
            add(
                "Storage.getCookies(browserContextId)",
                lambda: browser._run_cdp("Storage.getCookies", browserContextId=context_id),
            )

    return _dedupe_cookies(cookies), errors


def _extract_storage_token(page, cookie_name: str) -> str | None:
    try:
        value = page.run_js(
            r"""
const cookieName = arguments[0];
const candidates = [];

function add(key, value) {
    if (value === undefined || value === null) {
        return;
    }
    const text = String(value);
    if (!text || text.length > 50000) {
        return;
    }
    candidates.push([String(key || ''), text]);
}

add('document.cookie', document.cookie || '');
for (const storage of [window.localStorage, window.sessionStorage]) {
    try {
        for (let index = 0; index < storage.length; index += 1) {
            const key = storage.key(index);
            add(key, storage.getItem(key));
        }
    } catch (error) {
    }
}

for (const [key, text] of candidates) {
    for (const part of text.split(/;\s*/)) {
        const separator = part.indexOf('=');
        if (separator > 0 && part.slice(0, separator).trim() === cookieName) {
            const value = part.slice(separator + 1).trim();
            if (value) {
                return value;
            }
        }
    }
}

for (const [key, text] of candidates) {
    if (key.toLowerCase().includes(cookieName.toLowerCase()) && text) {
        return text;
    }
}

return '';
            """,
            cookie_name,
        )
        value = str(value or "").strip()
        return value or None
    except Exception:
        return None


def get_grok_clearance(session: DrissionBrowserSession) -> dict[str, str]:
    """Collect the Grok Cloudflare cookies tied to the active browser session."""
    try:
        page = session.page
        cookies, _errors = _collect_cookie_candidates(session, page)
    except Exception:
        return {}

    allowed_names = {"cf_clearance", "__cf_bm", "__cflb"}
    parts: list[str] = []
    seen_names: set[str] = set()
    for item in cookies:
        name, value, domain = _cookie_item_parts(item)
        normalized_name = name.lower()
        normalized_domain = domain.lower().lstrip(".")
        if (
            normalized_name not in allowed_names
            or not value
            or normalized_name in seen_names
            or not (normalized_domain == "grok.com" or normalized_domain.endswith(".grok.com"))
        ):
            continue
        seen_names.add(normalized_name)
        parts.append(f"{name}={value}")

    if not any(part.lower().startswith("cf_clearance=") for part in parts):
        return {}

    try:
        user_agent = str(page.run_js("return navigator.userAgent;") or "").strip()
    except Exception:
        user_agent = ""
    return {"cookies": "; ".join(parts), "userAgent": user_agent}


def wait_for_cookie(session: DrissionBrowserSession, cookie_name: str, timeout: int = 180, stop_event=None) -> str:
    """注册完成后等待指定 cookie 出现并返回其值。"""
    deadline = time.time() + timeout
    last_seen_names: set[str] = set()
    last_errors: list[str] = []

    while time.time() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")
        try:
            session.refresh_page()
            page = session.page
            if page is None:
                time.sleep(1)
                continue

            cookies, last_errors = _collect_cookie_candidates(session, page)
            for item in cookies:
                name, value, _domain = _cookie_item_parts(item)

                if name:
                    last_seen_names.add(name)

                if name == cookie_name and value:
                    print(f"[*] 注册完成后已获取到 {cookie_name} cookie。")
                    return value

            storage_token = _extract_storage_token(page, cookie_name)
            if storage_token:
                print(f"[*] 注册完成后已从页面存储获取到 {cookie_name} token。")
                return storage_token
        except PageDisconnectedError:
            session.refresh_page()
        except Exception as error:
            last_errors = [f"{error.__class__.__name__}: {error}"]

        time.sleep(1)

    error_text = f"，读取错误: {last_errors[-3:]}" if last_errors else ""
    raise Exception(f"注册完成后未获取到 {cookie_name} cookie，当前已见 cookie: {sorted(last_seen_names)}{error_text}")
