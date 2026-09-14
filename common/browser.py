from __future__ import annotations

import atexit
import fcntl
import ipaddress
import json
import os
import signal
import subprocess
import threading
import time
import urllib.request
from urllib.parse import urlsplit
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

try:
    import pwd
except ImportError:  # pragma: no cover - Windows 没有 pwd
    pwd = None  # type: ignore[assignment]

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright


DEFAULT_CHROME_EXECUTABLE = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
_ACTIVE_SESSIONS: list["ChromeCdpSession"] = []
_REGISTRY_LOCK = threading.RLock()
_ORIGINAL_SIGNAL_HANDLERS: dict[int, Any] = {}
_ATEXIT_REGISTERED = False
_HANDLING_SIGNAL = False


class ChromeCleanupError(RuntimeError):
    pass


def _require_main_thread(action: str) -> None:
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError(f"ChromeCdpSession {action} 只允许在 Python 主线程执行")


def _pid_command(pid: int) -> str:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _restore_global_hooks() -> None:
    global _ATEXIT_REGISTERED
    if _ATEXIT_REGISTERED:
        atexit.unregister(_cleanup_at_exit)
        _ATEXIT_REGISTERED = False
    for signum, previous in list(_ORIGINAL_SIGNAL_HANDLERS.items()):
        try:
            if signal.getsignal(signum) is _handle_global_signal:
                signal.signal(signum, previous)
        except (OSError, ValueError):
            pass
    _ORIGINAL_SIGNAL_HANDLERS.clear()


def _register_session(session: "ChromeCdpSession") -> None:
    global _ATEXIT_REGISTERED
    with _REGISTRY_LOCK:
        if session not in _ACTIVE_SESSIONS:
            _ACTIVE_SESSIONS.append(session)
        if not _ATEXIT_REGISTERED:
            atexit.register(_cleanup_at_exit)
            _ATEXIT_REGISTERED = True
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGHUP):
                if signum not in _ORIGINAL_SIGNAL_HANDLERS:
                    _ORIGINAL_SIGNAL_HANDLERS[signum] = signal.signal(signum, _handle_global_signal)


def _unregister_session(session: "ChromeCdpSession") -> None:
    with _REGISTRY_LOCK:
        if session in _ACTIVE_SESSIONS:
            _ACTIVE_SESSIONS.remove(session)
        if not _ACTIVE_SESSIONS:
            _restore_global_hooks()


def _cleanup_active_sessions() -> list[BaseException]:
    with _REGISTRY_LOCK:
        sessions = list(reversed(_ACTIVE_SESSIONS))
    errors: list[BaseException] = []
    for session in sessions:
        try:
            session.close()
        except BaseException as exc:
            errors.append(exc)
    return errors


def _cleanup_at_exit() -> None:
    errors = _cleanup_active_sessions()
    if errors:
        raise ChromeCleanupError("; ".join(str(error) for error in errors))


def _handle_global_signal(signum: int, frame: object) -> None:
    global _HANDLING_SIGNAL
    with _REGISTRY_LOCK:
        if _HANDLING_SIGNAL:
            return
        _HANDLING_SIGNAL = True
        previous = _ORIGINAL_SIGNAL_HANDLERS.get(signum, signal.SIG_DFL)
    try:
        errors = _cleanup_active_sessions()
        if errors:
            raise ChromeCleanupError("; ".join(str(error) for error in errors))
    finally:
        with _REGISTRY_LOCK:
            _HANDLING_SIGNAL = False
    if callable(previous):
        previous(signum, frame)
    elif previous != signal.SIG_IGN:
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)


def _pids_in_process_group(pgid: int) -> list[int]:
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,pgid="],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ChromeCleanupError(f"无法核对 Chrome 进程组 {pgid}：{exc}") from exc
    pids = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit() and int(parts[1]) == pgid and parts[0].isdigit():
            pids.append(int(parts[0]))
    return pids


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是 true/false 或 1/0")


def _validate_debug_host(host: str) -> str:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError as exc:
        raise ValueError(
            "UJC_CHROME_DEBUG_HOST 只允许 localhost 或 127.0.0.0/8 回环地址"
        ) from exc
    if not isinstance(address, ipaddress.IPv4Address) or not address.is_loopback:
        raise ValueError("UJC_CHROME_DEBUG_HOST 只允许 localhost 或 127.0.0.0/8 回环地址")
    return str(address)


def _known_daily_chrome_roots() -> list[Path]:
    homes = {Path.home()}
    if pwd is not None:
        try:
            homes.add(Path(pwd.getpwuid(os.getuid()).pw_dir))
        except (KeyError, OSError):
            pass
    roots = []
    for home in homes:
        roots.extend([
            home / "Library/Application Support/Google/Chrome",
            home / "Library/Application Support/Google/Chrome Beta",
            home / "Library/Application Support/Google/Chrome Dev",
            home / "Library/Application Support/Google/Chrome Canary",
            home / "Library/Application Support/Chromium",
            home / ".config/google-chrome",
            home / ".config/google-chrome-beta",
            home / ".config/google-chrome-unstable",
            home / ".config/chromium",
        ])
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        roots.extend([
            Path(local_app_data) / "Google/Chrome/User Data",
            Path(local_app_data) / "Google/Chrome Beta/User Data",
            Path(local_app_data) / "Google/Chrome Dev/User Data",
            Path(local_app_data) / "Google/Chrome SxS/User Data",
            Path(local_app_data) / "Chromium/User Data",
        ])
    return roots


def _validate_profile_name(profile_name: str) -> str:
    if (
        not profile_name
        or profile_name in {".", ".."}
        or Path(profile_name).is_absolute()
        or "/" in profile_name
        or "\\" in profile_name
        or "\x00" in profile_name
    ):
        raise ValueError("UJC_CHROME_PROFILE_NAME 只允许单个普通目录名")
    return profile_name


def _assert_dedicated_profile_dir(profile_dir: Path, profile_name: str | None = None) -> None:
    targets = [profile_dir.expanduser().resolve(strict=False)]
    if profile_name is not None:
        targets.append((profile_dir.expanduser() / profile_name).resolve(strict=False))
    for candidate in _known_daily_chrome_roots():
        daily_root = candidate.expanduser().resolve(strict=False)
        for resolved in targets:
            if resolved == daily_root or daily_root in resolved.parents:
                raise ValueError(
                    f"拒绝使用日常 Chrome profile：{resolved}。"
                    "请把 UJC_CHROME_PROFILE_DIR 指向独立的采集专用目录"
                )


@dataclass(frozen=True)
class ChromeCdpConfig:
    executable: Path
    profile_dir: Path
    profile_name: str = "Default"
    debug_host: str = "127.0.0.1"
    debug_port: int = 0
    start_timeout: float = 20.0
    headless: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "debug_host", _validate_debug_host(self.debug_host))
        object.__setattr__(self, "profile_name", _validate_profile_name(self.profile_name))
        if self.debug_port != 0:
            raise ValueError(
                "UJC_CHROME_DEBUG_PORT 只允许 0，由 Chrome 在回环地址自动分配专用端口"
            )

    @classmethod
    def from_env(cls) -> "ChromeCdpConfig":
        project_root = Path(__file__).resolve().parents[1]
        return cls(
            executable=Path(os.environ.get("UJC_CHROME_EXECUTABLE", str(DEFAULT_CHROME_EXECUTABLE))).expanduser(),
            profile_dir=Path(
                os.environ.get("UJC_CHROME_PROFILE_DIR", str(project_root / ".diagnostics" / "chrome-profile"))
            ).expanduser(),
            profile_name=os.environ.get("UJC_CHROME_PROFILE_NAME", "Default"),
            debug_host=os.environ.get("UJC_CHROME_DEBUG_HOST", "127.0.0.1"),
            debug_port=int(os.environ.get("UJC_CHROME_DEBUG_PORT", "0")),
            start_timeout=float(os.environ.get("UJC_CHROME_START_TIMEOUT", "20")),
            headless=_env_bool("UJC_CHROME_HEADLESS", False),
        )


class ChromeCdpSession:
    """Launch a dedicated external Chrome and let Playwright take it over via CDP."""

    def __init__(self, config: ChromeCdpConfig | None = None) -> None:
        self.config = config or ChromeCdpConfig.from_env()
        self.process: subprocess.Popen[bytes] | None = None
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.debug_port: int | None = None
        self._devtools_browser_path: str | None = None
        self._lock_file: IO[bytes] | None = None
        self._pages: list[Page] = []
        self._closing = False
        self._owns_profile_state = False

    def __enter__(self) -> "ChromeCdpSession":
        _require_main_thread("enter")
        try:
            self._start()
            return self
        except Exception:
            self.close()
            raise

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def cdp_url(self) -> str:
        if self.debug_port is None:
            raise RuntimeError("Chrome 尚未启动")
        return f"http://{self.config.debug_host}:{self.debug_port}"

    def new_page(self) -> Page:
        if self.context is None:
            raise RuntimeError("ChromeCdpSession 必须在 with 语句中使用")
        page = self.context.new_page()
        self._pages.append(page)
        return page

    def _start(self) -> None:
        cfg = self.config
        if not cfg.executable.is_file():
            raise FileNotFoundError(f"Google Chrome 可执行文件不存在：{cfg.executable}")
        _assert_dedicated_profile_dir(cfg.profile_dir, cfg.profile_name)
        cfg.profile_dir.mkdir(parents=True, exist_ok=True)
        _assert_dedicated_profile_dir(cfg.profile_dir, cfg.profile_name)
        lock_path = cfg.profile_dir / ".ujc-profile.lock"
        self._lock_file = lock_path.open("a+b")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Chrome profile 正被另一采集任务使用：{cfg.profile_dir}") from exc
        self._assert_previous_owner_stopped()

        active_port_path = cfg.profile_dir / "DevToolsActivePort"
        previous_active_port = self._active_port_signature(active_port_path)
        command = [
            str(cfg.executable),
            f"--remote-debugging-address={cfg.debug_host}",
            "--remote-debugging-port=0",
            f"--user-data-dir={cfg.profile_dir}",
            f"--profile-directory={cfg.profile_name}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
        ]
        if cfg.headless:
            command.append("--headless=new")
        command.append("about:blank")
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._write_owner_state("running", self.process.pid)
        self._owns_profile_state = True
        _register_session(self)
        self._wait_for_debug_port(active_port_path, previous_active_port)
        self._wait_until_ready()
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.connect_over_cdp(self.cdp_url, timeout=cfg.start_timeout * 1000)
        if not self.browser.contexts:
            raise RuntimeError("CDP 连接成功，但 Chrome 没有可用 context")
        self.context = self.browser.contexts[0]

    @staticmethod
    def _active_port_signature(path: Path) -> tuple[int, bytes] | None:
        try:
            return path.stat().st_mtime_ns, path.read_bytes()
        except FileNotFoundError:
            return None

    def _wait_for_debug_port(
        self,
        path: Path,
        previous_signature: tuple[int, bytes] | None,
    ) -> None:
        assert self.process is not None
        deadline = time.monotonic() + self.config.start_timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"Google Chrome 启动失败，退出码 {self.process.returncode}")
            try:
                signature = self._active_port_signature(path)
                if signature is None or signature == previous_signature:
                    time.sleep(0.1)
                    continue
                lines = signature[1].decode("utf-8").splitlines()
                port = int(lines[0])
                browser_path = lines[1]
                if not 1 <= port <= 65535 or not browser_path.startswith("/devtools/browser/"):
                    raise ValueError("DevToolsActivePort 格式无效")
                self.debug_port = port
                self._devtools_browser_path = browser_path
                return
            except (OSError, UnicodeDecodeError, ValueError, IndexError) as exc:
                last_error = exc
            time.sleep(0.1)
        raise TimeoutError(f"等待 Chrome DevToolsActivePort 超时：{path}; last_error={last_error}")

    def _wait_until_ready(self) -> None:
        assert self.process is not None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + self.config.start_timeout
        version_url = f"{self.cdp_url}/json/version"
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"Google Chrome 启动失败，退出码 {self.process.returncode}")
            try:
                with opener.open(version_url, timeout=0.5) as response:
                    if response.status == 200:
                        payload = json.load(response)
                        websocket_url = payload.get("webSocketDebuggerUrl", "")
                        if urlsplit(websocket_url).path != self._devtools_browser_path:
                            raise RuntimeError("CDP 端点不属于本次启动的 Chrome")
                        return
            except Exception as exc:
                last_error = exc
            time.sleep(0.1)
        raise TimeoutError(f"等待 Chrome CDP 超时：{version_url}; last_error={last_error}")

    def close(self) -> None:
        _require_main_thread("close")
        if self._closing:
            return
        self._closing = True
        try:
            for page in reversed(self._pages):
                try:
                    if not page.is_closed():
                        page.close()
                except Exception:
                    pass
            self._pages.clear()
            if self.browser is not None:
                try:
                    self.browser.close()
                except Exception:
                    pass
                self.browser = None
                self.context = None
            if self.playwright is not None:
                try:
                    self.playwright.stop()
                except Exception:
                    pass
                self.playwright = None
            self._stop_process()
            if self._owns_profile_state:
                self._write_owner_state("stopped", None)
                self._owns_profile_state = False
        except BaseException:
            self._closing = False
            raise
        else:
            if self._lock_file is not None:
                try:
                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
                finally:
                    self._lock_file.close()
                    self._lock_file = None
            _unregister_session(self)
            self._closing = False

    @property
    def _owner_path(self) -> Path:
        return self.config.profile_dir / ".ujc-profile-owner.json"

    def _assert_previous_owner_stopped(self) -> None:
        owner_path = self._owner_path
        if not owner_path.exists():
            return
        try:
            state = json.loads(owner_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Chrome profile owner 状态无法验证：{owner_path}: {exc}") from exc
        if state.get("status") != "running":
            return
        pid = state.get("chrome_pid")
        if not isinstance(pid, int) or pid <= 0:
            raise RuntimeError(f"Chrome profile owner 状态无效，拒绝复用：{owner_path}")
        pgid = state.get("chrome_pgid")
        if not isinstance(pgid, int) or pgid <= 0:
            raise RuntimeError(f"Chrome profile owner 缺少有效 PGID，无法确认旧进程组已退出：{owner_path}")
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            remaining = _pids_in_process_group(pgid)
            if remaining:
                raise RuntimeError(
                    f"Chrome profile 的旧主 PID {pid} 已退出，但 PGID {pgid} 仍有残留进程 "
                    f"{remaining}，拒绝复用：{self.config.profile_dir}"
                )
            return
        except PermissionError as exc:
            raise RuntimeError(
                f"Chrome profile 的旧 owner PID {pid} 仍可能存活且无权核对，拒绝复用：{self.config.profile_dir}"
            ) from exc
        command = _pid_command(pid)
        command_detail = command or "<unavailable>"
        raise RuntimeError(
            f"Chrome profile 记录的旧 owner PID {pid} 仍存活，只有确认该 PID 消失后才能复用："
            f"{self.config.profile_dir}; command={command_detail}"
        )

    def _write_owner_state(self, status: str, chrome_pid: int | None) -> None:
        state = {
            "status": status,
            "chrome_pid": chrome_pid,
            "chrome_pgid": chrome_pid,
            "python_pid": os.getpid(),
            "profile_dir": str(self.config.profile_dir.resolve()),
            "updated_at_epoch": time.time(),
        }
        self._owner_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _stop_process(self) -> None:
        process = self.process
        if process is None:
            return
        pgid = process.pid
        if pgid == os.getpgrp():
            raise ChromeCleanupError("拒绝清理当前 Python 所在进程组")
        permission_error: PermissionError | None = None
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            permission_error = exc
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            process.poll()
            if not _pids_in_process_group(pgid):
                break
            time.sleep(0.1)
        remaining = _pids_in_process_group(pgid)
        if remaining:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                permission_error = permission_error or exc
                for pid in remaining:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except PermissionError as pid_exc:
                        permission_error = permission_error or pid_exc
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=1)
            except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
                pass
        remaining = _pids_in_process_group(pgid)
        if process.poll() is None or remaining:
            detail = f"; PermissionError={permission_error}" if permission_error else ""
            raise ChromeCleanupError(
                f"Chrome 清理无法确认：direct_child={process.pid}, remaining={remaining}{detail}"
            )
        self.process = None
