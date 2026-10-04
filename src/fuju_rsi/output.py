"""同一进程内串行捕获私有输出，避免全局 stdout 被验收线程交叉替换。"""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import threading


_capture_lock = threading.RLock()


@contextmanager
def private_output(stream):
    # CLI 的 factory 捕获会嵌套 verify 捕获，因此使用可重入锁。业务回调仍应
    # 自己设置超时；需要并行执行私有验收时，由宿主启动独立进程。
    with _capture_lock, redirect_stdout(stream), redirect_stderr(stream):
        yield
