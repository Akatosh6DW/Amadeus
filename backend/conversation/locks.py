"""对话域共享进程锁。

历史读取、清空、归档和摘要提交都必须使用同一把可重入锁；这里是唯一
所有者，server 只保留兼容别名并在调用边界传入它。
"""

from __future__ import annotations

import threading


CONVERSATION_LOCK = threading.RLock()
