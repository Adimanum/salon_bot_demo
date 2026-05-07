import json
import aiosqlite
from typing import Any, Dict, Optional
from aiogram.fsm.storage.base import BaseStorage, StorageKey


class SQLiteStorage(BaseStorage):
    """FSM storage backed by SQLite — survives bot restarts."""

    def __init__(self, db_path: str):
        self._db_path = db_path

    async def _init(self):
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS fsm_data (
                    key   TEXT PRIMARY KEY,
                    state TEXT,
                    data  TEXT NOT NULL DEFAULT '{}'
                )
            """)
            await db.commit()

    def _key(self, key: StorageKey) -> str:
        return f"{key.bot_id}:{key.chat_id}:{key.user_id}"

    async def set_state(self, key: StorageKey, state: Optional[Any] = None) -> None:
        k = self._key(key)
        state_str = state.state if hasattr(state, "state") else state
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """INSERT INTO fsm_data (key, state, data) VALUES (?, ?, '{}')
                   ON CONFLICT(key) DO UPDATE SET state = excluded.state""",
                (k, state_str),
            )
            await db.commit()

    async def get_state(self, key: StorageKey) -> Optional[str]:
        k = self._key(key)
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT state FROM fsm_data WHERE key = ?", (k,)
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else None

    async def set_data(self, key: StorageKey, data: Dict[str, Any]) -> None:
        k = self._key(key)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """INSERT INTO fsm_data (key, state, data) VALUES (?, NULL, ?)
                   ON CONFLICT(key) DO UPDATE SET data = excluded.data""",
                (k, json.dumps(data, ensure_ascii=False)),
            )
            await db.commit()

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        k = self._key(key)
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT data FROM fsm_data WHERE key = ?", (k,)
            ) as cur:
                row = await cur.fetchone()
        return json.loads(row[0]) if row else {}

    async def close(self) -> None:
        pass
