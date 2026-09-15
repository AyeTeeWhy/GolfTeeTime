from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from urllib.parse import urlencode, urljoin

from playwright.sync_api import Page

TIME_RE = re.compile(r"\b(0?[1-9]|1[0-2]):([0-5]\d)\s*(AM|PM)\b", re.I)
UNAVAILABLE_RE = re.compile(
    r"\b(unavailable|not available|full|sold out|closed|maintenance|blocked)\b",
    re.I,
)
POSITIVE_RE = re.compile(
    r"\b(available|book|select|reserve|open|tee time|players?)\b",
    re.I,
)
CHALLENGE_RE = re.compile(
    r"(just a moment|access denied|cf-chl|captcha|verify you are human|enable javascript)",
    re.I,
)


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
    hour = int(m.group(1))
    minute = int(m.group(2))
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
    return start <= t < end


def _ancestor_info(locator, levels: int = 7):
    cur = locator
    for level in range(levels):
        try:
            text = _clean(cur.inner_text(), 900)
            href = cur.get_attribute("href")
            aria = cur.get_attribute("aria-label")
            title = cur.get_attribute("title")
            role = cur.get_attribute("role")
            cls = cur.get_attribute("class") or ""
            tag = cur.evaluate("e => e.tagName")
            onclick = cur.get_attribute("onclick")
            yield {
                "level": level,
                "locator": cur,
                "text": text,
                "href": href,
                "aria": aria or "",
                "title": title or "",
                "role": role or "",
                "class": cls,
                "tag": tag,
                "onclick": onclick or "",
            }
            cur = cur.locator("xpath=..")
        except Exception:
            return


def _looks_like_booking_context(info: dict) -> bool:
    combined = " ".join(
        [
            info["text"],
            info["aria"],
            info["title"],
            info["class"],
            info["role"],
            info["onclick"],
        ]
    )
    if UNAVAILABLE_RE.search(combined):
        return False
    if info["href"] or info["onclick"]:
        return True
    if info["tag"] in {"A", "BUTTON", "INPUT"}:
        return True
    return bool(POSITIVE_RE.search(combined))


def _candidate_time_nodes(page: Page):
    """Find tee-time UI across CPS deployments.

    CPS has multiple Online Reservations V5 page variants. Some render a tee
    time as a button/link; others render it inside a clickable card or div.
    We therefore inspect visible elements containing a clock time and walk up
    their ancestors looking for booking/availability context.
    """
    # Prefer smaller elements so a whole tee-time grid/card does not create
    # dozens of duplicate candidates. A fallback over common controls follows.
    loc = page.locator("body *")
    count = loc.count()
    for i in range(count):
        el = loc.nth(i)
        try:
            if not el.is_visible():
                continue
            text = _clean(el.inner_text(), 300)
            if not text or len(text) > 300:
                continue
            matches = list(TIME_RE.finditer(text))
            if len(matches) != 1:
                continue
        except Exception:
            continue

        match = matches[0]
        label_parts = [text]
        best = None
        for info in _ancestor_info(el):
            label_parts.extend([info["aria"], info["title"]])
            if UNAVAILABLE_RE.search(" ".join([info["text"], info["aria"], info["title"], info["class"]])):
                best = None
                break
            if _looks_like_booking_context(info):
                best = info
                break

        if best is not None:
            context = _clean(" ".join(p for p in label_parts if p), 900)
            yield el, match, context, best


def _write_diagnostic(
    page: Page,
    course: dict,
    tee_date: date,
    diagnostic_dir: Path,
    requested_players: int,
    url: str,
) -> None:
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
        "challenge_detected": bool(CHALLENGE_RE.search(body)),
    }
    candidates = []
    for el, tm, context, info in _candidate_time_nodes(page):
        candidates.append(
            {
                "time": tm.group(0),
                "context": context,
                "tag": info["tag"],
                "href": info["href"],
                "class": info["class"],
            }
        )
    report["candidate_time_nodes"] = candidates
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


def _verify_loaded_page(page: Page, course: dict) -> str:
    """Return body text when the CPS page is a real booking page.

    A blank/error/challenge page must not be interpreted as 'no tee times',
    because that would generate a false No Tee Time alert.
    """
    try:
        body = page.locator("body").inner_text(timeout=5000)
    except Exception as exc:
        raise RuntimeError(f"CPS page body could not be read: {exc}") from exc

    body_clean = _clean(body, 20000)
    if len(body_clean) < 80:
        raise RuntimeError(f"CPS booking page for {course['name']} loaded with unexpectedly little content")
    if CHALLENGE_RE.search(body_clean):
        raise RuntimeError(f"CPS booking page for {course['name']} appears blocked by a challenge/anti-bot page")

    booking_markers = re.search(
        r"(tee time|players|holes|search|sign in|reservation|book|available)",
        body_clean,
        re.I,
    )
    if not booking_markers:
        raise RuntimeError(f"CPS booking page for {course['name']} did not look like a reservation page")
    return body


def scan(
    page: Page,
    course: dict,
    tee_date: date,
    start_time: str,
    end_time: str,
    players: int,
    diagnostic_dir: Path | None = None,
) -> list[CPSSlot]:
    base = course["url"]
    params = {
        "Date": tee_date.isoformat(),
        "Player": str(players),
        "Hole": "18",
        "TeeOffTimeMin": str(int(start_time[:2])),
        "TeeOffTimeMax": str(int(end_time[:2]) - 1),
    }
    url = f"{base.split('?', 1)[0]}?{urlencode(params)}"
    print(f"CPS URL: {url}")

    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    # CPS renders parts of the reservation grid after the initial document.
    page.wait_for_timeout(2500)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass

    body = _verify_loaded_page(page, course)

    if diagnostic_dir is not None:
        _write_diagnostic(page, course, tee_date, diagnostic_dir, players, url)

    start = _parse_hhmm(start_time)
    end = _parse_hhmm(end_time)
    out: list[CPSSlot] = []
    seen: set[str] = set()

    candidates = list(_candidate_time_nodes(page))
    for el, tm, context, info in candidates:
        clock = _parse_time(tm.group(0))
        if not _in_window(clock, start, end):
            continue

        combined = " ".join(
            [
                context,
                info["text"],
                info["aria"],
                info["title"],
                info["class"],
            ]
        ).lower()
        if UNAVAILABLE_RE.search(combined):
            continue

        # Because the query explicitly asks CPS for Player=4 and Hole=18,
        # any returned bookable tee-time is valid for the requested foursome.
        key = clock.strftime("%H:%M")
        if key in seen:
            continue
        seen.add(key)

        href = info["href"] or el.get_attribute("href") or url
        if href and href.startswith("/"):
            href = urljoin(url, href)
        out.append(CPSSlot(tee_date.isoformat(), tm.group(0).upper(), players, href))
        print(
            f"CPS MATCH: {tee_date.isoformat()} {tm.group(0)} | "
            f"tag={info['tag']} | class={info['class'][:160]} | context={context[:500]}"
        )

    # A second, text-oriented fallback catches deployments where the time is
    # rendered as plain text and the clickable booking control is a sibling.
    # Only use a clock token when the nearby window contains positive booking
    # language, and never accept explicitly unavailable text.
    if not out:
        for match in TIME_RE.finditer(body):
            clock = _parse_time(match.group(0))
            if not _in_window(clock, start, end):
                continue
            lo = max(0, match.start() - 220)
            hi = min(len(body), match.end() + 220)
            nearby = _clean(body[lo:hi], 600)
            if UNAVAILABLE_RE.search(nearby):
                continue
            if not POSITIVE_RE.search(nearby):
                continue
            key = clock.strftime("%H:%M")
            if key in seen:
                continue
            seen.add(key)
            out.append(CPSSlot(tee_date.isoformat(), match.group(0).upper(), players, url))
            print(f"CPS TEXT MATCH: {tee_date.isoformat()} {match.group(0)} | context={nearby}")

    print(f"CPS RESULT {tee_date.isoformat()}: {len(out)} matching {players}-player slots")
    return out
