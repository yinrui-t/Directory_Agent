import os
import re
import json
import smtplib
import requests
import dns.resolver
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from mcp.server.fastmcp import FastMCP
from typing import Optional
from dotenv import load_dotenv


try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

load_dotenv()

# ─────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────

mcp = FastMCP("Directory-Manager")

WP_URL       = os.getenv("WP_URL")
AUTH         = (os.getenv("WP_USERNAME"), os.getenv("WP_APP_PASSWORD"))
TAVILY_KEY   = os.getenv("TAVILY_API_KEY")
ADMIN_EMAIL  = os.getenv("ADMIN_EMAIL")
SMTP_HOST    = os.getenv("SMTP_HOST",   "smtp.gmail.com")
SMTP_PORT    = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER    = os.getenv("SMTP_USER")
SMTP_PASS    = os.getenv("SMTP_PASS")
AUDIT_FILE   = "directory_audit.jsonl"

API_ENDPOINT = f"{WP_URL}/index.php?rest_route=/wp/v2/listdom-listing"
NZ_PHONE_RE  = re.compile(r"^(\+64|0)(2[0-9]|[3-9])\d{6,8}$")

# Known Listdom meta key variants (tried in order for REST API updates)
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


# ─────────────────────────────────────────────────────────
# INTERNAL — append entry to audit log
# ─────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────
# TOOL — get_listings
# ─────────────────────────────────────────────────────────

@mcp.tool()
def get_listings(query: str = None, per_page: int = 50):
    """
    Fetch community service listings from WordPress / Listdom.
    Use 'query' to search for a specific listing by name.
    Returns id, title, last_updated, phone, email, and website for each listing.
    Always call this first at the start of any audit or check.
    """
    url = API_ENDPOINT + f"&per_page={per_page}"
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

        def _parse_php_serialized(value: str) -> dict:
            """
            Parse a PHP serialized array into a Python dict.
            Handles: a:N:{s:K:"key";s:V:"value";...}
            Returns {} if parsing fails.
            """
            try:
                result = {}
                # Match all s:len:"key";s:len:"value"; pairs
                pairs = re.findall(
                    r's:\d+:"([^"]*?)";s:\d+:"([^"]*?)";', value)
                for k, v in pairs:
                    result[k] = v
                # Also match s:key;s:val with other types (i = int, b = bool)
                int_pairs = re.findall(r's:\d+:"([^"]*?)";i:(\d+);', value)
                for k, v in int_pairs:
                    result[k] = int(v)
                return result
            except Exception:
                return {}

        def _extract_cta_url(item: dict) -> str:
            """
            Extract the CTA contact URL — tries two approaches:
            1. Parse lsd_call_to_action from wp_metadata / raw meta (if REST exposes it)
            2. Scrape the listing's own frontend page and read the CTA button href
               (works with Listdom Free where REST doesn't expose the meta)
            """
            # ── Approach 1: REST meta ─────────────────────────────────────
            meta = item.get("wp_metadata", {})
            cta_raw = meta.get("call_to_action") or meta.get("lsd_call_to_action", "")
            if not cta_raw:
                for meta_item in item.get("meta", []):
                    if isinstance(meta_item, dict) and "call_to_action" in meta_item.get("key",""):
                        cta_raw = meta_item.get("value", "")
                        break
            if cta_raw:
                cta = _parse_php_serialized(cta_raw)
                url = cta.get("url", "").strip()
                if url and url.startswith("http") and cta.get("mode") in ("custom", ""):
                    return url

            # ── Approach 2: scrape listing frontend page ──────────────────
            # The listing slug is in the REST response link field
            listing_link = item.get("link", "")
            if not listing_link:
                return ""
            try:
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                }
                r = requests.get(listing_link, headers=headers, timeout=8, allow_redirects=True)
                if r.status_code >= 400:
                    return ""
                from bs4 import BeautifulSoup as _BS4
                soup = _BS4(r.text, "html.parser")

                # Listdom renders CTA as <a> with class containing 'cta' or 'contact'
                # Also look for buttons with href pointing to an external contact page
                for selector in [
                    'a.lsd-cta',
                    'a.listdom-cta',
                    'a[class*="cta"]',
                    'a[class*="contact"]',
                    'a[href*="contact"]',
                ]:
                    tag = soup.select_one(selector)
                    if tag and tag.get("href","").startswith("http"):
                        href = tag["href"]
                        # Must be external (not the same listing page)
                        if listing_link.rstrip("/") not in href.rstrip("/"):
                            return href

                # Fallback: any <a> whose visible text is "Contact Us" / "Contact"
                for a in soup.find_all("a", href=True):
                    txt = a.get_text(strip=True).lower()
                    if txt in ("contact us", "contact", "get in touch", "enquire"):
                        href = a["href"]
                        if href.startswith("http") and listing_link.rstrip("/") not in href.rstrip("/"):
                            return href
            except Exception:
                pass
            return ""

        def _norm_phone(raw):
            """Normalise phone on load — removes (0x) bracket style, standardises spacing."""
            if not raw or raw == "N/A":
                return raw
            normed = _normalise_nz_phone(raw)
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
                "link":         item.get("link", ""),
                "cta_url":      _extract_cta_url(item),
            }
            for item in data
        ]
        # Return as a dict so FastMCP serialises as a single JSON object,
        # not newline-delimited objects (which breaks JSON parsing in the GUI).
        return {"listings": listings, "count": len(listings)}
    except Exception as e:
        return f"Connection error: {str(e)}"


# ─────────────────────────────────────────────────────────
# TOOL — validate_listing  [Function 1]
# ─────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────
# TOOL — verify_listing_details  [Function 2]
# ─────────────────────────────────────────────────────────

def _playwright_scrape(url: str, own_domain: str = "") -> dict:
    """
    Use Playwright to render a JS-heavy page and extract contacts.
    Falls back gracefully if Playwright is not installed or fails.
    Returns dict with phones/emails lists, or None on failure.
    """
    try:
        from playwright.sync_api import sync_playwright
        from bs4 import BeautifulSoup
    except ImportError:
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page    = browser.new_page()
            page.set_extra_http_headers({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"
            })
            page.goto(url, wait_until="networkidle", timeout=20000)
            # Wait a moment for any lazy-loaded content
            page.wait_for_timeout(1500)
            html = page.content()
            browser.close()

        soup     = BeautifulSoup(html, "html.parser")
        contacts = _playwright_extract(soup, url, own_domain)
        return contacts if (contacts["phones"] or contacts["emails"]) else None
    except Exception as e:
        return None


def _playwright_extract(soup, page_url: str, own_domain: str = "") -> dict:
    """Extract contacts from a Playwright-rendered soup — reuses email/phone logic."""
    import re as _re
    text = soup.get_text(" ", strip=True)

    def _cl(p): return _re.sub(r"[\s\-\.\(\)]", "", p)
    raw    = _re.findall(r"(?:\+64|0)[\d\s\-\.\(\)]{7,14}", text)
    phones = list(dict.fromkeys(d for p in raw if 9 <= len(d := _cl(p)) <= 11))

    emails_raw = []
    for a in soup.find_all("a", href=_re.compile(r"^mailto:", _re.I)):
        addr = a["href"][7:].split("?")[0].strip().lower()
        if addr and "@" in addr:
            emails_raw.append(addr)
    for m in _re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", text):
        m = m.lower()
        if not m.endswith((".png",".jpg",".gif",".svg")) and "example" not in m \
                and len(m.split("@")[0]) <= 40:
            emails_raw.append(m)

    # Filter system/govt emails
    EXCLUDE = {"govt", "dhb", "noreply", "no-reply", "hrcoordinator",
               "businesssystems", "recruitment", "careers@", "system@"}

    def _bad(e):
        domain = e.split("@")[-1] if "@" in e else ""
        local  = e.split("@")[0]  if "@" in e else e
        if len(local) > 25: return True
        if _re.search(r"\b(govt|\.dhb)\b", domain): return True
        if any(p in e for p in EXCLUDE): return True
        return False

    def _rank(e):
        domain = e.split("@")[-1] if "@" in e else ""
        if own_domain and (own_domain in domain or domain in own_domain): return 0
        if domain.endswith(".co.nz") or domain.endswith(".org.nz"):       return 1
        if domain.endswith(".nz"):                                         return 2
        return 3

    emails = [e for e in sorted(dict.fromkeys(emails_raw), key=_rank) if not _bad(e)]
    return {"phones": phones[:4], "emails": emails[:4]}


@mcp.tool()
def verify_listing_details(name: str,
                            current_phone: Optional[str] = None,
                            current_email: Optional[str] = None,
                            current_website: Optional[str] = None,
                            cta_url: Optional[str] = None):
    """
    FUNCTION 2 — Verify contact details are current by scraping the org's own website.

    Strategy (in order):
      1. If cta_url is set: scrape it directly (it is the org's own Contact Us page)
      2. Otherwise scrape the stored website homepage + follow Contact page link
      3. If blocked/unreachable: try Google Cache, then archive.org
      4. If no website stored: search Facebook/Instagram for the org, then Tavily
      5. Facebook/Instagram searched for email regardless (fills in missing email)
    """
    if not BS4_AVAILABLE:
        return _tavily_fallback(name, current_phone, current_email, current_website,
                                reason="bs4 not installed — run: pip install beautifulsoup4")

    current_phone   = current_phone   or "N/A"
    current_email   = current_email   or "N/A"
    current_website = current_website or "N/A"
    cta_url         = cta_url         or ""

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

    def _extract_contacts(soup, page_url: str, own_domain: str = "") -> dict:
        """Pull phones, emails, and contact links from a parsed page.
        Decodes Cloudflare email protection, extracts from script JSON data.
        own_domain: if set, emails from this domain are ranked first."""
        text = soup.get_text(" ", strip=True)

        # ── Decode Cloudflare-protected emails ────────────────────────────
        # Cloudflare encodes emails as: /cdn-cgi/l/email-protection#<hex>
        # First byte of hex is XOR key; remaining pairs XOR'd to give email chars
        def _decode_cf_email(encoded_hex: str) -> str:
            try:
                key = int(encoded_hex[:2], 16)
                return "".join(
                    chr(int(encoded_hex[i:i+2], 16) ^ key)
                    for i in range(2, len(encoded_hex), 2)
                )
            except Exception:
                return ""

        cf_emails = []
        for a in soup.find_all("a", href=re.compile(r"cdn-cgi/l/email-protection", re.I)):
            href = a.get("href", "")
            if "#" in href:
                decoded = _decode_cf_email(href.split("#")[-1])
                if decoded and "@" in decoded:
                    cf_emails.append(decoded.lower())

        # Also extract text from <script> tags containing JSON (Next.js, JSON-LD, etc.)
        # These often contain pre-rendered contact data not visible in soup.get_text()
        script_text = ""
        for script in soup.find_all("script"):
            s_type = script.get("type", "")
            s_id   = script.get("id", "")
            content = script.string or ""
            if (s_id == "__NEXT_DATA__"
                    or s_type in ("application/json", "application/ld+json")
                    or (content and "@" in content and len(content) < 200000)):
                script_text += " " + content

        # Combine visible text + script data for email/phone extraction
        full_text = text + " " + script_text

        # Phones from visible text + script JSON data
        raw = re.findall(r"(?:\+64|0)[\d\s\-\.\(\)]{7,14}", full_text)
        phones = list(dict.fromkeys(
            _clean_ph(p) for p in raw if 9 <= len(_clean_ph(p)) <= 11
        ))

        # Emails: mailto links + visible text + script JSON data
        mailto_emails = []
        for a in soup.find_all("a", href=re.compile(r"^mailto:", re.I)):
            addr = a["href"][7:].split("?")[0].strip().lower()
            if addr and "@" in addr and not addr.endswith((".png",".jpg",".gif")):
                mailto_emails.append(addr)

        text_emails = list(dict.fromkeys(
            m.lower() for m in re.findall(
                r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", full_text)
            if not m.endswith((".png",".jpg",".gif",".svg"))
            and "example" not in m
            and len(m.split("@")[0]) <= 40
        ))

        emails_raw = list(dict.fromkeys(cf_emails + mailto_emails + text_emails))

        # Unified email exclusion: removes system, govt, unrelated-org emails
        EXCLUDE_DOMAIN_PATTERNS = [
            "hbdhb.govt.nz", "hawkesbaydhb.govt.nz", "govt.nz",
            "health.govt.nz", "msd.govt.nz", "workandincome.govt.nz",
            "mhaids.health.nz", "waitematadhb.govt.nz", "adhb.govt.nz",
            "privatecarenz.com", "freedommedical.co.nz", "healthcarerehab.co.nz",
        ]
        SYSTEM_PATTERNS = ["noreply", "no-reply", "donotreply", "do-not-reply",
                           "businesssystems", "businessteam", "itteam", "system@",
                           "hrcoordinator", "hr@", "recruitment", "careers@", "jobs@",
                           "payroll", "accounts@", "nursing@"]
        # admin@ is fine for small orgs — only exclude if local part is very generic on a non-org domain
        # (handled by length check and own_domain ranking instead)

        def _should_exclude(addr: str) -> bool:
            local  = addr.split("@")[0] if "@" in addr else addr
            domain = addr.split("@")[-1] if "@" in addr else ""
            # Very long local part = system/internal
            if len(local) > 25:
                return True
            # Domain contains govt or dhb keywords (catches truncated TLDs too)
            if re.search(r"\b(govt|\.dhb)\b", domain):
                return True
            # System/noreply patterns
            if any(p in addr.lower() for p in SYSTEM_PATTERNS):
                return True
            # Exact or suffix domain match against known exclusion list
            if any(domain == ex or domain.endswith("." + ex)
                   for ex in EXCLUDE_DOMAIN_PATTERNS):
                return True
            return False

        def _email_rank(addr: str) -> int:
            if _should_exclude(addr):
                return 99
            domain = addr.split("@")[-1] if "@" in addr else ""
            if own_domain and (own_domain in domain or domain in own_domain):
                return 0   # org's own domain — top priority
            if domain.endswith(".co.nz") or domain.endswith(".org.nz"):
                return 1
            if domain.endswith(".nz"):
                return 2
            return 3

        emails = [e for e in sorted(set(emails_raw), key=_email_rank)
                  if _email_rank(e) < 99]

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
        # Derive org's own domain for email preference ranking
        own_domain = re.sub(r"https?://(www\.)?", "", website_url).split("/")[0].lower()

        soup, final_url = _fetch(website_url)
        method = "direct"
        if soup is None:
            soup, final_url, method = _try_cache(website_url)
        if soup is None:
            return {"error": f"unreachable and not in cache", "url": website_url}

        home_contacts  = _extract_contacts(soup, final_url, own_domain)
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
                cp_contacts = _extract_contacts(cp_soup, cp_fin, own_domain)
                all_phones  = list(dict.fromkeys(
                    p for p in (all_phones + cp_contacts["phones"])
                    if isinstance(p, str)
                ))
                # Merge emails, keeping own-domain ones first
                merged_emails = cp_contacts["emails"] + [
                    e for e in all_emails if e not in cp_contacts["emails"]
                ]
                all_emails  = list(dict.fromkeys(
                    e for e in merged_emails if isinstance(e, str)
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

    def _try_playwright_if_no_email(website_url, own_domain, existing):
        """If scrape found no emails, try Playwright rendering."""
        if existing.get("emails"):
            return existing
        pw = _playwright_scrape(website_url, own_domain)
        if pw and pw.get("emails"):
            existing = dict(existing)
            existing["emails"] = pw["emails"]
            # Also supplement phones
            if pw.get("phones") and not existing.get("phones"):
                existing["phones"] = pw["phones"]
        return existing

    def _search_social(org_name: str, own_domain: str = "") -> dict:
        """
        Search for org contacts on Facebook/Instagram AND via targeted email search.
        If own_domain is provided, also search specifically for that domain's email.
        Returns phones and emails found.
        """
        if not TAVILY_KEY:
            return {}

        results_all = []

        queries = [
            # Query 1: Facebook/Instagram presence
            f'"{org_name}" Hawke\'s Bay site:facebook.com OR site:instagram.com',
            # Query 2: Targeted email search
            f'"{org_name}" email contact Hawke\'s Bay New Zealand "@"',
            # Query 3: Trusted NZ directories (healthpoint, familyservices)
            f'"{org_name}" email site:healthpoint.co.nz OR site:familyservices.govt.nz',
        ]
        # Query 4: Domain-specific email search (most targeted)
        if own_domain:
            queries.append(f'"{org_name}" "@{own_domain}" contact email')

        for query in queries:
            try:
                resp = requests.post(
                    "https://api.tavily.com/search",
                    json={"api_key": TAVILY_KEY, "query": query,
                          "search_depth": "basic", "max_results": 3},
                    timeout=15,
                )
                if resp.status_code >= 400:
                    continue
                results_all.extend(resp.json().get("results", []))
            except Exception:
                continue

        if not results_all:
            return {}

        phones, emails, source = [], [], ""
        SKIP = {"whitepages.co.nz", "yellow.co.nz", "finda.co.nz",
                "localist.co.nz", "hotfrog.co.nz",
                "nzqa.govt.nz", "charities.govt.nz", "companies.govt.nz",
                "businessdirectory.co.nz", "truelocal.com.au"}

        # Email domains that belong to different orgs in the region — exclude
        SKIP_EMAIL_DOMS_SOCIAL = {"healthhb.co.nz", "hbdhb.govt.nz", "hawkesbaydhb.govt.nz",
                                   "mhaids.health.nz", "privatecarenz.com",
                                   "freedommedical.co.nz", "healthcarerehab.co.nz"}
        SKIP_EMAIL_DOMAINS = {"govt.nz","health.govt.nz","msd.govt.nz",
                               "hbdhb.govt.nz","mhaids.health.nz"}

        def _bad_email(addr: str) -> bool:
            local  = addr.split("@")[0] if "@" in addr else addr
            domain = addr.split("@")[-1] if "@" in addr else ""
            if len(local) > 25: return True
            if re.search(r"\b(govt|\.dhb)\b", domain): return True
            if any(domain == d or domain.endswith("."+d) for d in SKIP_EMAIL_DOMAINS): return True
            if any(domain == d or domain.endswith("."+d) for d in SKIP_EMAIL_DOMS_SOCIAL): return True
            if any(p in addr for p in ("noreply","no-reply","donotreply","businesssystems","itteam","comms@","system@","nursing@")): return True
            return False

        # Trusted NZ directory sources — process these first, emails from them rank higher
        TRUSTED_SOURCES = {"healthpoint.co.nz", "familyservices.govt.nz"}
        results_sorted = sorted(results_all,
                                key=lambda r: 0 if any(t in r.get("url","") for t in TRUSTED_SOURCES) else 1)

        is_trusted = lambda url: any(t in url for t in TRUSTED_SOURCES)

        for r in results_sorted:
            url     = r.get("url", "")
            snippet = r.get("content", "") + " " + r.get("title", "")
            if any(d in url for d in SKIP):
                continue

            raw = re.findall(r"(?:\+64|0)[\d\s\-\.\(\)]{7,14}", snippet)
            for p in raw:
                d = _clean_ph(p)
                if 9 <= len(d) <= 11:
                    phones.append(d)

            for m in re.findall(
                    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", snippet):
                m = m.lower()
                if not m.endswith((".png",".jpg",".gif",".svg")) and "example" not in m \
                        and not _bad_email(m):
                    # From trusted sources: only accept own-domain emails
                    if is_trusted(url) and own_domain:
                        if own_domain in m.split("@")[-1]:
                            emails.append(m)
                    else:
                        emails.append(m)

            if (phones or emails) and not source:
                source = url

        phones = list(dict.fromkeys(phones))
        emails = list(dict.fromkeys(emails))
        # Sort emails: own domain first
        if own_domain and emails:
            emails = sorted(emails, key=lambda e: 0 if own_domain in e else 1)
        if phones or emails:
            return {"phones": phones[:2], "emails": emails[:2],
                    "source": source, "method": "social_media"}
        return {}

    # ── Main routing logic ────────────────────────────────────────────────

    scraped      = None
    social_data  = None
    method_log   = []

    # Step 1: if CTA URL is set, scrape it directly — it IS the Contact Us page
    if cta_url and cta_url.startswith("http"):
        method_log.append("cta_url")
        own_domain = re.sub(r"https?://(www\.)?", "", current_website).split("/")[0].lower() \
                     if current_website not in ("N/A","") else ""
        soup, final_url = _fetch(cta_url)
        if soup is None:
            soup, final_url, _ = _try_cache(cta_url)
        if soup:
            from bs4 import BeautifulSoup as _BS
            contacts = _extract_contacts(soup, final_url, own_domain)
            # Debug: log what was found
            import logging as _log
            _log.getLogger().setLevel(_log.DEBUG)
            print(f"[CTA scrape] emails found: {contacts['emails']}", flush=True)
            print(f"[CTA scrape] phones found: {contacts['phones']}", flush=True)
            # If no emails found, try Playwright JS rendering
            if not contacts["emails"]:
                contacts = _playwright_scrape(cta_url, own_domain) or contacts
            scraped = {
                "phones":       contacts["phones"][:4],
                "emails":       contacts["emails"][:4],
                "contact_page": final_url,
                "method":       "cta_url",
                "base_url":     final_url,
            }
        else:
            method_log.append("cta_url_failed")

    # Step 2: if no CTA or CTA failed, fall back to website scrape
    if scraped is None and current_website not in ("N/A", "", None):
        _wd = re.sub(r"https?://(www\.)?", "", current_website).split("/")[0].lower()
        scraped = _scrape_site(current_website)
        if "error" not in scraped:
            method_log.append(scraped.get("method", "direct"))
            # If no email found in static HTML, try JS rendering
            if not scraped.get("emails"):
                scraped = _try_playwright_if_no_email(current_website, _wd, scraped)
        else:
            method_log.append(f"scrape_failed: {scraped['error']}")
            scraped = None

    # Step 2.5: if no website stored, try deriving it from the email domain
    # e.g. info.c6@literacy.org.nz → try https://www.literacy.org.nz
    if scraped is None and current_email not in ("N/A", "", None) and "@" in current_email:
        email_domain = current_email.split("@")[-1].lower().strip()
        derived_url  = f"https://www.{email_domain}"
        derived_scrape = _scrape_site(derived_url)
        if "error" not in derived_scrape:
            method_log.append(f"email_domain({email_domain})")
            scraped = derived_scrape
        else:
            # Also try without www
            derived_url2 = f"https://{email_domain}"
            derived_scrape2 = _scrape_site(derived_url2)
            if "error" not in derived_scrape2:
                method_log.append(f"email_domain({email_domain})")
                scraped = derived_scrape2

    # Step 3: search Facebook/Instagram + domain-targeted email search
    # Derive own_domain from website or CTA url for targeted search
    _own_domain_for_search = ""
    if current_website not in ("N/A","",None):
        _own_domain_for_search = re.sub(r"https?://(www\.)?", "", current_website).split("/")[0].lower()
    elif current_email not in ("N/A","",None) and "@" in current_email:
        _own_domain_for_search = current_email.split("@")[-1].lower().strip()
    social_data = _search_social(name, own_domain=_own_domain_for_search)
    if social_data.get("emails"):
        method_log.append("web search")

    if scraped is None:
        # Website unavailable — use social as primary source
        if social_data.get("phones") or social_data.get("emails"):
            method_log = [m for m in method_log if m != "web search"]
            method_log.append("social_media")
            scraped = {
                "phones": [p for p in social_data.get("phones", []) if isinstance(p, str)],
                "emails": [e for e in social_data.get("emails", []) if isinstance(e, str)],
                "contact_page": social_data.get("source"),
                "method": "social_media",
            }
        else:
            method_log.append("tavily_fallback")
            reason = ("no website stored" if current_website in ("N/A","",None)
                      else "website unreachable")
            return _tavily_fallback(name, current_phone, current_email,
                                    current_website, reason=reason)
    else:
        # Website scraped — merge social emails if:
        # 1. Scrape found no emails, OR
        # 2. Social has own-domain emails the scrape missed
        own_domain_str = re.sub(r"https?://(www\.)?", "", current_website).split("/")[0].lower() \
                         if current_website not in ("N/A","") else ""
        social_emails = [e for e in social_data.get("emails", []) if isinstance(e, str)]
        scrape_emails = scraped.get("emails", [])

        if not scrape_emails:
            # No emails from scrape — use social
            if social_emails:
                scraped["emails"] = social_emails
                scraped["social_source"] = social_data.get("source", "")
        elif own_domain_str and social_emails:
            # Add social emails that belong to own domain and aren't already in scrape
            own_social = [e for e in social_emails
                          if own_domain_str in e and e not in scrape_emails]
            if own_social:
                scraped["emails"] = list(dict.fromkeys(own_social + scrape_emails))
                scraped["social_source"] = social_data.get("source", "")

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
    confirmed      = {}   # fields verified and matching stored value

    # ── Phone ─────────────────────────────────────────────────────────────
    if best_phone:
        norm_best = _normalise_nz_phone(best_phone)
        if 9 <= len(stored_phone_d) <= 11:
            if best_phone != stored_phone_d and norm_best != current_phone:
                discrepancies["phone"] = {"stored": current_phone, "found": norm_best}
            else:
                confirmed["phone"] = norm_best
        elif len(stored_phone_d) != 0:
            discrepancies["phone"] = {
                "stored": current_phone, "found": norm_best,
                "note":   "stored number has wrong digit count — found this on the website",
            }

    # ── Email ─────────────────────────────────────────────────────────────
    def _best_email(found_emails: list, stored_email: str, own_domain: str = "") -> str:
        """
        Pick the best matching email from found list.
        Priority:
          1. Exact match to stored
          2. Own domain (website domain) — strongest signal
          3. Same local part as stored
          4. Same domain as stored
          5. Generic role addresses (info@, admin@, contact@)
          6. NZ domains (.co.nz, .org.nz) over foreign
          7. Shared local-part prefix
        """
        if not found_emails:
            return ""
        stored_l = stored_email.lower().strip()
        stored_local  = stored_l.split("@")[0]  if "@" in stored_l else stored_l
        stored_domain = stored_l.split("@")[-1] if "@" in stored_l else ""

        GENERIC_LOCALS = {"info", "admin", "contact", "hello", "enquiries",
                          "enquiry", "general", "office", "team", "support"}

        def _score(e: str) -> int:
            s = 0
            el = e.lower().strip()
            elocal  = el.split("@")[0]  if "@" in el else el
            edomain = el.split("@")[-1] if "@" in el else ""
            if el == stored_l:                                           s += 1000
            if own_domain and (own_domain in edomain or edomain in own_domain):
                                                                         s += 500
            if elocal == stored_local:                                   s += 200
            if edomain == stored_domain:                                 s += 100
            if elocal in GENERIC_LOCALS:                                 s += 50
            if edomain.endswith(".co.nz") or edomain.endswith(".org.nz"):s += 30
            elif edomain.endswith(".nz"):                                s += 20
            for i in range(min(len(elocal), len(stored_local))):
                if elocal[i] == stored_local[i]:                         s += 10
                else:                                                     break
            return s

        return max(found_emails, key=_score)

    if all_emails:
        stored_email_l = (current_email or "").lower().strip()
        best_email = _best_email(all_emails, current_email or "",
                                  own_domain=_own_domain_for_search)
        found_email = best_email.lower().strip()
        found_edomain = found_email.split("@")[-1] if "@" in found_email else ""

        # Confidence check: if we have a known own_domain, only trust emails from:
        # 1. The own domain itself, OR
        # 2. A .nz domain (at minimum likely NZ-based)
        # Don't flag a foreign .com/.org as authoritative if own_domain is known
        _email_trustworthy = (
            not _own_domain_for_search  # no own domain known — trust anything
            or (_own_domain_for_search in found_edomain)  # own domain match
            or found_edomain.endswith(".nz")               # any NZ domain
        )

        if current_email not in ("N/A", "", None):
            if found_email == stored_email_l:
                confirmed["email"] = found_email
            elif _email_trustworthy:
                discrepancies["email"] = {"stored": current_email, "found": found_email}
            else:
                # Found email is from an unrelated foreign domain — don't report as discrepancy
                confirmed["email_not_found_online"] = True
        else:
            # No email stored
            if _email_trustworthy:
                confirmed["email_found_online"] = found_email
            else:
                confirmed["email_missing_and_not_found"] = True
    else:
        # No email found online — report explicitly
        if current_email not in ("N/A", "", None):
            confirmed["email_not_found_online"] = True
        else:
            confirmed["email_missing_and_not_found"] = True

    # ── Website ───────────────────────────────────────────────────────────
    # Derive the website found from the scrape source
    found_website_url = (scraped.get("base_url","") or
                         scraped.get("contact_page","") or
                         cta_url or "")
    found_domain = re.sub(r"https?://(www\.)?", "", found_website_url).split("/")[0].lower() \
                   if found_website_url else ""
    found_root   = f"https://{found_domain}" if found_domain else ""

    if current_website not in ("N/A", "", None):
        # Website stored — confirm or flag redirect
        stored_domain = re.sub(r"https?://(www\.)?", "", current_website).rstrip("/").lower()
        if found_domain and (stored_domain in found_domain or found_domain in stored_domain):
            confirmed["website"] = current_website
        elif found_domain and found_domain != stored_domain:
            discrepancies["website"] = {
                "stored": current_website,
                "found":  found_root,
                "note":   "site redirected to a different domain",
            }
    else:
        # No website stored — suggest the one we scraped from
        if found_root:
            confirmed["website_found_online"] = found_root

    social_src = scraped.get("social_source", "")
    return {
        "organisation":    name,
        "stored_details":  {"phone": current_phone, "email": current_email,
                            "website": current_website},
        "scraped_from":    ([current_website] if current_website not in ("N/A","") else [])
                           + ([contact_page] if contact_page else [])
                           + ([social_src] if social_src else []),
        "contact_page":    contact_page,
        "web_found":       {"phones": all_phones, "emails": all_emails[:2],
                            "best_phone": _normalise_nz_phone(best_phone) if best_phone else None},
        "confirmed":       confirmed,
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
        if resp.status_code == 402:
            return {"organisation": name, "has_discrepancy": False,
                    "error": "Tavily API credits exhausted — top up at tavily.com to re-enable web search fallback."}
        if resp.status_code == 401:
            return {"organisation": name, "has_discrepancy": False,
                    "error": "Tavily API key invalid or expired — check TAVILY_API_KEY in .env."}
        if resp.status_code == 429:
            return {"organisation": name, "has_discrepancy": False,
                    "error": "Tavily rate limit hit — try again in a few seconds."}
        if resp.status_code >= 400:
            return {"organisation": name, "has_discrepancy": False,
                    "error": f"Tavily error {resp.status_code}: {resp.text[:120]}"}
        results = resp.json().get("results", [])
    except Exception as e:
        return {"organisation": name, "error": str(e), "has_discrepancy": False}

    def _clean_ph(p): return re.sub(r"[\s\-\.\(\)]", "", p)

    # Filter: skip known aggregator/directory sites that list wrong contacts
    SKIP_DOMAINS = {"whitepages.co.nz", "yellow.co.nz", "finda.co.nz",
                    "localist.co.nz", "hotfrog.co.nz",
                    "nzqa.govt.nz", "charities.govt.nz", "companies.govt.nz",
                    "businessdirectory.co.nz"}
    filtered = [r for r in results
                if not any(d in r.get("url","") for d in SKIP_DOMAINS)]
    # If filtering leaves nothing, fall back to all results
    use_results = filtered if filtered else results

    # Try to extract org's own domain from email
    own_domain = ""
    if current_email and "@" in (current_email or ""):
        own_domain = current_email.split("@")[-1].lower()

    # Prefer results from org's own domain
    if own_domain:
        own_results = [r for r in use_results if own_domain in r.get("url","")]
        use_results = own_results if own_results else use_results

    all_text = " ".join(r.get("content", "") for r in use_results)
    raw      = re.findall(r"(?:\+64|0)[\d\s\-\.\(\)]{7,14}", all_text)
    found_phones = list(dict.fromkeys(
        _clean_ph(p) for p in raw if 9 <= len(_clean_ph(p)) <= 11
    ))
    found_emails = list(dict.fromkeys(
        m.lower() for m in re.findall(
            r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", all_text)
        if not m.endswith((".png",".jpg",".gif"))
        and "example" not in m
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

# ─────────────────────────────────────────────────────────
# TOOL — update_listing_meta  [Function 3]
# ─────────────────────────────────────────────────────────



@mcp.tool()
def update_listing_meta(listing_id: int, listing_name: str,
                         field: str, new_value: str, reason: str = ""):
    """
    FUNCTION 3 — Update a contact field in WordPress after user approval.

    Only call this AFTER the user has explicitly approved the change.
    field must be one of: 'phone', 'email', 'website'.

    Uses the Directory Agent Bridge plugin if installed (recommended).
    Falls back to direct REST API attempts if the plugin is not found.
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


# ─────────────────────────────────────────────────────────
# TOOL — notify_admin  [Function 4]
# ─────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────
# TOOL — audit_outdated  [Function 5]
# ─────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────
# TOOL — generate_report
# ─────────────────────────────────────────────────────────

@mcp.tool()
def generate_report(results: list):
    """
    Generate a plain-text + CSV audit report combining validate AND verify results.
    Saves both to timestamped files and returns paths + summary.
    """
    import csv as _csv

    total   = len(results)
    valid   = sum(1 for r in results if r.get("status") == "valid")
    review  = sum(1 for r in results if r.get("status") == "review")
    invalid = sum(1 for r in results if r.get("status") == "invalid")
    # Count verify discrepancies
    with_disc = sum(1 for r in results if r.get("verify_discrepancies"))
    ts      = datetime.now().strftime("%Y-%m-%d %H:%M")
    ts_file = datetime.now().strftime("%Y%m%d_%H%M%S")

    # ── Plain-text report ─────────────────────────────────────────────────
    lines = [
        "=" * 66,
        "  Hawke's Bay Community Directory — Full Audit Report",
        f"  {ts}",
        "=" * 66,
        f"  Total listings scanned    : {total}",
        f"  ✓ Format valid            : {valid}",
        f"  ⚠ Needs review            : {review}",
        f"  ✗ Invalid                 : {invalid}",
        f"  🌐 Online discrepancies   : {with_disc}",
        "",
    ]
    for r in results:
        status = r.get("status", "unknown")
        icon   = "✓" if status == "valid" else ("⚠" if status == "review" else "✗")
        lines.append(f"  {icon}  {r.get('listing_name')}  [score: {r.get('score','?')}/100]  [{status.upper()}]")
        for issue in r.get("issues", []):
            lines.append(f"       Format: {issue}")
        # Verify discrepancies
        disc = r.get("verify_discrepancies", {})
        for field, diff in disc.items():
            note = f" ({diff['note']})" if diff.get("note") else ""
            lines.append(f"       Online: {field}: '{diff.get('stored')}' → '{diff.get('found')}'{note}")
        if r.get("issues") or disc:
            lines.append("")

    report_text = "\n".join(lines)
    txt_path = f"audit_report_{ts_file}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(report_text)

    # ── CSV report ────────────────────────────────────────────────────────
    csv_path = f"audit_report_{ts_file}.csv"
    csv_cols = [
        "Listing Name", "Validate Status", "Score",
        "Phone (stored)", "Email (stored)", "Website (stored)",
        "Format Issues",
        "Online Phone Found", "Online Email Found", "Online Website Found",
        "Online Discrepancies", "Verify Source",
        "Last Updated", "Operational Notes",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = _csv.DictWriter(f, fieldnames=csv_cols)
        writer.writeheader()
        for r in results:
            issues_str = "; ".join(r.get("issues", [])) or "None"
            disc       = r.get("verify_discrepancies", {})
            conf       = r.get("verify_confirmed", {})
            disc_str   = "; ".join(
                f"{fld}: '{d.get('stored')}' → '{d.get('found')}'"
                for fld, d in disc.items()
            ) or ("None" if r.get("verify_method") else "Not verified")
            # Extract individual online-found values
            o_phone   = disc.get("phone",   {}).get("found", "") or conf.get("phone", "")
            o_email   = disc.get("email",   {}).get("found", "") or conf.get("email", "") \
                        or conf.get("email_found_online", "")
            o_website = disc.get("website", {}).get("found", "") or conf.get("website", "") \
                        or conf.get("website_found_online", "")
            writer.writerow({
                "Listing Name":       r.get("listing_name", r.get("listing_id", "")),
                "Validate Status":    r.get("status", "unknown").capitalize(),
                "Score":              r.get("score", ""),
                "Phone (stored)":     r.get("phone",        "N/A"),
                "Email (stored)":     r.get("email",        "N/A"),
                "Website (stored)":   r.get("website",      "N/A"),
                "Format Issues":      issues_str,
                "Online Phone Found": o_phone,
                "Online Email Found": o_email,
                "Online Website Found": o_website,
                "Online Discrepancies": disc_str,
                "Verify Source":      r.get("verify_contact_page", "") or r.get("verify_method", ""),
                "Last Updated":       r.get("last_updated", "N/A"),
                "Operational Notes":  "",
            })

    return {
        "summary":    f"{total} listings — {valid} valid, {review} review, {invalid} invalid, {with_disc} online discrepancies",
        "saved_to":   txt_path,
        "csv_path":   csv_path,
        "total":      total,
        "valid":      valid,
        "review":     review,
        "invalid":    invalid,
        "with_discrepancies": with_disc,
        "report":     report_text,
    }


# ─────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
