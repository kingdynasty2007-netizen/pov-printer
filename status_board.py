# ============================================================
# FILE: status_board.py
# PURPOSE: Simple live status. One line per scene, only prints on
# change. Heartbeat dots during idle stretches so it never looks frozen.
# ============================================================

import time
import threading


class StatusBoard:
    def __init__(self, scene_keys, heartbeat_seconds=5):
        self.state = {k: None for k in scene_keys}
        self.lock = threading.Lock()
        self.heartbeat_seconds = heartbeat_seconds
        self._last_activity = time.time()
        self._dots_open = False
        self._stop_flag = threading.Event()
        self._thread = None

    def update(self, scene_key, text, done=False):
        with self.lock:
            if self.state.get(scene_key) == text:
                self._last_activity = time.time()
                return
            self.state[scene_key] = text
            if self._dots_open:
                print()
                self._dots_open = False
            self._last_activity = time.time()
        marker = "✅" if done and "❌" not in text else ("❌" if done else "•")
        print(f"{marker} {scene_key}: {text}")

    def _heartbeat_loop(self):
        while not self._stop_flag.is_set():
            time.sleep(self.heartbeat_seconds)
            with self.lock:
                idle = time.time() - self._last_activity
                if idle >= self.heartbeat_seconds:
                    print(".", end="", flush=True)
                    self._dots_open = True
                    self._last_activity = time.time()

    def start(self):
        self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_flag.set()
        if self._thread:
            self._thread.join(timeout=1)
        with self.lock:
            if self._dots_open:
                print()
                self._dots_open = False