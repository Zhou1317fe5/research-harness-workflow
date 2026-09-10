"""文件事务的进程间锁；锁文件不随目标文件的原子替换而改变。"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path


@contextmanager
def file_lock(path: Path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield
