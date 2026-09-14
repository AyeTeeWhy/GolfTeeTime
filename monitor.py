from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, date, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dateutil import parser as dtparser
from playwright.sync_api import sync_playwright

from adapters.teequest import scan as teequest_scan

BASE = Path(__file__).resolve().parent
TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Slot:
    course: str
    tee_date: str
    tee_time: str
    players: int
    url: str


@dataclass(frozen=True)
class ScanResult:
    course: str
    slots: list[Slot]
    ok: bool
    error: str | None = None


def load_config() -> dict:
    with open(BASE / "config.json", "r", encoding="utf-8") as f:
        return json.load(f)


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS slots (
            slot_key TEXT PRIMARY KEY,
            course TEXT NOT NULL,
            tee_date TEXT NOT NULL,
            tee_time TEXT NOT NULL,
            players INTEGER NOT NULL,
            url TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 0,
            last_alerted TEXT
        )
    """)
    con.commit()
    return con


def weekend_dates(today: date, lookahead_days: int) -> list[date]:
    end = today + timedelta(days=lookahead_days)
    out: list[date] = []
    d = today
    while d <= end:
        if d.weekday() in (5, 6):
            out.append(d)
        d += timedelta(days=1)
    return out


def parse_clock(value: str) -> time:
    return dtparser.parse(value).time().replace(second=0, microsecond=0)


def dedupe(slots: list[Slot]) -> list[Slot]:
    seen: set[tuple[str, str, str, int]] = set()
    out: list[Slot] = []
    for s in slots:
        k = (s.course, s.tee_date, s.tee_time, s.players)
        if k not in seen:
            seen.add(k)
            out.append(s)
    return out


def record_scan(con: sqlite3.Connection, result: ScanResult) -> list[Slot]:
    """Mark the current inventory and return only newly available slots.

    A slot becomes alertable again after it disappears and later reappears.
    Failed scans never deactivate existing slots.
    """
    if not result.ok:
        return []

    now = datetime.now(TZ).isoformat()
    current = {f"{s.course}|{s.tee_date}|{s.tee_time}|{s.players}": s for s in result.slots}
    new_slots: list[Slot] = []

    for key, slot in current.items():
        row = con.execute("SELECT active FROM slots WHERE slot_key=?", (key,)).fetchone()
        if row is None:
            con.execute(
                "INSERT INTO slots(slot_key,course,tee_date,tee_time,players,url,first_seen,last_seen,active) VALUES(?,?,?,?,?,?,?,?,1)",
                (key, slot.course, slot.tee_date, slot.tee_time, slot.players, slot.url, now, now),
            )
            new_slots.append(slot)
        else:
            was_active = bool(row[0])
            con.execute(
                "UPDATE slots SET url=?, last_seen=?, active=1 WHERE slot_key=?",
                (slot.url, now, key),
            )
            if not was_active:
                con.execute("UPDATE slots SET last_alerted=? WHERE slot_key=?", (now, key))
                new_slots.append(slot)

    # Only deactivate prior slots in dates actually scanned for this course.
    scanned_dates = sorted({s.tee_date for s in result.slots})
    if not scanned_dates:
        # A successful zero-result scan must still be allowed to clear prior
        # weekend inventory. The caller passes all target dates via result.days.
        pass

    con.commit()
    return new_slots


def record_scan_with_dates(con: sqlite3.Connection, result: ScanResult, scanned_dates: list[date]) -> list[Slot]:
    if not result.ok:
        return []
    new_slots = record_scan(con, result)
    current_keys = {f"{s.course}|{s.tee_date}|{s.tee_time}|{s.players}" for s in result.slots}
    date_strings = [d.isoformat() for d in scanned_dates]
    placeholders = ",".join("?" for _ in date_strings)
    if date_strings:
        params = [result.course, *date_strings]
        rows = con.execute(
            f"SELECT slot_key FROM slots WHERE course=? AND tee_date IN ({placeholders}) AND active=1",
            params,
        ).fetchall()
        stale = [r[0] for r in rows if r[0] not in current_keys]
        for key in stale:
            con.execute("UPDATE slots SET active=0 WHERE slot_key=?", (key,))
    con.commit()
    return new_slots


def send_ntfy(slots: list[Slot], topic: str) -> None:
    import requests
    if not slots:
        return
    lines = ["⛳ Tee time opened"]
    for s in sorted(slots, key=lambda x: (x.tee_date, x.tee_time, x.course)):
        lines.append(f"{s.course} — {s.tee_date} — {s.tee_time} — {s.players} golfers")
        lines.append(s.url)
    requests.post(
        f"https://ntfy.sh/{topic}",
        data="\n".join(lines).encode("utf-8"),
        headers={"Title": "Golf tee time alert", "Priority": "high", "Tags": "golf,calendar"},
        timeout=20,
    ).raise_for_status()


def send_email(slots: list[Slot]) -> None:
    import smtplib
    from email.message import EmailMessage
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    to = os.environ.get("ALERT_EMAIL_TO")
    if not all([host, user, password, to]) or not slots:
        return
    msg = EmailMessage()
    msg["Subject"] = "⛳ Golf tee time opened"
    msg["From"] = user
    msg["To"] = to
    msg.set_content("\n\n".join(f"{s.course}\n{s.tee_date} {s.tee_time}\n{s.players} golfers\n{s.url}" for s in slots))
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(msg)


def monitor_teequest(page, course: dict, dates: list[date], cfg: dict) -> ScanResult:
    slots: list[Slot] = []
    try:
        for d in dates:
            found = teequest_scan(
                page,
                course,
                d,
                cfg["start_time"],
                cfg["end_time"],
                int(cfg["players"]),
                diagnostic_dir=(BASE / "debug" / "teequest") if cfg.get("diagnostic_mode", False) else None,
            )
            for s in found:
                slots.append(Slot(course["name"], s.tee_date, s.tee_time, s.players, s.url))
        return ScanResult(course["name"], dedupe(slots), True)
    except Exception as exc:
        return ScanResult(course["name"], [], False, str(exc))


def monitor_unsupported(course: dict) -> ScanResult:
    return ScanResult(course["name"], [], False, f"Adapter '{course['platform']}' is not activated yet.")


def save_debug(page, course: str) -> None:
    debug_dir = BASE / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() else "_" for c in course.lower()).strip("_")
    try:
        page.screenshot(path=str(debug_dir / f"{safe}.png"), full_page=True)
    except Exception:
        pass
    try:
        (debug_dir / f"{safe}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    today = datetime.now(tz).date()
    if cfg.get("diagnostic_dates"):
        dates = [dtparser.parse(x).date() for x in cfg["diagnostic_dates"]]
    else:
        dates = weekend_dates(today, int(cfg.get("lookahead_days", 21)))
    print(f"Scanning weekend dates: {', '.join(d.isoformat() for d in dates)}")
    print(f"Target: {cfg['players']} golfers, {cfg['start_time']}–{cfg['end_time']} {cfg.get('timezone','ET')}")

    db = init_db(BASE / cfg["state_db"])
    all_new: list[Slot] = []
    failures: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page(viewport={"width": 1440, "height": 1200})
        for course in cfg["courses"]:
            if not course.get("enabled"):
                continue
            platform = course["platform"]
            if platform == "teequest":
                result = monitor_teequest(page, course, dates, cfg)
            else:
                result = monitor_unsupported(course)

            if not result.ok:
                failures.append(f"{course['name']}: {result.error}")
                print(f"ERROR {course['name']}: {result.error}")
                if platform == "teequest":
                    save_debug(page, course["name"])
                continue

            new_slots = record_scan_with_dates(db, result, dates)
            all_new.extend(new_slots)
            print(f"{course['name']}: {len(result.slots)} matching slots; {len(new_slots)} newly opened")

        browser.close()

    if all_new and cfg.get("diagnostic_mode", False):
        print("DIAGNOSTIC MODE: alerts suppressed; matching slots were detected but no notification will be sent.")
    elif all_new:
        topic = os.environ.get("NTFY_TOPIC")
        if topic:
            send_ntfy(all_new, topic)
        send_email(all_new)
        for s in all_new:
            print(f"ALERT: {s.course} {s.tee_date} {s.tee_time} ({s.players} golfers)")
    else:
        print("No newly opened matching tee times.")

    if failures:
        print("\nMONITOR FAILURES")
        for failure in failures:
            print(f" - {failure}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
