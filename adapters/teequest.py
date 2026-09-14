from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from urllib.parse import urlencode, urljoin

from playwright.sync_api import Locator, Page

TIME_RE = re.compile(r"\b(0?[1-9]|1[0-2]):([0-5]\d)\s*(AM|PM)\b", re.I)
PLAYER_RANGE_RE = re.compile(r"\b(\d+)\s*(?:-|–|—|to)\s*(\d+)\b")
CURRENCY_RE = re.compile(r"\$\s*\d+(?:\.\d{2})?")


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
    hour = int(m.group(1))
    minute = int(m.group(2))
    if m.group(3).upper() == "PM" and hour != 12:
        hour += 12
    if m.group(3).upper() == "AM" and hour == 12:
        hour = 0
    return time(hour, minute)


def _parse_mm(value: str) -> time:
    hour, minute = map(int, value.split(":", 1))
    return time(hour, minute)


def _in_window(t: time, start: time, end: time) -> bool:
    return start <= t <= end


def _clean(text: str, max_len: int = 900) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def _book_link_card_text(link: Locator) -> str:
    """
    Return the smallest useful ancestor text around a visible Book link.

    TeeQuest's DOM is dynamic, and the Book link is not guaranteed to be within
    a fixed number of parent elements. We walk upward in the browser and choose
    the nearest ancestor whose text contains a clock time plus other tee-sheet
    markers. This is much more robust than a fixed xpath depth.
    """
    try:
        return link.evaluate(
            """el => {
                const timeRe = /\\b(?:0?[1-9]|1[0-2]):[0-5]\\d\\s*(?:AM|PM)\\b/i;
                const usefulRe = /(\\$\\s*\\d|holes|cart|book|player|pay at|tee)/i;

                let node = el;
                let fallback = el.innerText || '';

                for (let i = 0; i < 14 && node; i += 1, node = node.parentElement) {
                    const txt = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                    if (!txt) continue;
                    if (timeRe.test(txt)) {
                        if (usefulRe.test(txt) && txt.length <= 1200) return txt;
                        if (!fallback || txt.length < fallback.length) fallback = txt;
                    }
                }
                return fallback;
            }"""
        )
    except Exception:
        # Playwright fallback when JS evaluation is unavailable.
        best = ""
        current = link
        for _ in range(12):
            try:
                txt = _clean(current.inner_text())
                if TIME_RE.search(txt) and (
                    "$" in txt
                    or "book" in txt.lower()
                    or "holes" in txt.lower()
                    or "cart" in txt.lower()
                ):
                    return txt
                if len(txt) > len(best):
                    best = txt
                current = current.locator("xpath=..").first
            except Exception:
                break
        return best


def _extract_available_players(card_text: str, requested_players: int) -> int | None:
    """
    TeeQuest's selectedPlayers query is the primary four-player filter.
    This helper is a secondary safety check when the card explicitly exposes
    a player range such as '1 - 4'.
    """
    ranges = []
    for m in PLAYER_RANGE_RE.finditer(card_text):
        lo, hi = int(m.group(1)), int(m.group(2))
        # Ignore likely time fragments or unrelated numeric ranges.
        if 1 <= lo <= hi <= 8:
            ranges.append((lo, hi))

    if not ranges:
        return requested_players

    # If a range explicitly contains the requested group size, accept it.
    containing = [hi for lo, hi in ranges if lo <= requested_players <= hi]
    if containing:
        return max(containing)

    # If the only visible range is below the requested group size, reject.
    if max(hi for _, hi in ranges) < requested_players:
        return None

    # Otherwise defer to the selectedPlayers=4 search filter.
    return requested_players


def _write_diagnostic(
    page: Page,
    course_name: str,
    tee_date: date,
    diagnostic_dir: Path,
    requested_players: int,
) -> None:
    diagnostic_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9]+", "_", course_name).strip("_").lower()
    stem = diagnostic_dir / f"{safe}_{tee_date.isoformat()}"

    try:
        body = page.locator("body").inner_text()
    except Exception as exc:
        body = f"<body text unavailable: {exc}>"

    time_rows = []
    for idx, match in enumerate(TIME_RE.finditer(body)):
        token = match.group(0)
        snippet_start = max(0, match.start() - 280)
        snippet_end = min(len(body), match.end() + 280)
        time_rows.append(
            {
                "index": idx,
                "time": token,
                "context": _clean(body[snippet_start:snippet_end], 650),
            }
        )

    link_rows = []
    try:
        links = page.locator("a")
        for i in range(min(links.count(), 500)):
            link = links.nth(i)
            if not link.is_visible():
                continue
            text = _clean(link.inner_text(), 180)
            href = link.get_attribute("href") or ""
            if text or href:
                link_rows.append({"text": text, "href": href})
    except Exception as exc:
        link_rows.append({"error": str(exc)})

    buttons = []
    try:
        btns = page.locator("button")
        for i in range(min(btns.count(), 250)):
            btn = btns.nth(i)
            if not btn.is_visible():
                continue
            buttons.append({"text": _clean(btn.inner_text(), 180), "disabled": btn.is_disabled()})
    except Exception as exc:
        buttons.append({"error": str(exc)})

    report = {
        "course": course_name,
        "tee_date": tee_date.isoformat(),
        "requested_players": requested_players,
        "page_url": page.url,
        "title": page.title(),
        "time_token_count": len(time_rows),
        "time_tokens": time_rows,
        "visible_links": link_rows,
        "visible_buttons": buttons,
    }
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


def scan(
    page: Page,
    course: dict,
    tee_date: date,
    start_time: str,
    end_time: str,
    players: int,
    diagnostic_dir: Path | None = None,
) -> list[TeeQuestSlot]:
    """Scan one TeeQuest date for a bookable tee time for the requested group."""
    course_id = str(course["course_id"])
    variant = str(course.get("variant", "1"))
    base = course.get("search_base", "https://bookateetime.teequest.com/search")
    url = f"{base.rstrip('/')}/{course_id}-{variant}/{tee_date.isoformat()}"
    url += "?" + urlencode({"selectedPlayers": players, "selectedHoles": 18})

    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1400)

    if diagnostic_dir is not None:
        _write_diagnostic(page, course["name"], tee_date, diagnostic_dir, players)

    body = page.locator("body").inner_text()
    low = body.lower()
    if "course is currently offline" in low:
        raise RuntimeError("TeeQuest reports this course is currently offline")

    start = _parse_mm(start_time)
    end = _parse_mm(end_time)
    out: list[TeeQuestSlot] = []

    # TeeQuest may label the link "Book", "Book 1", etc. Keep the selector
    # broad and let the card parser determine whether it is a valid tee time.
    links = page.locator("a").filter(has_text=re.compile(r"\bBook\b", re.I))
    link_count = links.count()

    matched_details: list[str] = []

    for i in range(link_count):
        link = links.nth(i)
        try:
            if not link.is_visible():
                continue
        except Exception:
            continue

        card_text = _book_link_card_text(link)
        if not card_text:
            continue

        lower = card_text.lower()
        if any(x in lower for x in ("sold out", "call course to play", "unavailable")):
            continue

        m = TIME_RE.search(card_text)
        if not m:
            continue

        clock = _parse_time(m.group(0))
        if not _in_window(clock, start, end):
            continue

        available_players = _extract_available_players(card_text, players)
        if available_players is None or available_players < players:
            matched_details.append(
                f"REJECT {m.group(0)}: card does not support {players} players | {card_text[:240]}"
            )
            continue

        href = link.get_attribute("href") or url
        absolute = urljoin(url, href)
        out.append(
            TeeQuestSlot(
                tee_date=tee_date.isoformat(),
                tee_time=m.group(0).upper(),
                players=players,
                url=absolute,
            )
        )
        matched_details.append(
            f"MATCH {m.group(0)}: {players} players | {card_text[:300]}"
        )

    if diagnostic_dir is not None:
        safe = re.sub(r"[^A-Za-z0-9]+", "_", course["name"]).strip("_").lower()
        detail_path = diagnostic_dir / f"{safe}_{tee_date.isoformat()}_matches.txt"
        detail_path.write_text(
            "\n".join(matched_details) if matched_details else "No Book links matched.\n",
            encoding="utf-8",
        )

        time_count = len(TIME_RE.findall(body))
        print(
            f"TEEQUEST DIAGNOSTIC {tee_date.isoformat()}: "
            f"page={page.url} time_tokens={time_count} visible_book_links={link_count} "
            f"matched={len(out)}"
        )
        for detail in matched_details:
            print(f"TEEQUEST {detail}")
        if time_count and link_count and not out:
            print(
                "TEEQUEST DIAGNOSTIC: Book links were present but no card passed "
                "the time/player parser."
            )
        elif not time_count:
            print("TEEQUEST DIAGNOSTIC: no clock-time tokens found on the rendered page.")

    deduped: dict[tuple[str, str, int], TeeQuestSlot] = {}
    for slot in out:
        deduped[(slot.tee_date, slot.tee_time, slot.players)] = slot
    return list(deduped.values())
