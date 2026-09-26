"""Runtime settings. Demo mode never grants access to the real salon database."""

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    demo_mode: bool = True
    admin_ids: frozenset[int] = field(default_factory=frozenset)
    reminder_interval: int = 60

    def is_admin(self, user_id: int) -> bool:
        return self.demo_mode or user_id in self.admin_ids

    @classmethod
    def from_env(cls):
        mode = os.getenv("DEMO_MODE", "true").strip().lower()
        if mode not in {"true", "false", "1", "0"}:
            raise ValueError("DEMO_MODE must be true or false")
        raw_ids = os.getenv("ADMIN_IDS", "").replace(" ", "").split(",")
        try:
            admin_ids = frozenset(int(value) for value in raw_ids if value)
            if any(value <= 0 for value in admin_ids):
                raise ValueError
        except ValueError:
            raise ValueError("ADMIN_IDS must contain positive Telegram IDs separated by commas") from None
        return cls(demo_mode=mode in {"true", "1"}, admin_ids=admin_ids)
