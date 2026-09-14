from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

TZ = ZoneInfo("America/New_York")
BASE = Path(__file__).resolve().parent
WEBTRAC_HOME = "https://kylexingtonweb.myvscloud.com/webtrac/web/"
COURSES = {
    "Picadome": 5,
    "Lakeside": 3,
}


def weekend_dates(start: date, days: int = 14) -> list[date]:
    out = []
    for i in range(days + 1):
        d = start + timedelta(days=i)
        if d.weekday() in (5, 6):
            out.append(d)
    return out


def pick_browser(pw, headed: bool):
    # Prefer installed Edge/Chrome locally. These are less likely to be
    # challenged than a fresh bundled Chromium profile and let the user solve
    # any normal browser challenge once if the site presents one.
    launch_args = {"headless": not headed}
    for channel in ("msedge", "chrome"):
        try:
            return pw.chromium.launch(channel=channel, **launch_args)
        except Exception:
            pass
    return pw.chromium.launch(**launch_args)


def visible_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=5000)
    except Exception:
        return page.content()


def dump_debug(page, label: str, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)
    try:
        page.screenshot(path=str(out_dir / f"{safe}.png"), full_page=True)
    except Exception:
        pass
    try:
        (out_dir / f"{safe}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    try:
        (out_dir / f"{safe}.txt").write_text(visible_text(page), encoding="utf-8")
    except Exception:
        pass


def find_tee_time_link(page):
    candidates = [
        page.get_by_role("link", name=re.compile(r"tee\s*time\s*search", re.I)),
        page.get_by_text(re.compile(r"tee\s*time\s*search", re.I), exact=True),
    ]
    for loc in candidates:
        try:
            if loc.count() > 0 and loc.first.is_visible():
                return loc.first
        except Exception:
            continue
    return None


def reveal_if_needed(page):
    for _ in range(3):
        try:
            btn = page.get_by_role("button", name=re.compile(r"click\s*to\s*reveal", re.I))
            if btn.count() and btn.first.is_visible():
                btn.first.click()
                page.wait_for_timeout(800)
                continue
        except Exception:
            pass
        break


def list_controls(page):
    controls = {"selects": [], "inputs": [], "buttons": []}
    for i in range(page.locator("select").count()):
        el = page.locator("select").nth(i)
        try:
            controls["selects"].append({
                "i": i,
                "name": el.get_attribute("name"),
                "id": el.get_attribute("id"),
                "aria": el.get_attribute("aria-label"),
                "options": el.locator("option").all_inner_texts(),
            })
        except Exception:
            pass
    for i in range(page.locator("input").count()):
        el = page.locator("input").nth(i)
        try:
            controls["inputs"].append({
                "i": i,
                "type": el.get_attribute("type"),
                "name": el.get_attribute("name"),
                "id": el.get_attribute("id"),
                "aria": el.get_attribute("aria-label"),
                "placeholder": el.get_attribute("placeholder"),
                "value": el.input_value(),
            })
        except Exception:
            pass
    for i in range(page.locator("button").count()):
        el = page.locator("button").nth(i)
        try:
            controls["buttons"].append({
                "i": i,
                "text": el.inner_text(),
                "type": el.get_attribute("type"),
                "name": el.get_attribute("name"),
                "id": el.get_attribute("id"),
                "aria": el.get_attribute("aria-label"),
            })
        except Exception:
            pass
    return controls


def parse_results(page, course_name: str, target_date: date, start_h: int = 7, end_h: int = 9):
    text = visible_text(page)
    rows = []
    # WebTrac's rendered result text is regular enough to parse even if DOM
    # classes change. Capture blocks containing all four Available markers.
    blocks = re.split(r"\n\s*\n", text)
    for block in blocks:
        if course_name.lower() not in block.lower():
            continue
        if block.count("Available") < 4:
            continue
        m = re.search(r"\b(\d{1,2}:\d{2}\s*[ap]m)\b", block, flags=re.I)
        if not m:
            continue
        tm = m.group(1).lower().replace(" ", " ")
        hour = int(re.match(r"\d{1,2}", tm).group())
        if not (start_h <= hour < end_h):
            continue
        rows.append({"date": target_date.isoformat(), "time": tm, "players": 4})
    return rows


def scan_course(page, name: str, code: int, target_date: date, debug_dir: Path):
    search_url = f"{WEBTRAC_HOME}search.html?module=GR&secondarycode={code}"
    print(f"\n=== {name} {target_date.isoformat()} ===")
    print(f"Opening {search_url}")
    page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(1500)
    reveal_if_needed(page)

    controls = list_controls(page)
    print(json.dumps({"controls": controls}, indent=2, default=str))

    # Look for the date field by semantic attributes first, then by input order.
    date_candidates = []
    for sel in [
        "input[name*='Date' i]", "input[id*='Date' i]", "input[placeholder*='date' i]",
        "input[aria-label*='date' i]", "input[type='date']"
    ]:
        try:
            if page.locator(sel).count():
                date_candidates.append(page.locator(sel).first)
        except Exception:
            pass

    if not date_candidates:
        print("DATE CONTROL NOT FOUND IN LOCAL BROWSER")
        dump_debug(page, f"{name}_{target_date}_no_date", debug_dir)
        return []

    date_el = date_candidates[0]
    try:
        date_el.fill(target_date.strftime("%m/%d/%Y"))
    except Exception as exc:
        print(f"DATE FILL FAILED: {exc}")
        dump_debug(page, f"{name}_{target_date}_date_fill_failed", debug_dir)
        return []

    # Try to set four players / 18 holes through semantic select inspection.
    for idx in range(page.locator("select").count()):
        sel = page.locator("select").nth(idx)
        opts = [x.strip() for x in sel.locator("option").all_inner_texts()]
        joined = " | ".join(opts)
        try:
            if re.search(r"(^|\s)4(\s|$)", joined) and any("player" in o.lower() for o in opts):
                sel.select_option(label="4")
            elif re.search(r"18", joined) and any("hole" in o.lower() for o in opts):
                sel.select_option(label=re.compile(r"18"))
        except Exception:
            pass

    search_btn = None
    for candidate in [
        page.get_by_role("button", name=re.compile(r"^search$", re.I)),
        page.locator("input[type='submit']"),
    ]:
        try:
            if candidate.count() and candidate.first.is_visible():
                search_btn = candidate.first
                break
        except Exception:
            pass

    if not search_btn:
        print("SEARCH BUTTON NOT FOUND IN LOCAL BROWSER")
        dump_debug(page, f"{name}_{target_date}_no_search", debug_dir)
        return []

    search_btn.click()
    try:
        page.wait_for_load_state("domcontentloaded", timeout=20000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(1200)

    rows = parse_results(page, name, target_date)
    print(f"MATCHES: {rows}")
    if not rows:
        dump_debug(page, f"{name}_{target_date}_no_matches", debug_dir)
    return rows


def main():
    ap = argparse.ArgumentParser(description="Local Lexington WebTrac tee-time probe")
    ap.add_argument("--headed", action="store_true", default=True, help="Run a visible browser (default).")
    ap.add_argument("--course", choices=["Picadome", "Lakeside", "both"], default="both")
    ap.add_argument("--date", help="Specific date MM/DD/YYYY. Defaults to the next Saturday.")
    args = ap.parse_args()

    today = datetime.now(TZ).date()
    if args.date:
        target = datetime.strptime(args.date, "%m/%d/%Y").date()
    else:
        target = next((today + timedelta(days=i) for i in range(8) if (today + timedelta(days=i)).weekday() == 5), today)

    chosen = list(COURSES.items()) if args.course == "both" else [(args.course, COURSES[args.course])]
    debug_dir = BASE / "debug"

    with sync_playwright() as pw:
        browser = pick_browser(pw, headed=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.goto(WEBTRAC_HOME, wait_until="domcontentloaded", timeout=45000)
        print(f"HOME: {page.url}")
        print(f"TITLE: {page.title()}")

        try:
            for name, code in chosen:
                scan_course(page, name, code, target, debug_dir)
        finally:
            print("\nDebug files, if needed, are in:", debug_dir)
            print("The browser will remain open for 20 seconds for visual inspection.")
            page.wait_for_timeout(20_000)
            context.close()
            browser.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
