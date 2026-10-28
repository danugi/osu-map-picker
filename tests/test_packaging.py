from __future__ import annotations

import http.server
import socket
import threading
from pathlib import Path

import pytest

import config
import launcher
from storage import Storage, StorageError


def test_portable_data_dir_is_per_user(tmp_path, monkeypatch):
    portable = tmp_path / "local-appdata"
    monkeypatch.setenv("LOCALAPPDATA", str(portable))
    assert config.portable_data_dir() == portable / "OsuMapPicker"
    assert config.portable_data_dir() / "osu_picker.db" == (
        portable / "OsuMapPicker" / "osu_picker.db"
    )
    assert config.portable_data_dir() / "logs" == (
        portable / "OsuMapPicker" / "logs"
    )


def test_source_development_keeps_project_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config.sys, "frozen", False, raising=False)
    monkeypatch.delenv("OSU_PICKER_DATA_DIR", raising=False)
    assert config.data_dir() == config.PROJECT_ROOT / "data"


def test_data_and_database_overrides_win(tmp_path, monkeypatch):
    custom_data = tmp_path / "custom-data"
    custom_db = tmp_path / "custom-db" / "user.db"
    monkeypatch.setenv("OSU_PICKER_DATA_DIR", str(custom_data))
    monkeypatch.setenv("OSU_PICKER_DB", str(custom_db))
    assert config.data_dir() == custom_data
    assert config.db_path() == custom_db
    assert config.ensure_data_dir() == custom_data
    assert custom_data.is_dir()


def test_existing_database_is_preserved_across_storage_reopen(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("OSU_PICKER_DATA_DIR", str(data))
    monkeypatch.delenv("OSU_PICKER_DB", raising=False)
    first = Storage(str(config.db_path()))
    first.set_preference("preserved", "yes")
    second = Storage(str(config.db_path()))
    assert second.get_preference("preserved") == "yes"


def test_unavailable_data_path_is_an_error_not_a_silent_new_database(
    tmp_path, monkeypatch
):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file", encoding="utf-8")
    monkeypatch.setenv("OSU_PICKER_DATA_DIR", str(blocker))
    with pytest.raises(ValueError, match="Папка данных недоступна"):
        config.ensure_data_dir()


def test_launcher_binds_loopback_and_has_no_public_bind():
    args = launcher._streamlit_args(launcher.app_script_path(), 8765)
    assert "--server.address=127.0.0.1" in args
    assert "0.0.0.0" not in " ".join(args)
    assert "--server.port=8765" in args
    child = launcher._child_command(8765)
    assert child[0]
    assert "--streamlit-child" in child


def test_launcher_chooses_another_port_when_preferred_is_occupied():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        port = int(occupied.getsockname()[1])
        chosen = launcher.choose_port(port)
        assert chosen != port


def test_launcher_waits_for_health_and_stops_child(tmp_path):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class Process:
        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.killed = True

    proc = Process()
    try:
        launcher._wait_for_server(proc, port, 2.0)
    finally:
        launcher._stop(proc)
        server.shutdown()
        server.server_close()
    assert proc.terminated is True


def test_source_launcher_uses_repository_app_script():
    assert launcher.app_script_path() == config.PROJECT_ROOT / "app.py"


def test_data_directory_error_reports_non_directory_cause(tmp_path, monkeypatch):
    blocker = tmp_path / "data-is-a-file"
    blocker.write_text("keep", encoding="utf-8")
    monkeypatch.setenv("OSU_PICKER_DATA_DIR", str(blocker))
    with pytest.raises(config.DataDirectoryError) as info:
        config.ensure_data_dir()
    message = str(info.value)
    assert "не является папкой" in message
    assert "Причина:" in message
    assert blocker.read_text(encoding="utf-8") == "keep"


def test_write_failure_preserves_existing_database(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("OSU_PICKER_DATA_DIR", str(data))
    store = Storage(str(data / "osu_picker.db"))
    store.set_preference("keep", "yes")
    db = data / "osu_picker.db"
    before = db.read_bytes()
    original_open = Path.open

    def fail_probe(self, *args, **kwargs):
        if self.name.startswith(".osu-map-picker-write-test"):
            raise PermissionError(13, "probe denied")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_probe)
    with pytest.raises(config.DataDirectoryError) as info:
        config.ensure_data_dir()
    assert "проверка записи" in str(info.value)
    assert db.read_bytes() == before


def test_storage_error_does_not_replace_unavailable_database(tmp_path):
    parent = tmp_path / "parent-is-a-file"
    parent.write_text("keep", encoding="utf-8")
    db = parent / "osu_picker.db"
    with pytest.raises(StorageError) as info:
        Storage(str(db))
    assert "базы данных" in str(info.value)
    assert parent.read_text(encoding="utf-8") == "keep"


def test_run_bat_has_source_launcher_contract():
    text = (config.PROJECT_ROOT / "run.bat").read_text(encoding="utf-8")
    assert "python.org/downloads/windows" in text
    assert ".venv" in text
    assert "requirements.txt" in text
    assert "launcher.py" in text
    assert "OSU_PICKER_DATA_DIR" in text
    assert "0.0.0.0" not in text
    assert "pause" in text.lower()
    assert "127.0.0.1" in text
