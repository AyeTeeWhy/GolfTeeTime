from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import Page

TIME_RE = re.compile(r"\b(0?[1-9]|1[0-2]):([0-5]\d)\s*(AM|PM)\b", re.I)


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


def _parse_mm(value: str) -> time:
    hour, minute = map(int, value.split(":", 1))
    return time(hour, minute)


def _in_window(t: time, start: time, end: time) -> bool:
    return start <= t <= end


def _clean(text: str, max_len: int = 500) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def _ancestor_text(locator, levels: int = 5) -> str:
    current = locator
    best = ""
    for _ in range(levels):
        try:
            txt = _clean(current.inner_text())
            if len(txt) > len(best):
                best = txt
            if TIME_RE.search(txt) and ("book" in txt.lower() or "$" in txt or "holes" in txt.lower()):
                return txt
            current = current.locator("xpath=..").first
        except Exception:
            break
    return best


def _write_diagnostic(page: Page, course_name: str, tee_date: date, diagnostic_dir: Path, requested_players: int) -> None:
    diagnostic_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9]+", "_", course_name).strip("_").lower()
    stem = diagnostic_dir / f"{safe}_{tee_date.isoformat()}"

    try:
        body = page.locator("body").inner_text()
    except Exception as exc:
        body = f"<body text unavailable: {exc}>"

    time_rows = []
    for idx, match in enumerate(TIME_RE.finditer(body)):
        token = match.group(0)
        snippet_start = max(0, match.start() - 220)
        snippet_end = min(len(body), match.end() + 220)
        time_rows.append(
            {
                "index": idx,
                "time": token,
                "context": _clean(body[snippet_start:snippet_end], 500),
            }
        )

    link_rows = []
    try:
        links = page.locator("a")
        for i in range(min(links.count(), 400)):
            link = links.nth(i)
            if not link.is_visible():
                continue
            text = _clean(link.inner_text(), 160)
            href = link.get_attribute("href") or ""
            if text or href:
                link_rows.append({"text": text, "href": href})
    except Exception as exc:
        link_rows.append({"error": str(exc)})

    buttons = []
    try:
        btns = page.locator("button")
        for i in range(min(btns.count(), 200)):
            btn = btns.nth(i)
            if not btn.is_visible():
                continue
            buttons.append({"text": _clean(btn.inner_text(), 160), "disabled": btn.is_disabled()})
    except Exception as exc:
        buttons.append({"error": str(exc)})

    report = {
        "course": course_name,
        "tee_date": tee_date.isoformat(),
        "requested_players": requested_players,
        "page_url": page.url,
        "title": page.title(),
        "time_token_count": len(time_rows),
        "time_tokens": time_rows,
        "visible_links": link_rows,
        "visible_buttons": buttons,
    }
    stem.with_suffix(".json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    stem.with_suffix(".txt").write_text(body, encoding="utf-8")
    try:
        page.screenshot(path=str(stem.with_suffix(".png")), full_page=True)
    except Exception:
        pass
    try:
        stem.with_suffix(".html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def scan(
    page: Page,
    course: dict,
    tee_date: date,
    start_time: str,
    end_time: str,
    players: int,
    diagnostic_dir: Path | None = None,
) -> list[TeeQuestSlot]:
    """Scan one TeeQuest date and optionally write a full diagnostic snapshot."""
    course_id = str(course["course_id"])
    variant = str(course.get("variant", "1"))
    base = course.get("search_base", "https://bookateetime.teequest.com/search")
    url = f"{base.rstrip('/')}/{course_id}-{variant}/{tee_date.isoformat()}"
    url += "?" + urlencode({"selectedPlayers": players, "selectedHoles": 18})

    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1200)

    if diagnostic_dir is not None:
        _write_diagnostic(page, course["name"], tee_date, diagnostic_dir, players)

    body = page.locator("body").inner_text()
    low = body.lower()
    if "course is currently offline" in low:
        raise RuntimeError("TeeQuest reports this course is currently offline")

    start = _parse_mm(start_time)
    end = _parse_mm(end_time)
    out: list[TeeQuestSlot] = []

    # Strategy 1: visible Book links, which is the safest confirmation that a
    # tee time is actually purchasable for the requested player count.
    try:
        links = page.locator("a").filter(has_text=re.compile(r"\bBook\b", re.I))
        link_count = links.count()
    except Exception:
        link_count = 0
        links = None

    for i in range(link_count):
        link = links.nth(i)
        if not link.is_visible():
            continue
        card_text = _ancestor_text(link)
        lower = card_text.lower()
        if any(x in lower for x in ("sold out", "call course to play", "unavailable")):
            continue
        m = TIME_RE.search(card_text)
        if not m:
            continue
        clock = _parse_time(m.group(0))
        if not _in_window(clock, start, end):
            continue
        href = link.get_attribute("href") or url
        absolute = href if href.startswith("http") else url
        out.append(TeeQuestSlot(tee_date.isoformat(), m.group(0).upper(), players, absolute))

    # Diagnostic fallback: if the page contains time tokens but no Book links,
    # don't silently claim that the parser is correct. The diagnostic artifact
    # lets us see the exact rendered structure and fix the adapter based on it.
    if not out and diagnostic_dir is not None:
        time_count = len(TIME_RE.findall(body))
        print(
            f"TEEQUEST DIAGNOSTIC {tee_date.isoformat()}: "
            f"page={page.url} time_tokens={time_count} visible_book_links={link_count}"
        )
        if time_count:
            print("TEEQUEST DIAGNOSTIC: tee-time tokens were found but none matched the current availability parser.")
        else:
            print("TEEQUEST DIAGNOSTIC: no clock-time tokens found on the rendered page.")

    deduped = {}
    for slot in out:
        deduped[(slot.tee_date, slot.tee_time, slot.players)] = slot
    return list(deduped.values())
