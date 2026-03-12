import os
import re
import json
import smtplib
import requests
import dns.resolver
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

load_dotenv()
mcp = FastMCP("Directory-Manager")

WP_URL = os.getenv("WP_URL")
AUTH = (os.getenv("WP_USERNAME"), os.getenv("WP_APP_PASSWORD"))
TAVILY_KEY   = os.getenv("TAVILY_API_KEY")
ADMIN_EMAIL  = os.getenv("ADMIN_EMAIL")
SMTP_HOST    = os.getenv("SMTP_HOST",   "smtp.gmail.com")
SMTP_PORT    = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER    = os.getenv("SMTP_USER")
SMTP_PASS    = os.getenv("SMTP_PASS")
AUDIT_FILE   = "directory_audit.jsonl"

API_ENDPOINT = f"{WP_URL}/index.php?rest_route=/wp/v2/listdom-listing&per_page=30"
NZ_PHONE_RE  = re.compile(r"^(\+64|0)(2[0-9]|[3-9])\d{6,8}$")

LISTDOM_META_KEYS = {
    "phone":   ["lsd_phone",   "lsd_param_phone",   "phone",   "_sln_phone",   "listdom_phone"],
    "email":   ["lsd_email",   "lsd_param_email",   "email",   "_sln_email",   "listdom_email"],
    "website": ["lsd_website", "lsd_param_website", "website", "_sln_website", "listdom_website"],
}

def _normalise_nz_phone(raw: str) -> str:
    """
    Normalise any valid NZ phone number to a consistent format.

    Rules:
      +64 xx xxx xxxx  → 0xx xxx xxxx  (convert international to local)
      0800/0508 xxxxxx → 0800 xxx xxx  (freephone)
      02x xxxxxxx      → 02x xxx xxxx  (mobile, 10 digits)
      0x xxxxxxx       → 0x xxx xxxx   (landline, area code + 7 digits)

    Returns the normalised string, or the original if it cannot be parsed.
    """
    if not raw:
        return raw
    # Strip all formatting characters
    digits = re.sub(r"[\s\-\.\(\)]", "", raw)
    # Convert +64 prefix to leading 0
    if digits.startswith("+64"):
        digits = "0" + digits[3:]
    # Must start with 0 after normalisation
    if not digits.startswith("0"):
        return raw
    # Freephone: 0800 or 0508 + 6 digits
    if re.match(r"^0(800|508)\d{6,7}$", digits):
        prefix = digits[:4]       # 0800 or 0508
        rest   = digits[4:]
        if len(rest) == 6:
            return f"{prefix} {rest[:3]} {rest[3:]}"
        else:
            return f"{prefix} {rest[:3]} {rest[3:]}"
    # Mobile: 02x + 7-8 digits = 10-11 digits total
    if re.match(r"^02\d{8,9}$", digits):
        area = digits[:3]         # 021 / 022 / 027 etc.
        rest = digits[3:]
        if len(rest) == 7:
            return f"{area} {rest[:3]} {rest[3:]}"
        else:
            return f"{area} {rest[:4]} {rest[4:]}"
    # Landline: 0x + 7 digits = 9 digits total
    if re.match(r"^0[3-9]\d{7}$", digits):
        area = digits[:2]         # 03 / 04 / 06 / 07 / 09
        rest = digits[2:]         # 7 digits
        return f"{area} {rest[:3]} {rest[3:]}"
    # Unknown valid-ish number — return stripped with spaces every 3-4
    return raw

def _log(listing_id, listing_name, action, detail):
    entry = {
        "ts":     datetime.now().isoformat(),
        "id":     listing_id,
        "name":   listing_name,
        "action": action,
        "detail": detail,
    }
    with open(AUDIT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# Tool for fetching data from Wordpress site
@mcp.tool()
def get_listings(query: str = None, per_page: int = 50):
    """
   Fetch community service listings from WordPress / Listdom.
    Use 'query' to search for a specific listing by name.
    Returns id, title, last_updated, phone, email, and website for each listing.
    Always call this first at the start of any audit or check.
    """

    url = API_ENDPOINT + f"&per_ page={per_page}"
    if query:
        url += f"&search={query}"
    
    try:
        response = requests.get(url, auth=AUTH, timeout=10)

        if response.status_code != 200:
            return f"WordPress error {response.status_code}: {response.text[:120]}"

        # Strip any PHP debug output before the JSON array
        raw = response.text.strip()
        json_start = raw.find('[')
        if json_start > 0:
            raw = raw[json_start:]

        data = json.loads(raw)
        if not isinstance(data, list):
            return f"Unexpected response: {str(data)[:100]}"

        import html
        def _norm_phone(raw):
            """Normalise phone on load — removes (0x) bracket style, standardises spacing."""
            if not raw or raw == "N/A":
                return raw
            normed = _normalise_nz_phone(raw)
            # If normaliser didn't recognise it, at minimum strip brackets
            if normed == raw:
                import re as _re
                normed = _re.sub(r"[\(\)]", "", raw).strip()
            return normed
        
        listings = [
            {
                "id":           item["id"],
                "title":        html.unescape(item["title"]["rendered"]),
                "last_updated": item.get("modified", "N/A"),
                "phone":        _norm_phone(item.get("wp_metadata", {}).get("phone",   "N/A")),
                "email":        item.get("wp_metadata", {}).get("email",   "N/A"),
                "website":      item.get("wp_metadata", {}).get("website", "N/A"),
            }
            for item in data
        ]
        return {"listings": listings, "count": len(listings)}

    except Exception as e:
        return f"Connection Error: {str(e)}"

# Tools to validate listing details
@mcp.tool()
def validate_listing(listing_id: int, listing_name: str,
                     phone: Optional[str] = None, email: Optional[str] = None,
                     website: Optional[str] = None):
    """
    FUNCTION 1 — Check if a listing's contact details are valid.

    Runs three live checks:
      - Phone  : NZ E.164 format (+64 or 0x...)
      - Email  : format + DNS MX record lookup (confirms domain can receive mail)
      - Website: live HTTP request (flags 4xx/5xx or timeouts as broken)

    Returns a health score (0-100), a status (valid / review / invalid),
    and a specific list of issues. Logs result to the audit file.
    """
    issues = []
    score  = 100

    phone   = phone   or ""
    email   = email   or ""
    website = website or ""

    # Phone
    if not phone or phone == "N/A" or phone == "Missing":
        issues.append("phone: not provided")
        score -= 20
    else:
        digits = re.sub(r"[\s\-\.\(\)]", "", phone)
        # Convert +64 → 0 for matching
        if digits.startswith("+64"):
            digits = "0" + digits[3:]
        if not NZ_PHONE_RE.match(digits):
            issues.append(f"phone: invalid NZ format — '{phone}' "
                          f"(stored as {len(digits)} digits; expected 9-11)")
            score -= 25
        else:
            normalised = _normalise_nz_phone(phone)
            if normalised != phone:
                issues.append(f"phone: inconsistent format — '{phone}' "
                               f"should be '{normalised}'")
                score -= 5   # minor — valid but needs reformatting

    # Email
    if not email or email == "N/A":
        issues.append("email: not provided")
        score -= 20
    elif not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        issues.append(f"email: invalid format — '{email}'")
        score -= 25
    else:
        domain = email.split("@")[1]
        try:
            dns.resolver.resolve(domain, "MX")
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            issues.append(f"email: no MX record for '{domain}' — domain may be defunct")
            score -= 25
        except dns.exception.Timeout:
            issues.append(f"email: DNS lookup timed out for '{domain}'")
            score -= 10

    # Website
    if not website or website == "N/A":
        issues.append("website: not provided")
        score -= 10
    else:
        check_url = website if website.startswith("http") else "https://" + website
        try:
            resp = requests.head(check_url, timeout=8, allow_redirects=True,
                                 headers={"User-Agent": "DirectoryAgent/1.0"})
            if resp.status_code >= 400:
                issues.append(f"website: returned HTTP {resp.status_code} — '{website}'")
                score -= 20
        except requests.Timeout:
            issues.append(f"website: connection timed out — '{website}'")
            score -= 20
        except requests.RequestException as e:
            issues.append(f"website: unreachable — {e}")
            score -= 20

    score  = max(0, score)
    status = "valid" if not issues else ("invalid" if score < 40 else "review")

    _log(listing_id, listing_name, "validate",
         {"status": status, "score": score, "issues": issues})

    # Compute normalised phone for the return value
    _phone_digits = re.sub(r"[\s\-\.\(\)]", "", phone or "")
    if _phone_digits.startswith("+64"):
        _phone_digits = "0" + _phone_digits[3:]
    _phone_norm = (
        _normalise_nz_phone(phone)
        if phone and NZ_PHONE_RE.match(_phone_digits)
        else None
    )

    return {
        "listing_id":      listing_id,
        "listing_name":    listing_name,
        "score":           score,
        "status":          status,
        "issues":          issues,
        "phone":           phone,
        "phone_normalised": _phone_norm,
        "email":           email,
        "website":         website,
    }


@mcp.tool()
def verify_listing_details(name: str, 
                           current_website: Optional[str] = None, 
                           current_phone: Optional[str] = None, 
                           current_email: Optional[str] = None):
    """
    FUNCTION 2 — Verify contact details are current by scraping the org's own website.

    Strategy (in order):
      1. Scrape the stored website homepage + follow Contact page link
      2. If blocked/unreachable: try Google Cache, then archive.org
      3. If no website stored: search Facebook/Instagram for the org, then Tavily
      4. Homepage always checked even if no Contact page found (header/footer has phone)
    """
    if not BS4_AVAILABLE:
        return _tavily_fallback(name, current_phone, current_email, current_website,
                                reason="bs4 not installed — run: pip install beautifulsoup4")

    current_phone   = current_phone   or "N/A"
    current_email   = current_email   or "N/A"
    current_website = current_website or "N/A"

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-NZ,en;q=0.9",
    }

    # ── Helpers ───────────────────────────────────────────────────────────

    def _clean_ph(p: str) -> str:
        return re.sub(r"[\s\-\.\(\)]", "", p)

    def _abs_url(href: str, base: str) -> str:
        """Make a relative href absolute."""
        if href.startswith("http"):
            return href
        m = re.match(r"(https?://[^/]+)", base)
        root = m.group(1) if m else base.rstrip("/")
        return root + href if href.startswith("/") else base.rstrip("/") + "/" + href

    def _extract_contacts(soup, page_url: str) -> dict:
        """Pull phones, emails, and contact links from a parsed page."""
        text = soup.get_text(" ", strip=True)

        # Phones from visible text
        raw = re.findall(r"(?:\+64|0)[\d\s\-\.\(\)]{7,14}", text)
        phones = list(dict.fromkeys(
            _clean_ph(p) for p in raw if 9 <= len(_clean_ph(p)) <= 11
        ))

        # Emails: mailto hrefs first (most reliable), then plain text
        emails = []
        for a in soup.find_all("a", href=re.compile(r"^mailto:", re.I)):
            addr = a["href"][7:].split("?")[0].strip().lower()
            if addr and "@" in addr and not addr.endswith((".png",".jpg",".gif")):
                emails.append(addr)
        if not emails:
            emails = list(dict.fromkeys(
                m.lower() for m in re.findall(
                    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", text)
                if not m.endswith((".png",".jpg",".gif",".svg"))
                and "example" not in m
            ))

        # Contact page links
        contact_links = []
        for a in soup.find_all("a", href=True):
            lbl  = a.get_text(strip=True).lower()
            href = a["href"].lower()
            if "contact" in lbl or "contact" in href:
                contact_links.append(_abs_url(a["href"], page_url))

        return {
            "phones":        phones[:4],
            "emails":        emails[:4],
            "contact_links": list(dict.fromkeys(contact_links))[:3],
        }

    def _fetch(url: str) -> tuple:
        """GET a URL; return (soup, final_url) or (None, error_str)."""
        try:
            r = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
            if r.status_code >= 400:
                return None, f"HTTP {r.status_code}"
            # Detect JS-only shells (Cloudflare, SPAs) — very short or no visible text
            soup = BeautifulSoup(r.text, "html.parser")
            visible = soup.get_text(" ", strip=True)
            if len(visible) < 200:
                return None, "js_heavy"
            return soup, r.url
        except requests.RequestException as e:
            return None, str(e)

    def _try_cache(original_url: str) -> tuple:
        """Try Google Cache then archive.org for a blocked URL."""
        # Google Cache
        gc_url = f"https://webcache.googleusercontent.com/search?q=cache:{original_url}"
        soup, err = _fetch(gc_url)
        if soup:
            return soup, gc_url, "google_cache"
        # archive.org — latest snapshot
        try:
            api = f"https://archive.org/wayback/available?url={original_url}"
            snap = requests.get(api, timeout=8).json()
            wb_url = snap.get("archived_snapshots",{}).get("closest",{}).get("url","")
            if wb_url:
                soup, err2 = _fetch(wb_url)
                if soup:
                    return soup, wb_url, "archive_org"
        except Exception:
            pass
        return None, original_url, "cache_failed"

    def _scrape_site(website_url: str) -> dict:
        """
        Full scrape: homepage → contact page → fallback to cache.
        Always uses homepage contacts even if no contact page found.
        """
        soup, final_url = _fetch(website_url)
        method = "direct"
        if soup is None:
            soup, final_url, method = _try_cache(website_url)
        if soup is None:
            return {"error": f"unreachable and not in cache", "url": website_url}

        home_contacts  = _extract_contacts(soup, final_url)
        all_phones     = list(home_contacts["phones"])
        all_emails     = list(home_contacts["emails"])
        contact_page   = None
        contact_method = method

        # Follow first contact link
        if home_contacts["contact_links"]:
            cp_url          = home_contacts["contact_links"][0]
            cp_soup, cp_fin = _fetch(cp_url)
            cp_method       = "direct"
            if cp_soup is None:
                cp_soup, cp_fin, cp_method = _try_cache(cp_url)
            if cp_soup:
                cp_contacts = _extract_contacts(cp_soup, cp_fin)
                all_phones  = list(dict.fromkeys(
                    p for p in (all_phones + cp_contacts["phones"])
                    if isinstance(p, str)
                ))
                all_emails  = list(dict.fromkeys(
                    e for e in (all_emails + cp_contacts["emails"])
                    if isinstance(e, str)
                ))
                contact_page   = cp_fin
                contact_method = cp_method

        return {
            "phones":       all_phones[:4],
            "emails":       all_emails[:4],
            "contact_page": contact_page,
            "method":       contact_method,
            "base_url":     final_url,
        }

    def _search_social(org_name: str) -> dict:
        """
        Search Facebook and Instagram pages for the org using Tavily.
        Returns contacts if found on social profiles.
        """
        if not TAVILY_KEY:
            return {}
        query = (
            f'"{org_name}" Hawke\'s Bay '
            f'site:facebook.com OR site:instagram.com'
        )
        try:
            resp = requests.post(
                "https://api.tavily.com/search",
                json={"api_key": TAVILY_KEY, "query": query,
                      "search_depth": "basic", "max_results": 3},
                timeout=15,
            )
            results = resp.json().get("results", [])
        except Exception:
            return {}

        if not results:
            return {}

        # Try to scrape the Facebook/Instagram page directly
        for r in results:
            url = r.get("url", "")
            if "facebook.com" in url or "instagram.com" in url:
                # Social sites block scrapers — extract from Tavily snippet only
                snippet = r.get("content", "")
                raw = re.findall(r"(?:\+64|0)[\d\s\-\.\(\)]{7,14}", snippet)
                phones = list(dict.fromkeys(
                    _clean_ph(p) for p in raw if 9 <= len(_clean_ph(p)) <= 11
                ))
                emails = list(dict.fromkeys(
                    m.lower() for m in re.findall(
                        r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", snippet)
                    if not m.endswith((".png",".jpg",".gif"))
                ))
                if phones or emails:
                    return {"phones": phones[:2], "emails": emails[:2],
                            "source": url, "method": "social_media"}

        return {}

    # ── Main routing logic ────────────────────────────────────────────────

    scraped    = None
    method_log = []

    if current_website not in ("N/A", "", None):
        # Primary path: scrape own website
        scraped = _scrape_site(current_website)
        if "error" not in scraped:
            method_log.append(scraped.get("method", "direct"))
        else:
            method_log.append(f"scrape_failed: {scraped['error']}")
            scraped = None

    if scraped is None:
        # No website or scrape failed — try social media first
        social = _search_social(name)
        if social.get("phones") or social.get("emails"):
            method_log.append("social_media")
            all_phones = [p for p in social.get("phones", []) if isinstance(p, str)]
            all_emails = [e for e in social.get("emails", []) if isinstance(e, str)]
            scraped = {
                "phones": all_phones, "emails": all_emails,
                "contact_page": social.get("source"),
                "method": "social_media",
            }
        else:
            method_log.append("tavily_fallback")
            reason = ("no website stored" if current_website in ("N/A","",None)
                      else "website unreachable")
            return _tavily_fallback(name, current_phone, current_email,
                                    current_website, reason=reason)

    all_phones   = scraped.get("phones", [])
    all_emails   = scraped.get("emails", [])
    contact_page = scraped.get("contact_page")

    # ── Pick the best matching phone from all found ───────────────────────
    def _phone_type(digits: str) -> str:
        """Classify a cleaned NZ phone number."""
        if digits.startswith("0800") or digits.startswith("0508"):
            return "freephone"
        if digits.startswith("02"):
            return "mobile"
        return "landline"

    def _best_phone(found_phones: list, stored_digits: str) -> Optional[str]:
        """
        From a list of found phones, pick the one most likely to be
        the organisation's main contact number.

        Priority:
          1. Exact match to stored (same digits) — already up to date
          2. Same type as stored (landline vs mobile vs freephone)
          3. Shares the longest common prefix with stored
          4. First non-freephone number (freephones are often crisis lines,
             not the org's direct number)
        """
        if not found_phones:
            return None
        stored_type = _phone_type(stored_digits) if stored_digits else "landline"

        # Score each candidate
        def _score(p: str) -> int:
            s = 0
            if p == stored_digits:
                s += 1000                          # exact match
            if _phone_type(p) == stored_type:
                s += 100                           # same type
            # Shared prefix length (up to 6 digits)
            for i in range(min(6, len(p), len(stored_digits))):
                if p[i] == stored_digits[i]:
                    s += 10
                else:
                    break
            if not p.startswith("0800") and not p.startswith("0508"):
                s += 5                             # prefer non-freephone
            return s

        return max(found_phones, key=_score)

    stored_phone_d = _clean_ph(current_phone)
    best_phone     = _best_phone(all_phones, stored_phone_d)
    discrepancies  = {}

    # Report the best candidate phone regardless of stored validity
    if best_phone:
        norm_best = _normalise_nz_phone(best_phone)
        # Only flag as discrepancy if stored phone is valid AND differs
        if 9 <= len(stored_phone_d) <= 11:
            if best_phone != stored_phone_d and norm_best != current_phone:
                discrepancies["phone"] = {
                    "stored": current_phone, "found": norm_best}
        # If stored is invalid (wrong digit count), always report what was found
        elif len(stored_phone_d) != 0:
            discrepancies["phone"] = {
                "stored":  current_phone,
                "found":   norm_best,
                "note":    "stored number has wrong digit count — found this on the website",
            }

    if all_emails and current_email not in ("N/A", ""):
        if all_emails[0] != current_email.lower():
            discrepancies["email"] = {
                "stored": current_email, "found": all_emails[0]}

    return {
        "organisation":    name,
        "stored_details":  {"phone": current_phone, "email": current_email,
                            "website": current_website},
        "scraped_from":    ([current_website] if current_website not in ("N/A","") else [])
                           + ([contact_page] if contact_page else []),
        "contact_page":    contact_page,
        "web_found":       {"phones": all_phones, "emails": all_emails[:2],
                            "best_phone": _normalise_nz_phone(best_phone) if best_phone else None},
        "discrepancies":   discrepancies,
        "has_discrepancy": len(discrepancies) > 0,
        "method":          " → ".join(method_log),
    }


def _tavily_fallback(name, current_phone, current_email, current_website, reason=""):
    """Last-resort: Tavily web search when scraping and social media both failed."""
    if not TAVILY_KEY:
        return {
            "organisation":    name,
            "has_discrepancy": False,
            "error": f"Cannot verify ({reason}) — TAVILY_API_KEY not configured.",
        }

    query = f'"{name}" contact phone Hawke\'s Bay New Zealand'
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={"api_key": TAVILY_KEY, "query": query,
                  "search_depth": "basic", "max_results": 3},
            timeout=15,
        )
        results = resp.json().get("results", [])
    except Exception as e:
        return {"organisation": name, "error": str(e), "has_discrepancy": False}

    def _clean_ph(p): return re.sub(r"[\s\-\.\(\)]", "", p)

    all_text = " ".join(r.get("content", "") for r in results)
    raw      = re.findall(r"(?:\+64|0)[\d\s\-\.\(\)]{7,14}", all_text)
    found_phones = list(dict.fromkeys(
        _clean_ph(p) for p in raw if 9 <= len(_clean_ph(p)) <= 11
    ))
    found_emails = list(dict.fromkeys(
        m.lower() for m in re.findall(
            r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", all_text)
        if not m.endswith((".png",".jpg",".gif"))
    ))

    stored_d = _clean_ph(current_phone or "")
    discrepancies = {}
    if found_phones and 9 <= len(stored_d) <= 11 and found_phones[0] != stored_d:
        discrepancies["phone"] = {
            "stored": current_phone,
            "found":  _normalise_nz_phone(found_phones[0]),
        }
    if found_emails and current_email not in ("N/A",""):
        if found_emails[0] != (current_email or "").lower():
            discrepancies["email"] = {
                "stored": current_email, "found": found_emails[0]}

    return {
        "organisation":    name,
        "stored_details":  {"phone": current_phone, "email": current_email,
                            "website": current_website},
        "web_found":       {"phones": found_phones[:2], "emails": found_emails[:2]},
        "discrepancies":   discrepancies,
        "has_discrepancy": len(discrepancies) > 0,
        "sources_used":    [r.get("url","") for r in results[:2]],
        "method":          f"tavily_fallback ({reason})",
        "fallback_reason": reason,
    }

mcp.tool()
def update_listing_meta(listing_id: int, listing_name: str,
                         field: str, new_value: str, reason: str = ""):
    """
    FUNCTION 3 — Update a contact field in WordPress after user approval.

    Only call this AFTER the user has explicitly approved the change.
    field must be one of: 'phone', 'email', 'website'.

    """
    if field not in LISTDOM_META_KEYS:
        return f"Invalid field '{field}'. Must be one of: {list(LISTDOM_META_KEYS.keys())}"

    # ── REST API with known meta key variants ────────────────────────────
    last_r = None
    for meta_key in LISTDOM_META_KEYS[field]:
        for method in ("POST", "PUT"):
            for url in [
                f"{WP_URL}/index.php?rest_route=/wp/v2/listdom-listing/{listing_id}",
                f"{WP_URL}/wp-json/wp/v2/listdom-listing/{listing_id}",
            ]:
                try:
                    r = requests.request(
                        method, url,
                        json={"meta": {meta_key: new_value}},
                        auth=AUTH,
                        headers={"Content-Type": "application/json"},
                        timeout=10
                    )
                    last_r = r
                    if r.status_code in (200, 201):
                        # Verify the value actually changed in wp_metadata
                        verify = requests.get(
                            f"{WP_URL}/index.php?rest_route=/wp/v2/listdom-listing/{listing_id}",
                            auth=AUTH, timeout=10
                        )
                        if verify.status_code == 200:
                            stored = (verify.json().get("wp_metadata") or {}).get(field)
                            if stored == new_value:
                                _log(listing_id, listing_name, "update",
                                     {"field": field, "meta_key": meta_key,
                                      "new_value": new_value, "method": method, "reason": reason})
                                return {
                                    "success":         True,
                                    "updated":         True,
                                    "listing":         listing_name,
                                    "field":           field,
                                    "new_value":       new_value,
                                    "confirmed_in_db": True,
                                    "message":         f"SUCCESS: {field} is now '{new_value}' in WordPress.",
                                }
                except requests.RequestException:
                    pass

    # ── All strategies failed ──────────────────────────────────────────────
    status = last_r.status_code if last_r else "no response"
    _log(listing_id, listing_name, "update_failed",
         {"field": field, "last_status": status})
    return {
        "success":     False,
        "updated":     False,
        "error":       "WORDPRESS_NOT_UPDATED",
        "listing":     listing_name,
        "field":       field,
        "http_status": status,
        "action":      (
            "The update FAILED. WordPress was NOT changed. "
            "Tell the user the field was NOT updated in WordPress."
        ),
    }

@mcp.tool()
def notify_admin(listing_name: str, listing_id: int,
                 field: str, old_value: str, new_value: str, source_url: str = ""):
    """
    FUNCTION 4 — Alert the administrator when a contact detail change is detected.

    Call this as soon as verify_listing_details finds a discrepancy —
    before the user approves or rejects the update. The admin is always
    informed of detected changes, regardless of what the user decides.

    Sends an email alert (if SMTP is configured in .env) and logs the
    event to the audit file either way.
    """
    _log(listing_id, listing_name, "admin_alert",
         {"field": field, "old": old_value, "new": new_value, "source": source_url})

    if not all([ADMIN_EMAIL, SMTP_USER, SMTP_PASS]):
        return (
            "Admin alert logged to audit file. "
            "To also send email alerts, set ADMIN_EMAIL, SMTP_USER, and SMTP_PASS in .env."
        )

    subject = f"[Directory Alert] Contact change detected — {listing_name}"
    body    = (
        f"The AI Directory Agent detected a potential contact detail change.\n\n"
        f"Listing      : {listing_name} (ID {listing_id})\n"
        f"Field        : {field}\n"
        f"Stored value : {old_value}\n"
        f"Found online : {new_value}\n"
        f"Source       : {source_url or 'web search'}\n\n"
        f"The agent has shown this to the user and is awaiting their approval "
        f"before making any changes in WordPress.\n\n"
        f"— Directory AI Agent  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )

    try:
        msg            = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"]    = SMTP_USER
        msg["To"]      = ADMIN_EMAIL
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)

        return f"Admin alert emailed to {ADMIN_EMAIL} — '{listing_name}' {field} change detected."

    except Exception as e:
        return f"Email failed ({e}). Change is still logged in the audit file."

@mcp.tool()
def audit_outdated(days_threshold: int = 365):
    """
    FUNCTION 5 — Yearly full audit scan.

    Finds all listings in WordPress that have not been updated within
    the given number of days (default 365 = 1 year). Returns their id,
    title, last_updated date, and days since last update.

    After calling this, run validate_listing and verify_listing_details
    on every listing returned to complete the full yearly audit.
    Finish by calling generate_report with all results.
    """
    raw = get_listings(per_page=100)
    if isinstance(raw, str):
        return f"Could not fetch listings: {raw}"
    # get_listings returns {"listings": [...], "count": N}
    listings = raw.get("listings", raw) if isinstance(raw, dict) else raw
    if not isinstance(listings, list):
        return f"Unexpected listings format: {str(listings)[:100]}"

    cutoff     = datetime.now() - timedelta(days=days_threshold)
    outdated   = []
    parse_fmts = ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%b %d, %Y"]

    for item in listings:
        raw = item.get("last_updated", "")
        if not raw or raw == "N/A":
            outdated.append({**item, "days_since_update": "unknown"})
            continue

        parsed = None
        for fmt in parse_fmts:
            try:
                parsed = datetime.strptime(raw[:19], fmt)
                break
            except ValueError:
                continue

        if parsed is None or parsed < cutoff:
            days = (datetime.now() - parsed).days if parsed else "unknown"
            outdated.append({**item, "days_since_update": days})

    _log(0, "SYSTEM", "yearly_audit_scan", {
        "total":           len(listings),
        "outdated_count":  len(outdated),
        "threshold_days":  days_threshold,
        "run_date":        datetime.now().strftime("%Y-%m-%d"),
    })

    return {
        "total_listings": len(listings),
        "outdated_count": len(outdated),
        "threshold_days": days_threshold,
        "run_date":       datetime.now().strftime("%Y-%m-%d"),
        "outdated":       outdated,
    }

@mcp.tool()
def generate_report(results: list):
    """
    Generate a plain-text audit report from a list of validate_listing results.
    Saves to a timestamped .txt file and returns the report text.
    Call this at the end of every scan or audit to summarise findings for the admin.
    """
    total   = len(results)
    valid   = sum(1 for r in results if r.get("status") == "valid")
    review  = sum(1 for r in results if r.get("status") == "review")
    invalid = sum(1 for r in results if r.get("status") == "invalid")

    lines = [
        "=" * 62,
        "  Hawke's Bay Community Directory — Audit Report",
        f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "=" * 62,
        f"  Total listings scanned : {total}",
        f"  ✓ Valid                : {valid}",
        f"  ⚠ Needs review         : {review}",
        f"  ✗ Invalid              : {invalid}",
        "",
    ]

    for r in results:
        if r.get("issues"):
            lines.append(f"  ⚠  {r.get('listing_name')}  [score: {r.get('score', '?')}/100]")
            for issue in r.get("issues", []):
                lines.append(f"       • {issue}")
            lines.append("")

    report_text = "\n".join(lines)
    path = f"audit_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

    with open(path, "w", encoding="utf-8") as f:
        f.write(report_text)

    return {"report": report_text, "saved_to": path}

if __name__ == "__main__":
    mcp.run()