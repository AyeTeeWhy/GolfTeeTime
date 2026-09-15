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
class CPSSlot:
    tee_date: str
    tee_time: str
    players: int
    url: str


def _parse_time(value: str) -> time:
    m = TIME_RE.search(value)
    if not m:
        raise ValueError(f"Not a clock time: {value!r}")
    hour = int(m.group(1)); minute = int(m.group(2))
    ampm = m.group(3).upper()
    if ampm == "PM" and hour != 12:
        hour += 12
    if ampm == "AM" and hour == 12:
        hour = 0
    return time(hour, minute)


def _parse_hhmm(value: str) -> time:
    h, m = map(int, value.split(":", 1))
    return time(h, m)


def _clean(value: str, max_len: int = 900) -> str:
    text = " ".join((value or "").split())
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def _in_window(t: time, start: time, end: time) -> bool:
    # Config uses 09:00 as the exclusive upper bound, so 8:59 is included.
    return start <= t < end


def _ancestor_texts(locator, levels: int = 8):
    cur = locator
    for level in range(levels):
        try:
            yield level, cur, _clean(cur.inner_text())
            cur = cur.locator("xpath=..")
        except Exception:
            return


def _candidate_controls(page):
    """Yield visible links/buttons whose surrounding card contains a tee time.

    CPS changes markup between deployments. We therefore inspect common controls
    instead of depending on one brittle CSS class. Because the URL is requested
    with Player=4 and Hole=18, a returned tee-time is already filtered for a
    foursome and 18 holes by CPS itself.
    """
    loc = page.locator("a,button,[role='button'],input[type='button'],input[type='submit']")
    for i in range(loc.count()):
        el = loc.nth(i)
        try:
            if not el.is_visible():
                continue
        except Exception:
            continue
        label = _clean(" ".join([
            el.inner_text() if el.evaluate("e => e.tagName !== 'INPUT'") else "",
            el.get_attribute("value") or "",
            el.get_attribute("aria-label") or "",
            el.get_attribute("title") or "",
        ]), 250)
        found = None
        best = ""
        best_level = 99
        for level, anc, text in _ancestor_texts(el):
            tm = TIME_RE.search(text)
            if tm and level < best_level:
                found = tm
                best = text
                best_level = level
            if tm and re.search(r"\b(book|select|reserve|available|open)\b", text, re.I):
                found = tm
                best = text
                best_level = level
                break
        if found:
            yield el, label, found, best, best_level


def _write_diagnostic(page: Page, course: dict, tee_date: date, diagnostic_dir: Path, requested_players: int, url: str) -> None:
    diagnostic_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9]+", "_", course["name"]).strip("_").lower()
    stem = diagnostic_dir / f"{safe}_{tee_date.isoformat()}"
    try:
        body = page.locator("body").inner_text()
    except Exception as exc:
        body = f"<body text unavailable: {exc}>"
    report = {
        "course": course["name"],
        "tee_date": tee_date.isoformat(),
        "requested_players": requested_players,
        "url": url,
        "page_url": page.url,
        "title": page.title(),
        "time_tokens": [m.group(0) for m in TIME_RE.finditer(body)],
    }
    controls = []
    for _, label, tm, text, level in _candidate_controls(page):
        controls.append({"label": label, "time": tm.group(0), "ancestor_level": level, "context": text})
    report["candidate_controls"] = controls
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


def scan(page: Page, course: dict, tee_date: date, start_time: str, end_time: str, players: int, diagnostic_dir: Path | None = None) -> list[CPSSlot]:
    base = course["url"]
    # CPS V5 documents these external-search parameters as case-sensitive.
    params = {
        "Date": tee_date.isoformat(),
        "Player": str(players),
        "Hole": "18",
        "TeeOffTimeMin": str(int(start_time[:2])),
        # CPS treats the max as the entire hour, so 8 means through 8:59.
        "TeeOffTimeMax": str(int(end_time[:2]) - 1),
    }
    url = f"{base.split('?', 1)[0]}?{urlencode(params)}"
    print(f"CPS URL: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1800)

    if diagnostic_dir is not None:
        _write_diagnostic(page, course, tee_date, diagnostic_dir, players, url)

    start = _parse_hhmm(start_time)
    end = _parse_hhmm(end_time)
    out: list[CPSSlot] = []
    seen: set[str] = set()

    for el, label, tm, context, level in _candidate_controls(page):
        clock = _parse_time(tm.group(0))
        if not _in_window(clock, start, end):
            continue
        # Ignore controls clearly marked unavailable/disabled.
        combined = f"{label} {context}".lower()
        if re.search(r"\bunavailable\b|\bnot available\b|\bfull\b|\bsold out\b|\bclosed\b", combined):
            continue
        key = clock.strftime("%H:%M")
        if key in seen:
            continue
        seen.add(key)
        href = el.get_attribute("href") or url
        if href and href.startswith("/"):
            from urllib.parse import urljoin
            href = urljoin(url, href)
        out.append(CPSSlot(tee_date.isoformat(), tm.group(0).upper(), players, href))
        print(f"CPS MATCH: {tee_date.isoformat()} {tm.group(0)} | context={context[:500]}")

    print(f"CPS RESULT {tee_date.isoformat()}: {len(out)} matching {players}-player slots")
    return out
