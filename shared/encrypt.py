"""
AES-GCM encryption helper for Ferryrichman 系統
- 用 passphrase + PBKDF2 derive 256-bit key
- AES-GCM 加密 JSON content
- Output 為 base64-encoded JSON: {salt, iv, ciphertext}
- 設計上同 WebCrypto API 兼容 (browser 端可解)
"""
import base64
import json
import os
import sys
from pathlib import Path

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
except ImportError:
    print("Missing dep: pip install cryptography", file=sys.stderr)
    raise

# Must match frontend crypto.js
ITERATIONS = 250_000
KEY_LEN = 32  # AES-256
SALT_LEN = 16
IV_LEN = 12  # AES-GCM standard


def derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_LEN,
        salt=salt,
        iterations=ITERATIONS,
    )
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt_bytes(plaintext: bytes, passphrase: str) -> dict:
    salt = os.urandom(SALT_LEN)
    iv = os.urandom(IV_LEN)
    key = derive_key(passphrase, salt)
    ct = AESGCM(key).encrypt(iv, plaintext, None)
    return {
        "v": 1,
        "iter": ITERATIONS,
        "salt": base64.b64encode(salt).decode(),
        "iv": base64.b64encode(iv).decode(),
        "ct": base64.b64encode(ct).decode(),
    }


def decrypt_bytes(blob: dict, passphrase: str) -> bytes:
    salt = base64.b64decode(blob["salt"])
    iv = base64.b64decode(blob["iv"])
    ct = base64.b64decode(blob["ct"])
    key = derive_key(passphrase, salt)
    return AESGCM(key).decrypt(iv, ct, None)


def encrypt_file(in_path: Path, out_path: Path, passphrase: str):
    plaintext = Path(in_path).read_bytes()
    blob = encrypt_bytes(plaintext, passphrase)
    Path(out_path).write_text(json.dumps(blob, separators=(",", ":")))
    print(f"[encrypt] {in_path} → {out_path} ({len(plaintext)} → {Path(out_path).stat().st_size} bytes)")


if __name__ == "__main__":
    # CLI: python -m shared.encrypt <in> <out> [--env PASSPHRASE_VAR]
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("in_file")
    ap.add_argument("out_file")
    ap.add_argument("--env", default="INVESTBOT_PE_PASSPHRASE",
                    help="env var name holding passphrase")
    args = ap.parse_args()

    passphrase = os.environ.get(args.env)
    if not passphrase:
        print(f"❌ env var {args.env} not set", file=sys.stderr)
        sys.exit(1)

    encrypt_file(args.in_file, args.out_file, passphrase)
