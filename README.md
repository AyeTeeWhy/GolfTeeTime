# Wessinger Hills GitHub Tee-Time Alerts

Rules:
- Wessinger Hills / TeeQuest
- Sunday only
- Next 7 days
- 7:00 AM through 8:59 AM
- 4 golfers
- Hourly workflow with actual scans every 2 hours from 6 AM through 8 PM Eastern
- ntfy notification for every scan with qualifying availability
- "No Tee Time" notification when no qualifying availability exists

## Required GitHub secret

Repository Settings -> Secrets and variables -> Actions -> New repository secret

Name:
NTFY_TOPIC

Value:
your ntfy topic

## Manual test

Actions -> Golf Tee Time Alerts -> Run workflow

Check `force_scan` to run immediately even if the current time is outside the normal 6 AM-8 PM scan window.
