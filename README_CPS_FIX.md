# CPS parser correction

This update replaces only the CPS adapter used for:
- Lincoln Homestead
- My Old Kentucky Home

What changed:
- Detects CPS tee-time cards rendered as links, buttons, clickable divs, or plain text with nearby booking/availability context.
- Waits for the CPS reservation grid to render before parsing.
- Treats the CPS search as already filtered for Player=4 and Hole=18.
- Rejects explicitly unavailable/full/sold-out slots.
- Performs a page-validity check so a blank/blocked/error page is treated as a scan failure instead of falsely producing a "No Tee Time" alert.
- Adds better diagnostics if diagnostic mode is enabled.

Replace only:
`adapters/cps.py`

Leave all other files unchanged.
