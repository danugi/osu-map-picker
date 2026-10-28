"""Desktop launcher for the source Windows build.

The launcher starts Streamlit as a child process on loopback only, waits for
the local health endpoint, then opens the default browser. It never contacts
osu! itself. Runtime logs go to the per-user data directory and contain safe
startup errors only. No extra popup/control window is created; the run.bat
console stays open and Ctrl+C stops the child cleanly.
"""

from __future__ import annotations

import atexit
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import config

LOGGER = logging.getLogger("osu_picker.launcher")
HOST = "127.0.0.1"
DEFAULT_PORT = 8501
APP_NAME = "OsuMapPicker"


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def app_script_path() -> Path:
    """The app.py included as a onedir resource (never a temp data path)."""

    return _base_dir() / "app.py"


def log_path() -> Path:
    return config.data_dir() / "logs" / "launcher.log"


def _show_error(message: str) -> None:
    """Show a safe startup error even in a windowed release."""

    print(f"OsuMapPicker: {message}", file=sys.stderr)
    if os.name == "nt" and getattr(sys, "frozen", False):
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None,
                message,
                "OsuMapPicker",
                0x10,
            )
        except Exception:  # noqa: BLE001 - diagnostic best effort
            pass


def _configure_logging() -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(path),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        encoding="utf-8",
    )
    LOGGER.info("launcher start; data_dir=%s", config.data_dir())


def choose_port(preferred: int = DEFAULT_PORT) -> int:
    """Bind-probe loopback ports; an occupied preferred port is not fatal."""

    for port in range(max(1, preferred), max(1, preferred) + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((HOST, port))
            except OSError:
                continue
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return int(probe.getsockname()[1])


def health_url(port: int) -> str:
    return f"http://{HOST}:{int(port)}/_stcore/health"


def _streamlit_args(app_path: Path, port: int) -> list[str]:
    return [
        "streamlit",
        "run",
        str(app_path),
        f"--server.address={HOST}",
        f"--server.port={int(port)}",
        "--server.headless=true",
        "--server.fileWatcherType=none",
        "--server.runOnSave=false",
        "--browser.gatherUsageStats=false",
        "--global.developmentMode=false",
    ]


def _child_command(port: int) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--streamlit-child",
        "--port",
        str(int(port)),
        "--app",
        str(app_script_path()),
    ]


def _run_streamlit_child(argv: list[str]) -> int:
    port = DEFAULT_PORT
    app_path = app_script_path()
    index = 0
    while index < len(argv):
        if argv[index] == "--port" and index + 1 < len(argv):
            port = int(argv[index + 1])
            index += 2
            continue
        if argv[index] == "--app" and index + 1 < len(argv):
            app_path = Path(argv[index + 1])
            index += 2
            continue
        index += 1
    if not app_path.is_file():
        raise FileNotFoundError(f"app.py не найден в сборке: {app_path}")
    from streamlit.web import cli as streamlit_cli

    sys.argv = _streamlit_args(app_path, port)
    return int(streamlit_cli.main() or 0)


def _wait_for_server(proc: subprocess.Popen[bytes], port: int, timeout: float) -> None:
    deadline = time.monotonic() + max(1.0, float(timeout))
    url = health_url(port)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                "Streamlit завершился до открытия страницы; смотрите launcher.log."
            )
        try:
            with urllib.request.urlopen(url, timeout=0.8) as response:
                if 200 <= response.status < 500:
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.2)
    raise RuntimeError(
        f"Streamlit не стал доступен на {url} за {timeout:g} с; "
        "смотрите launcher.log."
    )


def _stop(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--streamlit-child" in args:
        return _run_streamlit_child(args)

    no_browser = "--no-browser" in args
    preferred = DEFAULT_PORT
    timeout = 30.0
    index = 0
    while index < len(args):
        if args[index] == "--port" and index + 1 < len(args):
            preferred = int(args[index + 1])
            index += 2
            continue
        if args[index] == "--timeout" and index + 1 < len(args):
            timeout = float(args[index + 1])
            index += 2
            continue
        index += 1

    try:
        config.ensure_data_dir()
        _configure_logging()
    except Exception as exc:  # noqa: BLE001 - startup boundary
        _show_error(str(exc))
        return 2

    port = choose_port(preferred)
    if port != preferred:
        LOGGER.warning("port %s occupied; using %s", preferred, port)
    url = f"http://{HOST}:{port}/"
    LOGGER.info("starting Streamlit on %s", url)
    proc: subprocess.Popen[bytes] | None = None
    log_handle = None
    try:
        log_handle = log_path().open("a", encoding="utf-8")
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        env = dict(os.environ)
        # Keep the first browser open from causing a scheduled auto-discovery
        # request. Explicit Analysis-page discovery requests remain available.
        env["OSU_PICKER_DEFER_AUTO_DISCOVERY"] = "1"
        proc = subprocess.Popen(
            _child_command(port),
            cwd=str(Path(__file__).resolve().parent),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        atexit.register(_stop, proc)
        _wait_for_server(proc, port, timeout)
        if not no_browser:
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001 - browser is optional
                LOGGER.exception("could not open default browser")
        LOGGER.info("server ready at %s", url)
        proc.wait()
        return int(proc.returncode or 0)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 - launcher boundary
        LOGGER.exception("launcher failed")
        _show_error(
            f"OsuMapPicker не запустился: {exc}\n"
            f"Диагностика: {log_path()}"
        )
        return 1
    finally:
        _stop(proc)
        if log_handle is not None:
            log_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
