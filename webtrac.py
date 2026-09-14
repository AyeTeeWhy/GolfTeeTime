from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

TIME_RE = re.compile(r"\b(0?[1-9]|1[0-2]):([0-5]\d)\s*(AM|PM)\b", re.I)
AVAILABLE_RE = re.compile(r"\bAvailable\b", re.I)

@dataclass(frozen=True)
class WebTracSlot:
    tee_date: str
    tee_time: str
    players: int
    url: str


def _parse_time(text: str) -> time | None:
    m = TIME_RE.search(text or "")
    if not m:
        return None
    h, minute = int(m.group(1)), int(m.group(2))
    ap = m.group(3).upper()
    if ap == "PM" and h != 12:
        h += 12
    if ap == "AM" and h == 12:
        h = 0
    return time(h, minute)


def _in_window(t: time, start: time, end: time) -> bool:
    return start <= t <= end


def _clean(text: str, max_len: int = 1600) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def _course_code(course: dict) -> str:
    code = course.get("secondarycode")
    if code:
        return str(code)
    # Verified Lexington WebTrac course codes:
    # 3 = Lakeside, 5 = Picadome.
    name = course.get("name", "").lower()
    if "lakeside" in name:
        return "3"
    if "picadome" in name or "gay brewer" in name:
        return "5"
    raise RuntimeError(f"No WebTrac secondarycode configured for {course.get('name')}")


def _write_diag(page: Page, course_name: str, tee_date: date, diagnostic_dir: Path | None) -> None:
    if diagnostic_dir is None:
        return
    diagnostic_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9]+", "_", course_name).strip("_").lower()
    stem = diagnostic_dir / f"webtrac_{safe}_{tee_date.isoformat()}"
    try:
        stem.with_suffix(".html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    try:
        stem.with_suffix(".txt").write_text(page.locator("body").inner_text(), encoding="utf-8")
    except Exception:
        pass
    try:
        page.screenshot(path=str(stem.with_suffix(".png")), full_page=True)
    except Exception:
        pass


def _find_result_rows(page: Page):
    # Search every frame for table rows, since WebTrac sometimes wraps its content.
    rows = []
    for frame in [page] + page.frames:
        try:
            count = frame.locator("tr").count()
            for i in range(count):
                rows.append(frame.locator("tr").nth(i))
        except Exception:
            continue
    return rows


def _parse_rows(page: Page, course: dict, tee_date: date, start: time, end: time, players: int) -> list[WebTracSlot]:
    expected_date_us = tee_date.strftime("%m/%d/%Y")
    aliases = [course.get("course_name", course.get("name", "")).lower()]
    if "picadome" in aliases[0] or "gay brewer" in aliases[0]:
        aliases += ["picadome golf course", "picadome"]
    if "lakeside" in aliases[0]:
        aliases += ["lakeside golf course", "lakeside"]

    slots: list[WebTracSlot] = []
    seen: set[str] = set()
    for row in _find_result_rows(page):
        try:
            text = _clean(row.inner_text())
        except Exception:
            continue
        low = text.lower()
        if not any(a in low for a in aliases):
            continue
        if expected_date_us not in text:
            continue
        tm = _parse_time(text)
        if tm is None or not _in_window(tm, start, end):
            continue
        available = len(AVAILABLE_RE.findall(text))
        if available < players:
            continue
        href = None
        try:
            links = row.locator("a")
            for j in range(links.count()):
                link = links.nth(j)
                label = _clean(link.inner_text(), 100)
                if re.search(r"add\s*to\s*cart", label, re.I):
                    href = link.get_attribute("href")
                    if href:
                        break
        except Exception:
            pass
        url = urljoin(page.url, href) if href else page.url
        display = tm.strftime("%I:%M %p").lstrip("0")
        key = f"{tee_date.isoformat()}|{display}"
        if key in seen:
            continue
        seen.add(key)
        print(f"WEBTRAC MATCH {course['name']} {tee_date.isoformat()}: {display} | available_slots={available}")
        slots.append(WebTracSlot(tee_date.isoformat(), display, players, url))
    return slots


def _set_date_if_possible(page: Page, tee_date: date) -> None:
    # WebTrac exposes a standard text/date input in the rendered HTML. Try labels and
    # likely selectors, but do not fail if the direct course URL already returns the
    # desired date/result page.
    candidates = [
        page.get_by_label(re.compile(r"^Date$", re.I)),
        page.locator("input[type='date']"),
        page.locator("input[name*='date' i]"),
        page.locator("input[id*='date' i]"),
        page.locator("input[placeholder*='date' i]"),
    ]
    for loc in candidates:
        try:
            if loc.count() == 0:
                continue
            el = loc.first
            val = tee_date.isoformat()
            typ = (el.get_attribute("type") or "").lower()
            if typ == "date":
                el.fill(val)
            else:
                el.fill(tee_date.strftime("%m/%d/%Y"))
            # Trigger any framework change handlers.
            el.press("Tab")
            return
        except Exception:
            continue


def _search_if_possible(page: Page) -> bool:
    candidates = [
        page.get_by_role("button", name=re.compile(r"^Search$", re.I)),
        page.get_by_text(re.compile(r"^Search$", re.I)),
        page.locator("input[type='submit'][value='Search']"),
        page.locator("button:has-text('Search')"),
    ]
    for loc in candidates:
        try:
            if loc.count() == 0:
                continue
            loc.first.click(force=True, timeout=10000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=30000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(1800)
            return True
        except Exception:
            continue
    return False


def scan(page: Page, course: dict, tee_date: date, start_time: str, end_time: str, players: int, diagnostic_dir: Path | None = None) -> list[WebTracSlot]:
    code = _course_code(course)
    # Critical change: select the course server-side with secondarycode instead of
    # trying to interact with the dynamic course dropdown.
    base = "https://kylexingtonweb.myvscloud.com/webtrac/web/search.html"
    url = f"{base}?module=GR&secondarycode={code}"
    print(f"WEBTRAC NAVIGATE {course['name']}: secondarycode={code} url={url}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)

    print(f"WEBTRAC PAGE {course['name']}: url={page.url} title={page.title()!r}")
    try:
        body = _clean(page.locator("body").inner_text(), 2200)
        print(f"WEBTRAC BODY PREVIEW {course['name']}: {body[:1800]}")
    except Exception:
        pass

    # Set requested date and re-run search if the form is available. If the form is
    # unavailable, the result page may already be populated; parse what we have.
    _set_date_if_possible(page, tee_date)
    _search_if_possible(page)
    page.wait_for_timeout(1200)

    _write_diag(page, course["name"], tee_date, diagnostic_dir)

    start = _parse_time(start_time) or time(7, 0)
    end = _parse_time(end_time) or time(9, 0)
    slots = _parse_rows(page, course, tee_date, start, end, players)
    print(f"WEBTRAC RESULT {course['name']} {tee_date.isoformat()}: {len(slots)} matching {players}-player slots")
    return slots
