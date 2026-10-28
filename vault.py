"""
Envelope encryption for third-party credentials (OAuth tokens).

    master key  --wraps-->  data key (one per business)  --encrypts-->  tokens

- The master key never touches the database. It comes from the
  INVISIBLE_MASTER_KEY environment variable, or from a key file created with
  owner-only permissions outside the project folder (default
  ~/.invisible/master.key). In production this is a KMS key (AWS KMS, Google
  Cloud KMS); only `_master()` changes.
- The wrapped data key is stored in the database. Stealing the database alone
  reveals no tokens.
- Each ciphertext is bound to its purpose ("google" etc.), so a token blob
  copied into another row fails to decrypt.

Uses Fernet (AES-128-CBC + HMAC-SHA256) from the `cryptography` package.
"""
import json
import os
import stat

try:
    from cryptography.fernet import Fernet, InvalidToken
    AVAILABLE = True
except ImportError:  # the prototype still runs in sandbox mode without it
    Fernet = InvalidToken = None
    AVAILABLE = False

DEFAULT_KEY_FILE = os.path.join(os.path.expanduser("~"), ".invisible", "master.key")


class VaultError(Exception):
    pass


class Vault:
    def __init__(self, load_wrapped, save_wrapped):
        """load_wrapped() -> str | None and save_wrapped(str) persist the wrapped data key."""
        if not AVAILABLE:
            raise VaultError("Encrypted token storage needs the cryptography package: "
                             "pip3 install cryptography")
        self._load_wrapped = load_wrapped
        self._save_wrapped = save_wrapped
        self._dek = None

    # -- keys ---------------------------------------------------------------

    @staticmethod
    def _master():
        env = os.environ.get("INVISIBLE_MASTER_KEY")
        if env:
            return Fernet(env.encode())
        path = os.environ.get("INVISIBLE_KEY_FILE", DEFAULT_KEY_FILE)
        if not os.path.exists(path):
            folder = os.path.dirname(path)
            os.makedirs(folder, mode=0o700, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            raise VaultError("Master key file %s is readable by other users (mode %o). "
                             "Run: chmod 600 %s" % (path, mode, path))
        with open(path, "rb") as f:
            return Fernet(f.read().strip())

    def _data_key(self):
        if self._dek is None:
            master = self._master()
            wrapped = self._load_wrapped()
            if wrapped:
                try:
                    self._dek = Fernet(master.decrypt(wrapped.encode()))
                except InvalidToken:
                    raise VaultError("The stored data key can't be unwrapped with this master key. "
                                     "Was the master key changed?")
            else:
                key = Fernet.generate_key()
                self._save_wrapped(master.encrypt(key).decode())
                self._dek = Fernet(key)
        return self._dek

    # -- API ----------------------------------------------------------------

    def seal(self, purpose, data):
        blob = json.dumps({"purpose": purpose, "data": data}).encode()
        return self._data_key().encrypt(blob).decode()

    def open(self, purpose, token):
        try:
            payload = json.loads(self._data_key().decrypt(token.encode()))
        except InvalidToken:
            raise VaultError("Stored credentials failed their integrity check.")
        if payload.get("purpose") != purpose:
            raise VaultError("Stored credentials belong to %r, not %r." % (payload.get("purpose"), purpose))
        return payload["data"]
