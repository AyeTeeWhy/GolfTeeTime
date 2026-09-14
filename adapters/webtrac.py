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
                print(f"WEBTRAC V15 REVEAL CLICKED {course_name} frame={frame.url}")
                return True
        except Exception as exc:
            print(f"WEBTRAC V15 REVEAL ATTEMPT FAILED {course_name} frame={frame.url}: {exc}")
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
        print(f"WEBTRAC V15 INVENTORY frame={frame.url} selects={selects} inputs={inputs} buttons={buttons}")
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
        print(f"WEBTRAC V15 DATE SET {course_name}: {target} frame={frame.url}")
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
            print(f"WEBTRAC V15 DATE JS SET {course_name}: {target} frame={frame.url}")
            return True
        except Exception as exc:
            print(f"WEBTRAC V15 DATE SET FAILED {course_name}: {exc}")
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
            print(f"WEBTRAC V15 SEARCH CLICKED {course_name} frame={frame.url}")
            return True
        except Exception as exc:
            print(f"WEBTRAC V15 SEARCH CLICK FAILED {course_name}: {exc}")
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
        print(f"WEBTRAC V15 MATCH {course.get('name')}: {tee_date.isoformat()} {display} | available={available}")
        slots.append(WebTracSlot(tee_date.isoformat(), display, players, url))
    return slots


def _direct_result_urls(code: str, tee_date: date, start_time: str, players: int) -> list[str]:
    """
    Vermont Systems WebTrac search pages accept their filter state through query
    parameters in many deployments. We try a few compatible parameter spellings.
    Results are filtered by course/date/time in the parser, so extra rows are safe.
    """
    d = tee_date.strftime("%m/%d/%Y")
    bt = start_time.lower().replace(" ", "+")
    base = "https://kylexingtonweb.myvscloud.com/webtrac/web/search.html"
    candidates = [
        f"{base}?module=GR&display=detail&secondarycode={code}&Date={d}&BeginDate={d}&begintime={bt}&players={players}&holes=18",
        f"{base}?module=GR&display=detail&secondarycode={code}&Date={d}&begintime={bt}&players={players}",
        f"{base}?module=GR&display=detail&secondarycode={code}&Date={d}&BeginTime={bt}&NumPlayers={players}&NumberOfHoles=18",
        f"{base}?module=GR&display=detail&secondarycode={code}&Date={d}&begin_time={bt}&players={players}&holes=18",
    ]
    return candidates

def _js_inventory(page: Page, course_name: str):
    try:
        data = page.evaluate("""
        () => ({
          forms: [...document.forms].map((f,i) => ({
            i,
            action: f.action,
            method: f.method,
            inputs: [...f.querySelectorAll('input,select,button')].map((e,j)=>({
              j, tag:e.tagName, type:e.type||'', name:e.name||'', id:e.id||'',
              value:e.value||'', aria:e.getAttribute('aria-label')||'',
              text:(e.innerText||e.textContent||'').trim().slice(0,120)
            }))
          })),
          buttons:[...document.querySelectorAll('button,input[type=button],input[type=submit]')].map((e,j)=>({
            j,tag:e.tagName,type:e.type||'',name:e.name||'',id:e.id||'',
            value:e.value||'',text:(e.innerText||e.textContent||'').trim().slice(0,120)
          }))
        })
        """)
        print(f"WEBTRAC V15 JS FORMS {course_name}: {data}")
        return data
    except Exception as exc:
        print(f"WEBTRAC V15 JS INVENTORY FAILED {course_name}: {exc}")
        return None

def _parse_document(page: Page, course: dict, tee_date: date, start: time, end: time, players: int) -> list[WebTracSlot]:
    # Parse the whole page body rather than relying on the table DOM, because
    # Vermont Systems can render result rows with nested custom markup.
    try:
        body = _clean(page.locator("body").inner_text(), 200000)
    except Exception:
        return []
    expected = tee_date.strftime("%m/%d/%Y")
    name = (course.get("name") or course.get("course_name") or "")
    low_name = name.lower()
    aliases = {low_name}
    if "picadome" in low_name or "gay brewer" in low_name:
        aliases |= {"picadome golf course", "picadome", "gay brewer", "gay brewer jr"}
    if "lakeside" in low_name:
        aliases |= {"lakeside golf course", "lakeside"}
    slots=[]
    # Split around "Item Action" rows by scanning line windows.
    lines=[ln.strip() for ln in body.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        if expected not in line:
            continue
        context=" ".join(lines[max(0,i-4):min(len(lines),i+6)])
        cl=context.lower()
        if not any(a in cl for a in aliases):
            continue
        tm=_parse_time(context)
        if not tm or not _in_window(tm,start,end):
            continue
        available=len(AVAILABLE_RE.findall(context))
        if available < players:
            continue
        display=tm.strftime("%I:%M %p").lstrip("0")
        key=f"{tee_date.isoformat()}|{display}|{low_name}"
        url=page.url
        if key in {f"{s.tee_date}|{s.tee_time}|{low_name}" for s in slots}:
            continue
        print(f"WEBTRAC V15 MATCH {name}: {tee_date.isoformat()} {display} | available={available}")
        slots.append(WebTracSlot(tee_date.isoformat(),display,players,url))
    return slots

def scan(page: Page, course: dict, tee_date: date, start_time: str, end_time: str, players: int, diagnostic_dir: Path | None = None) -> list[WebTracSlot]:
    code = _course_code(course)
    name = course.get("name") or "course"
    start = _parse_time(start_time) or time(7,0)
    end = _parse_time(end_time) or time(9,0)
    print(f"WEBTRAC V15 START {name}: secondarycode={code}")
    # First load the public tee-time search and reveal the filters if present.
    base = f"https://kylexingtonweb.myvscloud.com/webtrac/web/search.html?module=GR&secondarycode={code}"
    page.goto(base, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1200)
    print(f"WEBTRAC V15 BASE PAGE {name}: {page.url}")
    try:
        reveal = page.get_by_role("button", name=re.compile(r"click\s*to\s*reveal", re.I))
        if reveal.count():
            reveal.first.click(force=True, timeout=10000)
            page.wait_for_timeout(700)
            print(f"WEBTRAC V15 REVEAL CLICKED {name}")
    except Exception as exc:
        print(f"WEBTRAC V15 REVEAL FAILED {name}: {exc}")
    _js_inventory(page, name)

    # Try server-side result URLs. This avoids relying on the dynamic search widgets.
    candidates=_direct_result_urls(code, tee_date, start_time, players)
    for idx,url in enumerate(candidates,1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1000)
            body=_clean(page.locator("body").inner_text(), 200000)
            print(f"WEBTRAC V15 CANDIDATE {name} #{idx}: {page.url}")
            print(f"WEBTRAC V15 CANDIDATE {name} #{idx} BODY_HEAD: {_clean(body[:2500],2500)}")
            slots=_parse_document(page,course,tee_date,start,end,players)
            if slots:
                _write_diag(page,name,tee_date,diagnostic_dir)
                print(f"WEBTRAC V15 SUCCESS {name}: {len(slots)} matching slots via candidate #{idx}")
                return slots
            # If the page actually contains the target date and course, keep this
            # candidate as a likely valid result page even when there are no matches.
            if tee_date.strftime("%m/%d/%Y") in body and ("Tee Time Search Results" in body or "Search Results" in body):
                _write_diag(page,name,tee_date,diagnostic_dir)
                print(f"WEBTRAC V15 VALID RESULT PAGE {name}: no qualifying slots")
                return []
        except Exception as exc:
            print(f"WEBTRAC V15 CANDIDATE FAILED {name} #{idx}: {exc}")

    _write_diag(page,name,tee_date,diagnostic_dir)
    print(f"WEBTRAC V15 NO RESULT PAGE {name}")
    return []
