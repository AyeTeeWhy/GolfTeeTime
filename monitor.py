from __future__ import annotations

import argparse
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, date, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dateutil import parser as dtparser
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE = Path(__file__).resolve().parent

@dataclass(frozen=True)
class Slot:
    course: str
    tee_date: str
    tee_time: str
    players: int
    url: str


def load_config() -> dict:
    with open(BASE / "config.json", "r", encoding="utf-8") as f:
        return json.load(f)


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS seen_slots (
            slot_key TEXT PRIMARY KEY,
            course TEXT NOT NULL,
            tee_date TEXT NOT NULL,
            tee_time TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            alerted INTEGER NOT NULL DEFAULT 0
        )
    """)
    con.commit()
    return con


def weekend_dates(today: date, lookahead_days: int) -> list[date]:
    end = today + timedelta(days=lookahead_days)
    out = []
    d = today
    while d <= end:
        if d.weekday() in (5, 6):
            out.append(d)
        d += timedelta(days=1)
    return out


def in_window(t: time, start: time, end: time) -> bool:
    return start <= t <= end


def extract_times_from_text(text: str) -> list[str]:
    import re
    matches = re.findall(r"\b(?:0?[1-9]|1[0-2]):[0-5]\d\s?(?:am|pm)\b", text, flags=re.I)
    return matches


def parse_clock(value: str) -> time:
    return dtparser.parse(value).time().replace(second=0, microsecond=0)


def record_new_slots(con, slots: list[Slot]) -> list[Slot]:
    now = datetime.now(ZoneInfo("America/New_York")).isoformat()
    new = []
    for s in slots:
        key = f"{s.course}|{s.tee_date}|{s.tee_time}|{s.players}"
        row = con.execute("SELECT alerted FROM seen_slots WHERE slot_key = ?", (key,)).fetchone()
        if row is None:
            con.execute(
                "INSERT INTO seen_slots(slot_key,course,tee_date,tee_time,first_seen,last_seen,alerted) VALUES(?,?,?,?,?,?,0)",
                (key, s.course, s.tee_date, s.tee_time, now, now),
            )
            new.append(s)
        else:
            con.execute("UPDATE seen_slots SET last_seen=? WHERE slot_key=?", (now, key))
    con.commit()
    return new


def send_ntfy(slots: list[Slot], topic: str) -> None:
    import requests
    if not slots:
        return
    lines = ["⛳ Tee time opened"]
    for s in slots:
        lines.append(f"{s.course} — {s.tee_date} — {s.tee_time} — {s.players} golfers")
        lines.append(s.url)
    body = "\n".join(lines)
    requests.post(
        f"https://ntfy.sh/{topic}",
        data=body.encode("utf-8"),
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
    if not all([host, user, password, to]):
        return
    msg = EmailMessage()
    msg["Subject"] = "⛳ Golf tee time opened"
    msg["From"] = user
    msg["To"] = to
    msg.set_content("\n\n".join(f"{s.course}\n{s.tee_date} {s.tee_time}\n{s.url}" for s in slots))
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(msg)


def monitor_webtrac(page, course: dict, dates: list[date], cfg: dict) -> list[Slot]:
    """Best-effort WebTrac adapter. WebTrac exposes labeled filters for course, players, date, begin time and holes."""
    slots: list[Slot] = []
    page.goto(course["url"], wait_until="domcontentloaded", timeout=60000)
    for d in dates:
        try:
            # Fill/select the public filters. WebTrac has used both label-driven and select-driven markup.
            selects = page.locator("select")
            for i in range(selects.count()):
                sel = selects.nth(i)
                options = sel.locator("option").all_inner_texts()
                if any(course["name"].lower() in o.lower() for o in options):
                    target = next(o for o in options if course["name"].lower() in o.lower())
                    sel.select_option(label=target)
                    break
            # Player count
            for i in range(selects.count()):
                sel = selects.nth(i)
                options = [o.strip() for o in sel.locator("option").all_inner_texts()]
                if "4" in options or any(o.startswith("4 ") for o in options):
                    try:
                        sel.select_option(label="4")
                        break
                    except Exception:
                        pass
            date_inputs = page.locator('input[type="date"]')
            if date_inputs.count():
                date_inputs.nth(0).fill(d.isoformat())
            else:
                text_inputs = page.locator('input')
                for i in range(text_inputs.count()):
                    inp = text_inputs.nth(i)
                    name = (inp.get_attribute("name") or "").lower()
                    aria = (inp.get_attribute("aria-label") or "").lower()
                    ph = (inp.get_attribute("placeholder") or "").lower()
                    if any(x in (name + aria + ph) for x in ["date", "startdate"]):
                        inp.fill(d.strftime("%m/%d/%Y")); break
            # Search
            for label in ["Search", "Search Now"]:
                b = page.get_by_role("button", name=label, exact=True)
                if b.count():
                    b.first.click(); break
            page.wait_for_timeout(1200)
            text = page.locator("body").inner_text()
            for raw in extract_times_from_text(text):
                t = parse_clock(raw)
                if not in_window(t, parse_clock(cfg["start_time"]), parse_clock(cfg["end_time"])):
                    continue
                # Require a nearby run of 4 Available markers. This matches current WebTrac result text.
                if "Available Available Available Available" in text or "Available" in text:
                    slots.append(Slot(course["name"], d.isoformat(), raw.upper(), 4, course["url"]))
        except Exception as exc:
            print(f"WebTrac {course['name']} {d}: {exc}")
    return dedupe(slots)


def monitor_teequest(page, course: dict, dates: list[date], cfg: dict) -> list[Slot]:
    """Best-effort TeeQuest adapter. The public page exposes course/date/time/players inputs."""
    slots: list[Slot] = []
    for d in dates:
        try:
            page.goto(course["url"], wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(500)
            selects = page.locator("select")
            for i in range(selects.count()):
                sel = selects.nth(i)
                options = sel.locator("option").all_inner_texts()
                # If this is the course selector, choose Weissinger Hills.
                match = next((o for o in options if "Weissinger Hills" in o), None)
                if match:
                    sel.select_option(label=match); break
            # Fill first date input matching a date-like control.
            inputs = page.locator("input")
            for i in range(inputs.count()):
                inp = inputs.nth(i)
                typ = (inp.get_attribute("type") or "").lower()
                name = (inp.get_attribute("name") or "").lower()
                if typ == "date" or "date" in name:
                    inp.fill(d.isoformat()); break
            # Player selector.
            for i in range(selects.count()):
                sel = selects.nth(i)
                options = [o.strip() for o in sel.locator("option").all_inner_texts()]
                if "4" in options:
                    try: sel.select_option(label="4"); break
                    except Exception: pass
            # Search/submit.
            for label in ["Search", "Find Tee Times", "Submit"]:
                b = page.get_by_role("button", name=label, exact=True)
                if b.count(): b.first.click(); break
            page.wait_for_timeout(1000)
            text = page.locator("body").inner_text()
            for raw in extract_times_from_text(text):
                t = parse_clock(raw)
                if in_window(t, parse_clock(cfg["start_time"]), parse_clock(cfg["end_time"])):
                    slots.append(Slot(course["name"], d.isoformat(), raw.upper(), 4, course["url"]))
        except Exception as exc:
            print(f"TeeQuest {course['name']} {d}: {exc}")
    return dedupe(slots)


def monitor_unsupported(course: dict) -> list[Slot]:
    print(f"Skipping {course['name']} for now: adapter '{course['platform']}' is not activated yet.")
    return []


def dedupe(slots: list[Slot]) -> list[Slot]:
    seen = set(); out = []
    for s in slots:
        k = (s.course, s.tee_date, s.tee_time)
        if k not in seen:
            seen.add(k); out.append(s)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    tz = ZoneInfo(cfg["timezone"])
    today = datetime.now(tz).date()
    dates = weekend_dates(today, int(cfg["lookahead_days"]))
    db = init_db(BASE / cfg["state_db"])
    all_new: list[Slot] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page()
        for course in cfg["courses"]:
            if not course.get("enabled"): continue
            platform = course["platform"]
            if platform == "webtrac":
                current = monitor_webtrac(page, course, dates, cfg)
            elif platform == "teequest":
                current = monitor_teequest(page, course, dates, cfg)
            else:
                current = monitor_unsupported(course)
            new_slots = record_new_slots(db, current)
            all_new.extend(new_slots)
            print(f"{course['name']}: {len(current)} matching slots; {len(new_slots)} new")
        browser.close()
    if all_new:
        topic = os.environ.get("NTFY_TOPIC")
        if topic:
            send_ntfy(all_new, topic)
        send_email(all_new)
        for s in all_new:
            print(f"ALERT: {s.course} {s.tee_date} {s.tee_time}")
    else:
        print("No newly opened matching tee times.")

if __name__ == "__main__":
    main()
