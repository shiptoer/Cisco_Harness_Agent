"""Thư mục làm việc (cwd) của dsh: cố định, tên trung tính, nằm NGOÀI repo (D-025).

dsh gửi nguyên văn đường dẫn thư mục làm việc cho model (system prompt: "Your working directory is …";
message runtime-context: "session workspace: …"; thấy ở lượt thử 01/10/2026). Khi thư mục đó nằm trong
run-dir (`runs/<run_id>/.dsh_cwd`, D-023) thì `run_id`, tức tên case, đi vào prompt. Vì vậy thư mục làm
việc không được phụ thuộc vào case: một đường dẫn cố định, giống nhau ở mọi case và mọi lượt chạy.

Một thư mục dùng chung thì hai lượt chạy cùng lúc sẽ giẫm lên nhau, nên có khoá độc quyền:

    <cha>/workspace        thư mục làm việc, rỗng trước mỗi stage
    <cha>/workspace.lock   file khoá; cũng là dấu "thư mục này do runner tạo"

- Khoá là khoá file của hệ điều hành (`msvcrt.locking` trên Windows, `flock` trên POSIX), lấy ở chế độ
  KHÔNG chờ: lượt thứ hai nhận `WorkspaceError` ngay. Process giữ khoá chết thì hệ điều hành tự nhả, nên
  không có khoá "mồ côi" phải xoá tay. File khoá không bao giờ bị xoá.
- Runner chỉ dọn thư mục mà nó đang giữ khoá, và từ chối dùng một thư mục không rỗng mà không có file
  khoá của nó bên cạnh: không bao giờ xoá dữ liệu nó không tạo ra.

Chạy khô và test dùng `temporary()`: mỗi case một thư mục tạm riêng, không khoá, để chạy song song được.

Trạng thái: ĐỀ XUẤT (D-025).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

LOCK_MAGIC = "cha-workspace-lock v1"
TEMP_PREFIX = "cha-workspace-"
# Byte bị khoá nằm xa sau nội dung file, để process khác vẫn đọc được ai đang giữ khoá.
_LOCK_OFFSET = 1 << 30

# Khoá đang giữ trong process này: đường dẫn chuẩn hoá → [file descriptor, số lần giữ lồng nhau].
_held: dict[str, list[int]] = {}
# Thư mục tạm do `temporary()` tạo và còn sống.
_temp: set[str] = set()


class WorkspaceError(Exception):
    """Thư mục làm việc của dsh không dùng được: sai vị trí, đang bị khoá, hoặc không dọn được."""


def default_workspace() -> Path:
    """`%LOCALAPPDATA%\\cha\\workspace` trên Windows; `$XDG_CACHE_HOME/cha/workspace` hoặc
    `~/.cache/cha/workspace` trên hệ khác. Không chứa gì của case hay của repo."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    else:
        base = os.environ.get("XDG_CACHE_HOME")
        root = Path(base) if base else Path.home() / ".cache"
    return root / "cha" / "workspace"


def lock_file(workspace: Path) -> Path:
    return workspace.parent / f"{workspace.name}.lock"


def check_location(workspace: Path, repo_root: Path) -> Path:
    """Đường dẫn tuyệt đối đã chuẩn hoá của `workspace`, hoặc `WorkspaceError` nếu vị trí không an toàn.

    Runner xoá mọi thứ trong thư mục này trước mỗi stage, nên nó không được là (hay chứa) gốc ổ đĩa, thư
    mục home hay repo; và không được nằm trong repo (đường dẫn repo sẽ đi vào prompt của model).
    """
    if not workspace.is_absolute():
        raise WorkspaceError(f"thư mục làm việc của dsh phải là đường dẫn tuyệt đối: {workspace}")
    path, repo, home = workspace.resolve(), repo_root.resolve(), Path.home().resolve()
    if path == Path(path.anchor):
        raise WorkspaceError(f"thư mục làm việc của dsh không được là gốc ổ đĩa: {path}")
    for name, other in (("repo", repo), ("thư mục home", home)):
        if path == other or path in other.parents:
            raise WorkspaceError(f"thư mục làm việc của dsh không được là hay chứa {name}: {path}")
    if repo in path.parents:
        raise WorkspaceError(f"thư mục làm việc của dsh phải nằm ngoài repo: {path}")
    return path


@contextlib.contextmanager
def hold(workspace: Path) -> Iterator[Path]:
    """Giữ khoá độc quyền của `workspace` trong khối `with`; tạo thư mục nếu chưa có.

    Lồng nhau được trong cùng process: `run_cases.py` giữ khoá cho cả lô, `run_case` giữ lại cho từng
    case. Process khác đang giữ khoá → `WorkspaceError` ngay, không chờ.
    """
    key = _key(workspace)
    if key in _held:
        _held[key][1] += 1
    else:
        _held[key] = [_acquire(workspace), 1]
    try:
        yield workspace
    finally:
        entry = _held[key]
        entry[1] -= 1
        if entry[1] == 0:
            del _held[key]
            _release(entry[0])


@contextlib.contextmanager
def temporary() -> Iterator[Path]:
    """Một thư mục tạm riêng, tên trung tính, xoá khi ra khỏi khối `with`. Không khoá."""
    path = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
    _temp.add(_key(path))
    try:
        yield path
    finally:
        _temp.discard(_key(path))
        shutil.rmtree(path, ignore_errors=True)


def make_empty(workspace: Path, attempts: int = 5, delay: float = 0.3) -> None:
    """Xoá mọi thứ trong `workspace`. Thử lại vài lần (process con của dsh vừa thoát có thể còn giữ file
    trên Windows); vẫn không rỗng thì `WorkspaceError`: dsh không được khởi động trong thư mục có sẵn file.

    Chỉ dọn thư mục mà process này đang giữ khoá, hoặc thư mục tạm do `temporary()` tạo.
    """
    if _key(workspace) not in _held and _key(workspace) not in _temp:
        raise WorkspaceError(f"từ chối dọn thư mục chưa được khoá bởi lượt chạy này: {workspace}")
    left: list[str] = []
    for attempt in range(attempts):
        left = _clear(workspace)
        if not left:
            return
        if attempt + 1 < attempts:
            time.sleep(delay)
    raise WorkspaceError(
        f"không dọn rỗng được thư mục làm việc của dsh {workspace}: còn {len(left)} mục "
        "(một process khác đang giữ file?)"
    )


def holder(workspace: Path) -> str:
    """Thông tin lượt chạy ghi trong file khoá (pid, thời điểm), để báo cho người dùng.
    Chuỗi rỗng nếu không đọc được."""
    try:
        lines = lock_file(workspace).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return ", ".join(line for line in lines[1:4] if line)[:200]


def _key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _acquire(workspace: Path) -> int:
    lock = lock_file(workspace)
    if workspace.exists() and not workspace.is_dir():
        raise WorkspaceError(f"thư mục làm việc của dsh không phải thư mục: {workspace}")
    if workspace.is_dir() and any(workspace.iterdir()) and not _is_ours(lock):
        raise WorkspaceError(
            f"thư mục {workspace} không rỗng và không do runner tạo (không có {lock.name} bên cạnh). "
            "Runner sẽ xoá mọi thứ trong thư mục làm việc của dsh, nên không dùng thư mục này: chọn thư mục "
            "khác ở `[dsh] workspace`, hoặc tự dọn nó."
        )
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        # O_BINARY (chỉ có trên Windows): không đổi "\n" thành "\r\n", để số byte ghi khớp `ftruncate`.
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
    except OSError as exc:
        raise WorkspaceError(f"không tạo được thư mục làm việc của dsh {workspace}: {exc}") from exc
    try:
        _lock(fd)
    except OSError as exc:
        os.close(fd)
        who = holder(workspace)
        raise WorkspaceError(
            f"thư mục làm việc của dsh {workspace} đang được một lượt chạy khác dùng"
            + (f" ({who})" if who else "")
            + ". Chờ lượt đó xong rồi chạy lại, hoặc đặt `[dsh] workspace` khác trong config."
        ) from exc
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    data = f"{LOCK_MAGIC}\npid={os.getpid()}\nsince={stamp}\n".encode()
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, data)
    os.ftruncate(fd, len(data))
    return fd


def _release(fd: int) -> None:
    with contextlib.suppress(OSError):
        _unlock(fd)
    os.close(fd)


def _is_ours(lock: Path) -> bool:
    try:
        return lock.read_text(encoding="utf-8", errors="replace").startswith(LOCK_MAGIC)
    except OSError:
        return False


def _lock(fd: int) -> None:
    """Khoá độc quyền, không chờ. `OSError` nếu process khác (hoặc handle khác) đang giữ."""
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _clear(workspace: Path) -> list[str]:
    """Xoá từng mục trong `workspace`, không đi theo symlink hay junction. Trả về tên các mục còn lại."""
    left: list[str] = []
    with os.scandir(workspace) as entries:
        for entry in list(entries):
            try:
                if entry.is_dir(follow_symlinks=False):
                    try:
                        shutil.rmtree(entry.path)
                    except OSError:
                        os.rmdir(entry.path)  # junction trên Windows: gỡ liên kết, không đụng đích
                else:
                    os.unlink(entry.path)
            except OSError:
                left.append(entry.name)
    return left
