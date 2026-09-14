import threading
from enum import Enum
from typing import Callable, Optional


class TaskStatus(Enum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPING = "stopping"


StatusCallback = Callable[[TaskStatus], None]


class TaskManager:
    def __init__(self):
        self._status = TaskStatus.IDLE
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._callback: Optional[StatusCallback] = None

    @property
    def is_running(self) -> bool:
        return self._status == TaskStatus.RUNNING

    @property
    def status(self) -> TaskStatus:
        return self._status

    def set_status_callback(self, callback: StatusCallback):
        self._callback = callback

    def _notify(self):
        if self._callback:
            self._callback(self._status)

    def start(self, mode: str, config: dict):
        with self._lock:
            if self._status != TaskStatus.IDLE:
                return
            self._status = TaskStatus.RUNNING
            self._notify()

        self._thread = threading.Thread(
            target=self._run_loop, args=(mode, config), daemon=True
        )
        self._thread.start()

    def stop(self):
        with self._lock:
            if self._status != TaskStatus.RUNNING:
                return
            self._status = TaskStatus.STOPPING
            self._notify()

    def _run_loop(self, mode: str, config: dict):
        try:
            while True:
                with self._lock:
                    if self._status != TaskStatus.RUNNING:
                        break
                self._execute_tick(mode, config)
                import time
                time.sleep(0.5)
        finally:
            with self._lock:
                self._status = TaskStatus.IDLE
                self._notify()

    def _execute_tick(self, mode: str, config: dict):
        pass
