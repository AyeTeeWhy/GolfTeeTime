from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import Page, Frame, TimeoutError as PlaywrightTimeoutError

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


def _frames(page: Page) -> list[Frame]:
    return [page] + page.frames


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
    print("WEBTRAC FRAME/CONTROL INVENTORY")
    frames = _frames(page)
    for fi, frame in enumerate(frames):
        print(f"  FRAME[{fi}] url={frame.url}")
        try:
            print(f"    selects={frame.locator('select').count()} inputs={frame.locator('input').count()} buttons={frame.locator('button').count()}")
        except Exception as exc:
            print(f"    inventory error: {exc}")
        try:
            for i in range(frame.locator("select").count()):
                el = frame.locator("select").nth(i)
                opts = el.locator("option").all_text_contents()
                print(f"    select[{i}] name={el.get_attribute('name')} id={el.get_attribute('id')} visible={el.is_visible()} options={opts[:30]}")
        except Exception as exc:
            print(f"    select detail error: {exc}")
        try:
            for i in range(frame.locator("input").count()):
                el = frame.locator("input").nth(i)
                if not el.is_visible() and (el.get_attribute("type") or "").lower() not in {"hidden", "submit"}:
                    continue
                print(f"    input[{i}] type={el.get_attribute('type')} name={el.get_attribute('name')} id={el.get_attribute('id')} placeholder={el.get_attribute('placeholder')} value={el.get_attribute('value')}")
        except Exception as exc:
            print(f"    input detail error: {exc}")
        try:
            for i in range(frame.locator("button").count()):
                el = frame.locator("button").nth(i)
                if not el.is_visible():
                    continue
                print(f"    button[{i}] text={_clean(el.inner_text(), 250)!r} id={el.get_attribute('id')} name={el.get_attribute('name')}")
        except Exception as exc:
            print(f"    button detail error: {exc}")


def _open_tee_search(page: Page, base: str) -> None:
    # Start from the public portal. This establishes the normal WebTrac session.
    home = "https://kylexingtonweb.myvscloud.com/webtrac/web/"
    page.goto(home, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1200)

    link = page.get_by_role("link", name=re.compile(r"^Tee Time Search$", re.I))
    if link.count() > 0:
        try:
            link.first.click(timeout=15000)
            page.wait_for_timeout(1800)
            return
        except Exception as exc:
            print(f"WEBTRAC menu navigation failed: {exc}")

    page.goto(base, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1800)


def _pick_frame(page: Page, course_name: str):
    # Prefer a frame containing a Course/Number Of Players label or a select.
    candidates = []
    for frame in _frames(page):
        try:
            body = frame.locator("body").inner_text(timeout=5000)
        except Exception:
            body = ""
        score = 0
        low = body.lower()
        if "number of players" in low:
            score += 5
        if "number of holes" in low:
            score += 3
        if "course" in low:
            score += 2
        if frame.locator("select").count() > 0:
            score += 10
            try:
                opts = " | ".join(frame.locator("select option").all_text_contents()).lower()
                if "picadome" in opts or "lakeside" in opts:
                    score += 20
            except Exception:
                pass
        candidates.append((score, frame))
    return max(candidates, key=lambda x: x[0])[1]


def _selects(frame: Frame):
    return frame.locator("select")


def _find_course_select(frame: Frame, course_name: str):
    name_low = course_name.lower()
    aliases = {name_low}
    if "picadome" in name_low or "gay brewer" in name_low:
        aliases.update({"picadome", "picadome golf course", "gay brewer jr.", "gay brewer jr. course at picadome"})
    if "lakeside" in name_low:
        aliases.add("lakeside golf course")
    for i in range(_selects(frame).count()):
        s = _selects(frame).nth(i)
        try:
            opts = [x.strip() for x in s.locator("option").all_text_contents()]
        except Exception:
            continue
        joined = " | ".join(opts).lower()
        if any(a in joined for a in aliases):
            return s
    return None


def _find_numeric_select(frame: Frame, wanted: int, kind: str):
    selects = _selects(frame)
    for i in range(selects.count()):
        s = selects.nth(i)
        try:
            opts = [x.strip() for x in s.locator("option").all_text_contents()]
        except Exception:
            continue
        low = " | ".join(opts).lower()
        if kind == "players" and any(re.fullmatch(r"\d+", x) and int(x) == wanted for x in opts):
            return s
        if kind == "holes" and any(re.search(r"\b18\b", x) and "hole" in x.lower() for x in opts):
            return s
    return None


def _select_value(select, wanted: int | str) -> bool:
    texts = [x.strip() for x in select.locator("option").all_text_contents()]
    wanted_s = str(wanted).strip().lower()
    # First exact label match.
    for text in texts:
        if text.lower() == wanted_s:
            try:
                select.select_option(label=text, force=True)
                return True
            except Exception:
                pass
    # Then a safe substring match.
    for text in texts:
        if wanted_s in text.lower():
            try:
                select.select_option(label=text, force=True)
                return True
            except Exception:
                pass
    return False


def _find_date_input(frame: Frame):
    inputs = frame.locator("input")
    ranked, fallback = [], []
    for i in range(inputs.count()):
        el = inputs.nth(i)
        typ = (el.get_attribute("type") or "").lower()
        meta = " ".join([el.get_attribute("name") or "", el.get_attribute("id") or "", el.get_attribute("placeholder") or ""])
        if typ in {"date", "text"} and (el.is_visible() or re.search(r"date|begin", meta, re.I)):
            fallback.append(el)
            if re.search(r"date|begin.*date|searchdate", meta, re.I):
                ranked.append(el)
    return ranked[0] if ranked else (fallback[0] if fallback else None)


def _find_begin_input(frame: Frame):
    inputs = frame.locator("input")
    for i in range(inputs.count()):
        el = inputs.nth(i)
        meta = " ".join([el.get_attribute("name") or "", el.get_attribute("id") or "", el.get_attribute("placeholder") or ""])
        if re.search(r"begin.*time|begintime", meta, re.I):
            return el
    return None


def _find_search(frame: Frame):
    loc = frame.get_by_role("button", name=re.compile(r"^Search$", re.I))
    if loc.count() > 0:
        return loc.first
    loc = frame.locator("input[type='submit'][value='Search'], button:has-text('Search')")
    return loc.first if loc.count() else None


def _parse_result_rows(frame: Frame, course: dict, tee_date: date, start: time, end: time, players: int) -> list[WebTracSlot]:
    wanted = (course.get("course_name") or course["name"]).lower()
    aliases = [wanted]
    if "picadome" in wanted or "gay brewer" in wanted:
        aliases += ["picadome golf course", "picadome"]
    if "lakeside" in wanted:
        aliases += ["lakeside golf course", "lakeside"]

    slots: list[WebTracSlot] = []
    rows = frame.locator("tr")
    for i in range(rows.count()):
        row = rows.nth(i)
        try:
            text = _clean(row.inner_text(), 1600)
        except Exception:
            continue
        low = text.lower()
        if not any(a in low for a in aliases):
            continue
        tm = _parse_time(text)
        if tm is None or not _in_window(tm, start, end):
            continue
        # WebTrac lists one status token per golfer slot. Require all four.
        available = len(re.findall(r"\bAvailable\b", text, re.I))
        if available < players:
            continue
        href = None
        links = row.locator("a")
        for j in range(links.count()):
            link = links.nth(j)
            try:
                label = link.inner_text().strip()
                if re.search(r"add\s*to\s*cart", label, re.I):
                    href = link.get_attribute("href")
                    if href:
                        break
            except Exception:
                continue
        url = urljoin(frame.url, href) if href else frame.url
        display = tm.strftime("%I:%M %p").lstrip("0")
        slots.append(WebTracSlot(tee_date.isoformat(), display, players, url))
        print(f"WEBTRAC MATCH {course['name']} {tee_date.isoformat()}: {display} | available_slots={available}")
    return slots


def scan(page: Page, course: dict, tee_date: date, start_time: str, end_time: str, players: int, diagnostic_dir: Path | None = None) -> list[WebTracSlot]:
    base = course.get("url") or "https://kylexingtonweb.myvscloud.com/webtrac/web/search.html?module=GR&secondarycode=4"
    _open_tee_search(page, base)
    print(f"WEBTRAC PAGE {course['name']}: url={page.url} title={page.title()!r} frames={len(page.frames)}")
    page.wait_for_timeout(2500)

    frame = _pick_frame(page, course.get("course_name", course["name"]))
    print(f"WEBTRAC TARGET FRAME: url={frame.url}")

    course_select = _find_course_select(frame, course.get("course_name", course["name"]))
    player_select = _find_numeric_select(frame, players, "players")
    holes_select = _find_numeric_select(frame, 18, "holes")

    if not (course_select and player_select and holes_select):
        _dump_controls(page)
        _write_diag(page, course["name"], tee_date, diagnostic_dir)
        missing = []
        if not course_select: missing.append("course")
        if not player_select: missing.append("players")
        if not holes_select: missing.append("holes")
        raise RuntimeError(f"WebTrac search controls unavailable: missing={','.join(missing)} url={frame.url}")

    if not _select_value(course_select, course.get("course_name", course["name"])):
        raise RuntimeError(f"Could not select course {course.get('course_name', course['name'])}")
    if not _select_value(player_select, players):
        raise RuntimeError(f"Could not select {players} players")
    if not _select_value(holes_select, "18"):
        raise RuntimeError("Could not select 18 holes")

    date_input = _find_date_input(frame)
    if not date_input:
        raise RuntimeError("WebTrac date input unavailable")
    typ = (date_input.get_attribute("type") or "").lower()
    value = tee_date.isoformat() if typ == "date" else tee_date.strftime("%m/%d/%Y")
    date_input.fill(value)

    begin = _find_begin_input(frame)
    if begin:
        try:
            begin.fill(start_time)
        except Exception:
            pass

    search = _find_search(frame)
    if search is None:
        raise RuntimeError("WebTrac Search button unavailable")
    search.click(force=True)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=30000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(2500)

    _write_diag(page, course["name"], tee_date, diagnostic_dir)
    start = _parse_time(start_time) or time(7, 0)
    end = _parse_time(end_time) or time(9, 0)
    # Results can land in a different frame, so re-select the best frame.
    result_frame = _pick_frame(page, course.get("course_name", course["name"]))
    slots = _parse_result_rows(result_frame, course, tee_date, start, end, players)
    print(f"WEBTRAC RESULT {course['name']} {tee_date.isoformat()}: {len(slots)} matching {players}-player slots")
    return slots
