"""Security helpers shared by the API, the pipeline and the git integration.

Threat model (see docs/SECURITY.md): the analyzer reads source code and design
documents, so the assets are file contents on the server and credentials in
the environment. Attackers considered: anyone who can reach the HTTP port,
web pages open in the user's browser (CSRF / DNS rebinding against a local
server), forged webhooks, and repositories or documents crafted to escape the
checkout (symlinks, ../ paths) or exhaust resources (huge files, zip bombs).
"""
from __future__ import annotations

import hmac
import os
import re
import zipfile


class PathNotAllowed(ValueError):
    pass


def _real(p: str) -> str:
    return os.path.realpath(os.path.abspath(p))


def within(base: str, path: str) -> bool:
    b, p = _real(base), _real(path)
    return p == b or p.startswith(b.rstrip(os.sep) + os.sep)


def safe_join(base: str, rel: str) -> str | None:
    """base/rel, or None when rel escapes base (../, absolute paths, symlinks)."""
    if not base or rel is None:
        return None
    if os.path.isabs(rel) or "\0" in rel:
        return None
    p = os.path.join(base, rel)
    return p if within(base, p) else None


def allowed_roots() -> list[str]:
    raw = os.environ.get("HIMOTOKI_ALLOWED_ROOTS", "")
    return [_real(r) for r in raw.split(os.pathsep) if r.strip()]


def check_input_path(path: str | None, what: str = "path") -> str | None:
    """Paths supplied over the API (repository, documents, CSV exports) must lie
    under HIMOTOKI_ALLOWED_ROOTS when it is set (it is in the container setup)."""
    if not path:
        return path
    if "\0" in path:
        raise PathNotAllowed(f"invalid {what}")
    roots = allowed_roots()
    if roots and not any(within(r, path) for r in roots):
        raise PathNotAllowed(f"{what} is outside HIMOTOKI_ALLOWED_ROOTS")
    return path


# git refs coming from webhooks or the API end up as git arguments
_REF = re.compile(r"^(?!-)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/\-^~@{}]{1,200}$")


def check_ref(ref: str | None, what: str = "ref") -> str | None:
    if ref is None:
        return None
    if not isinstance(ref, str) or not _REF.match(ref) or ref.endswith((".lock", "/")):
        raise ValueError(f"invalid {what}: {str(ref)[:60]!r}")
    return ref


def constant_time_equals(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    return hmac.compare_digest(a.encode(), b.encode())


# --------------------------------------------------------------- resources
def max_file_bytes() -> int:
    return int(float(os.environ.get("HIMOTOKI_MAX_FILE_MB", "8")) * 1024 * 1024)


def office_zip_ok(data: bytes, limit_mb: float | None = None) -> str | None:
    """Reject zip bombs before openpyxl / python-docx inflate the package.
    Returns an error message, or None when the archive looks sane."""
    import io
    limit = int((limit_mb or float(os.environ.get("HIMOTOKI_MAX_OFFICE_MB", "200"))) * 1024 * 1024)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
            if len(infos) > 20000:
                return "too many entries in Office package"
            total = sum(i.file_size for i in infos)
            if total > limit:
                return f"Office package expands to {total // 1048576} MB (limit {limit // 1048576} MB)"
            packed = sum(i.compress_size for i in infos) or 1
            if total / packed > 200 and total > 50 * 1024 * 1024:
                return "suspicious compression ratio in Office package"
    except zipfile.BadZipFile:
        return "not a valid Office (zip) package"
    return None


_SECRET_RX = re.compile(r"(?i)(password|passwd|pwd|token|secret)=([^\s&;]+)|(//[^:/@\s]+:)([^@\s]+)@")


def redact(text: str) -> str:
    """Remove credentials from messages that may reach logs or API responses."""
    def sub(m):
        if m.group(1):
            return f"{m.group(1)}=***"
        return f"{m.group(3)}***@"
    return _SECRET_RX.sub(sub, text or "")


def atomic_write_json(path: str, obj, **kw) -> None:
    """Write JSON through a temporary file and rename, so a crash or a full disk
    never leaves a half-written workspace / source file behind."""
    import json
    import tempfile
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, **kw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
