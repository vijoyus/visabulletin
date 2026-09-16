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

Important:
- DOS retrieval does NOT depend exclusively on the DOS archive/index page.
- The script probes the official monthly bulletin URL directly.
- The PDF is downloaded from the official travel.state.gov domain.
- If DOS returns HTTP 403, the script reports the problem clearly.
"""

from __future__ import annotations

import io
import json
import os
import re
import smtplib
import sys
import time

from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DOS_PAGE = (
    "https://travel.state.gov/content/travel/en/legal/visa-law0/"
    "visa-bulletin.html"
)

USCIS_PAGE = "https://www.uscis.gov/visabulletininfo"

STATE_FILE = Path(
    os.getenv("STATE_FILE", "visa_bulletin_state.json")
)

TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "45"))

RETRIES = int(os.getenv("HTTP_RETRIES", "3"))

RETRY_DELAY = float(
    os.getenv("HTTP_RETRY_DELAY", "2")
)

MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

class WebsiteBlockedError(RuntimeError):
    """Raised when a site explicitly blocks the request."""


def get(
    url: str,
    *,
    accept: str | None = None,
) -> requests.Response:
    """
    GET a URL with retries.

    HTTP 403 is treated separately because retrying a permanent WAF block
    usually does not help.
    """

    headers = {}

    if accept:
        headers["Accept"] = accept

    last_error = None

    for attempt in range(1, RETRIES + 1):

        try:
            response = SESSION.get(
                url,
                headers=headers,
                timeout=TIMEOUT,
                allow_redirects=True,
            )

            if response.status_code == 403:
                raise WebsiteBlockedError(
                    f"HTTP 403 Forbidden from {url}"
                )

            if response.status_code in {
                429,
                500,
                502,
                503,
                504,
            }:
                if attempt < RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
                    continue

            response.raise_for_status()

            return response

        except WebsiteBlockedError:
            raise

        except requests.RequestException as exc:
            last_error = exc

            if attempt < RETRIES:
                time.sleep(RETRY_DELAY * attempt)
                continue

    raise RuntimeError(
        f"Unable to retrieve {url} after {RETRIES} attempts: "
        f"{last_error}"
    )


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_month_year(text: str) -> str | None:
    match = re.search(
        rf"({'|'.join(MONTHS)})\s*(20\d{{2}})",
        text,
        re.I,
    )

    if not match:
        return None

    return (
        f"{match.group(1).capitalize()} "
        f"{match.group(2)}"
    )


def month_year_from_date(year: int, month: int) -> tuple[int, int]:
    """
    Return normalized year/month after applying a month offset.
    """

    if month < 1:
        return year - 1, month + 12

    if month > 12:
        return year + 1, month - 12

    return year, month


# ---------------------------------------------------------------------------
# DOS bulletin URL generation
# ---------------------------------------------------------------------------

def dos_bulletin_html_url(
    month_name: str,
    year: int,
) -> str:
    """
    Official monthly Visa Bulletin HTML URL.

    Example:

    https://travel.state.gov/content/travel/en/legal/visa-law0/
    visa-bulletin/2026/visa-bulletin-for-september-2026.html
    """

    return (
        "https://travel.state.gov/content/travel/en/legal/"
        "visa-law0/visa-bulletin/"
        f"{year}/visa-bulletin-for-{month_name.lower()}-{year}.html"
    )


def dos_bulletin_pdf_url(
    month_name: str,
    year: int,
) -> str:
    """
    Official DOS PDF URL convention.
    """

    return (
        "https://travel.state.gov/content/dam/visas/"
        "Bulletins/"
        f"visabulletin_{month_name}{year}.pdf"
    )


# ---------------------------------------------------------------------------
# Find DOS bulletin without depending on the archive page
# ---------------------------------------------------------------------------

def candidate_bulletin_months() -> list[tuple[int, int]]:
    """
    Generate likely bulletin months.

    We try:

    1. Next month
    2. Current month
    3. Previous months

    This allows the monitor to discover a newly released bulletin
    before the month actually starts.
    """

    today = date.today()

    candidates = []

    # Next month first
    for offset in [1, 0, -1, -2, -3]:

        year, month = month_year_from_date(
            today.year,
            today.month + offset,
        )

        candidates.append((year, month))

    return candidates


def find_dos_bulletin() -> dict:
    """
    Find the newest available official DOS Visa Bulletin.

    We intentionally do not require the main DOS archive page to work.

    The DOS site currently exposes monthly pages such as:

    /visa-bulletin/2026/visa-bulletin-for-september-2026.html

    and corresponding official PDFs.
    """

    errors = []

    for year, month_number in candidate_bulletin_months():

        month_name = MONTHS[month_number - 1]

        html_url = dos_bulletin_html_url(
            month_name,
            year,
        )

        pdf_url = dos_bulletin_pdf_url(
            month_name,
            year,
        )

        # ---------------------------------------------------------------
        # Try the official monthly HTML page
        # ---------------------------------------------------------------

        try:
            response = get(
                html_url,
                accept=(
                    "text/html,"
                    "application/xhtml+xml,"
                    "application/xml"
                ),
            )

            soup = BeautifulSoup(
                response.text,
                "html.parser",
            )

            page_text = normalize(
                soup.get_text(" ", strip=True)
            )

            detected_id = parse_month_year(page_text)

            if detected_id is None:
                detected_id = f"{month_name} {year}"

            # Look for a PDF link on the bulletin page.
            discovered_pdf = None

            for anchor in soup.find_all(
                "a",
                href=True,
            ):

                href = urljoin(
                    response.url,
                    anchor["href"],
                )

                label = normalize(
                    anchor.get_text(
                        " ",
                        strip=True,
                    )
                )

                combined = (
                    f"{label} {href}"
                ).lower()

                if (
                    ".pdf" in combined
                    or "printer" in combined
                ):
                    if (
                        "travel.state.gov" in href
                        and ".pdf" in href.lower()
                    ):
                        discovered_pdf = href
                        break

            if discovered_pdf is None:
                discovered_pdf = pdf_url

            return {
                "id": detected_id,
                "url": html_url,
                "pdf_url": discovered_pdf,
            }

        except WebsiteBlockedError as exc:
            errors.append(
                f"{html_url}: {exc}"
            )

        except requests.RequestException as exc:
            errors.append(
                f"{html_url}: {exc}"
            )

        except RuntimeError as exc:
            errors.append(
                f"{html_url}: {exc}"
            )

    # -------------------------------------------------------------------
    # Last-resort discovery through the main DOS page.
    # -------------------------------------------------------------------

    try:
        response = get(
            DOS_PAGE,
            accept="text/html",
        )

        soup = BeautifulSoup(
            response.text,
            "html.parser",
        )

        page_text = normalize(
            soup.get_text(" ", strip=True)
        )

        current_match = re.search(
            rf"Current Visa Bulletin.*?"
            rf"({'|'.join(MONTHS)})\s*(20\d{{2}})",
            page_text,
            re.I,
        )

        if current_match:

            month_name = (
                current_match.group(1).capitalize()
            )

            year = int(
                current_match.group(2)
            )

            bulletin_id = (
                f"{month_name} {year}"
            )

            discovered_pdf = None

            for anchor in soup.find_all(
                "a",
                href=True,
            ):

                href = urljoin(
                    response.url,
                    anchor["href"],
                )

                label = normalize(
                    anchor.get_text(
                        " ",
                        strip=True,
                    )
                )

                blob = (
                    f"{label} {href}"
                ).lower()

                if (
                    month_name.lower() in blob
                    and str(year) in blob
                    and ".pdf" in href.lower()
                ):
                    discovered_pdf = href
                    break

            if discovered_pdf is None:
                discovered_pdf = dos_bulletin_pdf_url(
                    month_name,
                    year,
                )

            return {
                "id": bulletin_id,
                "url": DOS_PAGE,
                "pdf_url": discovered_pdf,
            }

    except Exception as exc:
        errors.append(
            f"{DOS_PAGE}: {exc}"
        )

    error_text = "\n".join(
        f"  - {error}"
        for error in errors
    )

    raise RuntimeError(
        "Could not retrieve an official DOS Visa Bulletin.\n"
        "The travel.state.gov server may be blocking automated "
        "requests with HTTP 403.\n\n"
        "Attempted URLs:\n"
        f"{error_text}"
    )


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------

def extract_pdf_text(pdf_url: str) -> str:
    """
    Download and extract text from the official DOS PDF.
    """

    response = get(
        pdf_url,
        accept="application/pdf,*/*",
    )

    content_type = (
        response.headers
        .get("Content-Type", "")
        .lower()
    )

    if (
        "pdf" not in content_type
        and not response.content.startswith(b"%PDF")
    ):
        raise RuntimeError(
            "DOS PDF URL did not return a PDF. "
            f"Content-Type={content_type}"
        )

    reader = PdfReader(
        io.BytesIO(response.content)
    )

    pages = []

    for page in reader.pages:
        pages.append(
            page.extract_text() or ""
        )

    text = "\n".join(pages)

    if not text.strip():
        raise RuntimeError(
            f"Could not extract text from DOS PDF: {pdf_url}"
        )

    return text


# ---------------------------------------------------------------------------
# DOS employment tables
# ---------------------------------------------------------------------------

def find_employment_table(
    text: str,
    heading: str,
) -> str:

    idx = text.find(heading)

    if idx < 0:

        # PDF extraction sometimes changes spacing/capitalization.
        normalized_text = normalize(text)
        normalized_heading = normalize(heading)

        idx = normalized_text.lower().find(
            normalized_heading.lower()
        )

    if idx < 0:
        raise RuntimeError(
            f"Could not find DOS table heading: {heading}"
        )

    return text[idx: idx + 12000]


def extract_india_dates(
    text: str,
    heading: str,
) -> dict:
    """
    Parse:

    All Chargeability
    China
    India
    Mexico
    Philippines

    and return the India column.
    """

    table = find_employment_table(
        text,
        heading,
    )

    result = {}

    patterns = {
        "EB-1": r"(?m)^\s*1st\s+(.+)$",
        "EB-2": r"(?m)^\s*2nd\s+(.+)$",
        "EB-3": r"(?m)^\s*3rd\s+(.+)$",
    }

    token_pattern = (
        r"(?:\d{2}[A-Z]{3}\d{2}|C|U)"
    )

    for category, pattern in patterns.items():

        match = re.search(
            pattern,
            table,
        )

        if not match:

            # Try a more permissive search when PDF
            # extraction wraps the row.
            fallback = re.search(
                rf"\b{category.replace('EB-', '')}"
                rf"(?:st|nd|rd)\b(.{{0,500}})",
                table,
                re.I | re.S,
            )

            if fallback:
                row_text = fallback.group(1)
            else:
                raise RuntimeError(
                    f"Could not parse {category} "
                    f"from {heading}."
                )

        else:
            row_text = match.group(1)

        tokens = re.findall(
            token_pattern,
            row_text.upper(),
        )

        if len(tokens) < 3:
            raise RuntimeError(
                f"Unexpected {category} row format "
                f"in {heading}: {tokens}"
            )

        # Standard DOS table:
        #
        # [All Chargeability, China, India, Mexico, Philippines]
        #
        result[category] = tokens[2]

    return result


# ---------------------------------------------------------------------------
# Date/movement helpers
# ---------------------------------------------------------------------------

def parse_date_token(
    value: str | None,
) -> date | None:

    if value is None:
        return None

    value = value.upper().strip()

    if value in {"C", "U"}:
        return None

    return datetime.strptime(
        value,
        "%d%b%y",
    ).date()


def movement(
    old: str | None,
    new: str,
) -> str:

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

        delta = (
            new_date - old_date
        ).days

        if delta > 0:
            return (
                f"+{delta} days "
                f"({old} -> {new})"
            )

        if delta < 0:
            return (
                f"{delta} days "
                f"({old} -> {new})"
            )

        return "NO CHANGE"

    return f"{old} -> {new}"


# ---------------------------------------------------------------------------
# USCIS chart selection
# ---------------------------------------------------------------------------

def find_uscis_chart_selection() -> dict:
    """
    USCIS is authoritative for the adjustment-of-status chart selection.

    Fail closed if the page does not provide a clear determination.
    """

    response = get(
        USCIS_PAGE,
        accept="text/html",
    )

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    text = normalize(
        soup.get_text(" ", strip=True)
    )

    month_year = parse_month_year(text)

    # Explicit language indicating Final Action Dates.
    final_patterns = [
        r"employment[- ]based.*?"
        r"(?:must use|use).*?"
        r"final action dates",

        r"employment[- ]based.*?"
        r"final action dates",

        r"final action dates.*?"
        r"employment[- ]based",
    ]

    # Explicit language indicating Dates for Filing.
    filing_patterns = [
        r"employment[- ]based.*?"
        r"(?:may use|use).*?"
        r"dates for filing",

        r"employment[- ]based.*?"
        r"dates for filing",

        r"dates for filing.*?"
        r"employment[- ]based",
    ]

    final_found = any(
        re.search(
            pattern,
            text,
            re.I,
        )
        for pattern in final_patterns
    )

    filing_found = any(
        re.search(
            pattern,
            text,
            re.I,
        )
        for pattern in filing_patterns
    )

    # Look for the strongest "must use" statement.
    must_final = re.search(
        r"employment[- ]based.*?"
        r"must use.*?"
        r"final action dates",
        text,
        re.I,
    )

    must_filing = re.search(
        r"employment[- ]based.*?"
        r"must use.*?"
        r"dates for filing",
        text,
        re.I,
    )

    if must_final and not must_filing:

        chart = "Final Action Dates"

    elif must_filing and not must_final:

        chart = "Dates for Filing"

    elif final_found and not filing_found:

        chart = "Final Action Dates"

    elif filing_found and not final_found:

        chart = "Dates for Filing"

    else:

        raise RuntimeError(
            "Could not determine the USCIS employment-based "
            "chart selection without ambiguity."
        )

    return {
        "month": month_year,
        "employment_based_chart": chart,
        "url": USCIS_PAGE,
    }


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> dict | None:

    if not STATE_FILE.exists():
        return None

    try:

        return json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )

    except (
        OSError,
        json.JSONDecodeError,
    ):

        return None


def save_state(state: dict) -> None:

    STATE_FILE.write_text(
        json.dumps(
            state,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def email_body(
    current: dict,
    previous: dict | None,
) -> str:

    lines = [
        "New Visa Bulletin detected",
        "",
        f"DOS Bulletin: "
        f"{current['bulletin']['id']}",

        f"USCIS employment-based chart: "
        f"{current['uscis']['employment_based_chart']}",

        "",
        "India employment-based dates:",
        "",

        "Category | Final Action | Filing | Movement",
        "-------- | ------------ | ------ | ---------",
    ]

    for category in (
        "EB-1",
        "EB-2",
        "EB-3",
    ):

        final_date = (
            current["dates"]
            ["final_action"]
            [category]
        )

        filing_date = (
            current["dates"]
            ["filing"]
            [category]
        )

        old_final = None
        old_filing = None

        if previous:

            old_final = (
                previous
                .get("dates", {})
                .get("final_action", {})
                .get(category)
            )

            old_filing = (
                previous
                .get("dates", {})
                .get("filing", {})
                .get(category)
            )

        lines.append(
            f"{category} | "
            f"{final_date} | "
            f"{filing_date} | "
            f"Final: {movement(old_final, final_date)}; "
            f"Filing: {movement(old_filing, filing_date)}"
        )

    lines += [
        "",
        f"DOS: {DOS_PAGE}",
        f"DOS Bulletin: {current['bulletin']['url']}",
        f"DOS PDF: {current['bulletin']['pdf_url']}",
        f"USCIS: {USCIS_PAGE}",
        "",
        "This is an informational monitoring alert, "
        "not legal advice.",
    ]

    return "\n".join(lines)


def send_email(
    current: dict,
    previous: dict | None,
) -> None:

    host = os.environ["SMTP_HOST"]

    port = int(
        os.getenv(
            "SMTP_PORT",
            "587",
        )
    )

    username = os.environ[
        "SMTP_USERNAME"
    ]

    password = os.environ[
        "SMTP_PASSWORD"
    ]

    sender = os.getenv(
        "EMAIL_FROM",
        username,
    )

    recipient = os.environ[
        "EMAIL_TO"
    ]

    msg = EmailMessage()

    msg["Subject"] = (
        f"Visa Bulletin "
        f"{current['bulletin']['id']} — "
        f"India EB-1/EB-2/EB-3 update"
    )

    msg["From"] = sender
    msg["To"] = recipient

    msg.set_content(
        email_body(
            current,
            previous,
        )
    )

    with smtplib.SMTP(
        host,
        port,
        timeout=30,
    ) as smtp:

        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()

        smtp.login(
            username,
            password,
        )

        smtp.send_message(msg)


# ---------------------------------------------------------------------------
# Build current state
# ---------------------------------------------------------------------------

def build_current_state() -> dict:

    bulletin = find_dos_bulletin()

    print(
        f"Using DOS bulletin: "
        f"{bulletin['id']}"
    )

    print(
        f"DOS PDF: "
        f"{bulletin['pdf_url']}"
    )

    pdf_text = extract_pdf_text(
        bulletin["pdf_url"]
    )

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
        "checked_at_utc": (
            datetime.utcnow()
            .isoformat(
                timespec="seconds"
            )
            + "Z"
        ),

        "bulletin": bulletin,

        "uscis": uscis,

        "dates": {
            "final_action": final_action,
            "filing": filing,
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:

    print(
        "Visa Bulletin monitor starting..."
    )

    try:

        current = build_current_state()

    except WebsiteBlockedError as exc:

        print(
            "",
            file=sys.stderr,
        )

        print(
            "ERROR: travel.state.gov "
            "blocked the HTTP request.",
            file=sys.stderr,
        )

        print(
            str(exc),
            file=sys.stderr,
        )

        print(
            "",
            file=sys.stderr,
        )

        print(
            "Try running this with a modern Python/OpenSSL "
            "environment and verify that the DOS site is "
            "reachable from this network.",
            file=sys.stderr,
        )

        return 2

    except Exception as exc:

        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        return 1

    previous = load_state()

    print(
        json.dumps(
            current,
            indent=2,
        )
    )

    # ---------------------------------------------------------------
    # Detect new bulletin
    # ---------------------------------------------------------------

    bulletin_changed = (
        previous is None
        or previous
        .get("bulletin", {})
        .get("id")
        != current["bulletin"]["id"]
    )

    # ---------------------------------------------------------------
    # Detect USCIS chart change
    # ---------------------------------------------------------------

    chart_changed = (
        previous is not None
        and previous
        .get("uscis", {})
        .get("employment_based_chart")
        != current["uscis"]
        .get("employment_based_chart")
    )

    # ---------------------------------------------------------------
    # First run
    # ---------------------------------------------------------------

    if previous is None:

        save_state(current)

        if (
            os.getenv(
                "ALERT_ON_FIRST_RUN",
                "false",
            ).lower()
            == "true"
        ):

            send_email(
                current,
                None,
            )

            print(
                "First run: alert sent."
            )

        else:

            print(
                "First run: state saved; "
                "no alert."
            )

        return 0

    # ---------------------------------------------------------------
    # Nothing changed
    # ---------------------------------------------------------------

    if (
        not bulletin_changed
        and not chart_changed
    ):

        print(
            "No new bulletin or USCIS "
            "chart-selection change."
        )

        return 0

    # ---------------------------------------------------------------
    # Change detected
    # ---------------------------------------------------------------

    send_email(
        current,
        previous,
    )

    save_state(
        current
    )

    print(
        "Alert sent and state updated."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
