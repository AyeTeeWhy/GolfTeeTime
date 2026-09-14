# Golf Tee Time Alerts

Purpose: alert when a new tee time becomes available for **4 golfers** on **Saturday or Sunday between 7:00 AM and 9:00 AM ET**.

## Current course list

- My Old Kentucky Home — pending platform verification
- Weissinger Hills — TeeQuest (active)
- Cherry Blossom — pending platform verification
- Maywood CC — pending platform verification
- Gibson Bay — foreUP (pending adapter)
- Connemara — TeeItUp (pending adapter)
- Rosewood CC — pending platform verification
- Thoroughbred — pending platform verification
- Picadome — WebTrac (active)
- Lakeside — WebTrac (active)
- Quail Chase — GolfBack (pending adapter)

## Notifications

The project supports:

### ntfy push notifications

1. Install the ntfy app on your phone.
2. Pick a long random topic name that nobody else can guess.
3. Add it to GitHub as a repository secret named `NTFY_TOPIC`.
4. Subscribe your phone to that topic.

### Email

Add these GitHub repository secrets:

- `SMTP_HOST`
- `SMTP_PORT` (normally `587`)
- `SMTP_USER`
- `SMTP_PASSWORD`
- `ALERT_EMAIL_TO`

## Scheduling

The included GitHub Action runs every 5 minutes. GitHub Actions does not provide a reliable 1–2 minute scheduled cadence. If we later want faster checks, we can move the exact same Python process to a small always-on host or trigger it from cron-job.org.

## Important

The first version uses Playwright so the checker can work with JavaScript-heavy booking pages. WebTrac and TeeQuest are enabled first. The remaining platforms are deliberately disabled until their public tee-time interfaces are mapped and tested.
