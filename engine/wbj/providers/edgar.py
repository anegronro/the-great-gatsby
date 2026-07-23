"""SEC EDGAR provider: ticker->CIK lookup, XBRL company facts, filing metadata.

Tier-1 per Cerebro/shared/SOURCE_HIERARCHY.md ("Regulatory filing and filing
acceptance metadata" ranks first). No API key is required — `EdgarProvider`
is always `available`. SEC's fair-access policy requires a descriptive
`User-Agent` identifying the requester on every request
(https://www.sec.gov/os/webmaster-faq#developers); `EDGAR_USER_AGENT` is
sent on every call via `wbj.providers.base.Provider.get_json`'s `headers`
pass-through.

Endpoints:
- `https://www.sec.gov/files/company_tickers.json` — ticker -> CIK map,
  one global payload (not per-ticker), refreshed roughly monthly by SEC,
  so cached for up to 30 days under a fixed global cache entry.
- `https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json` — all
  XBRL (dei/us-gaap/...) facts reported by the company across filings.
  Cached per-CIK for up to 1 day.
- `https://data.sec.gov/submissions/CIK{cik:010d}.json` — filing history
  including `acceptanceDateTime`, used to determine filing recency.
  Cached per-CIK for up to 1 day.
"""

from __future__ import annotations

import logging
import re
from datetime import date

import httpx

from wbj.providers.base import Provider

logger = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn}"

EDGAR_USER_AGENT = "warren-buffett-jr victor@infusioninvestments.com"
_EDGAR_HEADERS = {"User-Agent": EDGAR_USER_AGENT}

# The tickers map is one global, ticker-independent payload, so it is
# cached under a fixed pseudo-ticker rather than the caller's ticker —
# looking up a second ticker must reuse the same cache entry.
_GLOBAL_CACHE_TICKER = "_GLOBAL"

# 13F filings are immutable once accepted, and the same handful of notable
# filers is queried for every ticker, so their parsed holdings live under a
# fixed pseudo-ticker keyed by accession — shared across all analyses.
_NOTABLE_CACHE_TICKER = "_13F"

_MAX_AGE_TICKERS = 30
_MAX_AGE_COMPANYFACTS = 1
_MAX_AGE_SUBMISSIONS = 1
# A 13F info table never changes after acceptance; cache its parsed index for
# a full quarter-plus so re-analysis never re-downloads the multi-MB XML.
_MAX_AGE_13F_INDEX = 120
# A 13F reports positions as of a quarter-end, filed up to 45 days later.
# Past ~160 days the latest available filing is stale (the filer skipped a
# quarter or requested confidential treatment) — don't present it as current.
_MAX_AGE_13F_POSITION_DAYS = 160

# Curated registry of recognized institutional investors, by 13F-filer CIK.
# These are the "inversionistas importantes o relacionados" the project brief
# asks for — funds whose principals have a public track record. CIKs verified
# against data.sec.gov/submissions (each files 13F-HR currently). Names carry
# the principal so the report can say who is behind the fund.
NOTABLE_13F_FILERS: dict[int, str] = {
    1067983: "Berkshire Hathaway (Warren Buffett)",
    1336528: "Pershing Square (Bill Ackman)",
    1656456: "Appaloosa (David Tepper)",
    1536411: "Duquesne Family Office (Stanley Druckenmiller)",
    1040273: "Third Point (Daniel Loeb)",
    921669: "Icahn Capital (Carl Icahn)",
    1061768: "Baupost Group (Seth Klarman)",
    1350694: "Bridgewater Associates (Ray Dalio)",
    1037389: "Renaissance Technologies",
    1167483: "Tiger Global (Chase Coleman)",
    1135730: "Coatue Management (Philippe Laffont)",
    1061165: "Lone Pine Capital (Stephen Mandel)",
    1103804: "Viking Global Investors",
    1029160: "Soros Fund Management",
    1697748: "ARK Invest (Cathie Wood)",
    1541617: "Altimeter Capital (Brad Gerstner)",
    1709323: "Himalaya Capital (Li Lu)",
    1423053: "Citadel Advisors (Ken Griffin)",
}

# Corporate-suffix and descriptor tokens dropped when normalizing an issuer
# name so a 13F's "NVIDIA CORPORATION" matches EDGAR's title "NVIDIA CORP".
_ISSUER_STOP_TOKENS = frozenset({
    "CORP", "CORPORATION", "INC", "INCORPORATED", "LLC", "LP", "LTD",
    "LIMITED", "PLC", "CO", "COMPANY", "COMPANIES", "HOLDINGS", "HLDGS",
    "HOLDING", "GROUP", "GRP", "THE", "CLASS", "CL", "COM", "COMMON",
    "NEW", "SA", "NV", "AG", "ADR", "ADS", "ORD", "SHS", "SHARES", "SPON",
    "SPONSORED", "STOCK", "TR", "TRUST", "PARTNERS", "INTL", "INTERNATIONAL",
})

_INFOTABLE_ROW_RE = re.compile(
    r"<(?:\w+:)?infoTable\b.*?</(?:\w+:)?infoTable>", re.S
)


def _issuer_tag(block: str, tag: str) -> str | None:
    m = re.search(rf"<(?:\w+:)?{tag}>(.*?)</(?:\w+:)?{tag}>", block, re.S)
    return m.group(1).strip() if m else None


def normalize_issuer(name: str | None) -> str:
    """Normalize an issuer name to a stable, suffix-stripped token string.

    Uppercases, drops punctuation and corporate-suffix/descriptor tokens, and
    collapses whitespace so filer spellings reconcile with EDGAR's title
    (e.g. "NVIDIA CORPORATION" and "NVIDIA CORP" both -> "NVIDIA"). Returns ""
    for empty input.
    """
    if not name:
        return ""
    cleaned = re.sub(r"[^A-Z0-9 ]", " ", name.upper())
    tokens = [
        t for t in cleaned.split()
        if t not in _ISSUER_STOP_TOKENS and len(t) > 1
    ]
    return " ".join(tokens)


def _cik_cache_key(cik: int) -> str:
    return f"CIK{cik:010d}"


class EdgarProvider(Provider):
    """SEC EDGAR data provider (no API key required)."""

    @property
    def available(self) -> bool:
        """Always True — EDGAR requires no API key, only a User-Agent header."""
        return True

    def cik_for(self, ticker: str) -> int | None:
        """Look up the CIK for `ticker` via SEC's company_tickers.json map.

        Returns None if the ticker isn't found or the payload is malformed.
        """
        payload = self.get_json(
            TICKERS_URL,
            {},
            "tickers",
            _GLOBAL_CACHE_TICKER,
            max_age_days=_MAX_AGE_TICKERS,
            headers=_EDGAR_HEADERS,
        )
        if not isinstance(payload, dict):
            return None

        ticker_upper = ticker.upper()
        for entry in payload.values():
            if not isinstance(entry, dict):
                continue
            if str(entry.get("ticker", "")).upper() != ticker_upper:
                continue
            cik = entry.get("cik_str")
            try:
                return int(cik)
            except (TypeError, ValueError):
                return None
        return None

    def companyfacts(self, cik: int) -> dict | None:
        """Fetch all XBRL company facts (dei/us-gaap/...) for `cik`."""
        payload = self.get_json(
            COMPANYFACTS_URL.format(cik=cik),
            {},
            "companyfacts",
            _cik_cache_key(cik),
            max_age_days=_MAX_AGE_COMPANYFACTS,
            headers=_EDGAR_HEADERS,
        )
        return payload if isinstance(payload, dict) else None

    def filing_acceptance_times(self, cik: int) -> list[dict] | None:
        """Return recent filings' form/acceptanceDateTime/accessionNumber.

        Derived from `https://data.sec.gov/submissions/CIK{cik}.json`'s
        `filings.recent` arrays. Returns None if the payload is malformed
        or lacks the expected `filings.recent` structure.
        """
        payload = self.get_json(
            SUBMISSIONS_URL.format(cik=cik),
            {},
            "submissions",
            _cik_cache_key(cik),
            max_age_days=_MAX_AGE_SUBMISSIONS,
            headers=_EDGAR_HEADERS,
        )
        if not isinstance(payload, dict):
            return None

        recent = payload.get("filings", {}).get("recent")
        if not isinstance(recent, dict):
            return None

        forms = recent.get("form", [])
        accept_times = recent.get("acceptanceDateTime", [])
        accession_numbers = recent.get("accessionNumber", [])

        return [
            {
                "form": form,
                "acceptanceDateTime": accepted,
                "accessionNumber": accession,
            }
            for form, accepted, accession in zip(
                forms, accept_times, accession_numbers, strict=False
            )
        ]

    # ------------------------------------------------------------------
    # 13F institutional holdings ("inversionistas importantes")
    # ------------------------------------------------------------------

    def _latest_13f(self, cik: int) -> dict | None:
        """Most-recent 13F-HR (accession + filing date) for a filer CIK.

        Returns None if the filer has never filed a 13F-HR or the submissions
        payload is malformed.
        """
        payload = self.get_json(
            SUBMISSIONS_URL.format(cik=cik), {}, "submissions",
            _cik_cache_key(cik), max_age_days=_MAX_AGE_SUBMISSIONS,
            headers=_EDGAR_HEADERS,
        )
        recent = (payload or {}).get("filings", {}).get("recent")
        if not isinstance(recent, dict):
            return None
        forms = recent.get("form", [])
        accns = recent.get("accessionNumber", [])
        dates = recent.get("filingDate", [])
        for form, accn, fdate in zip(forms, accns, dates, strict=False):
            if str(form).startswith("13F-HR"):
                return {"accession": accn, "filing_date": fdate}
        return None

    def _get_text(self, url: str) -> str | None:
        """Fetch a raw text/XML document (info tables aren't JSON).

        Best-effort single call — returns None on any transport or HTTP
        error rather than raising, matching `get_json`'s contract.
        """
        try:
            resp = self.client.get(url, headers=_EDGAR_HEADERS)
        except httpx.TransportError as exc:
            logger.warning("edgar text fetch failed url=%s error=%s", url, exc)
            return None
        if resp.status_code >= 400:
            logger.warning("edgar text fetch status=%d url=%s", resp.status_code, url)
            return None
        return resp.text

    def _info_table_index(self, cik: int, accession: str) -> dict | None:
        """Parse a 13F info table into {normalized_issuer: aggregated position}.

        Each value is `{"name", "cusip", "value", "shares", "options"}` where
        `value`/`shares` sum the filer's SH (share) rows for that issuer and
        `options` counts Put/Call rows. Result is cached under a fixed
        pseudo-ticker keyed by accession — the filing is immutable and shared
        across every ticker's analysis, so the multi-MB XML is fetched once.
        """
        accn = accession.replace("-", "")
        cache_key = f"idx_{accn}"
        age = self.cache.age_days(_NOTABLE_CACHE_TICKER, cache_key)
        if age is not None and age <= _MAX_AGE_13F_INDEX:
            return self.cache.get(_NOTABLE_CACHE_TICKER, cache_key)

        base = ARCHIVE_BASE.format(cik=cik, accn=accn)
        listing = self.get_json(
            f"{base}/index.json", {}, f"filingidx_{accn}",
            _NOTABLE_CACHE_TICKER, max_age_days=_MAX_AGE_13F_INDEX,
            headers=_EDGAR_HEADERS,
        )
        items = ((listing or {}).get("directory", {}).get("item")) or []
        # The info table is the largest .xml that isn't the primary_doc or an
        # XSL-rendered copy; filers name it inconsistently (infotable.xml,
        # form13fInfoTable.xml, ...), so pick by shape rather than by name.
        candidates = [
            it for it in items
            if str(it.get("name", "")).lower().endswith(".xml")
            and it.get("name") != "primary_doc.xml"
            and "xsl" not in str(it.get("name", "")).lower()
        ]
        if not candidates:
            return None
        fname = max(candidates, key=lambda it: int(it.get("size", 0) or 0))["name"]

        xml = self._get_text(f"{base}/{fname}")
        if not xml:
            return None

        index: dict[str, dict] = {}
        for block in _INFOTABLE_ROW_RE.findall(xml):
            key = normalize_issuer(_issuer_tag(block, "nameOfIssuer"))
            if not key:
                continue
            try:
                value = int(float(_issuer_tag(block, "value") or 0))
            except ValueError:
                value = 0
            try:
                shares = int(float(_issuer_tag(block, "sshPrnamt") or 0))
            except ValueError:
                shares = 0
            row = index.setdefault(key, {
                "name": _issuer_tag(block, "nameOfIssuer"),
                "cusip": _issuer_tag(block, "cusip"),
                "value": 0, "shares": 0, "options": 0,
            })
            if _issuer_tag(block, "putCall"):
                row["options"] += 1
            else:
                row["value"] += value
                row["shares"] += shares
                if value:  # keep the CUSIP of a real share row
                    row["cusip"] = _issuer_tag(block, "cusip")

        self.cache.put(_NOTABLE_CACHE_TICKER, cache_key, index)
        return index

    def notable_holders(
        self, issuer_title: str | None, as_of: str | None = None
    ) -> list[dict]:
        """Recognized 13F investors holding `issuer_title`, largest first.

        For each filer in `NOTABLE_13F_FILERS`, reads its most-recent 13F-HR
        and reports a position if the issuer appears. Filings older than
        `_MAX_AGE_13F_POSITION_DAYS` (a skipped/confidential quarter) are
        excluded so nothing stale is shown as a current holding. Returns
        `[{"investor", "cik", "value", "shares", "options", "as_of",
        "cusip"}]`; empty when no recognized investor holds the name (a valid
        answer, not an error). Best-effort per filer — a failed fetch is
        skipped, never fatal.
        """
        target = normalize_issuer(issuer_title)
        if not target:
            return []
        target_tokens = set(target.split())
        today = date.fromisoformat(as_of) if as_of else date.today()

        out: list[dict] = []
        for cik, investor in NOTABLE_13F_FILERS.items():
            latest = self._latest_13f(cik)
            if not latest:
                continue
            try:
                filed = date.fromisoformat(latest["filing_date"])
            except (ValueError, KeyError, TypeError):
                continue
            if (today - filed).days > _MAX_AGE_13F_POSITION_DAYS:
                continue

            index = self._info_table_index(cik, latest["accession"])
            if not index:
                continue

            hit = index.get(target)
            if hit is None:  # fall back to a token-subset match
                matches = [
                    v for k, v in index.items()
                    if target_tokens and target_tokens <= set(k.split())
                ]
                hit = max(matches, key=lambda v: v["value"], default=None)
            if hit is None or (hit["value"] <= 0 and hit["options"] <= 0):
                continue

            out.append({
                "investor": investor,
                "cik": cik,
                "value": hit["value"],
                "shares": hit["shares"],
                "options": hit["options"],
                "as_of": latest["filing_date"],
                "cusip": hit.get("cusip"),
            })

        out.sort(key=lambda r: r["value"], reverse=True)
        return out
