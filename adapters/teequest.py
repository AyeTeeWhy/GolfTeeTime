from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import Page

TIME_RE = re.compile(r"\b(0?[1-9]|1[0-2]):([0-5]\d)\s*(AM|PM)\b", re.I)
PLAYER_RE = re.compile(r"(?:^|\s)(\d+)\s*-\s*(\d+)(?=\s+18\s+holes)|(?:^|\s)(\d+)(?=\s+18\s+holes)", re.I)

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
    hour = int(m.group(1)); minute = int(m.group(2))
    if m.group(3).upper() == "PM" and hour != 12: hour += 12
    if m.group(3).upper() == "AM" and hour == 12: hour = 0
    return time(hour, minute)


def _parse_mm(value: str) -> time:
    hour, minute = map(int, value.split(":", 1)); return time(hour, minute)


def _in_window(t: time, start: time, end: time) -> bool:
    return start <= t <= end


def _clean(text: str, max_len: int = 700) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def _ancestors(locator, levels: int = 8):
    cur = locator
    seen=[]
    for level in range(levels):
        try:
            seen.append((level, cur))
            cur = cur.locator("xpath=..").first
        except Exception:
            break
    return seen


def _best_card_text(locator) -> tuple[str,int]:
    best=""; best_level=-1
    for level, loc in _ancestors(locator, 8):
        try:
            txt=_clean(loc.inner_text())
        except Exception:
            continue
        if TIME_RE.search(txt) and "book" in txt.lower():
            # Prefer the smallest ancestor that contains both a time and Book.
            return txt, level
        if len(txt)>len(best): best, best_level=txt, level
    return best,best_level


def _extract_available_players(text: str) -> int:
    m=PLAYER_RE.search(text)
    if not m: return 0
    if m.group(1) and m.group(2): return int(m.group(2))
    if m.group(3): return int(m.group(3))
    return 0


def _write_diagnostic(page: Page, course_name: str, tee_date: date, diagnostic_dir: Path, requested_players: int) -> None:
    diagnostic_dir.mkdir(parents=True, exist_ok=True)
    safe=re.sub(r"[^A-Za-z0-9]+","_",course_name).strip("_").lower()
    stem=diagnostic_dir/f"{safe}_{tee_date.isoformat()}"
    try: body=page.locator("body").inner_text()
    except Exception as exc: body=f"<body text unavailable: {exc}>"
    time_rows=[]
    for idx,m in enumerate(TIME_RE.finditer(body)):
        time_rows.append({"index":idx,"time":m.group(0),"context":_clean(body[max(0,m.start()-220):min(len(body),m.end()+300)],650)})
    books=[]
    try:
        links=page.locator("a").filter(has_text=re.compile(r"^\s*Book\b",re.I))
        n=links.count()
        for i in range(n):
            link=links.nth(i)
            if not link.is_visible(): continue
            txt=_clean(link.inner_text(),200); href=link.get_attribute("href") or ""
            card_text,level=_best_card_text(link)
            books.append({"index":i,"text":txt,"href":href,"matched_ancestor_level":level,"card_text":card_text,"parsed_time":(TIME_RE.search(card_text).group(0) if TIME_RE.search(card_text) else None),"parsed_max_players":_extract_available_players(card_text)})
    except Exception as exc:
        books=[{"error":str(exc)}]
    report={"course":course_name,"tee_date":tee_date.isoformat(),"requested_players":requested_players,"page_url":page.url,"title":page.title(),"time_token_count":len(time_rows),"time_tokens":time_rows,"book_links":books}
    stem.with_suffix('.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    stem.with_suffix('.txt').write_text(body,encoding='utf-8')
    try: page.screenshot(path=str(stem.with_suffix('.png')),full_page=True)
    except Exception: pass
    try: stem.with_suffix('.html').write_text(page.content(),encoding='utf-8')
    except Exception: pass


def scan(page: Page, course: dict, tee_date: date, start_time: str, end_time: str, players: int, diagnostic_dir: Path|None=None) -> list[TeeQuestSlot]:
    course_id=str(course['course_id']); variant=str(course.get('variant','1'))
    base=course.get('search_base','https://bookateetime.teequest.com/search')
    url=f"{base.rstrip('/')}/{course_id}-{variant}/{tee_date.isoformat()}?"+urlencode({"selectedPlayers":players,"selectedHoles":18})
    page.goto(url,wait_until='domcontentloaded',timeout=60000); page.wait_for_timeout(1500)
    if diagnostic_dir is not None: _write_diagnostic(page,course['name'],tee_date,diagnostic_dir,players)
    start=_parse_mm(start_time); end=_parse_mm(end_time); out=[]
    links=page.locator("a").filter(has_text=re.compile(r"^\s*Book\b",re.I))
    for i in range(links.count()):
        link=links.nth(i)
        if not link.is_visible(): continue
        card_text,level=_best_card_text(link)
        tm=TIME_RE.search(card_text)
        if not tm: continue
        clock=_parse_time(tm.group(0))
        max_players=_extract_available_players(card_text)
        print(f"TEEQUEST BOOK {i}: {tm.group(0)} | max_players={max_players} | ancestor={level} | {card_text}")
        if not _in_window(clock,start,end): continue
        if max_players < players: continue
        href=link.get_attribute('href') or url
        absolute=href if href.startswith('http') else url
        out.append(TeeQuestSlot(tee_date.isoformat(),tm.group(0).upper(),players,absolute))
    print(f"TEEQUEST RESULT {tee_date.isoformat()}: {len(out)} matching {players}-player slots")
    return out
