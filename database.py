import hashlib
import secrets
import string
import aiosqlite
from config import DB_PATH

# PBKDF2-HMAC-SHA256: 260 000 iterations, salt = telegram_id (stored in DB as "pbkdf2$<hex>")
_PBKDF2_ITERS = 260_000


def _hash(password: str, telegram_id: int) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        str(telegram_id).encode(),
        _PBKDF2_ITERS,
    )
    return "pbkdf2$" + dk.hex()


def _hash_legacy(password: str, telegram_id: int) -> str:
    """Old SHA256 format — only used during migration check."""
    return hashlib.sha256(f"{telegram_id}:{password}".encode()).hexdigest()


def _invite_code() -> str:
    chars = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(7))


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS masters (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id   INTEGER UNIQUE,
                name          TEXT NOT NULL,
                service_type  TEXT NOT NULL,
                password_hash TEXT,
                price         INTEGER DEFAULT 0,
                is_active     INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS master_invites (
                code      TEXT PRIMARY KEY,
                master_id INTEGER NOT NULL,
                FOREIGN KEY (master_id) REFERENCES masters(id)
            );

            CREATE TABLE IF NOT EXISTS slots (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                master_id INTEGER NOT NULL,
                date      TEXT NOT NULL,
                time      TEXT NOT NULL,
                is_booked INTEGER DEFAULT 0,
                FOREIGN KEY (master_id) REFERENCES masters(id),
                UNIQUE(master_id, date, time)
            );

            CREATE TABLE IF NOT EXISTS bookings (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_id            INTEGER NOT NULL,
                client_name        TEXT NOT NULL,
                client_phone       TEXT NOT NULL,
                client_telegram_id INTEGER,
                created_at         TEXT DEFAULT CURRENT_TIMESTAMP,
                cancelled          INTEGER DEFAULT 0,
                cancelled_by       TEXT,
                reminder_24h_sent  INTEGER DEFAULT 0,
                reminder_1h_sent   INTEGER DEFAULT 0,
                FOREIGN KEY (slot_id) REFERENCES slots(id)
            );

            CREATE TABLE IF NOT EXISTS wrong_attempts (
                telegram_id  INTEGER PRIMARY KEY,
                attempts     INTEGER DEFAULT 0,
                locked_until TEXT
            );

            CREATE TABLE IF NOT EXISTS schedule_templates (
                master_id  INTEGER PRIMARY KEY,
                start_hour INTEGER DEFAULT 10,
                start_min  INTEGER DEFAULT 0,
                end_hour   INTEGER DEFAULT 19,
                end_min    INTEGER DEFAULT 0,
                interval   INTEGER DEFAULT 30,
                FOREIGN KEY (master_id) REFERENCES masters(id)
            );
        """)
        await _migrate(db)
        await db.commit()


async def _migrate(db):
    async with db.execute("PRAGMA table_info(masters)") as cur:
        master_cols = {row[1] for row in await cur.fetchall()}
    async with db.execute("PRAGMA table_info(bookings)") as cur:
        booking_cols = {row[1] for row in await cur.fetchall()}

    if "password_hash"       not in master_cols:
        await db.execute("ALTER TABLE masters ADD COLUMN password_hash TEXT")
    if "price"               not in master_cols:
        await db.execute("ALTER TABLE masters ADD COLUMN price INTEGER DEFAULT 0")
    if "is_active"           not in master_cols:
        await db.execute("ALTER TABLE masters ADD COLUMN is_active INTEGER DEFAULT 1")
    if "cancelled"           not in booking_cols:
        await db.execute("ALTER TABLE bookings ADD COLUMN cancelled INTEGER DEFAULT 0")
    if "cancelled_by"        not in booking_cols:
        await db.execute("ALTER TABLE bookings ADD COLUMN cancelled_by TEXT")
    if "reminder_24h_sent"   not in booking_cols:
        await db.execute("ALTER TABLE bookings ADD COLUMN reminder_24h_sent INTEGER DEFAULT 0")
    if "reminder_1h_sent"    not in booking_cols:
        await db.execute("ALTER TABLE bookings ADD COLUMN reminder_1h_sent INTEGER DEFAULT 0")
    if "reminder_8h_sent"    not in booking_cols:
        await db.execute("ALTER TABLE bookings ADD COLUMN reminder_8h_sent INTEGER DEFAULT 0")
    # 0=неизвестно, 1=пришёл, 2=не пришёл
    if "attended"            not in booking_cols:
        await db.execute("ALTER TABLE bookings ADD COLUMN attended INTEGER DEFAULT 0")
    if "pending"             not in booking_cols:
        await db.execute("ALTER TABLE bookings ADD COLUMN pending INTEGER DEFAULT 0")
    if "review_sent"         not in booking_cols:
        await db.execute("ALTER TABLE bookings ADD COLUMN review_sent INTEGER DEFAULT 0")
    if "review_score"        not in booking_cols:
        await db.execute("ALTER TABLE bookings ADD COLUMN review_score INTEGER DEFAULT 0")

    # Add locked_until to wrong_attempts if missing
    async with db.execute("PRAGMA table_info(wrong_attempts)") as cur:
        wa_cols = {row[1] for row in await cur.fetchall()}
    if wa_cols and "locked_until" not in wa_cols:
        await db.execute("ALTER TABLE wrong_attempts ADD COLUMN locked_until TEXT")

    # Ensure master_invites table exists (old DBs may not have it)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS master_invites (
            code      TEXT PRIMARY KEY,
            master_id INTEGER NOT NULL,
            FOREIGN KEY (master_id) REFERENCES masters(id)
        )
    """)

    # Fix: if telegram_id still has NOT NULL constraint, rebuild masters table
    async with db.execute("PRAGMA table_info(masters)") as cur:
        cols_info = await cur.fetchall()
    tg_col = next((c for c in cols_info if c[1] == "telegram_id"), None)
    if tg_col and tg_col[3] == 1:  # notnull flag == 1 means NOT NULL
        await db.executescript("""
            PRAGMA foreign_keys = OFF;

            CREATE TABLE IF NOT EXISTS masters_new (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id   INTEGER UNIQUE,
                name          TEXT NOT NULL,
                service_type  TEXT NOT NULL,
                password_hash TEXT,
                price         INTEGER DEFAULT 0,
                is_active     INTEGER DEFAULT 1
            );

            INSERT INTO masters_new (id, telegram_id, name, service_type, password_hash, price, is_active)
            SELECT id, telegram_id, name, service_type,
                   CASE WHEN typeof(password_hash) = 'null' THEN NULL ELSE password_hash END,
                   COALESCE(price, 0),
                   COALESCE(is_active, 1)
            FROM masters;

            DROP TABLE masters;
            ALTER TABLE masters_new RENAME TO masters;

            PRAGMA foreign_keys = ON;
        """)


# ─── Masters ──────────────────────────────────────────────────

async def get_master(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM masters WHERE telegram_id = ? AND is_active = 1", (telegram_id,)
        ) as cur:
            return await cur.fetchone()


async def register_master(telegram_id: int, name: str, service_type: str, password: str | None):
    """INSERT or UPDATE master. If password is None — update name/service only."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM masters WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            existing = await cur.fetchone()
        if existing:
            if password is None:
                await db.execute(
                    "UPDATE masters SET name=?, service_type=? WHERE telegram_id=?",
                    (name, service_type, telegram_id),
                )
            else:
                await db.execute(
                    "UPDATE masters SET name=?, service_type=?, password_hash=? WHERE telegram_id=?",
                    (name, service_type, _hash(password, telegram_id), telegram_id),
                )
        else:
            pw_hash = _hash(password, telegram_id) if password else None
            await db.execute(
                """INSERT INTO masters (telegram_id, name, service_type, password_hash)
                   VALUES (?, ?, ?, ?)""",
                (telegram_id, name, service_type, pw_hash),
            )
        await db.commit()


async def verify_password(telegram_id: int, password: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT password_hash FROM masters WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row or not row[0]:
        return False
    stored = row[0]
    if stored.startswith("pbkdf2$"):
        return stored == _hash(password, telegram_id)
    # Legacy SHA256 — verify and silently upgrade to PBKDF2
    if stored == _hash_legacy(password, telegram_id):
        await update_password(telegram_id, password)
        return True
    return False


async def update_password(telegram_id: int, new_password: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE masters SET password_hash = ? WHERE telegram_id = ?",
            (_hash(new_password, telegram_id), telegram_id),
        )
        await db.commit()


async def set_master_price(telegram_id: int, price: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE masters SET price = ? WHERE telegram_id = ?", (price, telegram_id)
        )
        await db.commit()


async def get_masters_by_service(service_type: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM masters
               WHERE service_type = ? AND is_active = 1 AND telegram_id IS NOT NULL""",
            (service_type,),
        ) as cur:
            return await cur.fetchall()


# ─── Admin ────────────────────────────────────────────────────

async def admin_create_master(name: str, service_type: str) -> str:
    """Creates a pre-registered master without telegram_id. Returns invite code."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO masters (name, service_type, is_active) VALUES (?, ?, 1)",
            (name, service_type),
        )
        master_id = (
            await (await db.execute("SELECT last_insert_rowid()")).fetchone()
        )[0]
        code = _invite_code()
        # Ensure code is unique
        while True:
            async with db.execute(
                "SELECT 1 FROM master_invites WHERE code = ?", (code,)
            ) as cur:
                if not await cur.fetchone():
                    break
            code = _invite_code()
        await db.execute(
            "INSERT INTO master_invites (code, master_id) VALUES (?, ?)", (code, master_id)
        )
        await db.commit()
    return code


async def get_invite(code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT i.code, m.id as master_id, m.name, m.service_type
               FROM master_invites i JOIN masters m ON i.master_id = m.id
               WHERE i.code = ?""",
            (code,),
        ) as cur:
            return await cur.fetchone()


async def use_invite(code: str, telegram_id: int):
    """Link telegram_id to the pre-created master and delete the invite."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT master_id FROM master_invites WHERE code = ?", (code,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return
        await db.execute(
            "UPDATE masters SET telegram_id = ? WHERE id = ?", (telegram_id, row[0])
        )
        await db.execute("DELETE FROM master_invites WHERE code = ?", (code,))
        await db.commit()


async def get_all_masters():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM masters WHERE telegram_id IS NOT NULL ORDER BY is_active DESC, name"
        ) as cur:
            return await cur.fetchall()


async def get_pending_invites():
    """Masters created by admin but not yet activated."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT m.id, m.name, m.service_type, i.code
               FROM masters m JOIN master_invites i ON m.id = i.master_id""",
        ) as cur:
            return await cur.fetchall()


async def toggle_master_active(master_id: int) -> bool:
    """Toggle active/inactive. Returns new is_active value."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT is_active FROM masters WHERE id = ?", (master_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return False
        new_val = 0 if row[0] == 1 else 1
        await db.execute(
            "UPDATE masters SET is_active = ? WHERE id = ?", (new_val, master_id)
        )
        await db.commit()
        return bool(new_val)


async def get_admin_monthly_report() -> dict:
    from datetime import date
    month_start = date.today().replace(day=1).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT m.id, m.name, m.service_type, m.price,
                      COUNT(CASE WHEN b.cancelled = 0 THEN 1 END) as bookings,
                      COUNT(CASE WHEN b.cancelled = 1 THEN 1 END) as cancelled,
                      COALESCE(SUM(CASE WHEN b.cancelled = 0 THEN m.price ELSE 0 END), 0) as revenue
               FROM masters m
               LEFT JOIN slots s ON m.id = s.master_id
               LEFT JOIN bookings b ON s.id = b.slot_id AND b.created_at >= ?
               WHERE m.is_active = 1
               GROUP BY m.id
               ORDER BY bookings DESC""",
            (month_start,),
        ) as cur:
            by_master = await cur.fetchall()

        async with db.execute(
            """SELECT
                 COUNT(CASE WHEN b.cancelled = 0 THEN 1 END) as total_bookings,
                 COUNT(CASE WHEN b.cancelled = 1 THEN 1 END) as total_cancelled,
                 COALESCE(SUM(CASE WHEN b.cancelled = 0 THEN m.price ELSE 0 END), 0) as total_revenue
               FROM bookings b
               JOIN slots s ON b.slot_id = s.id
               JOIN masters m ON s.master_id = m.id
               WHERE b.created_at >= ?""",
            (month_start,),
        ) as cur:
            totals = await cur.fetchone()

    return {"by_master": [dict(r) for r in by_master], "totals": dict(totals)}


# ─── Slots ────────────────────────────────────────────────────

async def add_slot(master_id: int, date: str, time: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO slots (master_id, date, time) VALUES (?, ?, ?)",
                (master_id, date, time),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def get_available_slots(master_id: int):
    from datetime import date
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM slots WHERE master_id=? AND is_booked=0 AND date>=? ORDER BY date,time",
            (master_id, today),
        ) as cur:
            return await cur.fetchall()


async def delete_free_slot(slot_id: int, master_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT is_booked FROM slots WHERE id=? AND master_id=?", (slot_id, master_id)
        ) as cur:
            slot = await cur.fetchone()
        if slot and slot[0] == 0:
            await db.execute("DELETE FROM slots WHERE id=?", (slot_id,))
            await db.commit()
            return True
        return False


# ─── Schedule / clients ───────────────────────────────────────

async def get_master_schedule(master_id: int, offset_days: int = 0, days: int = 7):
    from datetime import date, timedelta
    start = (date.today() + timedelta(days=offset_days)).isoformat()
    end   = (date.today() + timedelta(days=offset_days + days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT s.id, s.date, s.time, s.is_booked,
                      b.id as booking_id, b.client_name, b.client_phone, b.pending
               FROM slots s
               LEFT JOIN bookings b ON s.id = b.slot_id AND b.cancelled = 0
               WHERE s.master_id=? AND s.date>=? AND s.date<? ORDER BY s.date, s.time""",
            (master_id, start, end),
        ) as cur:
            return await cur.fetchall()


async def get_master_clients(master_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT b.client_name, b.client_phone, s.date, s.time
               FROM bookings b JOIN slots s ON b.slot_id = s.id
               WHERE s.master_id=? AND b.cancelled=0 ORDER BY s.date DESC, s.time DESC""",
            (master_id,),
        ) as cur:
            return await cur.fetchall()


async def get_master_stats(master_id: int) -> dict:
    from datetime import date
    month_start = date.today().replace(day=1).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT COUNT(*) FROM bookings b JOIN slots s ON b.slot_id=s.id
               WHERE s.master_id=? AND b.created_at>=? AND b.cancelled=0""",
            (master_id, month_start),
        ) as cur:
            this_month = (await cur.fetchone())[0]
        async with db.execute(
            """SELECT COUNT(*) FROM bookings b JOIN slots s ON b.slot_id=s.id
               WHERE s.master_id=? AND b.created_at>=? AND b.cancelled=1""",
            (master_id, month_start),
        ) as cur:
            cancelled_month = (await cur.fetchone())[0]
        async with db.execute(
            """SELECT COUNT(DISTINCT b.client_phone) FROM bookings b JOIN slots s ON b.slot_id=s.id
               WHERE s.master_id=? AND b.cancelled=0""",
            (master_id,),
        ) as cur:
            unique_clients = (await cur.fetchone())[0]
        async with db.execute(
            """SELECT COUNT(*) FROM bookings b JOIN slots s ON b.slot_id=s.id
               WHERE s.master_id=? AND b.cancelled=0""",
            (master_id,),
        ) as cur:
            total_all = (await cur.fetchone())[0]
    return {
        "this_month": this_month, "cancelled_month": cancelled_month,
        "unique_clients": unique_clients, "total_all": total_all,
    }


async def get_broadcast_client_ids(master_id: int) -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT DISTINCT b.client_telegram_id FROM bookings b
               JOIN slots s ON b.slot_id=s.id
               WHERE s.master_id=? AND b.client_telegram_id IS NOT NULL""",
            (master_id,),
        ) as cur:
            return [r[0] for r in await cur.fetchall()]


# ─── Bookings ─────────────────────────────────────────────────

async def book_slot(slot_id: int, client_name: str, client_phone: str, client_tg_id: int) -> int | None:
    """Returns new booking_id on success, None if slot already booked."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT is_booked FROM slots WHERE id=?", (slot_id,)) as cur:
            slot = await cur.fetchone()
        if not slot or slot[0] == 1:
            return None
        await db.execute("UPDATE slots SET is_booked=1 WHERE id=?", (slot_id,))
        await db.execute(
            "INSERT INTO bookings (slot_id,client_name,client_phone,client_telegram_id,pending) VALUES(?,?,?,?,1)",
            (slot_id, client_name, client_phone, client_tg_id),
        )
        await db.commit()
        async with db.execute("SELECT last_insert_rowid()") as cur:
            row = await cur.fetchone()
        return row[0] if row else None


async def cancel_booking(slot_id: int, cancelled_by: str, canceller_tg_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT b.id, b.client_telegram_id, b.client_name, b.client_phone,
                      s.date, s.time, s.master_id,
                      m.telegram_id as master_tg_id, m.name as master_name, m.service_type
               FROM bookings b JOIN slots s ON b.slot_id=s.id JOIN masters m ON s.master_id=m.id
               WHERE b.slot_id=? AND b.cancelled=0""",
            (slot_id,),
        ) as cur:
            booking = await cur.fetchone()
        if not booking:
            return None
        if cancelled_by == "master" and booking["master_tg_id"] != canceller_tg_id:
            return None
        if cancelled_by == "client" and booking["client_telegram_id"] != canceller_tg_id:
            return None
        await db.execute(
            "UPDATE bookings SET cancelled=1, cancelled_by=? WHERE slot_id=? AND cancelled=0",
            (cancelled_by, slot_id),
        )
        await db.execute("UPDATE slots SET is_booked=0 WHERE id=?", (slot_id,))
        await db.commit()
        return dict(booking)


async def get_client_bookings(client_tg_id: int):
    from datetime import date
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT b.id, b.pending, s.id as slot_id, s.date, s.time,
                      m.name as master_name, m.service_type
               FROM bookings b JOIN slots s ON b.slot_id=s.id JOIN masters m ON s.master_id=m.id
               WHERE b.client_telegram_id=? AND b.cancelled=0 AND s.date>=?
               ORDER BY s.date, s.time""",
            (client_tg_id, today),
        ) as cur:
            return await cur.fetchall()


async def get_slot_info(slot_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT s.date, s.time, m.telegram_id as master_telegram_id,
                      m.name as master_name, m.service_type
               FROM slots s JOIN masters m ON s.master_id=m.id WHERE s.id=?""",
            (slot_id,),
        ) as cur:
            return await cur.fetchone()


# ─── Cleanup ──────────────────────────────────────────────────

async def cleanup_old_data() -> dict:
    """Delete past free slots and bookings older than 90 days. Returns counts."""
    from datetime import date, timedelta
    today = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=90)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        # Delete past free (unbooked) slots
        cur = await db.execute(
            "DELETE FROM slots WHERE date < ? AND is_booked = 0", (today,)
        )
        free_deleted = cur.rowcount

        # Delete bookings older than 90 days (cancelled or attended)
        cur = await db.execute(
            """DELETE FROM bookings WHERE id IN (
               SELECT b.id FROM bookings b JOIN slots s ON b.slot_id = s.id
               WHERE s.date < ? AND (b.cancelled = 1 OR b.attended IN (1,2))
            )""",
            (cutoff,),
        )
        bookings_deleted = cur.rowcount

        # Delete orphan past booked slots with no active booking
        cur = await db.execute(
            """DELETE FROM slots WHERE date < ? AND is_booked = 1
               AND id NOT IN (SELECT slot_id FROM bookings WHERE cancelled = 0)""",
            (today,),
        )
        orphan_deleted = cur.rowcount

        await db.commit()
    return {"free_slots": free_deleted, "old_bookings": bookings_deleted, "orphan_slots": orphan_deleted}


# ─── Schedule templates ───────────────────────────────────────

async def get_schedule_template(master_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM schedule_templates WHERE master_id = ?", (master_id,)
        ) as cur:
            return await cur.fetchone()


async def set_schedule_template(master_id: int, start_hour: int, start_min: int,
                                 end_hour: int, end_min: int, interval: int = 30):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO schedule_templates (master_id, start_hour, start_min, end_hour, end_min, interval)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(master_id) DO UPDATE SET
                   start_hour=excluded.start_hour, start_min=excluded.start_min,
                   end_hour=excluded.end_hour, end_min=excluded.end_min,
                   interval=excluded.interval""",
            (master_id, start_hour, start_min, end_hour, end_min, interval),
        )
        await db.commit()


async def apply_schedule_template(master_id: int, days: int = 90) -> int:
    """Generate slots for the next N days from template. Returns count of new slots added."""
    from datetime import date, timedelta
    tmpl = await get_schedule_template(master_id)
    if not tmpl:
        return 0
    today = date.today()
    added = 0
    async with aiosqlite.connect(DB_PATH) as db:
        for i in range(days):
            d = (today + timedelta(days=i)).isoformat()
            h = tmpl["start_hour"]
            m = tmpl["start_min"]
            while (h * 60 + m) < (tmpl["end_hour"] * 60 + tmpl["end_min"]):
                time_str = f"{h:02d}:{m:02d}"
                try:
                    await db.execute(
                        "INSERT INTO slots (master_id, date, time) VALUES (?, ?, ?)",
                        (master_id, d, time_str),
                    )
                    added += 1
                except Exception:
                    pass  # уже существует (UNIQUE constraint)
                m += tmpl["interval"]
                if m >= 60:
                    h += m // 60
                    m = m % 60
        await db.commit()
    return added


async def delete_day_slots(master_id: int, date_str: str) -> tuple[int, int]:
    """Delete all FREE slots for a day. Returns (deleted, kept_booked)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM slots WHERE master_id=? AND date=? AND is_booked=0",
            (master_id, date_str),
        ) as cur:
            free_count = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM slots WHERE master_id=? AND date=? AND is_booked=1",
            (master_id, date_str),
        ) as cur:
            booked_count = (await cur.fetchone())[0]
        await db.execute(
            "DELETE FROM slots WHERE master_id=? AND date=? AND is_booked=0",
            (master_id, date_str),
        )
        await db.commit()
    return free_count, booked_count


# ─── History / attendance ─────────────────────────────────────

async def get_master_history(master_id: int, days: int = 30):
    """Past bookings for the last N days."""
    from datetime import date, timedelta
    today = date.today().isoformat()
    since = (date.today() - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT b.id as booking_id, b.client_name, b.client_phone,
                      b.client_telegram_id, b.cancelled, b.attended,
                      s.id as slot_id, s.date, s.time
               FROM bookings b JOIN slots s ON b.slot_id = s.id
               WHERE s.master_id = ? AND s.date >= ? AND s.date < ?
               ORDER BY s.date DESC, s.time DESC""",
            (master_id, since, today),
        ) as cur:
            return await cur.fetchall()


async def mark_attended(booking_id: int, status: int):
    """status: 1=пришёл, 2=не пришёл."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE bookings SET attended = ? WHERE id = ?", (status, booking_id)
        )
        await db.commit()


async def master_delete_booking(slot_id: int, master_id: int) -> dict | None:
    """Master manually removes a booking (client cancelled by phone)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT b.id, b.client_name, b.client_telegram_id,
                      s.date, s.time
               FROM bookings b JOIN slots s ON b.slot_id = s.id
               JOIN masters m ON s.master_id = m.id
               WHERE b.slot_id = ? AND s.master_id = ? AND b.cancelled = 0""",
            (slot_id, master_id),
        ) as cur:
            booking = await cur.fetchone()
        if not booking:
            return None
        await db.execute(
            "UPDATE bookings SET cancelled = 1, cancelled_by = 'master_manual' WHERE id = ?",
            (booking["id"],),
        )
        await db.execute("UPDATE slots SET is_booked = 0 WHERE id = ?", (slot_id,))
        await db.commit()
        return dict(booking)


async def auto_cancel_client(client_telegram_id: int):
    """Cancel all future bookings for a client who blocked the bot."""
    from datetime import date
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT b.id, b.slot_id FROM bookings b
               JOIN slots s ON b.slot_id = s.id
               WHERE b.client_telegram_id = ? AND b.cancelled = 0 AND s.date >= ?""",
            (client_telegram_id, today),
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            await db.execute(
                "UPDATE bookings SET cancelled = 1, cancelled_by = 'auto_blocked' WHERE id = ?",
                (row[0],),
            )
            await db.execute("UPDATE slots SET is_booked = 0 WHERE id = ?", (row[1],))
        await db.commit()
        return len(rows)


# ─── Excel export ─────────────────────────────────────────────

async def get_all_master_bookings(master_id: int) -> list[dict]:
    """All bookings (past + future) for Excel export."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT b.client_name, b.client_phone, b.created_at,
                      b.cancelled, b.cancelled_by, b.attended,
                      s.date, s.time
               FROM bookings b JOIN slots s ON b.slot_id = s.id
               WHERE s.master_id = ?
               ORDER BY s.date DESC, s.time DESC""",
            (master_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ─── Wrong attempts ───────────────────────────────────────────

_LOCKOUT_MINUTES = 15
_MAX_ATTEMPTS    = 5


async def get_lockout_seconds(telegram_id: int) -> int:
    """Returns seconds remaining in lockout, or 0 if not locked."""
    from datetime import datetime
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT locked_until FROM wrong_attempts WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row or not row[0]:
        return 0
    locked_until = datetime.fromisoformat(row[0])
    remaining = (locked_until - datetime.now()).total_seconds()
    return max(0, int(remaining))


async def increment_wrong_attempts(telegram_id: int) -> tuple[int, int]:
    """Returns (attempts, lockout_seconds). Sets lockout after MAX_ATTEMPTS."""
    from datetime import datetime, timedelta
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO wrong_attempts (telegram_id, attempts) VALUES (?, 1)
               ON CONFLICT(telegram_id) DO UPDATE SET attempts = attempts + 1""",
            (telegram_id,),
        )
        await db.commit()
        async with db.execute(
            "SELECT attempts FROM wrong_attempts WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
        attempts = row[0] if row else 1
        lockout_secs = 0
        if attempts >= _MAX_ATTEMPTS:
            locked_until = datetime.now() + timedelta(minutes=_LOCKOUT_MINUTES)
            await db.execute(
                "UPDATE wrong_attempts SET locked_until = ? WHERE telegram_id = ?",
                (locked_until.isoformat(), telegram_id),
            )
            await db.commit()
            lockout_secs = _LOCKOUT_MINUTES * 60
    return attempts, lockout_secs


async def reset_wrong_attempts(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM wrong_attempts WHERE telegram_id = ?", (telegram_id,))
        await db.commit()


# ─── Reminders ────────────────────────────────────────────────

async def get_pending_reminders():
    from datetime import date
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT b.id, b.client_telegram_id, b.client_name,
                      b.reminder_24h_sent, b.reminder_8h_sent, b.reminder_1h_sent,
                      s.date, s.time, m.name as master_name, m.service_type
               FROM bookings b JOIN slots s ON b.slot_id=s.id JOIN masters m ON s.master_id=m.id
               WHERE b.cancelled=0 AND b.pending=0 AND b.client_telegram_id IS NOT NULL AND s.date>=?""",
            (today,),
        ) as cur:
            return await cur.fetchall()


async def mark_reminder_sent(booking_id: int, reminder_type: str):
    col = {"24h": "reminder_24h_sent", "8h": "reminder_8h_sent", "1h": "reminder_1h_sent"}.get(reminder_type, "reminder_1h_sent")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE bookings SET {col}=1 WHERE id=?", (booking_id,))
        await db.commit()


# ─── Booking confirmation ─────────────────────────────────────

async def confirm_booking(booking_id: int, master_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT b.id, b.client_telegram_id, b.client_name, s.date, s.time
               FROM bookings b JOIN slots s ON b.slot_id = s.id
               WHERE b.id = ? AND s.master_id = ? AND b.pending = 1 AND b.cancelled = 0""",
            (booking_id, master_id),
        ) as cur:
            booking = await cur.fetchone()
        if not booking:
            return None
        await db.execute("UPDATE bookings SET pending = 0 WHERE id = ?", (booking_id,))
        await db.commit()
        return dict(booking)


async def reject_booking(booking_id: int, master_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT b.id, b.slot_id, b.client_telegram_id, b.client_name, s.date, s.time
               FROM bookings b JOIN slots s ON b.slot_id = s.id
               WHERE b.id = ? AND s.master_id = ? AND b.pending = 1 AND b.cancelled = 0""",
            (booking_id, master_id),
        ) as cur:
            booking = await cur.fetchone()
        if not booking:
            return None
        await db.execute(
            "UPDATE bookings SET cancelled = 1, cancelled_by = 'master_reject', pending = 0 WHERE id = ?",
            (booking_id,),
        )
        await db.execute("UPDATE slots SET is_booked = 0 WHERE id = ?", (booking["slot_id"],))
        await db.commit()
        return dict(booking)


# ─── Reviews ──────────────────────────────────────────────────

async def get_review_targets():
    """Past confirmed bookings that haven't been sent a review request yet."""
    from datetime import date
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT b.id, b.client_telegram_id, s.date, s.time,
                      m.name as master_name
               FROM bookings b JOIN slots s ON b.slot_id = s.id
               JOIN masters m ON s.master_id = m.id
               WHERE b.cancelled = 0 AND b.pending = 0
                 AND b.review_sent = 0 AND b.client_telegram_id IS NOT NULL
                 AND s.date < ?""",
            (today,),
        ) as cur:
            return await cur.fetchall()


async def save_review(booking_id: int, score: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE bookings SET review_sent = 1, review_score = ? WHERE id = ?",
            (score, booking_id),
        )
        await db.commit()


async def mark_review_asked(booking_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE bookings SET review_sent = 1 WHERE id = ?", (booking_id,))
        await db.commit()
