#!/usr/bin/env python3
"""
DOS Visa Bulletin -> USCIS chart selection -> India EB-1/EB-2/EB-3
movement monitor.

Checks:
1. DOS Visa Bulletin current month
2. USCIS adjustment-of-status chart selection
3. India EB-1, EB-2, EB-3 Final Action Dates
4. India EB-1, EB-2, EB-3 Dates for Filing
5. Month-over-month movement
6. Email alert when a new bulletin/chart determination is detected
"""

from __future__ import annotations

import io
import json
import os
import re
import smtplib
import sys
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

DOS_PAGE = (
    "https://travel.state.gov/content/travel/en/legal/visa-law0/"
    "visa-bulletin.html"
)
USCIS_PAGE = "https://www.uscis.gov/visabulletininfo"

STATE_FILE = Path(os.getenv("STATE_FILE", "visa_bulletin_state.json"))
TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "45"))

MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

HEADERS = {
    "User-Agent": "visa-bulletin-monitor/2.0 (+GitHub Actions)",
    "Accept": "text/html,application/xhtml+xml,application/pdf",
}


def get(url: str) -> requests.Response:
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return response


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_month_year(text: str) -> str | None:
    match = re.search(
        rf"({'|'.join(MONTHS)})\s*(20\d{{2}})", text, re.I
    )
    if not match:
        return None
    return f"{match.group(1).capitalize()} {match.group(2)}"


def find_dos_bulletin() -> dict:
    response = get(DOS_PAGE)
    soup = BeautifulSoup(response.text, "html.parser")
    page_text = normalize(soup.get_text(" ", strip=True))

    # Prefer the current bulletin link.
    current_area = page_text
    current = re.search(
        rf"Current Visa Bulletin.*?({'|'.join(MONTHS)})\s*(20\d{{2}})",
        current_area,
        re.I,
    )
    if not current:
        raise RuntimeError("Could not find current DOS Visa Bulletin month.")

    bulletin_id = f"{current.group(1).capitalize()} {current.group(2)}"

    # Look for the PDF associated with the bulletin month.
    pdf_links = []
    for a in soup.find_all("a", href=True):
        label = normalize(a.get_text(" ", strip=True))
        href = urljoin(response.url, a["href"])
        blob = f"{label} {href}"
        if bulletin_id.replace(" ", "") in blob.replace(" ", ""):
            if href.lower().endswith(".pdf"):
                pdf_links.append(href)

    # The archive link may be HTML that points to the PDF after another click.
    if not pdf_links:
        for a in soup.find_all("a", href=True):
            label = normalize(a.get_text(" ", strip=True))
            href = urljoin(response.url, a["href"])
            if (
                "visa bulletin" in label.lower()
                and bulletin_id.split()[0].lower() in label.lower()
                and bulletin_id.split()[1] in label
            ):
                pdf_links.append(href)

    if not pdf_links:
        # Known DOS PDF convention; only used as a fallback.
        month, year = bulletin_id.split()
        candidate = (
            f"https://travel.state.gov/content/dam/visas/Bulletins/"
            f"visabulletin_{month}{year}.pdf"
        )
        try:
            pdf_response = get(candidate)
            if "pdf" in pdf_response.headers.get("Content-Type", "").lower():
                pdf_url = candidate
            else:
                pdf_url = None
        except requests.RequestException:
            pdf_url = None
    else:
        pdf_url = pdf_links[0]

    if not pdf_url:
        raise RuntimeError(
            f"Found {bulletin_id}, but could not locate its DOS PDF."
        )

    return {
        "id": bulletin_id,
        "url": DOS_PAGE,
        "pdf_url": pdf_url,
    }


def extract_pdf_text(pdf_url: str) -> str:
    response = get(pdf_url)
    reader = PdfReader(io.BytesIO(response.content))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def find_employment_table(text: str, heading: str) -> str:
    idx = text.find(heading)
    if idx < 0:
        raise RuntimeError(f"Could not find DOS table heading: {heading}")
    return text[idx: idx + 12000]


def extract_india_dates(text: str, heading: str) -> dict:
    """
    Parse the DOS employment table. The PDF text extraction places the
    column headings before the rows, with INDIA as the third country column.

    We identify rows by category and read tokens after the category.
    For the standard table, the order is:
    All Chargeability, China, India, Mexico, Philippines.
    """
    table = find_employment_table(text, heading)

    result = {}
    patterns = {
        "EB-1": r"(?m)^\s*1st\s+(.+)$",
        "EB-2": r"(?m)^\s*2nd\s+(.+)$",
        "EB-3": r"(?m)^\s*3rd\s+(.+)$",
    }

    def clean_tokens(line: str) -> list[str]:
        return re.findall(
            r"(?:\d{2}[A-Z]{3}\d{2}|C|U)", line.upper()
        )

    for category, pattern in patterns.items():
        match = re.search(pattern, table)
        if not match:
            raise RuntimeError(
                f"Could not parse {category} from {heading}."
            )
        tokens = clean_tokens(match.group(1))
        if len(tokens) < 3:
            raise RuntimeError(
                f"Unexpected {category} row format in {heading}: {tokens}"
            )
        result[category] = tokens[2]  # India column

    return result


def parse_date_token(value: str) -> date | None:
    value = value.upper()
    if value in {"C", "U"}:
        return None
    return datetime.strptime(value, "%d%b%y").date()


def movement(old: str | None, new: str) -> str:
    if old is None:
        return "NEW"

    if old == new:
        return "NO CHANGE"

    if new == "C":
        return f"{old} -> C"
    if new == "U":
        return f"{old} -> U"
    if old == "C":
        return f"C -> {new}"
    if old == "U":
        return f"U -> {new}"

    old_date = parse_date_token(old)
    new_date = parse_date_token(new)
    if old_date and new_date:
        delta = (new_date - old_date).days
        if delta > 0:
            return f"+{delta} days ({old} -> {new})"
        return f"{delta} days ({old} -> {new})"

    return f"{old} -> {new}"


def find_uscis_chart_selection() -> dict:
    """
    USCIS is authoritative for which chart adjustment-of-status applicants
    should use. The page structure can change, so the parser searches the
    visible page text for the current month and employment-based language.

    We intentionally fail closed if no employment-based determination can be
    identified rather than guessing.
    """
    response = get(USCIS_PAGE)
    soup = BeautifulSoup(response.text, "html.parser")
    text = normalize(soup.get_text(" ", strip=True))

    month_year = parse_month_year(text)

    # Common USCIS wording variants.
    final_patterns = [
        r"employment[- ]based.*?(?:must use|use).*?final action dates",
        r"employment[- ]based.*?final action dates",
        r"final action dates.*?employment[- ]based",
    ]
    filing_patterns = [
        r"employment[- ]based.*?(?:may use|use).*?dates for filing",
        r"employment[- ]based.*?dates for filing",
        r"dates for filing.*?employment[- ]based",
    ]

    final_found = any(
        re.search(p, text, re.I) for p in final_patterns
    )
    filing_found = any(
        re.search(p, text, re.I) for p in filing_patterns
    )

    if filing_found and not final_found:
        chart = "Dates for Filing"
    elif final_found and not filing_found:
        chart = "Final Action Dates"
    elif final_found and filing_found:
        # Prefer the explicit "must use" statement when both occur on page.
        must_final = re.search(
            r"employment[- ]based.*?must use.*?final action dates",
            text,
            re.I,
        )
        must_filing = re.search(
            r"employment[- ]based.*?must use.*?dates for filing",
            text,
            re.I,
        )
        if must_final and not must_filing:
            chart = "Final Action Dates"
        elif must_filing and not must_final:
            chart = "Dates for Filing"
        else:
            raise RuntimeError(
                "USCIS page mentions both employment-based charts but "
                "does not expose a clear current selection."
            )
    else:
        raise RuntimeError(
            "Could not determine the USCIS employment-based chart selection."
        )

    return {
        "month": month_year,
        "employment_based_chart": chart,
        "url": USCIS_PAGE,
    }


def load_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )


def email_body(current: dict, previous: dict | None) -> str:
    lines = [
        "New Visa Bulletin detected",
        "",
        f"DOS Bulletin: {current['bulletin']['id']}",
        f"USCIS employment-based chart: "
        f"{current['uscis']['employment_based_chart']}",
        "",
        "India employment-based dates:",
        "",
        "Category | Final Action | Filing | Movement vs previous",
        "-------- | ------------ | ------ | --------------------",
    ]

    for category in ("EB-1", "EB-2", "EB-3"):
        final_date = current["dates"]["final_action"][category]
        filing_date = current["dates"]["filing"][category]

        old_final = None
        old_filing = None
        if previous:
            old_final = previous.get("dates", {}).get(
                "final_action", {}
            ).get(category)
            old_filing = previous.get("dates", {}).get(
                "filing", {}
            ).get(category)

        lines.append(
            f"{category} | {final_date} | {filing_date} | "
            f"Final: {movement(old_final, final_date)}; "
            f"Filing: {movement(old_filing, filing_date)}"
        )

    lines += [
        "",
        f"DOS: {DOS_PAGE}",
        f"DOS PDF: {current['bulletin']['pdf_url']}",
        f"USCIS: {USCIS_PAGE}",
        "",
        "This is an informational monitoring alert, not legal advice.",
    ]
    return "\n".join(lines)


def send_email(current: dict, previous: dict | None) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.environ["SMTP_USERNAME"]
    password = os.environ["SMTP_PASSWORD"]
    sender = os.getenv("EMAIL_FROM", username)
    recipient = os.environ["EMAIL_TO"]

    msg = EmailMessage()
    msg["Subject"] = (
        f"Visa Bulletin {current['bulletin']['id']} — "
        f"India EB-1/EB-2/EB-3 update"
    )
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(email_body(current, previous))

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(username, password)
        smtp.send_message(msg)


def build_current_state() -> dict:
    bulletin = find_dos_bulletin()
    pdf_text = extract_pdf_text(bulletin["pdf_url"])

    final_action = extract_india_dates(
        pdf_text,
        "A. Final Action Dates for Employment-Based Preference Cases",
    )
    filing = extract_india_dates(
        pdf_text,
        "B. Dates for Filing of Employment-Based Visa Applications",
    )

    uscis = find_uscis_chart_selection()

    return {
        "checked_at_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "bulletin": bulletin,
        "uscis": uscis,
        "dates": {
            "final_action": final_action,
            "filing": filing,
        },
    }


def main() -> int:
    try:
        current = build_current_state()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    previous = load_state()

    print(json.dumps(current, indent=2))

    # Treat a changed bulletin as the primary trigger.
    bulletin_changed = (
        previous is None
        or previous.get("bulletin", {}).get("id")
        != current["bulletin"]["id"]
    )

    # Also alert if USCIS changes its chart determination for the same month.
    chart_changed = (
        previous is not None
        and previous.get("uscis", {}).get("employment_based_chart")
        != current["uscis"]["employment_based_chart"]
    )

    if previous is None:
        save_state(current)
        if os.getenv("ALERT_ON_FIRST_RUN", "false").lower() == "true":
            send_email(current, None)
            print("First run: alert sent.")
        else:
            print("First run: state saved; no alert.")
        return 0

    if not bulletin_changed and not chart_changed:
        print("No new bulletin or USCIS chart-selection change.")
        return 0

    send_email(current, previous)
    save_state(current)
    print("Alert sent and state updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
