#!/usr/bin/env python3
"""Export or import a password-encrypted mailbridge credential bundle."""

from __future__ import annotations

import argparse
import base64
import getpass
import io
import json
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

from config import CONFIG_DIR

HEADER = {"format": "mailbridge-credential-bundle", "version": 1,
          "kdf": "scrypt", "cipher": "AES-256-GCM"}


def _crypto():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError as exc:
        raise SystemExit("Missing dependency. Run: python -m pip install cryptography") from exc
    return AESGCM, Scrypt


def _key(password: str, salt: bytes) -> bytes:
    _, scrypt = _crypto()
    return scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode())


def _password(confirm: bool) -> str:
    value = getpass.getpass("Encryption password (hidden): ")
    if len(value) < 12:
        raise SystemExit("Use a password of at least 12 characters.")
    if confirm and value != getpass.getpass("Confirm password (hidden): "):
        raise SystemExit("Passwords do not match.")
    return value


def _zip(source: Path) -> bytes:
    if not (source / "accounts.json").is_file():
        raise SystemExit(f"No accounts.json found in {source}")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source).as_posix())
    return output.getvalue()


def export_bundle(source: Path, output: Path, password: str) -> None:
    aesgcm, _ = _crypto()
    salt, nonce = os.urandom(16), os.urandom(12)
    aad = json.dumps(HEADER, sort_keys=True, separators=(",", ":")).encode()
    encrypted = aesgcm(_key(password, salt)).encrypt(nonce, _zip(source), aad)
    payload = {**HEADER, "salt": base64.b64encode(salt).decode(),
               "nonce": base64.b64encode(nonce).decode(),
               "data": base64.b64encode(encrypted).decode()}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    try:
        output.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _decrypt(bundle: Path, password: str) -> bytes:
    aesgcm, _ = _crypto()
    try:
        payload = json.loads(bundle.read_text(encoding="utf-8"))
        if any(payload.get(key) != value for key, value in HEADER.items()):
            raise ValueError("unsupported bundle")
        aad = json.dumps(HEADER, sort_keys=True, separators=(",", ":")).encode()
        return aesgcm(_key(password, base64.b64decode(payload["salt"]))).decrypt(
            base64.b64decode(payload["nonce"]), base64.b64decode(payload["data"]), aad)
    except Exception as exc:
        raise SystemExit("Could not decrypt bundle: wrong password or damaged file.") from exc


def import_bundle(bundle: Path, target: Path, password: str, replace: bool = False) -> None:
    if (target / "accounts.json").exists() and not replace:
        raise SystemExit(f"{target} is already configured. Use --replace to overwrite it.")
    raw = _decrypt(bundle, password)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as temp_name:
        temp = Path(temp_name)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            root = temp.resolve()
            for member in archive.infolist():
                if root not in (temp / member.filename).resolve().parents:
                    raise SystemExit("Unsafe path found in credential bundle.")
            archive.extractall(temp)
        if not (temp / "accounts.json").is_file():
            raise SystemExit("Bundle does not contain accounts.json.")
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(temp, target)
    for path in target.rglob("*"):
        if path.is_file():
            try:
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("output", type=Path)
    export.add_argument("--source", type=Path, default=Path(CONFIG_DIR))
    restore = commands.add_parser("import")
    restore.add_argument("bundle", type=Path)
    restore.add_argument("--target", type=Path, default=Path(CONFIG_DIR))
    restore.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    if args.command == "export":
        export_bundle(args.source.resolve(), args.output.resolve(), _password(True))
        print(f"Encrypted credential bundle written to {args.output.resolve()}")
    else:
        import_bundle(args.bundle.resolve(), args.target.resolve(), _password(False), args.replace)
        print(f"Credentials restored to {args.target.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
