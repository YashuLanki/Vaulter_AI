"""
release_signing.py
-------------------
Ed25519 signing/verification for auto-update packages (E1 in
vaulter-leak-guard's attack-surface checklist).

WHY THIS EXISTS: release.py publishes a code zip + a JSON marker to a
folder every teammate can write to (config.UPDATES_DIR, on the shared
OneDrive). Before this, nothing verified that a downloaded package
actually came from whoever runs release.py -- a hash stored in that same
writable folder only catches corruption, never tampering, since anyone
who can write the zip can just as easily rewrite the hash sitting next
to it. The fix has to be asymmetric: release.py signs with a PRIVATE key
that never leaves the machine it was generated on and never touches the
shared folder (system/confidentials/release_signing_key.pem, gitignored);
every instance verifies with the PUBLIC key, which isn't secret and ships
with the code (system/release_public_key.pem, tracked).

Ed25519 specifically: small keys and signatures, no padding scheme to get
wrong (unlike RSA), and it's the `cryptography` package's own recommended
default for new systems.
"""

from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature

PUBLIC_KEY_PATH = Path(__file__).resolve().parents[1] / "release_public_key.pem"


def sign_bytes(data: bytes, private_key_path: Path) -> bytes:
    """
    Signs data with the Ed25519 private key at private_key_path.

    Raises FileNotFoundError with an actionable message if the key
    doesn't exist -- release.py is the only caller, and publishing an
    unsigned package silently would defeat the entire point of this
    module, so this fails loudly rather than degrading.
    """
    if not private_key_path.exists():
        raise FileNotFoundError(
            f"No release signing key at {private_key_path}. Run "
            f"`python scripts/generate_release_key.py` once to create one, "
            f"then keep it private -- never commit it, never put it in the "
            f"shared OneDrive folder."
        )
    private_key = serialization.load_pem_private_key(
        private_key_path.read_bytes(), password=None,
    )
    return private_key.sign(data)


def verify_bytes(data: bytes, signature: bytes, public_key_path: Path = PUBLIC_KEY_PATH) -> bool:
    """
    Verifies data against signature using the Ed25519 public key at
    public_key_path. Never raises -- a missing key, a malformed
    signature, and a genuine mismatch are all just "not verified" to the
    caller, which should refuse to stage/apply in every one of those
    cases rather than trying to distinguish them.
    """
    try:
        public_key = serialization.load_pem_public_key(public_key_path.read_bytes())
        public_key.verify(signature, data)
        return True
    except (OSError, ValueError, InvalidSignature):
        return False
