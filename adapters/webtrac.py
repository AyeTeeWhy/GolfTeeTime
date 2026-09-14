from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import Page

TIME_RE = re.compile(r"\b(0?[1-9]|1[0-2]):([0-5]\d)\s*(AM|PM)\b", re.I)

@dataclass(frozen=True)
class WebTracSlot:
    tee_date: str
    tee_time: str
    players: int
    url: str

def _parse_time(text: str) -> time | None:
    m = TIME_RE.search(text or "")
    if not m: return None
    h, minute = int(m.group(1)), int(m.group(2))
    ap = m.group(3).upper()
    if ap == "PM" and h != 12: h += 12
    if ap == "AM" and h == 12: h = 0
    return time(h, minute)

def _in_window(t: time, start: time, end: time) -> bool:
    return start <= t <= end

def _clean(text: str, max_len: int = 700) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= max_len else text[: max_len - 3] + "..."

def _select_by_label(select, candidate: str) -> bool:
    try:
        select.select_option(label=re.compile(rf"^{re.escape(candidate)}$", re.I))
        return True
    except Exception:
        try:
            select.select_option(label=re.compile(re.escape(candidate), re.I))
            return True
        except Exception:
            return False

def _write_diagnostic(page: Page, course_name: str, tee_date: date, diagnostic_dir: Path, requested_players: int) -> None:
    diagnostic_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9]+", "_", course_name).strip("_").lower()
    stem = diagnostic_dir / f"webtrac_{safe}_{tee_date.isoformat()}"
    try: body = page.locator("body").inner_text()
    except Exception as exc: body = f"<body text unavailable: {exc}>"
    (stem.with_suffix(".txt")).write_text(body, encoding="utf-8")
    try: (stem.with_suffix(".html")).write_text(page.content(), encoding="utf-8")
    except Exception: pass
    try: page.screenshot(path=str(stem.with_suffix(".png")), full_page=True)
    except Exception: pass

def _find_date_input(page: Page):
    # WebTrac currently renders Date as a visible text input. Prefer inputs whose
    # id/name mentions date; otherwise use the first visible text/date control.
    inputs = page.locator("input")
    ranked = []
    fallback = []
    for i in range(inputs.count()):
        inp = inputs.nth(i)
        try:
            if not inp.is_visible(): continue
            typ = (inp.get_attribute("type") or "").lower()
            name = inp.get_attribute("name") or ""
            ident = inp.get_attribute("id") or ""
            ph = inp.get_attribute("placeholder") or ""
            meta = f"{name} {ident} {ph}"
            if typ in ("date", "text"):
                fallback.append(inp)
                if re.search(r"date|begindate|searchdate", meta, re.I): ranked.append(inp)
        except Exception:
            pass
    return ranked[0] if ranked else (fallback[0] if fallback else None)

def _find_begin_time_input(page: Page):
    inputs = page.locator("input")
    for i in range(inputs.count()):
        inp = inputs.nth(i)
        try:
            if not inp.is_visible(): continue
            typ = (inp.get_attribute("type") or "").lower()
            meta = f"{inp.get_attribute('name') or ''} {inp.get_attribute('id') or ''} {inp.get_attribute('placeholder') or ''}"
            if re.search(r"begin.*time|begintime", meta, re.I): return inp, typ
        except Exception:
            pass
    return None, None

def scan(page: Page, course: dict, tee_date: date, start_time: str, end_time: str, players: int, diagnostic_dir: Path | None = None) -> list[WebTracSlot]:
    base = course.get("url", "https://parks.lexingtonky.gov/wbwsc/webtrac.wsc/search.html?module=GR")
    page.goto(base, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1000)

    selects = page.locator("select")
    inventory = []
    course_select = player_select = holes_select = None
    for i in range(selects.count()):
        s = selects.nth(i)
        try:
            txt = s.inner_text()
            inventory.append({"i": i, "id": s.get_attribute("id"), "name": s.get_attribute("name"), "text": _clean(txt, 350)})
            if course.get("course_name", course["name"]).lower() in txt.lower(): course_select = s
            if re.search(r"\b1\b.*\b2\b.*\b3\b.*\b4\b.*\b5\b", txt, re.S): player_select = s
            if "18 Holes" in txt and "9 Holes" in txt: holes_select = s
        except Exception:
            pass
    if not course_select:
        raise RuntimeError(f"WebTrac Course select not found. Select inventory: {inventory}")
    if not player_select:
        raise RuntimeError(f"WebTrac player select not found. Select inventory: {inventory}")
    if not holes_select:
        raise RuntimeError(f"WebTrac holes select not found. Select inventory: {inventory}")
    if not _select_by_label(course_select, course.get("course_name", course["name"])):
        raise RuntimeError(f"Could not select course {course.get('course_name', course['name'])}")
    if not _select_by_label(player_select, str(players)):
        raise RuntimeError(f"Could not select {players} players")
    if not _select_by_label(holes_select, "18 Holes"):
        raise RuntimeError("Could not select 18 Holes")

    date_input = _find_date_input(page)
    if date_input is None:
        raise RuntimeError("WebTrac Date input not found")
    typ = (date_input.get_attribute("type") or "").lower()
    date_input.fill(tee_date.isoformat() if typ == "date" else tee_date.strftime("%m/%d/%Y"))

    begin_input, begin_type = _find_begin_time_input(page)
    if begin_input is not None:
        try:
            begin_input.fill(start_time if begin_type == "time" else "7:00 AM")
        except Exception:
            pass

    search = page.get_by_role("button", name=re.compile(r"^Search$", re.I))
    if search.count() == 0:
        raise RuntimeError("WebTrac Search button not found")
    search.first.click()
    page.wait_for_load_state("domcontentloaded", timeout=60000)
    page.wait_for_timeout(1200)

    if diagnostic_dir is not None:
        _write_diagnostic(page, course["name"], tee_date, diagnostic_dir, players)

    start = _parse_time(start_time) or time(7,0)
    end = _parse_time(end_time) or time(9,0)
    out = []
    rows = page.locator("tr")
    course_name = course.get("course_name", course["name"])
    for i in range(rows.count()):
        row = rows.nth(i)
        try: text = _clean(row.inner_text(), 1200)
        except Exception: continue
        if course_name.lower() not in text.lower(): continue
        tm = _parse_time(text)
        if tm is None or not _in_window(tm, start, end): continue
        available = len(re.findall(r"\bAvailable\b", text, re.I))
        if available < players: continue
        href = None
        links = row.locator("a")
        for j in range(links.count()):
            try:
                if "add to cart" in links.nth(j).inner_text().strip().lower():
                    href = links.nth(j).get_attribute("href"); break
            except Exception: pass
        absolute = urljoin(page.url, href) if href else page.url
        display = tm.strftime("%I:%M %p").lstrip("0")
        out.append(WebTracSlot(tee_date.isoformat(), display, players, absolute))
        print(f"WEBTRAC MATCH {course['name']} {tee_date.isoformat()}: {display} | available={available}")
    print(f"WEBTRAC RESULT {course['name']} {tee_date.isoformat()}: {len(out)} matching {players}-player slots")
    return out
