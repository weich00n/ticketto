"""scripts/ci/publish_contracts_python.py against a fake GitLab PyPI registry on localhost."""

import importlib.util
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tools import generate_definitions as gen

SPEC = importlib.util.spec_from_file_location(
    "publish_contracts_python", gen.ROOT / "scripts" / "ci" / "publish_contracts_python.py"
)
publish = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publish)


@pytest.fixture()
def registry():
    """A registry whose simple index lists `files`; it 404s until a test adds one."""
    files: list[str] = []
    listing: dict = {"files": files, "auth": None}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # pylint: disable=invalid-name  # http.server's API
            listing["auth"] = self.headers.get("Authorization")
            if not files:
                self.send_error(404)
                return
            page = "".join(f'<a href="#">{name}</a>' for name in files).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *args):
            del args

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/api/v4", listing
    server.shutdown()


@pytest.fixture()
def ci(tmp_path, monkeypatch, registry):
    api, listing = registry
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "VERSION").write_text("1.2.0\n")
    (tmp_path / "dist").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CI_API_V4_URL", api)
    monkeypatch.setenv("CI_PROJECT_ID", "42")
    monkeypatch.setenv("CI_JOB_TOKEN", "job-token")
    uploads = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs: uploads.append((args, kwargs)))
    return tmp_path, listing, uploads


def _wheel(root, version):
    (root / "dist" / f"ticketto_contracts-{version}-py3-none-any.whl").write_bytes(b"wheel")


def test_uploads_a_new_release(ci):
    root, listing, uploads = ci
    _wheel(root, "1.2.0")
    listing["files"].append("ticketto_contracts-1.1.0-py3-none-any.whl")
    assert publish.main([]) == 0
    ((args, kwargs),) = uploads
    assert args[-1] == "dist/ticketto_contracts-1.2.0-py3-none-any.whl"
    assert "/projects/42/packages/pypi" in args[args.index("--repository-url") + 1]
    assert kwargs["env"]["TWINE_PASSWORD"] == "job-token"
    assert listing["auth"].startswith("Basic ")
    assert not (root / "prerelease.env").exists()


def test_uploads_the_first_version_when_nothing_is_published(ci):
    root, _, uploads = ci  # the registry 404s
    _wheel(root, "1.2.0")
    assert publish.main([]) == 0
    assert len(uploads) == 1


def test_skips_a_version_that_is_already_published(ci, capsys):
    root, listing, uploads = ci
    _wheel(root, "1.2.0")
    listing["files"].append("ticketto_contracts-1.2.0-py3-none-any.whl")
    assert publish.main([]) == 0
    assert uploads == []
    assert "already published" in capsys.readouterr().out


def test_prerelease_uploads_and_reports_the_version(ci):
    root, _, uploads = ci
    _wheel(root, "1.2.0.dev4821")
    assert publish.main(["--prerelease"]) == 0
    assert uploads[0][0][-1].endswith("ticketto_contracts-1.2.0.dev4821-py3-none-any.whl")
    assert (root / "prerelease.env").read_text() == "PRERELEASE_VERSION=1.2.0.dev4821\n"


def test_prerelease_already_published_still_reports_the_version(ci):
    root, listing, uploads = ci
    _wheel(root, "1.2.0.dev4821")
    listing["files"].append("ticketto_contracts-1.2.0.dev4821-py3-none-any.whl")
    assert publish.main(["--prerelease"]) == 0
    assert uploads == []
    assert (root / "prerelease.env").exists()


@pytest.mark.parametrize(
    ("version", "args"),
    [
        ("1.2.0.dev4821", []),  # a pre-release is never released from main
        ("1.2.0", ["--prerelease"]),  # a merge request never uploads a release
        ("1.1.0.dev4821", ["--prerelease"]),  # not this contract version
        ("1.3.0", []),  # VERSION and the wheel disagree
    ],
)
def test_refuses_a_wheel_of_the_wrong_kind(ci, version, args):
    root, _, uploads = ci
    _wheel(root, version)
    assert publish.main(args) == 1
    assert uploads == []


@pytest.mark.usefixtures("ci")
def test_fails_without_exactly_one_wheel(tmp_path):
    assert publish.main([]) == 1
    _wheel(tmp_path, "1.2.0")
    _wheel(tmp_path, "1.2.0.dev1")
    assert publish.main([]) == 1
