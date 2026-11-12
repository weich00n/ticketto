"""Generate an RSA-2048 key pair for signing admission tokens (local dev only).

Writes <out-dir>/jwt_private.pem (mode 0600) and <out-dir>/<kid>.public.pem.
Production keys come from a secret store, never from this script.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def generate(out_dir: Path, kid: str, force: bool = False) -> tuple[Path, Path]:
    private_path = out_dir / "jwt_private.pem"
    public_path = out_dir / f"{kid}.public.pem"
    if not force and (private_path.exists() or public_path.exists()):
        raise FileExistsError(f"{private_path} or {public_path} already exists (use --force)")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    private_path.write_bytes(private_pem)
    private_path.chmod(0o600)
    public_path.write_bytes(public_pem)
    return private_path, public_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=Path("secrets"))
    parser.add_argument("--kid", default="queue-local-1")
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    args = parser.parse_args(argv)

    try:
        private_path, public_path = generate(args.out_dir, args.kid, args.force)
    except FileExistsError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"wrote {private_path} and {public_path} (kid={args.kid})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
