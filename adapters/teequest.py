from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time
from urllib.parse import urlencode

from playwright.sync_api import Page

TIME_RE = re.compile(r"\b(0?[1-9]|1[0-2]):([0-5]\d)\s*(AM|PM)\b", re.I)
PLAYER_RE = re.compile(r"\b(\d+)(?:\s*-\s*(\d+))?\s+18\s+holes\b", re.I)


@dataclass(frozen=True)
class TeeQuestSlot:
    tee_date: str
    tee_time: str
    players: int
    url: str


def _parse_time(value: str) -> time:
    m = TIME_RE.search(value)
    if not m:
        raise ValueError(f"Not a clock time: {value!r}")
    hour = int(m.group(1))
    minute = int(m.group(2))
    if m.group(3).upper() == "PM" and hour != 12:
        hour += 12
    if m.group(3).upper() == "AM" and hour == 12:
        hour = 0
    return time(hour, minute)


def _in_window(t: time, start: time, end: time) -> bool:
    return start <= t <= end


def _parse_mm(value: str) -> time:
    hour, minute = map(int, value.split(":", 1))
    return time(hour, minute)


def _card_text(link) -> str:
    """Walk up from a Book link until the surrounding tee-time card is found."""
    current = link
    best = ""
    for _ in range(6):
        try:
            text = " ".join(current.inner_text().split())
        except Exception:
            text = ""
        if len(text) > len(best):
            best = text
        if TIME_RE.search(text) and ("Book" in text or "$" in text):
            return text
        try:
            current = current.locator("xpath=..").first
        except Exception:
            break
    return best


def scan(page: Page, course: dict, tee_date: date, start_time: str, end_time: str, players: int) -> list[TeeQuestSlot]:
    """
    TeeQuest exposes a public, date-specific search URL. We avoid the hidden
    select controls on the landing page and go directly to the results page.
    selectedPlayers=4 is important: it makes TeeQuest return only inventory
    that can satisfy a foursome rather than merely showing single-player slots.
    """
    course_id = str(course["course_id"])
    variant = str(course.get("variant", "1"))
    base = course.get("search_base", "https://bookateetime.teequest.com/search")
    url = f"{base.rstrip('/')}/{course_id}-{variant}/{tee_date.isoformat()}"
    url += "?" + urlencode({"selectedPlayers": players, "selectedHoles": 18})

    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(700)

    body = page.locator("body").inner_text()
    low = body.lower()
    if "course is currently offline" in low:
        raise RuntimeError("TeeQuest reports this course is currently offline")

    start = _parse_mm(start_time)
    end = _parse_mm(end_time)
    out: list[TeeQuestSlot] = []

    # A real booking result has a Book link. The card around that link contains
    # the tee time and usually the number of available players.
    links = page.locator("a").filter(has_text=re.compile(r"\bBook\b", re.I))
    for i in range(links.count()):
        link = links.nth(i)
        if not link.is_visible():
            continue
        text = _card_text(link)
        lower = text.lower()
        if any(x in lower for x in ("sold out", "call course to play", "unavailable")):
            continue
        m = TIME_RE.search(text)
        if not m:
            continue
        clock = _parse_time(m.group(0))
        if not _in_window(clock, start, end):
            continue

        # selectedPlayers=4 already filters the returned inventory. If the card
        # explicitly reports a player range, enforce it as a second safety check.
        player_match = PLAYER_RE.search(text)
        if player_match:
            low_players = int(player_match.group(1))
            high_players = int(player_match.group(2) or player_match.group(1))
            if high_players < players:
                continue
            if low_players == 0:
                continue

        href = link.get_attribute("href") or url
        absolute = href if href.startswith("http") else url
        out.append(TeeQuestSlot(tee_date.isoformat(), m.group(0).upper(), players, absolute))

    # De-duplicate in case the same booking card contains multiple Book links.
    deduped = {}
    for slot in out:
        deduped[(slot.tee_date, slot.tee_time, slot.players)] = slot
    return list(deduped.values())
