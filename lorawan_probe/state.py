"""Frame counters persisted between runs, keyed by DevAddr."""

import json
import os
from pathlib import Path


class StateError(RuntimeError):
    pass


class State:
    def __init__(self, path: Path, dev_addr: int, fcnt_override: int | None = None):
        self.path = path
        self._key = f"{dev_addr:08x}"
        self._all = {}
        try:
            if path.exists():
                self._all = json.loads(path.read_text())
            entry = self._all.get(self._key, {})
            self.fcnt_up = int(entry.get("fcnt_up", 0))  # next one to use
            self.fcnt_down = int(entry.get("fcnt_down", 0))  # last one seen
        except (OSError, ValueError, TypeError, AttributeError) as error:
            # Starting again from zero would make the server reject every frame
            raise StateError(f"cannot read {path}: {error}") from None
        if fcnt_override is not None:
            self.fcnt_up = fcnt_override

    def next_fcnt_up(self) -> int:
        """Reserve a counter value. It is saved before the caller can send it."""
        fcnt = self.fcnt_up
        if fcnt > 0xFFFFFFFF:
            raise StateError("uplink frame counter is exhausted")
        self.fcnt_up = fcnt + 1
        self._save()
        return fcnt

    def saw_fcnt_down(self, fcnt: int):
        self.fcnt_down = fcnt
        self._save()

    def _save(self):
        self._all[self._key] = {"fcnt_up": self.fcnt_up, "fcnt_down": self.fcnt_down}
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            with temp.open("w") as file:
                json.dump(self._all, file, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp, self.path)
        except OSError as error:
            raise StateError(f"cannot write {self.path}: {error}") from None
