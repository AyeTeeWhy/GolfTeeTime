from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

TIME_RE = re.compile(r"\b(0?[1-9]|1[0-2]):([0-5]\d)\s*(AM|PM)\b", re.I)
DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
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


def _clean(text: str, max_len: int = 2200) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def _course_code(course: dict) -> str:
    if course.get("secondarycode"):
        return str(course["secondarycode"])
    name = (course.get("name") or course.get("course_name") or "").lower()
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
    stem = diagnostic_dir / f"webtrac_v12_{safe}_{tee_date.isoformat()}"
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



def _reveal_search_controls(page: Page, course_name: str) -> None:
    """WebTrac can initially expose only a collapsed search panel with a Click to reveal button."""
    candidates = [
        page.get_by_role("button", name=re.compile(r"click\s*to\s*reveal", re.I)),
        page.get_by_text(re.compile(r"click\s*to\s*reveal", re.I)),
        page.locator("button").filter(has_text=re.compile(r"click\s*to\s*reveal", re.I)),
        page.locator("input[type='button'], input[type='submit']").filter(has_text=re.compile(r"click\s*to\s*reveal", re.I)),
    ]
    for loc in candidates:
        try:
            if loc.count() > 0:
                loc.first.scroll_into_view_if_needed(timeout=5000)
                loc.first.click(force=True, timeout=10000)
                page.wait_for_timeout(750)
                print(f"WEBTRAC V13 REVEAL CLICKED {course_name}")
                return
        except Exception as exc:
            print(f"WEBTRAC V13 REVEAL ATTEMPT FAILED {course_name}: {exc}")
    print(f"WEBTRAC V13 REVEAL BUTTON NOT FOUND {course_name}")


def _print_form_inventory(page: Page, course_name: str) -> None:
    print(f"WEBTRAC V13 FORM INVENTORY {course_name}")
    try:
        selects = page.locator("select")
        for i in range(selects.count()):
            s = selects.nth(i)
            print(
                f"  SELECT {i}: name={s.get_attribute('name')!r} id={s.get_attribute('id')!r} "
                f"aria={s.get_attribute('aria-label')!r} value={s.input_value()}"
            )
    except Exception:
        pass
    try:
        inputs = page.locator("input")
        for i in range(min(inputs.count(), 20)):
            inp = inputs.nth(i)
            print(
                f"  INPUT {i}: type={inp.get_attribute('type')!r} name={inp.get_attribute('name')!r} "
                f"id={inp.get_attribute('id')!r} placeholder={inp.get_attribute('placeholder')!r} "
                f"aria={inp.get_attribute('aria-label')!r} value={inp.input_value()!r}"
            )
    except Exception:
        pass
    try:
        buttons = page.get_by_role("button")
        for i in range(min(buttons.count(), 20)):
            b = buttons.nth(i)
            print(f"  BUTTON {i}: text={_clean(b.inner_text(),120)!r} type={b.get_attribute('type')!r}")
    except Exception:
        pass


def _find_date_input(page: Page):
    patterns = [
        page.get_by_label(re.compile(r"^Date$", re.I)),
        page.locator("input[name*='date' i]"),
        page.locator("input[id*='date' i]"),
        page.locator("input[aria-label*='date' i]"),
        page.locator("input[placeholder*='date' i]"),
    ]
    for loc in patterns:
        try:
            if loc.count() > 0:
                return loc.first
        except Exception:
            continue
    return None


def _set_date(page: Page, tee_date: date, course_name: str) -> bool:
    inp = _find_date_input(page)
    if inp is None:
        print(f"WEBTRAC V13 DATE INPUT NOT FOUND {course_name}")
        return False
    target = tee_date.strftime("%m/%d/%Y")
    try:
        inp.scroll_into_view_if_needed()
        inp.fill(target)
        inp.press("Tab")
        print(f"WEBTRAC V13 DATE SET {course_name}: {target}")
        return True
    except Exception as exc:
        print(f"WEBTRAC V13 DATE SET FAILED {course_name}: {exc}")
        # JS fallback for date/masked inputs.
        try:
            page.evaluate(
                """([el, value]) => { const proto=el.constructor?.prototype; const desc=proto && Object.getOwnPropertyDescriptor(proto,'value'); if(desc?.set){desc.set.call(el,value);} else {el.value=value;} el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true})); }""",
                [inp, target],
            )
            print(f"WEBTRAC V13 DATE JS SET {course_name}: {target}")
            return True
        except Exception as jsex:
            print(f"WEBTRAC V13 DATE JS FAILED {course_name}: {jsex}")
            return False


def _click_search(page: Page, course_name: str) -> bool:
    locators = [
        page.get_by_role("button", name=re.compile(r"^Search$", re.I)),
        page.locator("input[type='submit'][value='Search']"),
        page.locator("button:has-text('Search')"),
    ]
    for loc in locators:
        try:
            if loc.count() == 0:
                continue
            loc.first.click(force=True, timeout=10000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=30000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(1500)
            print(f"WEBTRAC V13 SEARCH CLICKED {course_name}: url={page.url}")
            return True
        except Exception as exc:
            print(f"WEBTRAC V13 SEARCH CLICK FAILED {course_name}: {exc}")
    return False


def _rows(page: Page):
    try:
        n = page.locator("tr").count()
        return [page.locator("tr").nth(i) for i in range(n)]
    except Exception:
        return []


def _parse_results(page: Page, course: dict, tee_date: date, start: time, end: time, players: int) -> list[WebTracSlot]:
    expected_date = tee_date.strftime("%m/%d/%Y")
    name = (course.get("name") or course.get("course_name") or "").lower()
    aliases = {name}
    if "picadome" in name or "gay brewer" in name:
        aliases.update({"picadome golf course", "picadome"})
    if "lakeside" in name:
        aliases.update({"lakeside golf course", "lakeside"})

    slots: list[WebTracSlot] = []
    seen: set[str] = set()
    for row in _rows(page):
        try:
            text = _clean(row.inner_text())
        except Exception:
            continue
        low = text.lower()
        if not any(a and a in low for a in aliases):
            continue
        if expected_date not in text:
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
            for i in range(links.count()):
                link = links.nth(i)
                label = _clean(link.inner_text(), 100)
                if re.search(r"add\s*to\s*cart", label, re.I):
                    href = link.get_attribute("href")
                    if href:
                        break
        except Exception:
            pass
        url = urljoin(page.url, href) if href else page.url
        display = tm.strftime("%I:%M %p").lstrip("0")
        key = f"{tee_date.isoformat()}|{display}|{name}"
        if key in seen:
            continue
        seen.add(key)
        print(f"WEBTRAC V13 MATCH {course.get('name')}: {tee_date.isoformat()} {display} | available={available}")
        slots.append(WebTracSlot(tee_date.isoformat(), display, players, url))
    return slots


def scan(page: Page, course: dict, tee_date: date, start_time: str, end_time: str, players: int, diagnostic_dir: Path | None = None) -> list[WebTracSlot]:
    code = _course_code(course)
    base = "https://kylexingtonweb.myvscloud.com/webtrac/web/search.html"
    url = f"{base}?module=GR&secondarycode={code}"
    print(f"WEBTRAC V13 START {course.get('name')}: secondarycode={code}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1000)
    print(f"WEBTRAC V13 PAGE {course.get('name')}: {page.url}")
    _reveal_search_controls(page, course.get("name") or "course")
    _print_form_inventory(page, course.get("name") or "course")
    _set_date(page, tee_date, course.get("name") or "course")
    _click_search(page, course.get("name") or "course")
    page.wait_for_timeout(1000)
    _write_diag(page, course.get("name") or "course", tee_date, diagnostic_dir)
    start = _parse_time(start_time) or time(7, 0)
    end = _parse_time(end_time) or time(9, 0)
    slots = _parse_results(page, course, tee_date, start, end, players)
    print(f"WEBTRAC V13 RESULT {course.get('name')}: {tee_date.isoformat()} -> {len(slots)} matching slots")
    return slots
