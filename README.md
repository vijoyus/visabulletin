# Visa Bulletin Monitor — DOS + USCIS + India EB Dates

Automated GitHub Actions monitor for the U.S. immigration Visa Bulletin.

It connects these pieces:

**DOS Visa Bulletin → USCIS chart selection → India EB-1/EB-2/EB-3 dates → month-over-month movement → email alert**

## What it monitors

### 1. Department of State

The monitor checks the official DOS Visa Bulletin page and identifies the current bulletin.

Source:

https://travel.state.gov/content/travel/en/legal/visa-law0/visa-bulletin.html

It then downloads the corresponding DOS PDF and extracts:

- EB-1 India Final Action Date
- EB-2 India Final Action Date
- EB-3 India Final Action Date
- EB-1 India Dates for Filing
- EB-2 India Dates for Filing
- EB-3 India Dates for Filing

### 2. USCIS chart selection

The monitor separately checks:

https://www.uscis.gov/visabulletininfo

For employment-based adjustment of status, it records whether USCIS says to use:

- **Final Action Dates**, or
- **Dates for Filing**

The program fails closed if it cannot confidently determine the current USCIS selection. It does not guess.

### 3. Month-over-month movement

The previous result is stored in:

```text
visa_bulletin_state.json
```

For each India category the email reports movement such as:

```text
+31 days
-45 days
NO CHANGE
C -> 15JAN25
15JAN25 -> U
```

This makes retrogression and advancement visible immediately.

## Example email

```text
Subject:
Visa Bulletin October 2026 — India EB-1/EB-2/EB-3 update

New Visa Bulletin detected

DOS Bulletin: October 2026
USCIS employment-based chart: Final Action Dates

India employment-based dates:

Category | Final Action | Filing | Movement vs previous
-------- | ------------ | ------ | --------------------
EB-1 | 15OCT22 | 01DEC23 | Final: +30 days; Filing: NO CHANGE
EB-2 | 01SEP13 | 15JAN15 | Final: NO CHANGE; Filing: +14 days
EB-3 | 01JAN14 | 15JAN15 | Final: +31 days; Filing: NO CHANGE

DOS: ...
USCIS: ...

This is an informational monitoring alert, not legal advice.
```

The dates above are only an illustrative email format. The program obtains the actual dates from the live DOS bulletin.

## Current September 2026 structure

The DOS September 2026 bulletin explicitly says that, unless USCIS indicates otherwise, adjustment-of-status applicants use the Final Action Dates charts; it also directs applicants to USCIS for any determination allowing use of Dates for Filing.

For September 2026, the DOS employment-based tables include separate Final Action and Dates for Filing tables with India as a chargeability column.

## Setup

### Clone

```bash
git clone https://github.com/YOUR_USERNAME/visa-bulletin-monitor.git
cd visa-bulletin-monitor
```

### Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Test locally

```bash
python visa_bulletin_monitor.py
```

The first run stores the current state without sending an email.

To test an email on the first run:

```bash
ALERT_ON_FIRST_RUN=true python visa_bulletin_monitor.py
```

## SMTP configuration

Set:

```bash
export SMTP_HOST="smtp.gmail.com"
export SMTP_PORT="587"
export SMTP_USERNAME="your-email@gmail.com"
export SMTP_PASSWORD="your-app-password"
export EMAIL_TO="your-email@gmail.com"
```

For Gmail, use a Google App Password when applicable. Never commit credentials to Git.

## GitHub Actions setup

The repository contains:

```text
.github/workflows/monitor.yml
```

The workflow runs once daily and can also be started manually.

### Add repository secrets

GitHub:

**Settings → Secrets and variables → Actions**

Add:

```text
SMTP_HOST
SMTP_PORT
SMTP_USERNAME
SMTP_PASSWORD
EMAIL_TO
```

Example:

```text
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-email@gmail.com
SMTP_PASSWORD=xxxxxxxxxxxxxxxx
EMAIL_TO=your-email@gmail.com
```

The workflow has:

```yaml
permissions:
  contents: write
```

because it commits `visa_bulletin_state.json` back to the repository.

## Why both DOS and USCIS are monitored

The DOS bulletin contains the underlying visa-number tables, but USCIS determines which chart adjustment-of-status applicants should use for filing in a particular month.

USCIS guidance explains that applicants should check the USCIS Visa Bulletin information page to determine whether to use Final Action Dates or Dates for Filing.

Therefore, this project does not assume that the DOS Dates for Filing table is automatically the chart available for an I-485 filing.

## Fail-closed behavior

This is intentionally conservative.

If:

- DOS changes its HTML structure,
- the bulletin PDF cannot be found,
- the PDF table cannot be parsed,
- USCIS changes its page structure, or
- USCIS does not expose a clear employment-based chart selection,

the workflow exits with an error rather than sending an alert containing guessed data.

## Files

```text
visa-bulletin-monitor/
├── .github/
│   └── workflows/
│       └── monitor.yml
├── tests/
├── visa_bulletin_monitor.py
├── requirements.txt
├── README.md
└── .gitignore
```

## Future enhancements

Possible additions:

- Store every month's result in CSV/JSON rather than only the previous month.
- Generate a historical India EB-1/EB-2/EB-3 movement chart.
- Add a personal priority date and alert when it becomes current.
- Add EB-5 India monitoring.
- Add separate alerts for retrogression.
- Send Slack or Teams notifications.
- Monitor family-based categories.
- Keep a GitHub Pages dashboard.

## Disclaimer

This project is an automated information monitor. It is not legal advice. Immigration filing eligibility can depend on additional facts and USCIS instructions.
