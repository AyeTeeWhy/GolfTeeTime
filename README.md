# Local WebTrac Test

This is a local-only diagnostic for Lexington's WebTrac golf tee-time system.

Why local? GitHub Actions was blocked by the site's Cloudflare layer while requesting WebTrac search URLs. This test runs from your normal Windows PC and uses an installed Edge/Chrome browser when available.

## Setup

From the repo root:

```powershell
pip install -r requirements.txt
playwright install chromium
```

## Run

From the repo root:

```powershell
python local_webtrac_test/webtrac_local.py --course both
```

To test only one course:

```powershell
python local_webtrac_test/webtrac_local.py --course Picadome
python local_webtrac_test/webtrac_local.py --course Lakeside
```

To test a specific Saturday/Sunday:

```powershell
python local_webtrac_test/webtrac_local.py --course both --date 09/19/2026
```

The browser is intentionally visible. If the site presents a normal browser verification/challenge, you can complete it manually during the test. The script saves HTML, text, and screenshots under `local_webtrac_test/debug/` whenever it cannot find a control or a matching result.

## Scope

The test only looks for:
- Saturday/Sunday
- 7:00 AM through 8:59 AM
- 4-player availability
- 18-hole tee times

It does not book or add anything to a cart.
