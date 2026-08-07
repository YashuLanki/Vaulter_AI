"""
generate_release_key.py
------------------------
One-time setup: generates the Ed25519 keypair release.py signs with and
every instance verifies against. Run this once, on whichever machine
will run `python release.py` going forward -- not by every teammate, and
not on every machine.

Writes:
  system/confidentials/release_signing_key.pem   PRIVATE. Gitignored.
                                                  Never copy this anywhere,
                                                  especially not the shared
                                                  OneDrive folder -- anyone
                                                  who gets it can sign a
                                                  malicious update that every
                                                  instance will trust.
  system/release_public_key.pem                  PUBLIC. Tracked in git,
                                                  ships with every install.
                                                  Not secret by design --
                                                  verification only needs
                                                  this half of the pair.

Refuses to overwrite an existing key pair -- rotating the signing key
means every already-installed instance's release_public_key.pem is now
wrong until it receives a build containing the new one, which is exactly
the kind of change that needs a deliberate decision, not an accidental
overwrite from re-running this script.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()

PRIVATE_KEY_PATH = PROJECT_ROOT / "confidentials" / "release_signing_key.pem"
PUBLIC_KEY_PATH = PROJECT_ROOT / "release_public_key.pem"


def main() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if PRIVATE_KEY_PATH.exists() or PUBLIC_KEY_PATH.exists():
        print(f"A key already exists ({PRIVATE_KEY_PATH if PRIVATE_KEY_PATH.exists() else PUBLIC_KEY_PATH}).")
        print("Refusing to overwrite -- delete both files yourself first if you really mean to rotate the key.")
        sys.exit(1)

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    PRIVATE_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    PRIVATE_KEY_PATH.write_bytes(private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    PUBLIC_KEY_PATH.write_bytes(public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ))

    print(f"Generated a new release signing keypair.")
    print(f"  Private (gitignored, keep secret): {PRIVATE_KEY_PATH}")
    print(f"  Public (tracked, ships everywhere): {PUBLIC_KEY_PATH}")
    print()
    print("Commit the public key. Never commit the private key, and never copy it")
    print("into the shared OneDrive folder or anywhere another teammate can read it.")


if __name__ == "__main__":
    main()
