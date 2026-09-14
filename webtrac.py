from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

TIME_RE = re.compile(r"\b(0?[1-9]|1[0-2]):([0-5]\d)\s*(AM|PM)\b", re.I)

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


def _clean(text: str, max_len: int = 900) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def _write_diag(page: Page, course_name: str, tee_date: date, diagnostic_dir: Path | None) -> None:
    if diagnostic_dir is None:
        return
    diagnostic_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9]+", "_", course_name).strip("_").lower()
    stem = diagnostic_dir / f"webtrac_{safe}_{tee_date.isoformat()}"
    try:
        (stem.with_suffix(".html")).write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    try:
        (stem.with_suffix(".txt")).write_text(page.locator("body").inner_text(), encoding="utf-8")
    except Exception:
        pass
    try:
        page.screenshot(path=str(stem.with_suffix(".png")), full_page=True)
    except Exception:
        pass


def _dump_controls(page: Page) -> None:
    print("WEBTRAC CONTROL INVENTORY")
    try:
        for i in range(page.locator("input").count()):
            el = page.locator("input").nth(i)
            if not el.is_visible():
                continue
            print(
                f"  input[{i}] type={el.get_attribute('type')} "
                f"name={el.get_attribute('name')} id={el.get_attribute('id')} "
                f"placeholder={el.get_attribute('placeholder')}"
            )
    except Exception as exc:
        print(f"  input inventory error: {exc}")
    try:
        for i in range(page.locator("button").count()):
            el = page.locator("button").nth(i)
            if not el.is_visible():
                continue
            print(f"  button[{i}] text={_clean(el.inner_text(), 250)!r} id={el.get_attribute('id')} name={el.get_attribute('name')}")
    except Exception as exc:
        print(f"  button inventory error: {exc}")
    try:
        for i in range(page.locator("select").count()):
            el = page.locator("select").nth(i)
            if not el.is_visible():
                continue
            opts = el.locator("option").all_text_contents()
            print(f"  select[{i}] name={el.get_attribute('name')} id={el.get_attribute('id')} options={opts[:20]}")
    except Exception as exc:
        print(f"  select inventory error: {exc}")


def _open_tee_search(page: Page, base: str) -> None:
    """Open Tee Time Search through the WebTrac home page to establish a normal session."""
    home = "https://kylexingtonweb.myvscloud.com/webtrac/web/"
    page.goto(home, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)

    link = page.get_by_role("link", name=re.compile(r"^Tee Time Search$", re.I))
    if link.count() > 0:
        try:
            link.first.click(timeout=15000)
            page.wait_for_load_state("domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)
            return
        except Exception as exc:
            print(f"WEBTRAC menu navigation failed: {exc}")

    # Fallback to the supplied URL.
    page.goto(base, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)


def _find_course_select(page: Page, course_name: str):
    candidates = [course_name, course_name.replace("Gay Brewer Jr. Course at ", "")]
    selects = page.locator("select:visible")
    for i in range(selects.count()):
        s = selects.nth(i)
        opts = s.locator("option").all_text_contents()
        joined = " | ".join(opts).lower()
        for candidate in candidates:
            if candidate.lower() in joined:
                return s
    return None


def _find_player_select(page: Page):
    selects = page.locator("select:visible")
    for i in range(selects.count()):
        s = selects.nth(i)
        opts = [o.strip() for o in s.locator("option").all_text_contents()]
        if any(o == "4" or re.search(r"\b4\s*(player|players)?\b", o, re.I) for o in opts):
            return s
    return None


def _find_holes_select(page: Page):
    selects = page.locator("select:visible")
    for i in range(selects.count()):
        s = selects.nth(i)
        opts = [o.strip() for o in s.locator("option").all_text_contents()]
        if any(re.search(r"\b18\s*\(?\s*holes?\b", o, re.I) for o in opts):
            return s
    return None


def _select_label(select, wanted: str) -> bool:
    opts = select.locator("option")
    texts = [t.strip() for t in opts.all_text_contents()]
    for text in texts:
        if text.lower() == wanted.lower():
            try:
                select.select_option(label=text)
                return True
            except Exception:
                return False
    for text in texts:
        if wanted.lower() in text.lower() or text.lower() in wanted.lower():
            try:
                select.select_option(label=text)
                return True
            except Exception:
                return False
    return False


def _find_date_input(page: Page):
    inputs = page.locator("input:visible")
    ranked = []
    fallback = []
    for i in range(inputs.count()):
        el = inputs.nth(i)
        typ = (el.get_attribute("type") or "").lower()
        meta = " ".join([
            el.get_attribute("name") or "",
            el.get_attribute("id") or "",
            el.get_attribute("placeholder") or "",
        ])
        if typ in ("date", "text"):
            fallback.append(el)
            if re.search(r"date|begindate|searchdate", meta, re.I):
                ranked.append(el)
    return ranked[0] if ranked else (fallback[0] if fallback else None)


def _find_begin_input(page: Page):
    inputs = page.locator("input:visible")
    for i in range(inputs.count()):
        el = inputs.nth(i)
        meta = " ".join([
            el.get_attribute("name") or "",
            el.get_attribute("id") or "",
            el.get_attribute("placeholder") or "",
        ])
        if re.search(r"begin.*time|begintime", meta, re.I):
            return el
    return None


def _parse_result_rows(page: Page, course: dict, tee_date: date, start: time, end: time, players: int) -> list[WebTracSlot]:
    wanted = course.get("course_name", course["name"])
    slots: list[WebTracSlot] = []

    # WebTrac search results are represented as rows with Time / Date / Course /
    # Status columns. Prefer rows that contain an Add To Cart link.
    rows = page.locator("tr")
    for i in range(rows.count()):
        row = rows.nth(i)
        try:
            text = _clean(row.inner_text(), 1500)
        except Exception:
            continue
        if wanted.lower() not in text.lower():
            continue
        if not re.search(r"Add\s+To\s+Cart", text, re.I):
            continue
        tm = _parse_time(text)
        if tm is None or not _in_window(tm, start, end):
            continue
        statuses = re.findall(r"\bAvailable\b", text, re.I)
        if len(statuses) < players:
            continue

        href = None
        links = row.locator("a")
        for j in range(links.count()):
            link = links.nth(j)
            try:
                if re.search(r"add\s*to\s*cart", link.inner_text(), re.I):
                    href = link.get_attribute("href")
                    if href:
                        break
            except Exception:
                pass
        url = urljoin(page.url, href) if href else page.url
        display = tm.strftime("%I:%M %p").lstrip("0")
        slots.append(WebTracSlot(tee_date.isoformat(), display, players, url))
        print(f"WEBTRAC MATCH {course['name']} {tee_date.isoformat()}: {display} | available={len(statuses)}")
    return slots


def scan(page: Page, course: dict, tee_date: date, start_time: str, end_time: str, players: int, diagnostic_dir: Path | None = None) -> list[WebTracSlot]:
    base = course.get("url") or "https://kylexingtonweb.myvscloud.com/webtrac/web/search.html?module=GR"
    _open_tee_search(page, base)

    print(f"WEBTRAC PAGE {course['name']}: url={page.url} title={page.title()!r}")

    # Give the modern WebTrac shell time to finish injecting its golf form.
    try:
        page.get_by_text("Course", exact=True).first.wait_for(state="visible", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(2000)

    course_select = _find_course_select(page, course.get("course_name", course["name"]))
    player_select = _find_player_select(page)
    holes_select = _find_holes_select(page)

    if not (course_select and player_select and holes_select):
        _dump_controls(page)
        _write_diag(page, course["name"], tee_date, diagnostic_dir)
        missing = []
        if not course_select: missing.append("course")
        if not player_select: missing.append("players")
        if not holes_select: missing.append("holes")
        raise RuntimeError(f"WebTrac search controls unavailable: missing={','.join(missing)} url={page.url}")

    if not _select_label(course_select, course.get("course_name", course["name"])):
        raise RuntimeError(f"Could not select course {course.get('course_name', course['name'])}")
    if not _select_label(player_select, str(players)):
        raise RuntimeError(f"Could not select {players} players")
    if not _select_label(holes_select, "18 Holes"):
        raise RuntimeError("Could not select 18 holes")

    date_input = _find_date_input(page)
    if not date_input:
        raise RuntimeError("WebTrac date input unavailable")
    typ = (date_input.get_attribute("type") or "").lower()
    date_value = tee_date.isoformat() if typ == "date" else tee_date.strftime("%m/%d/%Y")
    date_input.fill(date_value)

    begin = _find_begin_input(page)
    if begin:
        try:
            begin.fill("7:00 AM")
        except Exception:
            pass

    search = page.get_by_role("button", name=re.compile(r"^Search$", re.I))
    if search.count() == 0:
        # Some WebTrac builds use an input/button hybrid.
        search = page.locator("input[type='submit'][value='Search'], button:has-text('Search')")
    if search.count() == 0:
        raise RuntimeError("WebTrac Search button unavailable")

    search.first.click()
    try:
        page.wait_for_load_state("domcontentloaded", timeout=30000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(1500)

    _write_diag(page, course["name"], tee_date, diagnostic_dir)
    start = _parse_time(start_time) or time(7, 0)
    end = _parse_time(end_time) or time(9, 0)
    slots = _parse_result_rows(page, course, tee_date, start, end, players)
    print(f"WEBTRAC RESULT {course['name']} {tee_date.isoformat()}: {len(slots)} matching {players}-player slots")
    return slots
