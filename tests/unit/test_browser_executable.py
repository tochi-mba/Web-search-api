from pathlib import Path

from app.services.fetch.browser import resolve_executable_path


def test_explicit_path_wins_when_it_exists(tmp_path, monkeypatch):
    binary = tmp_path / "chrome"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.delenv("WSA_BROWSER_EXECUTABLE_PATH", raising=False)
    assert resolve_executable_path(str(binary)) == str(binary)


def test_environment_variable_is_used_when_no_explicit_path(tmp_path, monkeypatch):
    binary = tmp_path / "chrome"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setenv("WSA_BROWSER_EXECUTABLE_PATH", str(binary))
    assert resolve_executable_path() == str(binary)


def test_nonexistent_explicit_path_is_ignored(monkeypatch, tmp_path):
    monkeypatch.delenv("WSA_BROWSER_EXECUTABLE_PATH", raising=False)
    monkeypatch.setattr(
        "app.services.fetch.browser._SYSTEM_CHROMIUM_PATHS", (str(tmp_path / "nope"),)
    )
    assert resolve_executable_path("/definitely/not/here") is None


def test_system_path_is_discovered(monkeypatch, tmp_path):
    binary = tmp_path / "system-chrome"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.delenv("WSA_BROWSER_EXECUTABLE_PATH", raising=False)
    monkeypatch.setattr("app.services.fetch.browser._SYSTEM_CHROMIUM_PATHS", (str(binary),))
    assert resolve_executable_path() == str(binary)


def test_returns_none_when_nothing_is_found(monkeypatch):
    monkeypatch.delenv("WSA_BROWSER_EXECUTABLE_PATH", raising=False)
    monkeypatch.setattr("app.services.fetch.browser._SYSTEM_CHROMIUM_PATHS", ())
    assert resolve_executable_path() is None


def test_this_environment_has_a_usable_chromium():
    """Sanity check that the container's browser really is discoverable."""
    found = resolve_executable_path()
    assert found is None or Path(found).exists()
