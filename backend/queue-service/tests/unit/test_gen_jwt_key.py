from __future__ import annotations

import stat
from pathlib import Path

import jwt
import pytest

from scripts.gen_jwt_key import generate, main

pytestmark = pytest.mark.unit


def test_generates_usable_rs256_pair(tmp_path: Path) -> None:
    private_path, public_path = generate(tmp_path, "kid-a")

    assert private_path.name == "jwt_private.pem"
    assert public_path.name == "kid-a.public.pem"
    assert stat.S_IMODE(private_path.stat().st_mode) == 0o600

    token = jwt.encode({"x": 1}, private_path.read_bytes(), algorithm="RS256")
    assert jwt.decode(token, public_path.read_bytes(), algorithms=["RS256"]) == {"x": 1}


def test_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    generate(tmp_path, "kid-a")
    first = (tmp_path / "jwt_private.pem").read_bytes()

    assert main(["--out-dir", str(tmp_path), "--kid", "kid-a"]) == 1
    assert (tmp_path / "jwt_private.pem").read_bytes() == first

    assert main(["--out-dir", str(tmp_path), "--kid", "kid-a", "--force"]) == 0
    assert (tmp_path / "jwt_private.pem").read_bytes() != first
