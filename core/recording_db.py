"""
錄音歷史資料庫管理

使用 SQLite 儲存錄音記錄，包含：
- 錄音時間戳
- 錄音長度
- 原始音頻（WAV 格式 blob）
- ASR 識別結果
- LLM 處理結果（可選）
- 使用的角色 ID

資料庫位置：%APPDATA%/zhouzhou-voice/history.db
"""

from __future__ import annotations

import io
import sqlite3
import struct
import threading
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from utils.logger import get_logger

logger = get_logger("recording_db")


@dataclass
class RecordingRecord:
    """錄音記錄"""
    id: int
    timestamp: datetime
    duration: float               # 秒
    audio_data: bytes             # WAV 格式
    asr_text: str = ""
    llm_text: str = ""
    role_id: str = ""
    model_key: str = ""


class RecordingDatabase:
    """錄音歷史資料庫"""

    DB_PATH = Path.home() / "AppData" / "Roaming" / "zhouzhou-voice" / "history.db"

    def __init__(self) -> None:
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        self._ensure_db()

    def _ensure_db(self) -> None:
        """確保資料庫存在且 schema 正確"""
        self.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.DB_PATH), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()
        logger.info("錄音歷史資料庫已初始化: %s", self.DB_PATH)

    def _create_tables(self) -> None:
        """建立資料表"""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS recordings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                duration REAL NOT NULL,
                audio_data BLOB NOT NULL,
                asr_text TEXT DEFAULT '',
                llm_text TEXT DEFAULT '',
                role_id TEXT DEFAULT '',
                model_key TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_timestamp
            ON recordings(timestamp DESC);
        """)
        self._conn.commit()

    def insert(
        self,
        audio_bytes: bytes,
        duration: float,
        asr_text: str = "",
        llm_text: str = "",
        role_id: str = "",
        model_key: str = "",
    ) -> int:
        """插入新記錄，返回 record_id"""
        wav_data = self._encode_wav(audio_bytes, duration)

        with self._lock:
            cursor = self._conn.execute(
                """INSERT INTO recordings
                   (timestamp, duration, audio_data, asr_text, llm_text, role_id, model_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now().isoformat(),
                    duration,
                    wav_data,
                    asr_text,
                    llm_text,
                    role_id,
                    model_key,
                ),
            )
            self._conn.commit()
            record_id = cursor.lastrowid
        logger.debug("已儲存錄音記錄: id=%d, duration=%.2fs", record_id, duration)
        return record_id

    def get_by_id(self, record_id: int) -> Optional[RecordingRecord]:
        """根據 ID 取得記錄"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM recordings WHERE id = ?", (record_id,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def get_recent(self, limit: int = 50) -> List[RecordingRecord]:
        """取得最近的記錄"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM recordings ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def update(
        self,
        record_id: int,
        asr_text: Optional[str] = None,
        llm_text: Optional[str] = None,
        role_id: Optional[str] = None,
    ) -> bool:
        """更新記錄"""
        updates = []
        params = []
        if asr_text is not None:
            updates.append("asr_text = ?")
            params.append(asr_text)
        if llm_text is not None:
            updates.append("llm_text = ?")
            params.append(llm_text)
        if role_id is not None:
            updates.append("role_id = ?")
            params.append(role_id)

        if not updates:
            return False

        params.append(record_id)
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE recordings SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            self._conn.commit()
            changed = cursor.rowcount > 0
        return changed

    def delete(self, record_id: int) -> bool:
        """刪除記錄"""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM recordings WHERE id = ?", (record_id,)
            )
            self._conn.commit()
            deleted = cursor.rowcount > 0
        if deleted:
            logger.debug("已刪除錄音記錄: id=%d", record_id)
        return deleted

    def cleanup_old(self, days: int) -> int:
        """清理超過指定天數的記錄，返回刪除數量"""
        with self._lock:
            cursor = self._conn.execute(
                """DELETE FROM recordings
                   WHERE date(timestamp) < date('now', ?)""",
                (f"-{days} days",),
            )
            self._conn.commit()
            deleted = cursor.rowcount
        if deleted > 0:
            logger.info("已清理 %d 筆過期錄音記錄", deleted)
        return deleted

    def count(self) -> int:
        """取得總記錄數"""
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM recordings"
            ).fetchone()[0]

    @staticmethod
    def _encode_wav(audio_bytes: bytes, duration: float) -> bytes:
        """將 float32 PCM 編碼為 WAV 格式"""
        n_samples = len(audio_bytes) // 4
        if n_samples == 0:
            return b""

        # float32 → int16
        samples = struct.unpack(f"{n_samples}f", audio_bytes)
        int16_samples = [int(max(-1, min(1, s)) * 32767) for s in samples]

        # WAV header
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)      # 16-bit
            wf.setframerate(16000)
            wf.writeframes(struct.pack(f"{len(int16_samples)}h", *int16_samples))
        return buffer.getvalue()

    @staticmethod
    def decode_wav(wav_bytes: bytes) -> bytes:
        """將 WAV 格式解碼為 float32 PCM"""
        if not wav_bytes:
            return b""

        buffer = io.BytesIO(wav_bytes)
        with wave.open(buffer, "rb") as wf:
            frames = wf.readframes(wf.getnframes())
            n_samples = len(frames) // 2
            if n_samples == 0:
                return b""
            int16_samples = struct.unpack(f"{n_samples}h", frames)
            float_samples = [s / 32767.0 for s in int16_samples]
            return struct.pack(f"{n_samples}f", *float_samples)

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> RecordingRecord:
        """轉換資料庫行為 Record 物件"""
        return RecordingRecord(
            id=row["id"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            duration=row["duration"],
            audio_data=row["audio_data"],
            asr_text=row["asr_text"] or "",
            llm_text=row["llm_text"] or "",
            role_id=row["role_id"] or "",
            model_key=row["model_key"] or "",
        )

    def close(self) -> None:
        """關閉資料庫連接"""
        if self._conn:
            self._conn.close()
            self._conn = None
            logger.info("錄音歷史資料庫已關閉")
