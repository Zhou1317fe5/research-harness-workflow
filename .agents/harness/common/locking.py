"""文件事务的进程间锁；锁文件不随目标文件的原子替换而改变。

注意：本实现不防同进程嵌套重入。同一进程内对同一锁文件第二次调用
`file_lock` 会因 `fcntl.flock` 阻塞而卡死。若未来需要支持同进程重入，
应引入 `threading.local` 或 fd 引用计数。当前所有已知调用点均为跨进程
或单次使用，故保持简单实现。
"""
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
