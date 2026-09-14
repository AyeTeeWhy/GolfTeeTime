from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import Page, Frame, TimeoutError as PlaywrightTimeoutError

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

def _frames(page: Page) -> list[Frame]:
    # Include main frame first, then nested frames.
    return list(page.frames)

def _reveal(frame: Frame, course_name: str) -> bool:
    patterns = [
        frame.get_by_role("button", name=re.compile(r"click\s*to\s*reveal", re.I)),
        frame.get_by_text(re.compile(r"click\s*to\s*reveal", re.I)),
        frame.locator("button").filter(has_text=re.compile(r"click\s*to\s*reveal", re.I)),
        frame.locator("input[type='button'], input[type='submit']").filter(
            has_text=re.compile(r"click\s*to\s*reveal", re.I)
        ),
    ]
    for loc in patterns:
        try:
            if loc.count() > 0:
                loc.first.scroll_into_view_if_needed(timeout=5000)
                loc.first.click(force=True, timeout=10000)
                frame.wait_for_timeout(1000)
                print(f"WEBTRAC V14 REVEAL CLICKED {course_name} frame={frame.url}")
                return True
        except Exception as exc:
            print(f"WEBTRAC V14 REVEAL ATTEMPT FAILED {course_name} frame={frame.url}: {exc}")
    return False

def _inventory(frame: Frame, course_name: str) -> tuple[int, int, int]:
    selects = inputs = buttons = 0
    try:
        selects = frame.locator("select").count()
    except Exception:
        pass
    try:
        inputs = frame.locator("input").count()
    except Exception:
        pass
    try:
        buttons = frame.get_by_role("button").count()
    except Exception:
        pass
    if selects or inputs or buttons:
        print(f"WEBTRAC V14 INVENTORY frame={frame.url} selects={selects} inputs={inputs} buttons={buttons}")
        for i in range(min(selects, 20)):
            try:
                s = frame.locator("select").nth(i)
                print(
                    f"  SELECT {i}: name={s.get_attribute('name')!r} id={s.get_attribute('id')!r} "
                    f"aria={s.get_attribute('aria-label')!r}"
                )
            except Exception:
                pass
        for i in range(min(inputs, 40)):
            try:
                inp = frame.locator("input").nth(i)
                print(
                    f"  INPUT {i}: type={inp.get_attribute('type')!r} name={inp.get_attribute('name')!r} "
                    f"id={inp.get_attribute('id')!r} placeholder={inp.get_attribute('placeholder')!r} "
                    f"aria={inp.get_attribute('aria-label')!r} value={inp.input_value()!r}"
                )
            except Exception:
                pass
        for i in range(min(buttons, 30)):
            try:
                b = frame.get_by_role("button").nth(i)
                print(f"  BUTTON {i}: text={_clean(b.inner_text(),120)!r} type={b.get_attribute('type')!r}")
            except Exception:
                pass
    return selects, inputs, buttons

def _find_date_input(frame: Frame):
    patterns = [
        frame.get_by_label(re.compile(r"^Date$", re.I)),
        frame.locator("input[name*='date' i]"),
        frame.locator("input[id*='date' i]"),
        frame.locator("input[aria-label*='date' i]"),
        frame.locator("input[placeholder*='date' i]"),
    ]
    for loc in patterns:
        try:
            if loc.count() > 0:
                return loc.first
        except Exception:
            continue
    return None

def _set_date(frame: Frame, tee_date: date, course_name: str) -> bool:
    inp = _find_date_input(frame)
    if inp is None:
        return False
    target = tee_date.strftime("%m/%d/%Y")
    try:
        inp.scroll_into_view_if_needed()
        inp.fill(target)
        inp.press("Tab")
        print(f"WEBTRAC V14 DATE SET {course_name}: {target} frame={frame.url}")
        return True
    except Exception:
        try:
            inp.evaluate(
                """(el, value) => {
                    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                    setter.call(el, value);
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                }""",
                target,
            )
            print(f"WEBTRAC V14 DATE JS SET {course_name}: {target} frame={frame.url}")
            return True
        except Exception as exc:
            print(f"WEBTRAC V14 DATE SET FAILED {course_name}: {exc}")
            return False

def _click_search(frame: Frame, course_name: str) -> bool:
    patterns = [
        frame.get_by_role("button", name=re.compile(r"^Search$", re.I)),
        frame.locator("input[type='submit'][value='Search']"),
        frame.locator("button").filter(has_text=re.compile(r"^\s*Search\s*$", re.I)),
        frame.get_by_text(re.compile(r"^\s*Search\s*$", re.I)),
    ]
    for loc in patterns:
        try:
            if loc.count() == 0:
                continue
            loc.first.click(force=True, timeout=10000)
            try:
                frame.wait_for_load_state("domcontentloaded", timeout=30000)
            except PlaywrightTimeoutError:
                pass
            frame.wait_for_timeout(1500)
            print(f"WEBTRAC V14 SEARCH CLICKED {course_name} frame={frame.url}")
            return True
        except Exception as exc:
            print(f"WEBTRAC V14 SEARCH CLICK FAILED {course_name}: {exc}")
    return False

def _write_diag(page: Page, course_name: str, tee_date: date, diagnostic_dir: Path | None) -> None:
    if diagnostic_dir is None:
        return
    diagnostic_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9]+", "_", course_name).strip("_").lower()
    stem = diagnostic_dir / f"webtrac_v14_{safe}_{tee_date.isoformat()}"
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

def _parse_results(frame: Frame, course: dict, tee_date: date, start: time, end: time, players: int) -> list[WebTracSlot]:
    expected = tee_date.strftime("%m/%d/%Y")
    name = (course.get("name") or course.get("course_name") or "").lower()
    aliases = {name}
    if "picadome" in name or "gay brewer" in name:
        aliases |= {"picadome", "gay brewer", "gay brewer jr"}
    if "lakeside" in name:
        aliases |= {"lakeside"}
    slots: list[WebTracSlot] = []
    seen: set[str] = set()
    try:
        rows = frame.locator("tr")
        count = rows.count()
    except Exception:
        return slots
    for i in range(count):
        row = rows.nth(i)
        try:
            text = _clean(row.inner_text())
        except Exception:
            continue
        low = text.lower()
        if not any(a and a in low for a in aliases):
            continue
        if expected not in text:
            continue
        tm = _parse_time(text)
        if not tm or not _in_window(tm, start, end):
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
                if re.search(r"add\s*to\s*cart|book|reserve", label, re.I):
                    href = link.get_attribute("href")
                    if href:
                        break
        except Exception:
            pass
        url = urljoin(frame.url, href) if href else frame.url
        display = tm.strftime("%I:%M %p").lstrip("0")
        key = f"{tee_date.isoformat()}|{display}|{name}"
        if key in seen:
            continue
        seen.add(key)
        print(f"WEBTRAC V14 MATCH {course.get('name')}: {tee_date.isoformat()} {display} | available={available}")
        slots.append(WebTracSlot(tee_date.isoformat(), display, players, url))
    return slots

def scan(page: Page, course: dict, tee_date: date, start_time: str, end_time: str, players: int, diagnostic_dir: Path | None = None) -> list[WebTracSlot]:
    code = _course_code(course)
    url = f"https://kylexingtonweb.myvscloud.com/webtrac/web/search.html?module=GR&secondarycode={code}"
    name = course.get("name") or "course"
    print(f"WEBTRAC V14 START {name}: secondarycode={code}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)
    print(f"WEBTRAC V14 PAGE {name}: {page.url}")
    print(f"WEBTRAC V14 FRAMES {name}: {len(page.frames)}")
    target_frame: Frame | None = None
    # First pass: click any reveal and inspect all frames.
    for frame in _frames(page):
        try:
            _reveal(frame, name)
        except Exception:
            pass
    page.wait_for_timeout(1000)
    # Second pass: inspect all frames; pick a frame with a date-like input if possible.
    for frame in _frames(page):
        selects, inputs, buttons = _inventory(frame, name)
        if _find_date_input(frame) is not None:
            target_frame = frame
            break
    if target_frame is None:
        # Some WebTrac versions use text inputs with a surrounding label that doesn't map to aria.
        for frame in _frames(page):
            try:
                text = frame.locator("body").inner_text()
                if re.search(r"\bDate\b", text, re.I) and frame.locator("input").count() > 0:
                    target_frame = frame
                    break
            except Exception:
                continue
    if target_frame is None:
        print(f"WEBTRAC V14 DATE INPUT NOT FOUND {name} IN ANY FRAME")
        _write_diag(page, name, tee_date, diagnostic_dir)
        return []
    if not _set_date(target_frame, tee_date, name):
        _write_diag(page, name, tee_date, diagnostic_dir)
        return []
    _click_search(target_frame, name)
    page.wait_for_timeout(1000)
    _write_diag(page, name, tee_date, diagnostic_dir)
    start = _parse_time(start_time) or time(7, 0)
    end = _parse_time(end_time) or time(9, 0)
    slots = _parse_results(target_frame, course, tee_date, start, end, players)
    # Results may navigate into a different frame/page; scan all frames as fallback.
    if not slots:
        for frame in _frames(page):
            alt = _parse_results(frame, course, tee_date, start, end, players)
            if alt:
                slots.extend(alt)
    print(f"WEBTRAC V14 RESULT {name}: {tee_date.isoformat()} -> {len(slots)} matching slots")
    return slots
