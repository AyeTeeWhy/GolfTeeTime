from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

BASE_URL = "https://kylexingtonweb.myvscloud.com/webtrac/web/search.html?module=GR&secondarycode=4"
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


def _clean(text: str, max_len: int = 1200) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def _all_frames(page: Page):
    # Main frame first, then nested frames if the site introduces one.
    yield page
    for frame in page.frames:
        if frame != page.main_frame:
            yield frame


def _visible(locator) -> bool:
    try:
        return locator.count() > 0 and locator.first.is_visible()
    except Exception:
        return False


def _find_labeled(frame, label: str):
    # WebTrac exposes accessible labels such as Course, Number Of Players,
    # Date, Begin Time, and Number Of Holes. Prefer those over brittle CSS.
    candidates = [
        frame.get_by_label(re.compile(rf"^{re.escape(label)}$", re.I)),
        frame.get_by_label(re.compile(re.escape(label), re.I)),
    ]
    for loc in candidates:
        try:
            if loc.count() and loc.first.is_visible():
                return loc.first
        except Exception:
            pass
    return None


def _find_combo(frame):
    try:
        combos = frame.get_by_role("combobox")
        for i in range(combos.count()):
            c = combos.nth(i)
            if c.is_visible():
                return c
    except Exception:
        pass
    return None


def _select_option_texts(select) -> list[str]:
    try:
        return [x.strip() for x in select.locator("option").all_text_contents()]
    except Exception:
        return []


def _pick_select(select, wanted: str, numeric: bool = False) -> bool:
    options = _select_option_texts(select)
    if not options:
        return False

    # Exact visible-label match.
    for opt in options:
        if opt.strip().lower() == wanted.strip().lower():
            try:
                select.select_option(label=opt)
                return True
            except Exception:
                pass

    # Substring match, useful for "18 (Front)" etc.
    needle = wanted.strip().lower()
    for opt in options:
        low = opt.strip().lower()
        if needle in low or low in needle:
            try:
                select.select_option(label=opt)
                return True
            except Exception:
                pass

    # Numeric player selector: pick the option that is exactly "4" or starts
    # with "4 " / "4-player" / "4 players".
    if numeric and wanted.isdigit():
        n = wanted
        for opt in options:
            low = opt.lower()
            if low.strip() == n or re.match(rf"^{re.escape(n)}(\s+|-)", low.strip()):
                try:
                    select.select_option(label=opt)
                    return True
                except Exception:
                    pass
    return False


def _find_labeled_or_combo(page: Page, labels: list[str]):
    for frame in _all_frames(page):
        for label in labels:
            loc = _find_labeled(frame, label)
            if loc is not None:
                return frame, loc
        # Fallback: first visible combobox when labels are not wired up.
        c = _find_combo(frame)
        if c is not None:
            return frame, c
    return None, None


def _diagnostic(page: Page, diagnostic_dir: Path, course_name: str, tee_date: date) -> None:
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
    print(f"WEBTRAC PAGE title={page.title()!r} url={page.url!r}")
    for frame in _all_frames(page):
        try:
            print(f"WEBTRAC FRAME url={frame.url!r}")
            loc = frame.locator("select, input, button, [role='combobox']")
            print(f"  controls={loc.count()}")
            for i in range(min(loc.count(), 40)):
                el = loc.nth(i)
                try:
                    if not el.is_visible():
                        continue
                    print(
                        "  CONTROL "
                        f"tag={el.evaluate('(e)=>e.tagName')} "
                        f"type={el.get_attribute('type')!r} "
                        f"id={el.get_attribute('id')!r} "
                        f"name={el.get_attribute('name')!r} "
                        f"aria={el.get_attribute('aria-label')!r} "
                        f"text={_clean(el.inner_text(), 300)!r}"
                    )
                except Exception:
                    pass
        except Exception:
            pass


def _wait_ready(page: Page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PlaywrightTimeoutError:
        pass
    # WebTrac can populate controls asynchronously after DOMContentLoaded.
    page.wait_for_timeout(4000)


def scan(
    page: Page,
    course: dict,
    tee_date: date,
    start_time: str,
    end_time: str,
    players: int,
    diagnostic_dir: Path | None = None,
) -> list[WebTracSlot]:
    base = course.get("url") or BASE_URL
    page.goto(base, wait_until="domcontentloaded", timeout=60000)
    _wait_ready(page)

    if diagnostic_dir is not None:
        _diagnostic(page, diagnostic_dir, course["name"], tee_date)

    course_name = course.get("course_name", course["name"])

    # --- Course ---
    course_select = None
    player_select = None
    holes_select = None

    # First use explicit accessible labels.
    for frame in _all_frames(page):
        course_select = _find_labeled(frame, "Course")
        player_select = _find_labeled(frame, "Number Of Players")
        holes_select = _find_labeled(frame, "Number Of Holes")
        if course_select or player_select or holes_select:
            break

    # Fallback to all selects in the page/frame and classify by options.
    if not (course_select and player_select and holes_select):
        for frame in _all_frames(page):
            selects = frame.locator("select")
            for i in range(selects.count()):
                s = selects.nth(i)
                try:
                    if not s.is_visible():
                        continue
                    opts = _select_option_texts(s)
                    joined = " | ".join(opts).lower()
                    if course_select is None and course_name.lower() in joined:
                        course_select = s
                    if player_select is None and any(
                        re.fullmatch(r"\s*4(?:\s+players?)?\s*", x, re.I) for x in opts
                    ):
                        player_select = s
                    if holes_select is None and any(re.search(r"\b18\b.*holes?", x, re.I) for x in opts):
                        holes_select = s
                except Exception:
                    pass

    _dump_controls(page)

    if course_select is None:
        raise RuntimeError("WebTrac Course control not found after accessible-label and select fallback.")
    if player_select is None:
        raise RuntimeError("WebTrac Number Of Players control not found.")
    if holes_select is None:
        raise RuntimeError("WebTrac Number Of Holes control not found.")

    if not _pick_select(course_select, course_name):
        raise RuntimeError(f"Could not select WebTrac course {course_name!r}")
    if not _pick_select(player_select, str(players), numeric=True):
        raise RuntimeError(f"Could not select {players} players")
    if not _pick_select(holes_select, "18", numeric=True):
        if not _pick_select(holes_select, "18 Holes"):
            raise RuntimeError("Could not select 18 holes")

    # --- Date ---
    date_input = None
    for frame in _all_frames(page):
        date_input = _find_labeled(frame, "Date")
        if date_input is not None:
            break
        for selector in ("input[type='date']", "input[name*='date' i]", "input[id*='date' i]"):
            loc = frame.locator(selector)
            try:
                if loc.count() and loc.first.is_visible():
                    date_input = loc.first
                    break
            except Exception:
                pass
        if date_input is not None:
            break
    if date_input is None:
        raise RuntimeError("WebTrac Date input not found.")
    typ = (date_input.get_attribute("type") or "").lower()
    date_input.fill(tee_date.isoformat() if typ == "date" else tee_date.strftime("%m/%d/%Y"))

    # --- Begin Time ---
    begin = None
    for frame in _all_frames(page):
        begin = _find_labeled(frame, "Begin Time")
        if begin is not None:
            break
        for selector in ("input[type='time']", "input[name*='begin' i][name*='time' i]", "input[id*='begin' i][id*='time' i]"):
            loc = frame.locator(selector)
            try:
                if loc.count() and loc.first.is_visible():
                    begin = loc.first
                    break
            except Exception:
                pass
        if begin is not None:
            break

    if begin is not None:
        try:
            btyp = (begin.get_attribute("type") or "").lower()
            begin.fill(start_time if btyp == "time" else "7:00 AM")
        except Exception:
            pass

    # --- Search ---
    search = None
    for frame in _all_frames(page):
        for pattern in (r"^Search$", r"Search"):
            btn = frame.get_by_role("button", name=re.compile(pattern, re.I))
            if btn.count() and btn.first.is_visible():
                search = btn.first
                break
        if search is not None:
            break
    if search is None:
        raise RuntimeError("WebTrac Search button not found.")

    search.click()
    _wait_ready(page)

    if diagnostic_dir is not None:
        _diagnostic(page, diagnostic_dir, course["name"], tee_date)

    start = _parse_time(start_time) or time(7, 0)
    end = _parse_time(end_time) or time(9, 0)
    out: list[WebTracSlot] = []

    # WebTrac's actual results are table rows. Official page output shows:
    # Time | Date | Holes | Course | Status, with four "Available" tokens
    # when four player spots are available.
    for frame in _all_frames(page):
        rows = frame.locator("tr")
        for i in range(rows.count()):
            row = rows.nth(i)
            try:
                if not row.is_visible():
                    continue
                text = _clean(row.inner_text(), 1500)
            except Exception:
                continue

            if course_name.lower() not in text.lower():
                continue

            tm = _parse_time(text)
            if tm is None or not _in_window(tm, start, end):
                continue

            available = len(re.findall(r"\bAvailable\b", text, re.I))
            if available < players:
                continue

            href = None
            links = row.locator("a")
            for j in range(links.count()):
                link = links.nth(j)
                try:
                    link_text = link.inner_text().strip()
                    if re.search(r"add\s+to\s+cart", link_text, re.I):
                        href = link.get_attribute("href")
                        break
                except Exception:
                    pass

            absolute = urljoin(frame.url, href) if href else frame.url
            display = tm.strftime("%I:%M %p").lstrip("0")
            out.append(WebTracSlot(tee_date.isoformat(), display, players, absolute))
            print(
                f"WEBTRAC MATCH {course['name']} {tee_date.isoformat()}: "
                f"{display} | available={available}"
            )

    # De-dupe identical rows returned by nested frames/duplicate markup.
    dedup: dict[tuple[str, str], WebTracSlot] = {(s.tee_date, s.tee_time): s for s in out}
    out = list(dedup.values())
    print(
        f"WEBTRAC RESULT {course['name']} {tee_date.isoformat()}: "
        f"{len(out)} matching {players}-player slots"
    )
    return out
