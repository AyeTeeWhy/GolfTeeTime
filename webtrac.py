from __future__ import annotations

import json
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

BASE_URL = "https://kylexingtonweb.myvscloud.com/webtrac/web/"


async def discover_webtrac(page, debug_dir: str = "debug/webtrac") -> dict:
    """Diagnostic-only WebTrac probe."""
    out = Path(debug_dir)
    out.mkdir(parents=True, exist_ok=True)

    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except PlaywrightTimeoutError:
        pass
    await page.wait_for_timeout(3000)

    page_info = {"url": page.url, "title": await page.title()}

    controls = await page.locator(
        "select, input, button, textarea, [role='button'], [role='combobox']"
    ).evaluate_all(
        """els => els.map((e, i) => ({
            index: i,
            tag: e.tagName,
            type: e.getAttribute('type'),
            id: e.id,
            name: e.getAttribute('name'),
            role: e.getAttribute('role'),
            ariaLabel: e.getAttribute('aria-label'),
            placeholder: e.getAttribute('placeholder'),
            value: e.value ?? '',
            text: (e.innerText || e.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 500),
            visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length),
            disabled: !!e.disabled,
            options: e.tagName === 'SELECT'
                ? Array.from(e.options).map(o => ({
                    text: (o.textContent || '').trim(),
                    value: o.value,
                    selected: o.selected
                  }))
                : []
        }))"""
    )

    links = await page.locator("a").evaluate_all(
        """els => els.map((e, i) => ({
            index: i,
            text: (e.innerText || e.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 300),
            href: e.href,
            visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)
        }))"""
    )

    body_text = await page.locator("body").inner_text()
    (out / "page.html").write_text(await page.content(), encoding="utf-8")
    (out / "body.txt").write_text(body_text, encoding="utf-8")
    (out / "page_info.json").write_text(json.dumps(page_info, indent=2), encoding="utf-8")
    (out / "controls.json").write_text(json.dumps(controls, indent=2), encoding="utf-8")
    (out / "links.json").write_text(json.dumps(links, indent=2), encoding="utf-8")
    await page.screenshot(path=str(out / "page.png"), full_page=True)

    return {"page": page_info, "controls": controls, "links": links}


async def check_course(page, course: dict) -> list[dict]:
    """Compatibility wrapper used by the existing monitor."""
    result = await discover_webtrac(page, debug_dir=course.get("debug_dir", "debug/webtrac"))
    visible_controls = [c for c in result["controls"] if c.get("visible")]
    print(
        f"WEBTRAC DIAGNOSTIC: title={result['page']['title']!r} "
        f"url={result['page']['url']!r} visible_controls={len(visible_controls)} "
        f"links={len(result['links'])}"
    )
    for c in visible_controls:
        print(
            "WEBTRAC CONTROL "
            f"index={c['index']} tag={c['tag']} type={c['type']} "
            f"id={c['id']!r} name={c['name']!r} role={c['role']!r} "
            f"text={c['text']!r}"
        )
    return []
