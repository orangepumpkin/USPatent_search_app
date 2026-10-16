import json
import re
import time
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup


st.set_page_config(
    page_title="Patent Portfolio Explorer",
    page_icon="🔎",
    layout="wide",
)

BASE = "https://api.uspto.gov/api/v1/patent/applications"

COLUMNS = [
    "Publication Number",
    "Publication Date",
    "Publication Number Verification",
    "Application Number",
    "Application Number - API",
    "Application Date",
    "Title",
    "Applicant",
    "Original Assignee",
    "Original Assignee Verification",
    "Current Assignee",
    "Current Assignee Candidate",
    "Current Assignee Verification",
    "Ownership Recording Date",
    "Historical Assignment Recipients",
    "All Recorded Assignor Names",
    "Assignment Events Retrieved",
    "Assignment Retrieval Check",
    "Abstract",
    "Abstract Source URL",
    "Abstract Retrieval",
    "Alive / Active",
    "Legal Status - Source",
    "Application Status",
    "Application Status Code",
    "Application Status Date",
    "Application Type",
    "US Entity Status",
    "IPC Subgroups",
    "CPC Subgroups",
    "US Classifications",
    "All Complete US IDs Found",
    "Ownership / Legal Status Source",
    "Metadata Retrieved UTC",
    "Enrichment Retrieved UTC",
    "Notices",
]


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def as_list(value):
    if isinstance(value, list):
        return value
    return [value] if isinstance(value, dict) else []


def api_call(path, key, body=None, params=None):
    headers = {
        "X-API-KEY": key,
        "Accept": "application/json",
    }

    for attempt in range(4):
        response = requests.request(
            "POST" if body is not None else "GET",
            BASE + path,
            headers=headers,
            json=body,
            params=params,
            timeout=(15, 60),
        )

        if response.status_code in (429, 500, 502, 503, 504):
            if attempt < 3:
                response.close()
                time.sleep(2 ** (attempt + 1))
                continue

        if not response.ok:
            detail = response.text.replace(key, "[REDACTED]")
            detail = re.sub(r"<[^>]+>", " ", detail)
            status = response.status_code
            response.close()
            raise RuntimeError(
                f"USPTO HTTP {status}: {detail[:500]}"
            )

        try:
            return response.json()
        finally:
            response.close()


def extract_events(value):
    events = []

    if isinstance(value, dict):
        for field, child in value.items():
            if field == "assignmentBag":
                events.extend(as_list(child))
            else:
                events.extend(extract_events(child))

    elif isinstance(value, list):
        for child in value:
            events.extend(extract_events(child))

    return events


def party_names(event, side):
    fields = (
        ["assigneeNameText"]
        if side == "assignee"
        else ["assignorName", "assignorNameText"]
    )

    names = []

    for party in as_list(event.get(side + "Bag")):
        for field in fields:
            if party.get(field):
                names.append(str(party[field]).strip())
                break

    return sorted(set(names))


def normalized_name(value):
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def owner_candidate(events):
    eligible = []
    review = False

    for event in events:
        conveyance = str(
            event.get("conveyanceText", "")
        ).upper()

        date = str(
            event.get("assignmentRecordedDate", "")
        )[:10]

        if any(term in conveyance for term in (
            "SECURITY", "RELEASE", "LICENSE", "LIEN"
        )):
            continue

        if any(term in conveyance for term in (
            "CORRECT", "CONFIRM", "NUNC", "PARTIAL",
            "COURT", "RESCIND", "VACAT"
        )):
            review = True
            continue

        if (
            any(term in conveyance for term in (
                "ASSIGNMENT OF ASSIGNOR",
                "MERGER",
                "CHANGE OF NAME",
            ))
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date)
            and party_names(event, "assignee")
        ):
            eligible.append((date, event))
        else:
            review = True

    if not eligible:
        return (
            "",
            "",
            "Unresolved: no interpretable ownership event",
        )

    latest_date = max(date for date, _ in eligible)

    latest = [
        event
        for date, event in eligible
        if date == latest_date
    ]

    recipients = {
        tuple(sorted(
            normalized_name(name)
            for name in party_names(event, "assignee")
        ))
        for event in latest
    }

    if len(recipients) != 1:
        return (
            "",
            latest_date,
            "Conflicting latest recipients; review required",
        )

    note = (
        "Latest eligible recorded recipient; "
        "ownership chain not verified"
    )

    if review:
        note += "; additional recordings require review"

    return (
        "; ".join(party_names(latest[0], "assignee")),
        latest_date,
        note,
    )


def complete_ids(value):
    matches = re.findall(
        r"US[\s_-]*(\d{4,13})[\s_-]*([AB]\d)(?![A-Z0-9])",
        json.dumps(value),
        flags=re.I,
    )

    return sorted({
        f"US{number.lstrip('0')} {kind.upper()}"
        for number, kind in matches
    })

def find_applications(
    company, start_year, end_year, key, progress
):
    records = {}
    notices = []

    entered_name = company.strip()

    if entered_name.casefold() in {
        "ibm",
        "international business machines",
        "international business machines corporation",
    }:
        search_names = [
            "International Business Machines",
            "IBM",
        ]
    else:
        search_names = [entered_name]

    date_filter = (
        "(applicationMetaData.filingDate:"
        f"[{start_year}-01-01 TO {end_year}-12-31])"
    )

    queries = []

    for search_name in search_names:
        phrase = re.sub(
            r'[+\-!(){}\[\]^"~*?:\\/|&]', " ", search_name
        )
        phrase = " ".join(phrase.split())

        if not phrase:
            continue

        queries.extend([
            (
                f"Applicant — {phrase}",
                f'(applicationMetaData.firstApplicantName:"{phrase}") '
                f"AND {date_filter}",
            ),
            (
                f"Broader discovery — {phrase}",
                f'("{phrase}") AND {date_filter}',
            ),
        ])

    if not queries:
        raise ValueError("Enter a valid company name.")

    for label, query in queries:
        offset = 0
        page_size = 25
        seen_pages = set()
        first_page_reported = False

        try:
            while True:
                progress.info(
                    f"{label}: {offset:,} records retrieved; "
                    f"page size {page_size}; "
                    f"{len(records):,} unique applications overall"
                )

                try:
                    try:
                        response = api_call(
                            "/search",
                            key,
                            body={
                                "q": query,
                                "pagination": {
                                    "offset": offset,
                                    "limit": page_size,
                                },
                            },
                        )

                    except RuntimeError as error:
                        if "HTTP 403" not in str(error):
                            raise

                        response = api_call(
                            "/search",
                            key,
                            params={
                                "q": query,
                                "offset": offset,
                                "limit": page_size,
                            },
                        )

                except RuntimeError as error:
                    if "HTTP 413" in str(error):
                        if page_size > 1:
                            old_size = page_size
                            page_size = max(1, page_size // 2)

                            progress.warning(
                                f"{label}: response too large. "
                                f"Reducing page size from "
                                f"{old_size} to {page_size}; "
                                f"retrying offset {offset:,}."
                            )
                            continue

                        raise RuntimeError(
                            "A single record exceeds the response-size "
                            "limit. Search stopped without skipping it."
                        ) from None

                    raise

                if "patentFileWrapperDataBag" not in response:
                    raise ValueError(
                        "Unexpected USPTO response structure."
                    )

                page = response["patentFileWrapperDataBag"] or []
                total = response.get("count")
                reported_total = (
                    int(total) if total is not None else None
                )

                if not first_page_reported:
                    st.write({
                        "Search": label,
                        "Query": query,
                        "API reported count": reported_total,
                        "First-page records": len(page),
                        "Page size": page_size,
                    })
                    first_page_reported = True

                if not page:
                    if (
                        reported_total is not None
                        and offset < reported_total
                    ):
                        notices.append(
                            f"{label}: ended early at {offset:,} "
                            f"of {reported_total:,} reported records."
                        )
                    break

                signature = tuple(
                    str(record.get("applicationNumberText", ""))
                    for record in page
                )

                if signature in seen_pages:
                    raise ValueError(
                        "Repeated page detected; coverage is incomplete."
                    )

                seen_pages.add(signature)

                for record in page:
                    application = record.get("applicationNumberText")
                    if application:
                        records.setdefault(str(application), record)

                offset += len(page)

                if (
                    reported_total is not None
                    and offset >= reported_total
                ):
                    break

                time.sleep(0.3)

        except (
            requests.RequestException,
            RuntimeError,
            ValueError,
        ) as error:
            notices.append(
                f"{label}: incomplete at offset {offset:,}: {error}"
            )

    return records, notices

def metadata_row(application, record, key):
    notes = []
    events = extract_events(record)

    metadata = dict(
        record.get("applicationMetaData") or {}
    )

    source_counts = {}

    for suffix in ("/meta-data", "/assignment", ""):
        try:
            response = api_call(
                f"/{application}{suffix}", key
            )

            retrieved_events = extract_events(response)
            events.extend(retrieved_events)

            if suffix in ("/assignment", ""):
                source_counts[suffix or "/application"] = (
                    len(retrieved_events)
                )

            for item in as_list(
                response.get("patentFileWrapperDataBag")
            ):
                if (
                    str(item.get("applicationNumberText"))
                    == application
                ):
                    metadata.update(
                        item.get("applicationMetaData") or {}
                    )

        except (
            requests.RequestException,
            RuntimeError,
            ValueError,
        ) as error:
            notes.append(
                f"{suffix or '/application'}: {error}"
            )

    events = list({
        json.dumps(event, sort_keys=True): event
        for event in events
    }.values())

    identifiers = complete_ids(metadata)

    try:
        documents = api_call(
            f"/{application}/associated-documents", key
        )
        identifiers = sorted(set(
            identifiers + complete_ids(documents)
        ))

    except (
        requests.RequestException,
        RuntimeError,
        ValueError,
    ) as error:
        notes.append(f"Document metadata: {error}")

    grant = str(metadata.get("patentNumber") or "")
    pregrant = str(
        metadata.get("earliestPublicationNumber") or ""
    )

    exact = next((
        value
        for value in identifiers
        if grant and value.startswith(f"US{grant} B")
    ), "")

    if not grant:
        exact = next((
            value
            for value in identifiers
            if pregrant and value.startswith(f"US{pregrant} A")
        ), "")

    candidate, recorded_date, owner_note = (
        owner_candidate(events)
    )

    row = dict.fromkeys(COLUMNS, "")

    assignment_check = "; ".join(
        f"{source}: {count} events"
        for source, count in source_counts.items()
    )

    if len(set(source_counts.values())) > 1:
        assignment_check += "; endpoint event counts differ"

    assignment_check += (
        "; completeness not guaranteed"
        if assignment_check
        else "Assignment retrieval unavailable"
    )

    row.update({
        "Publication Number": exact or (
            "US" + (grant or pregrant)
            if grant or pregrant else ""
        ),
        "Publication Date": (
            metadata.get("grantDate", "")
            if grant
            else metadata.get("earliestPublicationDate", "")
        ),
        "Publication Number Verification": (
            "Complete identifier in USPTO metadata"
            if exact
            else "Exact kind code unavailable"
        ),
        "Application Number": (
            f"US {application[:2]}/"
            f"{application[2:5]},{application[5:]}"
            if re.fullmatch(r"\d{8}", application)
            else application
        ),
        "Application Number - API": application,
        "Application Date": metadata.get("filingDate", ""),
        "Title": metadata.get("inventionTitle", ""),
        "Applicant": metadata.get("firstApplicantName", ""),
        "Original Assignee Verification": (
            "Not established by USPTO application metadata"
        ),
        "Current Assignee Candidate": candidate,
        "Current Assignee Verification": owner_note,
        "Ownership Recording Date": recorded_date,
        "Historical Assignment Recipients": "; ".join(sorted({
            name
            for event in events
            for name in party_names(event, "assignee")
        })),
        "All Recorded Assignor Names": "; ".join(sorted({
            name
            for event in events
            for name in party_names(event, "assignor")
        })),
        "Assignment Events Retrieved": str(len(events)),
        "Assignment Retrieval Check": assignment_check,
        "Abstract Retrieval": "Not requested",
        "Alive / Active": "Unknown",
        "Application Status": metadata.get(
            "applicationStatusDescriptionText", ""
        ),
        "Application Status Code": str(
            metadata.get("applicationStatusCode", "")
        ),
        "Application Status Date": metadata.get(
            "applicationStatusDate", ""
        ),
        "Application Type": metadata.get(
            "applicationTypeLabelName", ""
        ),
        "US Entity Status": (
            metadata.get("entityStatusData") or {}
        ).get("businessEntityStatusCategory", ""),
        "CPC Subgroups": "; ".join(
            str(code)
            for code in (
                metadata.get("cpcClassificationBag") or []
            )
        ),
        "US Classifications": metadata.get(
            "uspcSymbolText", ""
        ),
        "All Complete US IDs Found": "; ".join(identifiers),
        "Metadata Retrieved UTC": utc_now(),
        "Notices": " ".join(notes),
    })

    return row


def google_fields(row):
    publication = (
        row.get("Publication Number", "").upper().strip()
    )

    match = re.fullmatch(
        r"US\s*(\d+)(?:\s*([AB]\d))?", publication
    )

    if not match:
        raise ValueError("No usable US publication number.")

    number, known_kind = match.groups()

    # These are lookup candidates, not asserted kind codes.
    kinds = [known_kind] if known_kind else (
        ["A1", "A9"]
        if len(number) == 11
        else ["B2", "B1"]
    )

    for index, kind in enumerate(kinds):
        identifier = f"US{number}{kind}"
        url = (
            "https://patents.google.com/patent/"
            f"{identifier}/en"
        )

        response = requests.get(
            url,
            headers={
                "User-Agent": "PatentfolioResearch/1.0"
            },
            timeout=(15, 60),
        )

        if (
            response.status_code == 404
            and index + 1 < len(kinds)
        ):
            response.close()
            continue

        try:
            response.raise_for_status()
            soup = BeautifulSoup(
                response.content, "html.parser"
            )
        finally:
            response.close()

        break

    def field(prop):
        values = []

        for element in soup.select(
            f'[itemprop="{prop}"]'
        ):
            value = (
                element.get("content")
                or element.get_text(" ", strip=True)
            )

            value = re.sub(r"\s+", " ", value).strip()

            if value and value not in values:
                values.append(value)

        return "; ".join(values)

    declared = field("publicationNumber")
    canonical = soup.select_one(
        'link[rel="canonical"]'
    )

    declared_ids = complete_ids(
        declared + " " + (
            canonical.get("href", "")
            if canonical else ""
        )
    )

    if any(
        value.split()[0] != "US" + number.lstrip("0")
        for value in declared_ids
    ):
        raise ValueError(
            "Returned page has a different publication number."
        )

    current = field("assigneeCurrent")
    original = field("assigneeOriginal")
    status = field("status")

    element = soup.select_one('[itemprop="abstract"]')
    abstract = ""

    if element:
        for heading in element.select("h1,h2,h3,h4"):
            heading.decompose()

        abstract = element.get_text(" ", strip=True)

    if not abstract:
        meta = soup.find(
            "meta",
            attrs={"name": "DC.description"},
        )
        abstract = (
            (meta.get("content") or "")
            if meta else ""
        )

    if not any((current, original, status, abstract)):
        raise ValueError(
            "No recognized fields; page access or parsing failed."
        )

    updated = dict(row)

    if declared_ids:
        updated["Publication Number"] = declared_ids[0]
        updated["Publication Number Verification"] = (
            "Identifier declared on Google Patents page"
        )

    if current:
        updated["Current Assignee"] = current
        updated["Current Assignee Verification"] = (
            "Google Patents; source-reported, "
            "not independently verified"
        )

    if original:
        updated["Original Assignee"] = original
        updated["Original Assignee Verification"] = (
            "Google Patents; source-reported"
        )

    if abstract:
        updated["Abstract"] = abstract.strip()
        updated["Abstract Source URL"] = url

    updated["Abstract Retrieval"] = (
        "Succeeded"
        if abstract
        else "Unavailable in returned page"
    )

    updated["Legal Status - Source"] = status

    updated["Alive / Active"] = (
        "Alive (source-reported)"
        if re.match(r"^active\b", status, re.I)
        else "Not active (source-reported)"
        if re.search(
            r"expired|abandoned|revoked|withdrawn|ceased",
            status,
            re.I,
        )
        else "Unknown"
    )

    updated["Ownership / Legal Status Source"] = url
    updated["Enrichment Retrieved UTC"] = utc_now()

    return updated


def csv_bytes(rows):
    frame = pd.DataFrame(rows).fillna("")

    # Keep the expected schema, plus any imported extra columns.
    ordered = [
        column for column in COLUMNS
        if column in frame.columns
    ]
    ordered += [
        column for column in frame.columns
        if column not in ordered
    ]
    frame = frame.reindex(columns=ordered)

    # Protect spreadsheet users against formula interpretation.
    frame = frame.map(
        lambda value:
            "'" + value
            if isinstance(value, str)
            and value.startswith(
                ("=", "+", "-", "@", "\t", "\r")
            )
            else value
    )

    return frame.to_csv(index=False).encode("utf-8-sig")


for name, default in (
    ("rows", []),
    ("before", []),
    ("notices", []),
    ("label", "patents"),
):
    if name not in st.session_state:
        st.session_state[name] = default


st.title("Patent Portfolio Explorer")

st.caption(
    "U.S. application discovery → USPTO metadata → optional "
    "abstracts, reported ownership and legal status."
)

with st.sidebar:
    st.header("Search")

    key = st.text_input(
        "Your USPTO API key",
        type="password",
        key="uspto_key",
        help=(
            "Each visitor supplies their own key. "
            "No shared cache is used."
        ),
    )

    company = st.text_input("Company name", "Exagan")

    year1, year2 = st.columns(2)

    start_year = year1.number_input(
        "Application year from",
        min_value=1790,
        max_value=2100,
        value=2010,
    )

    end_year = year2.number_input(
        "Through",
        min_value=1790,
        max_value=2100,
        value=datetime.now().year,
    )

    search_clicked = st.button(
        "Search patents",
        type="primary",
    )

    st.caption(
        "Year filtering uses USPTO filingDate. "
        "It can differ from international filing "
        "and priority dates."
    )


if search_clicked:
    if (
        not key.strip()
        or not company.strip()
        or start_year > end_year
    ):
        st.error(
            "Enter your key, company name and a valid year range."
        )

    else:
        st.session_state.rows = []
        st.session_state.before = []
        st.session_state.notices = []

        st.session_state.label = re.sub(
            r"[^A-Za-z0-9]+", "_", company
        )

        progress = st.empty()

        try:
            records, notices = find_applications(
                company,
                start_year,
                end_year,
                key.strip(),
                progress,
            )

            st.session_state.notices = notices

            for index, (application, record) in enumerate(
                records.items(), start=1
            ):
                progress.info(
                    "Reading USPTO details: "
                    f"{index}/{len(records)}"
                )

                row = metadata_row(
                    application, record, key.strip()
                )

                st.session_state.rows.append(row)
                st.session_state.before.append(dict(row))

            progress.success(
                f"{len(st.session_state.rows)} "
                "applications retrieved"
            )

        except Exception as error:
            safe_message = str(error)
            if key:
                safe_message = safe_message.replace(
                    key, "[REDACTED]"
                )
            st.error(safe_message)


uploaded = st.file_uploader(
    "Or import a previous result CSV",
    type="csv",
)

if st.button(
    "Load uploaded CSV",
    disabled=uploaded is None,
):
    try:
        frame = pd.read_csv(
            uploaded, dtype=str
        ).fillna("")

        if "Publication Number" not in frame.columns:
            raise ValueError(
                "CSV needs a Publication Number column."
            )

        st.session_state.rows = frame.to_dict("records")
        st.session_state.before = [
            dict(row)
            for row in st.session_state.rows
        ]
        st.session_state.notices = [
            "Imported snapshot; no fresh lookup yet."
        ]
        st.session_state.label = "imported_patents"

    except Exception as error:
        st.error(str(error))


if st.session_state.rows:
    st.subheader(
        f"Results: {len(st.session_state.rows)} applications"
    )

    # Downloading does not trigger an app rerun.
    st.download_button(
        "Download before-enrichment CSV",
        data=csv_bytes(st.session_state.before),
        file_name=(
            st.session_state.label
            + "_before_enrichment.csv"
        ),
        mime="text/csv",
        on_click="ignore",
        key="download_before",
    )

    st.info(
        "Optional enrichment uses Google Patents. "
        "Hosting providers may receive 403/503 errors "
        "even when the same code works locally."
    )

    if st.button(
        "Add / retry abstracts, ownership and legal status"
    ):
        progress = st.empty()

        for index, row in enumerate(
            st.session_state.rows
        ):
            progress.info(
                f"Enriching {index + 1}/"
                f"{len(st.session_state.rows)}"
            )

            try:
                st.session_state.rows[index] = (
                    google_fields(row)
                )

            except Exception as error:
                # Preserve any previously retrieved abstract.
                if not row.get("Abstract"):
                    row["Abstract Retrieval"] = "Failed"

                message = (
                    f"{row.get('Application Number', '')}: "
                    f"{error}"
                )

                row["Notices"] = (
                    row.get("Notices", "")
                    + " "
                    + message
                ).strip()

                st.session_state.notices.append(message)

                status = getattr(
                    getattr(error, "response", None),
                    "status_code",
                    None,
                )

                if status in (403, 429, 503):
                    st.warning(
                        "Source denied or could not serve "
                        "this request. Remaining lookups "
                        "stopped; existing data is preserved."
                    )
                    break

            time.sleep(1)

        progress.empty()

    frame = pd.DataFrame(
        st.session_state.rows
    ).fillna("")

    # Updated Streamlit parameter.
    st.dataframe(
        frame,
        hide_index=True,
        width="stretch",
    )

    # Downloading does not trigger an app rerun.
    st.download_button(
        "Download current / enriched CSV",
        data=csv_bytes(st.session_state.rows),
        file_name=(
            st.session_state.label + "_current.csv"
        ),
        mime="text/csv",
        on_click="ignore",
        key="download_current",
    )


if st.session_state.notices:
    with st.expander("Coverage and retrieval notices"):
        for notice in st.session_state.notices:
            st.write(notice)


st.caption(
    "Results are name-search candidates, not a verified "
    "complete portfolio. One row per U.S. application; "
    "no international family deduplication. Applicant "
    "is separate from assignee. Current-owner candidates "
    "are not legal ownership determinations. Alive/active "
    "is source-reported and separate from application "
    "status. Unavailable fields remain blank or Unknown. "
    "Results are held in each browser session, "
    "not a shared cache."
)
