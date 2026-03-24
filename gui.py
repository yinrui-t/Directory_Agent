import asyncio
import json
import os
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from datetime import datetime
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:2b")

# ─────────────────────────────────────────────────────────
# COLOURS + FONTS
# ─────────────────────────────────────────────────────────

BG       = "#0a0e17"
SURFACE  = "#111827"
SURFACE2 = "#1a2235"
BORDER   = "#1e2d45"
ACCENT   = "#00d4ff"
ACCENT2  = "#7c3aed"
WARN     = "#f59e0b"
DANGER   = "#ef4444"
SUCCESS  = "#10b981"
TEXT     = "#e2e8f0"
MUTED    = "#64748b"

F_TITLE = ("Courier New", 15, "bold")
F_HEAD  = ("Courier New", 12, "bold")
F_MONO  = ("Courier New", 10)
F_SM    = ("Courier New", 9)


# ─────────────────────────────────────────────────────────
# JSON HELPERS
# ─────────────────────────────────────────────────────────

def _extract_json(text: str):
    """Parse JSON that may have WP debug prefix or be newline-delimited."""
    if not text:
        return text
    # Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Strip WP debug prefix before first [ or {
    for ch in ('[', '{'):
        idx = text.find(ch)
        if idx > 0:
            try:
                return json.loads(text[idx:])
            except json.JSONDecodeError:
                pass
    # Newline-delimited JSON objects
    objects = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            objects.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    if objects:
        return objects if len(objects) > 1 else objects[0]
    return text


def _safe_text(text: str) -> str:
    parsed = _extract_json(text)
    if isinstance(parsed, (dict, list)):
        return json.dumps(parsed, ensure_ascii=False)
    return str(parsed)


# ─────────────────────────────────────────────────────────
# ARG SANITISER  — fix Ollama passing schema objects as values
# and auto-fill fields from the listings cache
# ─────────────────────────────────────────────────────────

_BLANK = {"", "N/A", "Missing", None}

def sanitise_args(tool_name: str, raw_args: dict, cache: list,
                  pinned: dict = None) -> dict:
    """Clean and resolve tool arguments. If pinned is provided, it takes
    absolute priority over whatever listing the model passed."""
    # Step 1: strip schema objects {"type": "string"} passed as values
    clean = {}
    for k, v in raw_args.items():
        if isinstance(v, dict) and "type" in v and len(v) <= 3:
            continue          # schema object — drop it
        # Guard: only test hashable values against _BLANK set
        try:
            if v in _BLANK:
                continue      # empty / placeholder — drop it
        except TypeError:
            pass              # v is a list/dict — keep it
        # Drop placeholder strings the model sometimes passes for name fields
        if isinstance(v, str) and v.strip() in ("?", "??", "unknown", "Unknown",
                                                  "listing", "Listing", "N/A", "n/a"):
            continue
        clean[k] = v

    # Step 2: for listing tools, resolve real listing data from cache
    # Priority: pinned (from user message) > title match > ID match
    lid = clean.get("listing_id")
    lookup_name = (
        clean.get("listing_name") or clean.get("name") or
        clean.get("title") or clean.get("organisation") or ""
    )
    # Strip surrounding quotes the model sometimes adds e.g. "'WIT'" or '"WIT"'
    lookup_name = lookup_name.strip().strip("'\"").strip()

    # Pinned listing takes absolute priority — overrides whatever model sent
    _listing_tools = ("validate_listing","verify_listing_details",
                      "update_listing_meta","notify_admin")
    match = pinned if (pinned and tool_name in _listing_tools) else None

    if not match:
        # Title match
        if lookup_name and cache:
            name_l = lookup_name.lower().strip()
            match = next((r for r in cache if r.get("title","").lower().strip() == name_l), None)
            if not match:
                match = next((r for r in cache if name_l in r.get("title","").lower()), None)
            if not match:
                match = next((r for r in cache
                              if r.get("title","").lower() in name_l
                              and len(r.get("title","")) > 2), None)
        # ID match — only for update/notify (stale IDs cause wrong matches for validate/verify)
        if not match and lid and cache and tool_name in ("update_listing_meta","notify_admin"):
            match = next((r for r in cache if r.get("id") == lid), None)
        # Last resort for verify: match by phone or email
        if not match and tool_name == "verify_listing_details" and cache:
            cp = clean.get("current_phone","")
            ce = clean.get("current_email","")
            if cp and cp not in ("N/A",""):
                match = next((r for r in cache if r.get("phone","") == cp), None)
            if not match and ce and ce not in ("N/A",""):
                match = next((r for r in cache if r.get("email","") == ce), None)

    if match:
        if tool_name == "validate_listing":
            # Always overwrite — never trust Ollama's listing_id or contact values
            clean["listing_id"]   = match["id"]
            clean["listing_name"] = match["title"]
            clean["phone"]   = match.get("phone",   "") or ""
            clean["email"]   = match.get("email",   "") or ""
            clean["website"] = match.get("website", "") or ""
            for f in ("phone", "email", "website"):
                try:
                    if clean[f] in _BLANK:
                        clean[f] = ""
                except TypeError:
                    clean[f] = ""

        elif tool_name == "verify_listing_details":
            # Always overwrite contact fields from cache
            clean["listing_id"]      = match["id"]
            clean["name"]            = match["title"]
            clean["current_phone"]   = match.get("phone",   "N/A") or "N/A"
            clean["current_email"]   = match.get("email",   "N/A") or "N/A"
            clean["current_website"] = match.get("website", "N/A") or "N/A"
            # Pass CTA URL if available — scraper will use it as primary contact page
            cta = match.get("cta_url", "")
            if cta:
                clean["cta_url"] = cta
            for f in ("current_phone", "current_email", "current_website"):
                try:
                    if clean[f] in _BLANK:
                        clean[f] = "N/A"
                except TypeError:
                    clean[f] = "N/A"

        elif tool_name in ("update_listing_meta", "notify_admin"):
            clean["listing_id"]   = match["id"]
            clean["listing_name"] = match["title"]

    # Step 3: required-field fallbacks
    if tool_name == "get_listings":
        # Always fetch all listings so the model can find any listing by name
        clean["per_page"] = 100

    if tool_name == "validate_listing":
        clean.setdefault("listing_name", f"Listing {clean.get('listing_id', '?')}")
        for f in ("phone", "email", "website"):
            clean.setdefault(f, "")

    if tool_name == "verify_listing_details":
        # Ensure cta_url is always passed through if available
        if match and match.get("cta_url") and not clean.get("cta_url"):
            clean["cta_url"] = match["cta_url"]
        # Ensure name is always a real org name, never "Listing ?"
        if "name" not in clean or not clean.get("name") or clean["name"].startswith("Listing "):
            # Try every possible key the model might have used
            candidate = (
                clean.pop("listing_name", None) or
                clean.pop("title", None) or
                clean.pop("organisation", None) or
                clean.get("name")
            )
            if not candidate or candidate.startswith("Listing "):
                # Last resort: look up by phone/email/website in cache
                cp = clean.get("current_phone", "")
                ce = clean.get("current_email", "")
                cache_hit = next(
                    (r for r in [] if  # cache not available here — handled below
                     r.get("phone") == cp or r.get("email") == ce), None
                )
                candidate = candidate or "Unknown"
            clean["name"] = candidate or "Unknown"
        clean.setdefault("current_phone",   "N/A")
        clean.setdefault("current_email",   "N/A")
        clean.setdefault("current_website", "N/A")

    return clean


def flatten_schema(schema: dict) -> dict:
    """Strip anyOf / $defs from FastMCP schemas before sending to Ollama."""
    if not schema or "properties" not in schema:
        return schema
    flat = {}
    for name, prop in schema["properties"].items():
        entry = {"type": prop.get("type", "string")}
        if "description" in prop:
            entry["description"] = prop["description"]
        if "default" in prop:
            entry["default"] = prop["default"]
        flat[name] = entry
    return {"type": "object", "properties": flat, "required": schema.get("required", [])}


# ─────────────────────────────────────────────────────────
# SYSTEM PROMPT
# ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an AI agent for a Hawke's Bay, New Zealand community service directory.

TOOLS AVAILABLE:
- get_listings: fetch all listings from WordPress
- validate_listing: check phone, email, website validity for one listing
- verify_listing_details: scrape the org website to check if details are current — ALWAYS pass name, current_phone, current_email, current_website
- update_listing_meta: update a field in WordPress (only after user approval)
- notify_admin: alert the admin of a detected change
- audit_outdated: find listings not updated in >1 year (only for full audits)
- generate_report: produce an audit summary (only when user asks for full audit/report)

WORKFLOW A — user asks to "validate" contact details:
Step 1: Call get_listings with query=<name>. Use the EXACT values returned.
Step 2: Call validate_listing with listing_id, listing_name, phone, email, website from results.
Step 3: Report the result — status, score, and each issue clearly.
Step 4: Ask "Would you like me to search online to verify the correct details?" then STOP.
        Do NOT call verify_listing_details. Do NOT call any other tool. Wait for the user.

WORKFLOW B — user asks to "verify", "search online", "check if up to date", or confirms after Workflow A:
Step 1: Call get_listings with query=<name> ONLY if you don't already have the listing data in context.
        If you already fetched the listing this conversation, skip straight to Step 2.
Step 2: Call verify_listing_details with name, current_phone, current_email, current_website.
        Use values from get_listings — never values the user typed.
Step 3: Report discrepancies found. If any: call notify_admin, then ask user permission to update.
Step 4: Only call update_listing_meta after explicit user approval.

DECIDING WHICH WORKFLOW:
- "validate" / "check format" / "check details" → Workflow A
- "verify online" / "search online" / "check if current" / "yes" after being asked → Workflow B (skip straight to verify_listing_details)
- "validate AND verify" / "validate and search online" → Workflow A then B in sequence

NZ PHONE FORMAT RULES:
- Landline: 06 835 2154  (9 digits: 2-digit area code + 7 digits)
- Mobile:   021 123 4567 (10 digits: 3-digit prefix + 7 digits)
- Freephone: 0800 123 456 (10 digits: 0800 + 6 digits)
- MORE than 11 digits = ALWAYS invalid, flag it immediately regardless of spacing
- "inconsistent format" = valid digit count, wrong spacing/brackets → offer phone_normalised
- "invalid format" = wrong digit count → state stored count vs expected count
WORKFLOW C — user says "yes" / "update" / "go ahead" AFTER being asked permission to update:
Step 1: Call update_listing_meta with the field and new value from the discrepancy you already found.
        Do NOT call get_listings, validate_listing, verify_listing_details, audit_outdated, or generate_report.
Step 2: Report success or failure of the update.

DECIDING WHICH WORKFLOW:
- User says "yes" or "update" after "Would you like me to update?" → Workflow C ONLY
- DO NOT re-validate, re-verify, or run any audit when the user approves an update

STRICT RULES:
- NEVER use listing_id=1 or any guessed id — the id MUST come from get_listings results
- NEVER use phone/email/website values from the user's message — use values from get_listings
- If user says "search online", "verify", or "yes" after a validation → call verify_listing_details IMMEDIATELY, do NOT call validate_listing again
- After Workflow A (validate only), STOP and wait — do NOT automatically call verify_listing_details
- If user says "yes" after "Would you like me to update?" → call update_listing_meta IMMEDIATELY, no other tools
- NEVER call audit_outdated or generate_report unless the user explicitly says "full audit" or "run audit"
- NEVER call notify_admin unless a real discrepancy was found by verify_listing_details
- NEVER update without explicit user permission
- Keep responses short — no markdown bold or bullet formatting
"""

AUDIT_PROMPT = (
    "Run the full yearly audit: call audit_outdated, then validate_listing and "
    "verify_listing_details for each listing found, then generate_report with results."
)


def _fmt_phone(digits: str) -> str:
    """Format a cleaned NZ phone number (digits only) into standard display format."""
    import re as _re
    d = _re.sub(r"[\s\-\.\(\)]", "", digits)
    if d.startswith("+64"):
        d = "0" + d[3:]
    if _re.match(r"^0(800|508)\d{6,7}$", d):
        return f"{d[:4]} {d[4:7]} {d[7:]}"
    if _re.match(r"^02\d{8,9}$", d):
        return f"{d[:3]} {d[3:7]} {d[7:]}"
    if _re.match(r"^0[3-9]\d{7}$", d):
        return f"{d[:2]} {d[2:5]} {d[5:]}"
    return digits   # unchanged if pattern not recognised


# ─────────────────────────────────────────────────────────
# MCP BRIDGE
# ─────────────────────────────────────────────────────────

class MCPBridge:

    def __init__(self, on_log, on_listings, on_stats, on_reply, on_tool, on_ready, on_csv_save):
        self.on_log      = on_log
        self.on_listings = on_listings
        self.on_stats    = on_stats
        self.on_reply    = on_reply
        self.on_tool     = on_tool
        self.on_ready    = on_ready
        self.on_csv_save = on_csv_save

        self._loop    = None
        self._session = None
        self._tools   = []
        self._msgs    = [{"role": "system", "content": SYSTEM_PROMPT}]
        self._ready   = False
        self._cache   = []
        self._stop_flag = False   # set True to abort current agent turn
        self._last_action    = None   # "validate" | "verify" — track last completed action
        self._pinned_listing = None   # cache entry from last validate turn
        self._pending_update = None   # persists across turns until update succeeds or new verify runs

    def start(self):
        t = threading.Thread(target=self._thread, daemon=True)
        t.start()

    def _thread(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connect())

    async def _connect(self):
        self.on_log("Connecting to MCP server…", "INFO")
        params = StdioServerParameters(
            command=sys.executable, args=["server.py"], env=os.environ.copy()
        )
        try:
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as session:
                    self._session = session
                    await session.initialize()

                    mt = await session.list_tools()
                    self._tools = [
                        {"type": "function", "function": {
                            "name":        t.name,
                            "description": t.description,
                            "parameters":  flatten_schema(t.inputSchema),
                        }}
                        for t in mt.tools
                    ]
                    self.on_log(f"MCP ready — {len(self._tools)} tools loaded.", "OK")
                    self._ready = True
                    self.on_ready()
                    await self._fetch()
                    # Heartbeat — keeps the MCP session alive
                    while True:
                        await asyncio.sleep(30)
        except Exception as e:
            import traceback
            self.on_log(f"MCP error: {e}", "ERR")
            self.on_log(traceback.format_exc()[:300], "ERR")

    async def _fetch(self):
        self.on_log("Fetching listings from WordPress…", "INFO")
        try:
            res  = await self._session.call_tool("get_listings", {})
            text = "\n".join(c.text for c in res.content if hasattr(c, "text"))
            data = _extract_json(text)

            rows = None
            if isinstance(data, dict) and "listings" in data:
                rows = data["listings"]
            elif isinstance(data, list):
                rows = data

            if rows is not None:
                self._cache = rows
                self.on_listings(rows)
                self.on_stats({"total": len(rows), "valid": 0, "review": 0, "invalid": 0})
                self.on_log(f"Loaded {len(rows)} listings.", "OK")
            else:
                self.on_log(f"Could not parse listings: {str(data)[:120]}", "ERR")
        except Exception as e:
            self.on_log(f"Fetch error: {e}", "ERR")

    def fetch(self):
        if self._loop and self._ready:
            asyncio.run_coroutine_threadsafe(self._fetch(), self._loop)

    async def _validate_row(self, row):
        """Directly validate a single row (from right-click or audit)."""
        phone   = row.get("phone",   "") or ""
        email   = row.get("email",   "") or ""
        website = row.get("website", "") or ""
        # Normalise placeholders
        if phone   in _BLANK: phone   = ""
        if email   in _BLANK: email   = ""
        if website in _BLANK: website = ""

        self.on_log(f"Validating: {row['title']}…", "INFO")
        try:
            res = await self._session.call_tool("validate_listing", {
                "listing_id":   row["id"],
                "listing_name": row["title"],
                "phone":        phone,
                "email":        email,
                "website":      website,
            })
            text = "\n".join(c.text for c in res.content if hasattr(c, "text"))
            d    = _extract_json(text)
            if isinstance(d, dict):
                s = d.get("status", "unknown")
                tag = "OK" if s == "valid" else ("WARN" if s == "review" else "ERR")
                issues = d.get("issues", [])
                msg = f"  {row['title']} — {s.upper()} (score {d.get('score',0)})"
                if issues:
                    msg += ": " + "; ".join(issues)
                self.on_log(msg, tag)
            return d
        except Exception as e:
            self.on_log(f"Validation error: {e}", "ERR")
            return None

    def validate_row(self, row):
        if self._loop and self._ready:
            asyncio.run_coroutine_threadsafe(self._validate_row(row), self._loop)

    async def _run_audit(self, cutoff_date=None):
        label = f"updated since {cutoff_date}" if cutoff_date else "all listings"
        self.on_log(f"━━━ Full audit starting ({label}) ━━━", "INFO")
        try:
            res  = await self._session.call_tool("get_listings", {"per_page": 100})
            text = "\n".join(c.text for c in res.content if hasattr(c, "text"))
            data = _extract_json(text)
            all_listings = data.get("listings", []) if isinstance(data, dict) else []

            # Filter by chosen cutoff date if provided
            if cutoff_date:
                def _since(row):
                    raw = row.get("last_updated", "")
                    if not raw or raw == "N/A":
                        return True
                    try:
                        return raw[:10] >= cutoff_date
                    except Exception:
                        return True
                scan_listings = [r for r in all_listings if _since(r)]
            else:
                scan_listings = all_listings

            self.on_log(f"Auditing {len(scan_listings)} of {len(all_listings)} listings "
                        f"(validate + verify each)…", "INFO")

            results = []
            valid = review = invalid = 0

            for row in scan_listings:
                # ── Step 1: Validate ──────────────────────────────────────
                d = await self._validate_row(row)
                if not d:
                    continue
                d["phone"]        = row.get("phone",        "N/A")
                d["email"]        = row.get("email",        "N/A")
                d["website"]      = row.get("website",      "N/A")
                d["last_updated"] = row.get("last_updated", "N/A")

                # ── Step 2: Verify online ─────────────────────────────────
                self.on_log(f"  Verifying online: {row['title']}…", "INFO")
                try:
                    verify_args = {
                        "name":            row["title"],
                        "current_phone":   row.get("phone",   "N/A") or "N/A",
                        "current_email":   row.get("email",   "N/A") or "N/A",
                        "current_website": row.get("website", "N/A") or "N/A",
                    }
                    if row.get("cta_url"):
                        verify_args["cta_url"] = row["cta_url"]
                    vres  = await self._session.call_tool("verify_listing_details", verify_args)
                    vtxt  = "\n".join(c.text for c in vres.content if hasattr(c, "text"))
                    vdata = _extract_json(vtxt)
                    if isinstance(vdata, dict):
                        d["verify_discrepancies"] = vdata.get("discrepancies", {})
                        d["verify_confirmed"]     = vdata.get("confirmed", {})
                        d["verify_method"]        = vdata.get("method", "")
                        d["verify_contact_page"]  = vdata.get("contact_page", "")
                        has_disc = vdata.get("has_discrepancy", False)
                        if has_disc:
                            disc_list = [f"{f}: {v.get('stored')} → {v.get('found')}"
                                         for f, v in vdata.get("discrepancies",{}).items()]
                            self.on_log(f"  ⚠ Discrepancies: {'; '.join(disc_list)}", "WARN")
                        else:
                            self.on_log(f"  ✓ Online verified", "OK")
                except Exception as e:
                    self.on_log(f"  Verify error: {e}", "ERR")
                    d["verify_discrepancies"] = {}
                    d["verify_confirmed"]     = {}
                    d["verify_method"]        = "error"

                results.append(d)
                s = d.get("status")
                if s == "valid":    valid   += 1
                elif s == "review": review  += 1
                else:               invalid += 1

            self.on_stats({"total": len(all_listings), "valid": valid,
                           "review": review, "invalid": invalid})

            if results:
                rep   = await self._session.call_tool("generate_report", {"results": results})
                rtext = "\n".join(c.text for c in rep.content if hasattr(c, "text"))
                rd    = _extract_json(rtext)
                if isinstance(rd, dict):
                    txt_path = rd.get("saved_to", "")
                    csv_path = rd.get("csv_path", "")
                    self.on_log(f"Report saved: {txt_path}", "OK")
                    if csv_path:
                        self.on_log(f"CSV saved:   {csv_path}", "OK")
                        self.on_csv_save(csv_path)

            self.on_log("━━━ Audit complete ━━━", "OK")
        except Exception as e:
            import traceback
            self.on_log(f"Audit error: {e}\n{traceback.format_exc()}", "ERR")

    def run_audit(self, cutoff_date=None):
        if self._loop and self._ready:
            asyncio.run_coroutine_threadsafe(self._run_audit(cutoff_date), self._loop)

    def stop(self):
        """Signal the agent to abort after the current tool call completes."""
        self._stop_flag = True

    def _resolve_target_listing(self, user_input: str):
        """
        Extract the listing name from the user's message and return the matching
        cache entry. Returns None if no clear listing name is found or on approval
        messages where we don't want to change the pinned listing.
        """
        if not self._cache:
            return None

        # Don't re-pin on approval/continuation messages
        _lower = user_input.strip().lower()
        _skip_words = {"yes", "yeah", "yep", "no", "ok", "okay", "sure",
                       "go ahead", "update", "stop", "cancel", "do it"}
        if _lower in _skip_words or len(_lower) < 4:
            return None

        # Try to find a cache title mentioned in the user message
        # Sort by length descending so longer/more specific titles match first
        sorted_cache = sorted(self._cache, key=lambda r: len(r.get("title","")), reverse=True)

        for row in sorted_cache:
            title = row.get("title", "")
            if not title or len(title) < 2:
                continue
            if title.lower() in _lower:
                return row

        # No direct match — try word-by-word for short titles like "WIT"
        words = set(_lower.split())
        for row in sorted_cache:
            title = row.get("title", "")
            if title.lower() in words:
                return row

        return None

    async def _chat(self, user_input: str):
        import ollama

        # ── Pre-resolve listing from user message ──────────────────────────
        _pinned = self._resolve_target_listing(user_input)
        if _pinned:
            pin_msg = {
                "role": "system",
                "content": (
                    f"CURRENT TASK LISTING (use these exact values — do not substitute another listing):\n"
                    f"  listing_id:   {_pinned['id']}\n"
                    f"  listing_name: {_pinned['title']}\n"
                    f"  phone:        {_pinned.get('phone','N/A')}\n"
                    f"  email:        {_pinned.get('email','N/A')}\n"
                    f"  website:      {_pinned.get('website','N/A')}\n"
                )
            }
            self._msgs = [m for m in self._msgs
                          if not (m.get("role") == "system"
                                  and "CURRENT TASK LISTING" in m.get("content",""))]
            self._msgs.append(pin_msg)
            cta_note = f" | CTA: {_pinned['cta_url']}" if _pinned.get("cta_url") else ""
            self.on_log(f"Pinned listing: {_pinned['title']} (id {_pinned['id']}){cta_note}", "INFO")

        self._msgs.append({"role": "user", "content": user_input})

        # ── Per-turn state ─────────────────────────────────────────────────
        _validate_result = None
        _verify_result   = None
        _update_result   = None
        _update_approved = False

        _approval_words = {"yes","yeah","yep","go ahead","update","do it",
                           "confirm","approve","ok","okay","sure","please update"}
        _verify_words   = {"verify","search online","check online","check if current",
                           "up to date","up-to-date","current","search"}
        _validate_words = {"validate","check","details","format"}
        _user_lower     = user_input.strip().lower()
        _simple_yes     = {"yes","yeah","yep","ok","okay","sure","go ahead","please"}
        _simple_no      = {"no","nope","nah","not now","skip","cancel","nevermind","never mind"}
        _user_words     = set(_user_lower.split())
        _is_decline     = (
            not _update_approved
            # Exact word match only — "not" in "email is not correct" must NOT trigger this
            and any(w in _user_words or w == _user_lower.strip() for w in _simple_no)
            and self._last_action in ("validate", "verify")
            and len(_user_lower.split()) <= 5   # short decline, not a new instruction
        )
        # Detect correction feedback: "email is wrong/incorrect/not right/not correct"
        # Clear pending_update so model doesn't try to apply a wrong value
        _correction_words = {"incorrect", "wrong", "not right", "not correct", "invalid",
                              "not accurate", "different", "that's wrong", "thats wrong"}
        _is_correction = (
            self._pending_update is not None
            and any(w in _user_lower for w in _correction_words)
        )
        if _is_correction:
            self._pending_update = None
            _update_approved = False

        # Only approve update if a pending update is already waiting
        if self._pending_update and any(w in _user_lower for w in _approval_words):
            _update_approved = True
        else:
            self._pending_update = None

        # Detect validate-only vs verify-confirmation vs validate+verify combined
        _has_validate = any(w in _user_lower for w in _validate_words)
        _has_verify   = any(w in _user_lower for w in _verify_words)

        _is_validate_only = (
            _has_validate and not _has_verify and not _update_approved
        )
        _is_validate_and_verify = (
            _has_validate and _has_verify and not _update_approved
        )
        # Short verify-intent phrases that should route straight to verify after validate
        _pure_verify_phrases = {"verify online", "verify", "search online", "check online",
                                "search", "yes", "yeah", "yep", "ok", "okay", "sure",
                                "go ahead", "please", "check if current", "check now"}
        _is_verify_confirm = (
            not _update_approved
            and not _pinned          # no new listing name mentioned
            and self._last_action == "validate"
            and self._pinned_listing is not None
            and (
                any(w in _user_lower for w in _simple_yes)
                or any(w in _user_lower for w in _pure_verify_phrases)
                or _user_lower.strip() in _pure_verify_phrases
            )
            and len(_user_lower.split()) <= 6   # short phrase, not a complex new request
        )

        _consecutive_blocks = 0
        MAX_BLOCKS = 3
        final = ""   # always initialised so the fallback reply never raises UnboundLocalError

        try:
            # ── Decline shortcut: user said no to verify/update ─────────────
            if _is_decline:
                org = (self._pinned_listing or {}).get("title", "the listing")
                self.on_reply(f"Understood. No online verification for {org}.")
                self._last_action = None

            # ── Verify-confirmation shortcut: bypass Ollama ─────────────────
            elif _is_verify_confirm and self._pinned_listing:
                p = self._pinned_listing
                self.on_log(f"Routing 'yes' → verify for {p['title']}", "INFO")
                try:
                    res  = await self._session.call_tool("verify_listing_details", {
                        "name":            p["title"],
                        "current_phone":   p.get("phone",   "N/A") or "N/A",
                        "current_email":   p.get("email",   "N/A") or "N/A",
                        "current_website": p.get("website", "N/A") or "N/A",
                        **( {"cta_url": p["cta_url"]} if p.get("cta_url") else {} ),
                    })
                    rtxt = "\n".join(c.text for c in res.content if hasattr(c, "text"))
                    _verify_result = json.loads(_safe_text(rtxt))
                except Exception as e:
                    _verify_result = {"has_discrepancy": False, "error": str(e)}
                if isinstance(_verify_result, dict) and _verify_result.get("has_discrepancy"):
                    disc = _verify_result.get("discrepancies", {})
                    if disc:
                        field = next(iter(disc))
                        self._pending_update = {
                            "field":        field,
                            "new_value":    disc[field].get("found", ""),
                            "listing_id":   p["id"],
                            "listing_name": p["title"],
                        }
                self._last_action = "verify"
                final = ""

            else:
                # ── Normal Ollama path (new task or complex request) ────────
                if _is_validate_only and _pinned:
                    self._last_action    = "validate"
                    self._pinned_listing = _pinned
                elif any(w in _user_lower for w in _verify_words):
                    self._last_action = "verify"

                resp = ollama.chat(model=OLLAMA_MODEL, messages=self._msgs, tools=self._tools)

                while resp.get("message", {}).get("tool_calls"):
                    if self._stop_flag:
                        self._stop_flag = False
                        self.on_reply("⏹ Stopped.")
                        return
                    self._msgs.append(resp["message"])
                    _tools_run_this_batch = []
                    for tc in resp["message"]["tool_calls"]:
                        name     = tc["function"]["name"]
                        raw_args = tc["function"]["arguments"]
                        args     = sanitise_args(name, raw_args, self._cache, pinned=_pinned)
                        # Log name resolution mismatches
                        if name in ("validate_listing", "verify_listing_details"):
                            model_name = raw_args.get("listing_name") or raw_args.get("name") or "?"
                            resolved   = args.get("listing_name") or args.get("name") or "?"
                            if model_name.strip("'\"").strip().lower() != resolved.lower():
                                self.on_log(f"  Name resolved: '{model_name}' → '{resolved}'", "INFO")
                        self.on_tool(name)
                        self.on_log(f"→ {name}({json.dumps(args)[:90]})", "AI")

                        # ── Hard guards ─────────────────────────────────────
                        blocked = None
                        if name == "update_listing_meta" and not _update_approved:
                            blocked = ("BLOCKED: requires explicit user approval. "
                                       "Ask 'Would you like me to update this in WordPress?' and wait.")
                            self.on_log("BLOCKED: update without approval", "WARN")
                        elif name == "verify_listing_details" and _is_validate_only:
                            blocked = ("BLOCKED: user only asked to validate — do not call "
                                       "verify_listing_details. Ask the user if they want to verify online.")
                            self.on_log("BLOCKED: verify called on validate-only request", "WARN")
                        elif name in ("audit_outdated", "generate_report"):
                            blocked = (f"BLOCKED: {name} must not be called when validating or "
                                       "verifying a single listing. Only use for full audits.")
                            self.on_log(f"BLOCKED: {name} called outside audit", "WARN")
                        elif name == "notify_admin":
                            if not (_verify_result is not None and _verify_result.get("has_discrepancy")
                                    and "verify_listing_details" not in _tools_run_this_batch):
                                blocked = ("BLOCKED: notify_admin requires verify_listing_details "
                                           "to have confirmed a discrepancy first.")
                                self.on_log("BLOCKED: notify_admin — no confirmed discrepancy yet", "WARN")

                        if blocked:
                            _consecutive_blocks += 1
                            self._msgs.append({
                                "role": "tool",
                                "content": json.dumps({"error": blocked}),
                                "name": name
                            })
                            self.on_log(f"← {name}: [blocked]", "WARN")
                            if _consecutive_blocks >= MAX_BLOCKS:
                                self.on_log(f"Auto-stopped after {MAX_BLOCKS} blocked calls.", "WARN")
                                break
                            continue

                        _consecutive_blocks = 0
                        _tools_run_this_batch.append(name)

                        try:
                            res  = await self._session.call_tool(name, args)
                            rtxt = "\n".join(c.text for c in res.content if hasattr(c, "text"))
                            clean = _safe_text(rtxt)
                        except Exception as e:
                            clean = json.dumps({"error": str(e)})

                        # ── Intercept results ───────────────────────────────
                        if name == "validate_listing":
                            try:
                                _validate_result = json.loads(clean)
                            except Exception:
                                _validate_result = {"status": "unknown", "raw": clean}
                            if _validate_result:
                                clean = json.dumps({**_validate_result,
                                    "_note": "Already validated — do not call validate_listing again."})

                        if name == "verify_listing_details":
                            try:
                                _verify_result = json.loads(clean)
                            except Exception:
                                _verify_result = {"has_discrepancy": False, "raw": clean}
                            if isinstance(_verify_result, dict) and _verify_result.get("has_discrepancy"):
                                disc = _verify_result.get("discrepancies", {})
                                if disc:
                                    field    = next(iter(disc))
                                    org_name = _verify_result.get("organisation", args.get("name",""))
                                    cache_match = next(
                                        (r for r in self._cache
                                         if r.get("title","").lower() == org_name.lower()), None)
                                    if not cache_match:
                                        cache_match = next(
                                            (r for r in self._cache
                                             if org_name.lower() in r.get("title","").lower()
                                             or r.get("title","").lower() in org_name.lower()), None)
                                    lid   = cache_match["id"]    if cache_match else args.get("listing_id","")
                                    lname = cache_match["title"] if cache_match else org_name
                                    self._pending_update = {
                                        "field":        field,
                                        "new_value":    disc[field].get("found",""),
                                        "listing_id":   lid,
                                        "listing_name": lname,
                                    }
                            elif isinstance(_verify_result, dict) and not _verify_result.get("has_discrepancy"):
                                self._pending_update = None

                        if name == "update_listing_meta":
                            try:
                                _update_result = json.loads(clean)
                            except Exception:
                                _update_result = {"success": False, "raw": clean}
                            if isinstance(_update_result, dict) and \
                               _update_result.get("success") and _update_result.get("confirmed_in_db"):
                                self.on_log(f"WordPress UPDATED: {_update_result.get('field')} → "
                                            f"{_update_result.get('new_value')}", "OK")
                            else:
                                self.on_log("WordPress NOT updated.", "WARN")

                        self._msgs.append({
                            "role": "tool", "content": f"TOOL RESULT: {clean}", "name": name
                        })
                        self.on_log(f"← {name}: {clean[:120]}", "OK")

                    # break out of while if auto-stopped
                    if _consecutive_blocks >= MAX_BLOCKS:
                        break
                    resp = ollama.chat(model=OLLAMA_MODEL, messages=self._msgs, tools=self._tools)

                # Set final reply text
                if _consecutive_blocks >= MAX_BLOCKS:
                    final = ""
                else:
                    final = resp["message"]["content"]
                    self._msgs.append(resp["message"])

            # ── Safety net A: if validate+verify requested but model skipped verify ──
            if _is_validate_and_verify and _validate_result is not None and _verify_result is None:
                p = _pinned or self._pinned_listing
                if p:
                    self.on_log(f"Force-calling verify (model skipped it after valid score)", "INFO")
                    try:
                        res  = await self._session.call_tool("verify_listing_details", {
                            "name":            p["title"],
                            "current_phone":   p.get("phone",   "N/A") or "N/A",
                            "current_email":   p.get("email",   "N/A") or "N/A",
                            "current_website": p.get("website", "N/A") or "N/A",
                            **( {"cta_url": p["cta_url"]} if p.get("cta_url") else {} ),
                        })
                        rtxt = "\n".join(c.text for c in res.content if hasattr(c, "text"))
                        _verify_result = json.loads(_safe_text(rtxt))
                        if isinstance(_verify_result, dict) and _verify_result.get("has_discrepancy"):
                            disc = _verify_result.get("discrepancies", {})
                            if disc:
                                field = next(iter(disc))
                                self._pending_update = {
                                    "field":        field,
                                    "new_value":    disc[field].get("found",""),
                                    "listing_id":   p["id"],
                                    "listing_name": p["title"],
                                }
                    except Exception as e:
                        self.on_log(f"Force-verify error: {e}", "ERR")

            # ── Safety net B: call update directly if approved and model missed it ──
            if _update_approved and _update_result is None and self._pending_update:
                p = self._pending_update
                if p.get("listing_id") and p.get("field") and p.get("new_value"):
                    self.on_log("Calling update directly (model missed it)", "WARN")
                    try:
                        res  = await self._session.call_tool("update_listing_meta", {
                            "listing_id":   p["listing_id"],
                            "listing_name": p["listing_name"],
                            "field":        p["field"],
                            "new_value":    p["new_value"],
                            "reason":       "User approved update after verify_listing_details",
                        })
                        rtxt = "\n".join(c.text for c in res.content if hasattr(c, "text"))
                        _update_result = json.loads(_safe_text(rtxt))
                        if _update_result.get("success") and _update_result.get("confirmed_in_db"):
                            self.on_log(f"WordPress UPDATED: {p['field']} → {p['new_value']}", "OK")
                            self._pending_update = None
                        else:
                            self.on_log("WordPress NOT updated.", "WARN")
                    except Exception as e:
                        _update_result = {"success": False, "error": str(e)}

            # ── Build reply from intercepted tool results ───────────────────
            if _update_result is not None:
                if _update_result.get("success") and _update_result.get("confirmed_in_db"):
                    field = _update_result.get("field", "field")
                    val   = _update_result.get("new_value", "")
                    lst   = _update_result.get("listing", "the listing")
                    self.on_reply(f"Done. {lst}: {field} updated to '{val}' in WordPress.")
                else:
                    lst = (_update_result.get("listing")
                           or (self._pending_update or {}).get("listing_name")
                           or "the listing")
                    fld = _update_result.get("field", "the field")
                    self.on_reply(f"The update failed — WordPress was NOT changed for {lst} ({fld}).")

            elif _verify_result is not None:
                # Optionally prepend validate summary if same listing was also validated this turn
                _v_name = (_validate_result or {}).get("listing_name", "")
                _r_name = _verify_result.get("organisation", "")
                if _validate_result is not None and _v_name.lower() == _r_name.lower():
                    vstatus = _validate_result.get("status", "unknown")
                    vscore  = _validate_result.get("score", 0)
                    vicon   = {"valid":"✓","review":"⚠","invalid":"✗"}.get(vstatus, "?")
                    vissues = _validate_result.get("issues", [])
                    vline   = f"{_v_name} — {vicon} {vstatus.capitalize()} (score {vscore}/100)"
                    if vissues:
                        vline += "\n  Issues: " + "; ".join(vissues)
                    self.on_reply(vline)

                org          = _verify_result.get("organisation", "the listing")
                disc         = _verify_result.get("discrepancies", {})
                found        = _verify_result.get("web_found", {})
                method       = _verify_result.get("method", "")
                contact_page = _verify_result.get("contact_page", "")
                scraped_from = _verify_result.get("scraped_from", [])
                srcs         = _verify_result.get("sources_used", scraped_from)
                fallback     = _verify_result.get("fallback_reason", "")
                err          = _verify_result.get("error", "")
                lines_out    = []

                METHOD_LABELS = {
                    "cta_url":      "scraped from contact page",
                    "direct":       "scraped from website",
                    "google_cache": "scraped from Google Cache",
                    "archive_org":  "scraped from archive.org",
                    "social_media": "found online",
                    "web search":   "found online",
                    "email_domain": "derived from email domain",
                }
                method_label = next(
                    (v for k, v in METHOD_LABELS.items() if k in method),
                    "verified online"
                )
                best_phone = found.get("best_phone")
                all_phones = found.get("phones", [])

                if err:
                    lines_out.append(f"{org} — could not verify: {err}")
                elif disc or any(_verify_result.get("confirmed", {}).get(k)
                                  for k in ("email_found_online","website_found_online")):
                    confirmed_d = _verify_result.get("confirmed", {})
                    email_found   = confirmed_d.get("email_found_online", "")
                    website_found = confirmed_d.get("website_found_online", "")
                    # Treat missing fields found online as discrepancies (suggestions to add)
                    if email_found and "email" not in disc:
                        disc = dict(disc)
                        disc["email"] = {"stored": "not stored", "found": email_found,
                                         "note": "not in directory — found online"}
                    if website_found and "website" not in disc:
                        disc = dict(disc)
                        disc["website"] = {"stored": "not stored", "found": website_found,
                                           "note": "not in directory — found online"}
                    lines_out.append(f"{org} — discrepancies found ({method_label}):")
                    for fld, diff in disc.items():
                        note_str = f" ({diff['note']})" if diff.get("note") else ""
                        lines_out.append(f"  • {fld}: stored '{diff['stored']}' → found '{diff['found']}'{note_str}")
                    src = contact_page or (srcs[0] if srcs else "")
                    if src:
                        lines_out.append(f"  Source: {src}")
                    confirmed = _verify_result.get("confirmed", {})
                    conf_parts = []
                    if confirmed.get("phone"):   conf_parts.append("phone ✓")
                    if confirmed.get("email"):   conf_parts.append("email ✓")
                    if confirmed.get("website"): conf_parts.append("website ✓")
                    if conf_parts:
                        lines_out.append(f"  Confirmed OK: {', '.join(conf_parts)}")
                    # Show email status when it couldn't be verified
                    if confirmed.get("email_not_found_online"):
                        lines_out.append(f"  Email:   ⚠ could not verify online")
                    elif confirmed.get("email_missing_and_not_found"):
                        lines_out.append(f"  Email:   not stored and not found online")
                    if len(all_phones) > 1:
                        lines_out.append(f"  All phones on page: {', '.join(_fmt_phone(p) for p in all_phones if isinstance(p, str))}")
                    lines_out.append("Would you like me to update these in WordPress?")
                else:
                    src = contact_page or (scraped_from[0] if scraped_from else "")
                    confirmed = _verify_result.get("confirmed", {})
                    email_found = confirmed.get("email_found_online", "")
                    lines_out.append(f"{org} — online verification complete ({method_label})" +
                                     (f": {src}" if src else "."))
                    # Phone
                    if confirmed.get("phone"):
                        lines_out.append(f"  Phone:   ✓ {confirmed['phone']} (matches stored)")
                    elif best_phone:
                        lines_out.append(f"  Phone:   {best_phone} (found online)")
                    # Email
                    if confirmed.get("email"):
                        lines_out.append(f"  Email:   ✓ {confirmed['email']} (matches stored)")
                    elif email_found:
                        lines_out.append(f"  Email:   ⚠ not stored — found online: {email_found}")
                    elif found.get("emails"):
                        lines_out.append(f"  Email:   {found['emails'][0]} (found online)")
                    else:
                        lines_out.append(f"  Email:   not found online")
                    # Website
                    if confirmed.get("website"):
                        lines_out.append(f"  Website: ✓ {confirmed['website']} (reachable)")
                    elif confirmed.get("website_found_online"):
                        lines_out.append(f"  Website: ⚠ not stored — found online: {confirmed['website_found_online']}")
                    else:
                        lines_out.append(f"  Website: not found online")
                    # Email not found note
                    if confirmed.get("email_not_found_online"):
                        lines_out.append(f"  Email:   ⚠ could not verify — not found on website")
                    elif confirmed.get("email_missing_and_not_found"):
                        lines_out.append(f"  Email:   not stored and not found online")
                    # Suggest adding missing fields
                    if email_found or confirmed.get("website_found_online"):
                        lines_out.append(f"  → Would you like me to add the missing details to WordPress?")
                    if len(all_phones) > 1:
                        lines_out.append(f"  All phones found: {', '.join(_fmt_phone(p) for p in all_phones if isinstance(p, str))}")
                    if fallback:
                        lines_out.append(f"  Note: {fallback}")
                self.on_reply("\n".join(lines_out))

            elif _validate_result is not None:
                status  = _validate_result.get("status", "unknown")
                score   = _validate_result.get("score", 0)
                icon    = {"valid": "✓", "review": "⚠", "invalid": "✗"}.get(status, "?")
                label   = {"valid": "Valid", "review": "Needs Review", "invalid": "Invalid"}.get(status, status)
                phone   = _validate_result.get("phone",  "N/A")
                email   = _validate_result.get("email",  "N/A")
                website = _validate_result.get("website","N/A")
                pnorm   = _validate_result.get("phone_normalised")
                issues  = _validate_result.get("issues", [])
                name_v  = _validate_result.get("listing_name", "the listing")
                phone_line = f"  Phone:   {phone}"
                if pnorm and pnorm != phone:
                    phone_line += f" → correct format: {pnorm}"
                lines_out = [
                    f"{name_v} — {icon} {label} (score {score}/100)",
                    phone_line,
                    f"  Email:   {email}",
                    f"  Website: {website}",
                ]
                if issues:
                    lines_out.append("Issues:")
                    for iss in issues:
                        lines_out.append(f"  • {iss}")
                if status in ("review", "invalid"):
                    lines_out.append("Would you like me to search online to verify the correct details?")
                elif status == "valid":
                    lines_out.append("All contact details look correct.")
                self.on_reply("\n".join(lines_out))

            else:
                self.on_reply(final)

        except Exception as e:
            self.on_reply(f"Error: {e}")
            self.on_log(f"Chat error: {e}", "ERR")


    def chat(self, text: str):
        if self._loop and self._ready:
            asyncio.run_coroutine_threadsafe(self._chat(text), self._loop)
        else:
            self.on_reply("Agent not ready yet — please wait.")


# ─────────────────────────────────────────────────────────
# GUI  — uses pack throughout (no grid/pack mixing)
# ─────────────────────────────────────────────────────────

class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Directory AI Agent — Hawke's Bay")
        self.geometry("1300x800")
        self.minsize(1100, 680)
        self.configure(bg=BG)

        self._data  = []
        self._stats = {"total": 0, "valid": 0, "review": 0, "invalid": 0}

        self._build_ui()
        self._start_bridge()

    # ── TOP-LEVEL LAYOUT ──────────────────────────────────
    # header (fixed height) → body (fill+expand)
    # body = sidebar (fixed 250px) | main (expand)

    def _build_ui(self):
        self._build_header()
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._build_sidebar(body)
        self._build_main(body)

    # ── HEADER ────────────────────────────────────────────

    def _build_header(self):
        hdr = tk.Frame(self, bg=SURFACE, height=52)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        row = tk.Frame(hdr, bg=SURFACE)
        row.pack(fill="both", expand=True, padx=18)

        ico = tk.Frame(row, bg=ACCENT2, width=32, height=32)
        ico.pack(side="left", pady=10)
        ico.pack_propagate(False)
        tk.Label(ico, text="⚙", bg=ACCENT2, fg="black", font=("Courier New", 15)).pack(expand=True)

        tk.Label(row, text="  Directory", bg=SURFACE, fg=TEXT, font=F_TITLE).pack(side="left")
        tk.Label(row, text="AI",          bg=SURFACE, fg=ACCENT, font=F_TITLE).pack(side="left")
        tk.Label(row, text=" Agent",      bg=SURFACE, fg=TEXT, font=F_TITLE).pack(side="left")

        sub = tk.Frame(row, bg=SURFACE2, padx=10, pady=4)
        sub.pack(side="right", pady=12)
        self._dot = tk.Label(sub, text="●", bg=SURFACE2, fg=MUTED, font=F_SM)
        self._dot.pack(side="left", padx=(0, 4))
        self._status = tk.Label(sub, text="Connecting…", bg=SURFACE2, fg=MUTED, font=F_SM)
        self._status.pack(side="left")

        tk.Label(row, text="Listdom / WordPress · Hawke's Bay NZ",
                 bg=SURFACE, fg=MUTED, font=F_SM).pack(side="right", padx=14)

    # ── SIDEBAR ───────────────────────────────────────────
    # packed LEFT with fixed width — no grid at all

    def _build_sidebar(self, body):
        sb = tk.Frame(body, bg=BG, width=250)
        sb.pack(side="left", fill="y", padx=(0, 10), pady=10)
        sb.pack_propagate(False)   # locks the 250px width

        # Summary card — _card_frame returns the inner content frame directly
        sc = self._card_frame(sb, "📊  Summary")
        for label_text, color, attr in [
            ("Total Listings", ACCENT,   "_s_total"),
            ("✓  Valid",        SUCCESS, "_s_valid"),
            ("⚠  Needs Review", WARN,    "_s_review"),
            ("✗  Invalid",      DANGER,  "_s_invalid"),
            ("Last Scan",       MUTED,   "_s_scan"),
        ]:
            row = tk.Frame(sc, bg=SURFACE)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=label_text, bg=SURFACE, fg=MUTED, font=F_SM).pack(side="left")
            lbl = tk.Label(row, text="—", bg=SURFACE, fg=color, font=F_MONO)
            lbl.pack(side="right")
            setattr(self, attr, lbl)

        # Health bar — appended directly to sc (no winfo_children lookup)
        tk.Frame(sc, bg=BORDER, height=1).pack(fill="x", pady=(6, 4))
        bar_row = tk.Frame(sc, bg=SURFACE)
        bar_row.pack(fill="x")
        tk.Label(bar_row, text="Health", bg=SURFACE, fg=MUTED, font=F_SM).pack(side="left")
        self._hpct = tk.Label(bar_row, text="—", bg=SURFACE, fg=ACCENT, font=F_SM)
        self._hpct.pack(side="right")
        bg_bar = tk.Frame(sc, bg=BORDER, height=4)
        bg_bar.pack(fill="x", pady=(2, 0))
        self._hbar = tk.Frame(bg_bar, bg=ACCENT, height=4)
        self._hbar.place(relx=0, rely=0, relheight=1, relwidth=0)

        # Actions card
        ac = self._card_frame(sb, "⚡  Actions")
        self._btn_fetch = self._btn(ac, "↻  Refresh Listings", self._do_fetch,  ACCENT)
        self._btn_audit = self._btn(ac, "▶  Run Full Audit",    self._do_audit,  SUCCESS)

        # Yearly scheduler card
        yc = self._card_frame(sb, "🗓  Yearly Audit")
        tk.Label(yc, text="Scheduled: Jan 1st  09:00",
                 bg=SURFACE, fg=ACCENT, font=F_SM).pack(anchor="w", pady=(0, 6))
        self._btn_sched = self._btn(yc, "⏱  Scheduler Info", self._do_sched, WARN)

        for b in (self._btn_fetch, self._btn_audit, self._btn_sched):
            b.config(state="disabled")

    def _card_frame(self, parent, title):
        outer = tk.Frame(parent, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        outer.pack(fill="x", pady=(0, 8))
        tk.Label(outer, text=title, bg=SURFACE, fg=MUTED, font=F_SM
                 ).pack(anchor="w", padx=12, pady=(8, 4))
        tk.Frame(outer, bg=BORDER, height=1).pack(fill="x", padx=12)
        inner = tk.Frame(outer, bg=SURFACE)
        inner.pack(fill="both", padx=12, pady=8)
        return inner

    def _btn(self, parent, text, cmd, color):
        b = tk.Button(parent, text=text, command=cmd,
                      bg=SURFACE2, fg=color,
                      activebackground=BORDER, activeforeground=color,
                      font=F_SM, relief="flat", cursor="hand2",
                      highlightbackground=BORDER, highlightthickness=1,
                      padx=8, pady=5)
        b.pack(fill="x", pady=2)
        return b

    # ── MAIN AREA ─────────────────────────────────────────
    # packed RIGHT, fills remaining space
    # top: listings table (weight 2) | bottom: console + chat (weight 1)

    def _build_main(self, body):
        main = tk.Frame(body, bg=BG)
        main.pack(side="left", fill="both", expand=True, pady=10)

        self._build_table(main)
        self._build_bottom(main)

    # ── LISTINGS TABLE ────────────────────────────────────

    def _build_table(self, parent):
        wrap = tk.Frame(parent, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        wrap.pack(fill="x", pady=(0, 8))
        wrap.configure(height=300)
        wrap.pack_propagate(False)

        # Header bar
        hbar = tk.Frame(wrap, bg=SURFACE2)
        hbar.pack(fill="x")
        tk.Label(hbar, text="Community Listings", bg=SURFACE2, fg=TEXT,
                 font=F_HEAD).pack(side="left", padx=12, pady=8)

        self._search = tk.StringVar()
        self._search.trace_add("write", self._filter)
        e = tk.Entry(hbar, textvariable=self._search,
                     bg=SURFACE, fg=TEXT, insertbackground=ACCENT,
                     relief="flat", font=F_MONO, width=20,
                     highlightbackground=BORDER, highlightthickness=1)
        e.pack(side="right", padx=12, pady=8, ipady=3)
        tk.Label(hbar, text="🔍", bg=SURFACE2, fg=MUTED, font=F_SM).pack(side="right")

        # Treeview style
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("D.Treeview",
                     background=SURFACE, foreground=TEXT,
                     fieldbackground=SURFACE, rowheight=28,
                     font=F_MONO, borderwidth=0)
        s.configure("D.Treeview.Heading",
                     background=SURFACE2, foreground=MUTED,
                     font=F_SM, relief="flat")
        s.map("D.Treeview",
              background=[("selected", SURFACE2)],
              foreground=[("selected", ACCENT)])

        cols = ("title", "phone", "email", "website", "updated", "op_status")
        self._tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                   style="D.Treeview", selectmode="browse")
        self._tree.pack(fill="both", expand=True, side="left")

        cfg = [("Listing", 210), ("Phone", 130), ("Email", 185),
               ("Website", 155), ("Last Updated", 100), ("Operational Status", 140)]
        for col, (hd, w) in zip(cols, cfg):
            self._tree.heading(col, text=hd)
            self._tree.column(col, width=w, anchor="w", minwidth=60)

        self._tree.tag_configure("valid",    foreground=SUCCESS)
        self._tree.tag_configure("review",   foreground=WARN)
        self._tree.tag_configure("invalid",  foreground=DANGER)
        self._tree.tag_configure("normal",   foreground=TEXT)
        self._tree.tag_configure("op_closed_temp", foreground=WARN)
        self._tree.tag_configure("op_closed_perm", foreground=DANGER)
        self._tree.tag_configure("op_reopened",    foreground=SUCCESS)
        self._tree.tag_configure("op_active",      foreground=TEXT)

        # Operational status choices (cycling order)
        self._OP_LABELS = ["Active", "Temporarily Closed", "Permanently Closed", "Re-opened"]
        self._OP_TAGS   = {
            "Active":              "op_active",
            "Temporarily Closed":  "op_closed_temp",
            "Permanently Closed":  "op_closed_perm",
            "Re-opened":           "op_reopened",
        }
        # Per-listing op status store  {listing_id: label}
        self._op_status: dict = {}

        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self._tree.yview)
        vsb.pack(side="right", fill="y")
        self._tree.configure(yscrollcommand=vsb.set)

        # Click on Operational Status column → open label picker menu
        self._tree.bind("<Button-1>", self._on_tree_click)

        # Right-click menu
        self._ctx = tk.Menu(self, tearoff=0, bg=SURFACE2, fg=TEXT,
                             activebackground=BORDER, activeforeground=ACCENT,
                             font=F_SM)
        self._ctx.add_command(label="✓  Validate this listing",  command=self._ctx_validate)
        self._ctx.add_command(label="🔍  Verify online",          command=self._ctx_verify)
        self._ctx.add_command(label="💬  Ask agent about this",   command=self._ctx_ask)
        self._tree.bind("<Button-3>", self._show_ctx)
        self._tree.bind("<Button-2>", self._show_ctx)

    # ── BOTTOM: CONSOLE + CHAT ────────────────────────────

    def _build_bottom(self, parent):
        bot = tk.Frame(parent, bg=BG)
        bot.pack(fill="both", expand=True)   # expands to fill remaining space

        self._build_console(bot)
        self._build_chat(bot)

    def _build_console(self, parent):
        wrap = tk.Frame(parent, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        wrap.pack(side="left", fill="both", expand=True, padx=(0, 6))

        hbar = tk.Frame(wrap, bg=SURFACE2)
        hbar.pack(fill="x")
        tk.Label(hbar, text="🖥  Console", bg=SURFACE2, fg=TEXT, font=F_HEAD
                 ).pack(side="left", padx=12, pady=6)
        tk.Button(hbar, text="Clear", command=self._clear_console,
                  bg=SURFACE2, fg=MUTED, relief="flat", font=F_SM, cursor="hand2"
                  ).pack(side="right", padx=8)

        self._console = scrolledtext.ScrolledText(
            wrap, bg=BG, fg=TEXT, font=F_MONO,
            relief="flat", state="disabled", wrap="word")
        self._console.pack(fill="both", expand=True, padx=2, pady=2)
        for tag, color in [("INFO", ACCENT), ("OK", SUCCESS), ("WARN", WARN),
                            ("ERR", DANGER), ("AI", "#a78bfa"),
                            ("TIME", MUTED),  ("MSG", TEXT)]:
            self._console.tag_config(tag, foreground=color)

    def _build_chat(self, parent):
        wrap = tk.Frame(parent, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        wrap.pack(side="left", fill="both", expand=True)

        hbar = tk.Frame(wrap, bg=SURFACE2)
        hbar.pack(fill="x")
        tk.Label(hbar, text="💬  Ask the Agent", bg=SURFACE2, fg=TEXT, font=F_HEAD
                 ).pack(side="left", padx=12, pady=6)
        tk.Label(hbar, text="Ollama", bg=SURFACE2, fg=MUTED, font=F_SM
                 ).pack(side="right", padx=10)

        self._chat_log = scrolledtext.ScrolledText(
            wrap, bg=BG, fg=TEXT, font=F_MONO,
            relief="flat", state="disabled", wrap="word")
        self._chat_log.pack(fill="both", expand=True, padx=2, pady=2)
        self._chat_log.tag_config("you",   foreground=ACCENT)
        self._chat_log.tag_config("agent", foreground=SUCCESS)
        self._chat_log.tag_config("lbl",   foreground=MUTED)

        inp = tk.Frame(wrap, bg=SURFACE2)
        inp.pack(fill="x", padx=2, pady=(0, 2))
        self._input = tk.Entry(inp, bg=SURFACE, fg=TEXT, insertbackground=ACCENT,
                               relief="flat", font=F_MONO,
                               highlightbackground=BORDER, highlightthickness=1)
        self._input.pack(side="left", fill="x", expand=True, padx=(6, 4), pady=6, ipady=5)
        self._input.bind("<Return>", self._send)
        self._btn_stop = tk.Button(inp, text="⏹ Stop", command=self._do_stop,
                  bg=DANGER, fg="black", font=F_SM,
                  relief="flat", cursor="hand2", padx=10, pady=5, state="disabled")
        self._btn_stop.pack(side="right", padx=(0, 4), pady=6)
        tk.Button(inp, text="Send ➤", command=self._send,
                  bg=ACCENT2, fg="black", font=F_SM,
                  relief="flat", cursor="hand2", padx=10, pady=5
                  ).pack(side="right", padx=(0, 4), pady=6)

        self._append_chat("Agent",
            "Hi! I manage the Hawke's Bay community directory. "
            "I can validate contact details, check if they're up to date, "
            "and update WordPress with your approval. What would you like to do?",
            "agent")

    # ── BRIDGE CALLBACKS ──────────────────────────────────

    def _start_bridge(self):
        self._bridge = MCPBridge(
            on_log      = lambda m, t: self.after(0, self._log, m, t),
            on_listings = lambda rows: self.after(0, self._set_listings, rows),
            on_stats    = lambda s:    self.after(0, self._set_stats, s),
            on_reply    = lambda t:    self.after(0, self._agent_reply, t),
            on_tool     = lambda n:    self.after(0, self._log, f"Calling tool: {n}", "AI"),
            on_ready    = lambda:      self.after(0, self._set_ready),
            on_csv_save = lambda p:    self.after(0, self._offer_csv_save, p),
        )
        self._bridge.start()

    def _log(self, msg: str, tag: str = "INFO"):
        ts = datetime.now().strftime("%H:%M:%S")
        self._console.config(state="normal")
        self._console.insert("end", ts + "  ", "TIME")
        self._console.insert("end", f"[{tag:<4}]  ", tag)
        self._console.insert("end", msg + "\n", "MSG")
        self._console.see("end")
        self._console.config(state="disabled")

    def _set_listings(self, rows):
        self._data = rows
        self._render(rows)

    def _render(self, rows):
        self._tree.delete(*self._tree.get_children())
        for r in rows:
            last = r.get("last_updated", "")
            try:
                last = datetime.fromisoformat(last[:19]).strftime("%Y-%m-%d")
            except Exception:
                pass
            val_status = r.get("_status", "")
            val_tag    = val_status if val_status in ("valid", "review", "invalid") else "normal"
            iid        = str(r["id"])
            op_label   = self._op_status.get(iid, "Active")
            op_tag     = self._OP_TAGS.get(op_label, "op_active")
            self._tree.insert("", "end", iid=iid, values=(
                r.get("title",   ""),
                r.get("phone",   "N/A"),
                r.get("email",   "N/A"),
                r.get("website", "N/A"),
                last or "N/A",
                op_label,
            ), tags=(val_tag, op_tag))

    def _on_tree_click(self, event):
        """If user clicks the Operational Status column, show a label picker."""
        col = self._tree.identify_column(event.x)
        iid = self._tree.identify_row(event.y)
        if not iid or col != "#6":   # column 6 = op_status
            return
        # Build a small popup menu at click position
        menu = tk.Menu(self, tearoff=0, bg=SURFACE2, fg=TEXT,
                       activebackground=ACCENT2, activeforeground="white", font=F_SM)
        for label in self._OP_LABELS:
            menu.add_command(
                label=label,
                command=lambda l=label, i=iid: self._set_op_status(i, l)
            )
        menu.tk_popup(event.x_root, event.y_root)

    def _set_op_status(self, iid: str, label: str):
        """Set operational status label for a listing row."""
        self._op_status[iid] = label
        # Re-render just this row
        vals = list(self._tree.item(iid, "values"))
        vals[5] = label
        # Keep existing validation tag, add op colour as second tag
        existing_tags = [t for t in self._tree.item(iid, "tags")
                         if not t.startswith("op_")]
        op_tag = self._OP_TAGS.get(label, "op_active")
        self._tree.item(iid, values=vals, tags=tuple(existing_tags) + (op_tag,))

    def _filter(self, *_):
        q = self._search.get().lower()
        self._render(self._data if not q else [
            r for r in self._data
            if q in r.get("title",   "").lower()
            or q in r.get("email",   "").lower()
            or q in r.get("phone",   "").lower()
        ])

    def _set_stats(self, s):
        self._stats = s
        total = s.get("total", 0)
        self._s_total.config(  text=str(total))
        self._s_valid.config(  text=str(s.get("valid",   0)))
        self._s_review.config( text=str(s.get("review",  0)))
        self._s_invalid.config(text=str(s.get("invalid", 0)))
        self._s_scan.config(   text=datetime.now().strftime("%H:%M"))
        pct = int(s.get("valid", 0) / total * 100) if total else 0
        self._hpct.config(text=f"{pct}%")
        self._hbar.place(relwidth=pct / 100)

    def _set_ready(self):
        self._dot.config(fg=SUCCESS)
        self._status.config(fg=SUCCESS, text="Agent Active")
        for b in (self._btn_fetch, self._btn_audit, self._btn_sched):
            b.config(state="normal")

    def _do_stop(self):
        """Ask the bridge to abort the current agent turn."""
        self._bridge.stop()
        self._btn_stop.config(state="disabled")
        self.after(0, self._log, "Stop requested — will halt after current tool.", "WARN")

    def _agent_reply(self, text: str):
        self._btn_stop.config(state="disabled")
        # Remove "Thinking…" line
        self._chat_log.config(state="normal")
        content = self._chat_log.get("1.0", "end-1c")
        if "Thinking…" in content:
            lines = [ln for ln in content.splitlines() if "Thinking…" not in ln]
            self._chat_log.delete("1.0", "end")
            for ln in lines:
                self._chat_log.insert("end", ln + "\n")
        self._chat_log.insert("end", "Agent:  ", "lbl")
        self._chat_log.insert("end", text + "\n\n", "agent")
        self._chat_log.see("end")
        self._chat_log.config(state="disabled")

    # ── ACTIONS ───────────────────────────────────────────

    def _do_fetch(self):
        self._bridge.fetch()

    def _do_audit(self):
        """Show audit options dialog with date range picker before running."""
        dlg = tk.Toplevel(self)
        dlg.title("Run Full Audit")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.grab_set()

        # Centre over main window
        dlg.update_idletasks()
        x = self.winfo_x() + self.winfo_width()  // 2 - 220
        y = self.winfo_y() + self.winfo_height() // 2 - 130
        dlg.geometry(f"440x260+{x}+{y}")

        tk.Label(dlg, text="Run Full Audit", bg=BG, fg=TEXT,
                 font=(F_HEAD[0], 13, "bold")).pack(pady=(18, 4))
        tk.Label(dlg, text="Validate and check all listings.\nOptionally filter to a date range.",
                 bg=BG, fg=MUTED, font=F_SM, justify="center").pack()

        sep = tk.Frame(dlg, bg=BORDER, height=1)
        sep.pack(fill="x", padx=20, pady=12)

        # Date range row
        row = tk.Frame(dlg, bg=BG)
        row.pack(padx=20, fill="x")

        use_date = tk.BooleanVar(value=False)
        tk.Checkbutton(row, text="Only listings updated since:", variable=use_date,
                       bg=BG, fg=TEXT, selectcolor=SURFACE2, activebackground=BG,
                       font=F_SM, command=lambda: _toggle()).pack(side="left")

        from datetime import date as _date
        today = _date.today()
        # Year / Month / Day spinners
        spin_frame = tk.Frame(row, bg=BG)
        spin_frame.pack(side="left", padx=(8, 0))

        yr_var  = tk.StringVar(value=str(today.year))
        mo_var  = tk.StringVar(value=f"{today.month:02d}")
        dy_var  = tk.StringVar(value=f"{today.day:02d}")

        spin_kw = dict(bg=SURFACE2, fg=TEXT, insertbackground=ACCENT,
                       relief="flat", font=F_SM, width=5,
                       highlightbackground=BORDER, highlightthickness=1)
        yr_spin = tk.Spinbox(spin_frame, from_=2000, to=2099, textvariable=yr_var,
                              format="%04.0f", **spin_kw)
        mo_spin = tk.Spinbox(spin_frame, from_=1, to=12, textvariable=mo_var,
                              format="%02.0f", **spin_kw)
        dy_spin = tk.Spinbox(spin_frame, from_=1, to=31, textvariable=dy_var,
                              format="%02.0f", **spin_kw)
        tk.Label(spin_frame, text="Year", bg=BG, fg=MUTED, font=F_SM).grid(row=0, column=0, padx=2)
        tk.Label(spin_frame, text="Month", bg=BG, fg=MUTED, font=F_SM).grid(row=0, column=1, padx=2)
        tk.Label(spin_frame, text="Day", bg=BG, fg=MUTED, font=F_SM).grid(row=0, column=2, padx=2)
        yr_spin.grid(row=1, column=0, padx=2)
        mo_spin.grid(row=1, column=1, padx=2)
        dy_spin.grid(row=1, column=2, padx=2)

        def _toggle():
            state = "normal" if use_date.get() else "disabled"
            for w in (yr_spin, mo_spin, dy_spin):
                w.config(state=state)

        _toggle()   # start disabled

        # Buttons
        sep2 = tk.Frame(dlg, bg=BORDER, height=1)
        sep2.pack(fill="x", padx=20, pady=12)

        btn_row = tk.Frame(dlg, bg=BG)
        btn_row.pack()

        def _start():
            cutoff = None
            if use_date.get():
                try:
                    cutoff = f"{yr_var.get()}-{mo_var.get()}-{dy_var.get()}"
                    _date.fromisoformat(cutoff)   # validate
                except ValueError:
                    messagebox.showerror("Invalid date",
                        "Please enter a valid date.", parent=dlg)
                    return
            dlg.destroy()
            self._bridge.run_audit(cutoff_date=cutoff)

        tk.Button(btn_row, text="Cancel", command=dlg.destroy,
                  bg=SURFACE2, fg=MUTED, relief="flat", font=F_SM,
                  padx=18, pady=6, cursor="hand2").pack(side="left", padx=6)
        tk.Button(btn_row, text="▶  Start Audit", command=_start,
                  bg=ACCENT2, fg="black", relief="flat", font=F_SM,
                  padx=18, pady=6, cursor="hand2").pack(side="left", padx=6)

    def _offer_csv_save(self, csv_path: str):
        """Save-As dialog for the generated CSV — runs on the tkinter thread."""
        import shutil
        dest = filedialog.asksaveasfilename(
            title="Save Audit CSV Report",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=os.path.basename(csv_path),
        )
        if dest:
            try:
                shutil.copy2(csv_path, dest)
                messagebox.showinfo("Saved", f"CSV report saved to:\n{dest}")
            except Exception as e:
                messagebox.showerror("Save failed", str(e))

    def _do_sched(self):
        """Scheduler config dialog — set date/time, generate launch command."""
        dlg = tk.Toplevel(self)
        dlg.title("Yearly Audit Scheduler")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.update_idletasks()
        x = self.winfo_x() + self.winfo_width()  // 2 - 250
        y = self.winfo_y() + self.winfo_height() // 2 - 180
        dlg.geometry(f"520x390+{x}+{y}")

        tk.Label(dlg, text="⏱  Yearly Audit Scheduler", bg=BG, fg=TEXT,
                 font=(F_HEAD[0], 13, "bold")).pack(pady=(18, 2))
        tk.Label(dlg,
                 text="The scheduler runs main.py in the background and triggers\n"
                      "a full audit automatically on the date and time you choose.",
                 bg=BG, fg=MUTED, font=F_SM, justify="center").pack(pady=(0, 10))

        tk.Frame(dlg, bg=BORDER, height=1).pack(fill="x", padx=20, pady=4)

        # ── Date / time pickers ───────────────────────────────────────────
        grid = tk.Frame(dlg, bg=BG)
        grid.pack(padx=30, pady=8, fill="x")

        lbl_kw  = dict(bg=BG, fg=MUTED,  font=F_SM, anchor="w")
        spin_kw = dict(bg=SURFACE2, fg=TEXT, relief="flat", font=F_SM, width=6,
                       highlightbackground=BORDER, highlightthickness=1)

        from datetime import date as _date
        today = _date.today()

        mo_var  = tk.StringVar(value="01")
        dy_var  = tk.StringVar(value="01")
        hr_var  = tk.StringVar(value="09")
        min_var = tk.StringVar(value="00")

        tk.Label(grid, text="Run on (Month / Day):", **lbl_kw).grid(
            row=0, column=0, sticky="w", pady=4)
        mo_spin = tk.Spinbox(grid, from_=1, to=12, textvariable=mo_var,
                              format="%02.0f", **spin_kw)
        dy_spin = tk.Spinbox(grid, from_=1, to=31, textvariable=dy_var,
                              format="%02.0f", **spin_kw)
        mo_spin.grid(row=0, column=1, padx=(8,2))
        tk.Label(grid, text="/", bg=BG, fg=TEXT, font=F_SM).grid(row=0, column=2)
        dy_spin.grid(row=0, column=3, padx=(2,0))

        tk.Label(grid, text="Run at (HH : MM):", **lbl_kw).grid(
            row=1, column=0, sticky="w", pady=4)
        hr_spin  = tk.Spinbox(grid, from_=0, to=23, textvariable=hr_var,
                               format="%02.0f", **spin_kw)
        min_spin = tk.Spinbox(grid, from_=0, to=59, textvariable=min_var,
                               format="%02.0f", **spin_kw)
        hr_spin.grid(row=1, column=1, padx=(8,2))
        tk.Label(grid, text=":", bg=BG, fg=TEXT, font=F_SM).grid(row=1, column=2)
        min_spin.grid(row=1, column=3, padx=(2,0))

        tk.Frame(dlg, bg=BORDER, height=1).pack(fill="x", padx=20, pady=8)

        # ── Generated command display ─────────────────────────────────────
        tk.Label(dlg, text="Run this command in a terminal to start the scheduler:",
                 bg=BG, fg=MUTED, font=F_SM).pack(anchor="w", padx=28)

        cmd_var = tk.StringVar()
        cmd_box = tk.Entry(dlg, textvariable=cmd_var, bg=SURFACE2, fg=ACCENT,
                           relief="flat", font=F_MONO, state="readonly",
                           highlightbackground=BORDER, highlightthickness=1,
                           readonlybackground=SURFACE2)
        cmd_box.pack(fill="x", padx=28, pady=(4, 0), ipady=5)

        def _refresh_cmd(*_):
            cmd_var.set(
                f"python main.py --schedule "
                f"--month {mo_var.get()} --day {dy_var.get()} "
                f"--time {hr_var.get()}:{min_var.get()}"
            )

        for v in (mo_var, dy_var, hr_var, min_var):
            v.trace_add("write", _refresh_cmd)
        _refresh_cmd()

        # ── Buttons ───────────────────────────────────────────────────────
        tk.Frame(dlg, bg=BORDER, height=1).pack(fill="x", padx=20, pady=8)
        btn_row = tk.Frame(dlg, bg=BG)
        btn_row.pack()

        def _copy():
            self.clipboard_clear()
            self.clipboard_append(cmd_var.get())
            messagebox.showinfo("Copied", "Command copied to clipboard.", parent=dlg)

        def _install():
            """Install a launchd plist (macOS) or cron job (Linux) for auto-start."""
            import platform, subprocess, stat
            month    = mo_var.get().zfill(2)
            day      = dy_var.get().zfill(2)
            run_time = f"{hr_var.get().zfill(2)}:{min_var.get().zfill(2)}"
            hr_int   = int(hr_var.get())
            min_int  = int(min_var.get())
            mo_int   = int(month)
            dy_int   = int(day)

            python_exe  = sys.executable
            main_script = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "main.py"))
            log_path = os.path.expanduser("~/Library/Logs/directory_agent_scheduler.log")

            system = platform.system()

            if system == "Darwin":
                # ── macOS launchd plist ───────────────────────────────────
                plist_dir  = os.path.expanduser("~/Library/LaunchAgents")
                plist_path = os.path.join(plist_dir, "com.directoryagent.scheduler.plist")
                os.makedirs(plist_dir, exist_ok=True)

                plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.directoryagent.scheduler</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_exe}</string>
        <string>{main_script}</string>
        <string>--schedule</string>
        <string>--month</string><string>{mo_int}</string>
        <string>--day</string><string>{dy_int}</string>
        <string>--time</string><string>{run_time}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
</dict>
</plist>"""
                with open(plist_path, "w") as f:
                    f.write(plist)

                # Unload existing (ignore errors), then load
                subprocess.run(["launchctl", "unload", plist_path],
                               capture_output=True)
                result = subprocess.run(["launchctl", "load", plist_path],
                                        capture_output=True, text=True)
                if result.returncode == 0:
                    messagebox.showinfo("Auto-Start Installed",
                        f"Scheduler installed and running.\n\n"
                        f"It will trigger a full audit every year on "
                        f"{month}/{day} at {run_time}.\n\n"
                        f"Logs: {log_path}", parent=dlg)
                else:
                    messagebox.showerror("Install Failed",
                        f"launchctl error:\n{result.stderr}", parent=dlg)

            elif system == "Linux":
                # ── Linux cron job ────────────────────────────────────────
                cron_line = (
                    f"{min_int} {hr_int} {dy_int} {mo_int} * "
                    f"{python_exe} {main_script} --schedule "
                    f"--month {mo_int} --day {dy_int} --time {run_time} "
                    f">> ~/directory_agent_scheduler.log 2>&1"
                )
                result = subprocess.run(
                    ["crontab", "-l"], capture_output=True, text=True)
                existing = result.stdout if result.returncode == 0 else ""
                # Remove any previous directory agent cron line
                lines = [l for l in existing.splitlines()
                         if "directory_agent" not in l and "main.py --schedule" not in l]
                lines.append(cron_line)
                new_cron = "\n".join(lines) + "\n"
                proc = subprocess.run(["crontab", "-"], input=new_cron,
                                      capture_output=True, text=True)
                if proc.returncode == 0:
                    messagebox.showinfo("Auto-Start Installed",
                        f"Cron job installed.\n\nWill run: {month}/{day} at {run_time} yearly.",
                        parent=dlg)
                else:
                    messagebox.showerror("Install Failed",
                        f"crontab error:\n{proc.stderr}", parent=dlg)

            else:
                messagebox.showwarning("Not Supported",
                    f"Auto-install is not supported on {system}.\n"
                    f"Use the copied command to start the scheduler manually.",
                    parent=dlg)

        def _uninstall():
            """Remove the launchd plist / cron job."""
            import platform, subprocess
            system = platform.system()
            if system == "Darwin":
                plist_path = os.path.expanduser(
                    "~/Library/LaunchAgents/com.directoryagent.scheduler.plist")
                subprocess.run(["launchctl", "unload", plist_path], capture_output=True)
                try:
                    os.remove(plist_path)
                    messagebox.showinfo("Removed", "Auto-start scheduler removed.", parent=dlg)
                except FileNotFoundError:
                    messagebox.showinfo("Not found", "No scheduler was installed.", parent=dlg)
            elif system == "Linux":
                import subprocess
                result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
                lines  = [l for l in result.stdout.splitlines()
                          if "directory_agent" not in l and "main.py --schedule" not in l]
                subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n",
                               capture_output=True, text=True)
                messagebox.showinfo("Removed", "Scheduler cron job removed.", parent=dlg)
            else:
                messagebox.showwarning("Not Supported",
                    "Manual removal required on this OS.", parent=dlg)

        tk.Button(btn_row, text="Close",  command=dlg.destroy,
                  bg=SURFACE2, fg=MUTED, relief="flat", font=F_SM,
                  padx=12, pady=6, cursor="hand2").pack(side="left", padx=4)
        tk.Button(btn_row, text="📋  Copy Command", command=_copy,
                  bg=SURFACE2, fg=TEXT, relief="flat", font=F_SM,
                  padx=12, pady=6, cursor="hand2").pack(side="left", padx=4)
        tk.Button(btn_row, text="✅  Install Auto-Start", command=_install,
                  bg=SUCCESS, fg="black", relief="flat", font=F_SM,
                  padx=12, pady=6, cursor="hand2").pack(side="left", padx=4)
        tk.Button(btn_row, text="🗑  Remove", command=_uninstall,
                  bg=DANGER, fg="black", relief="flat", font=F_SM,
                  padx=12, pady=6, cursor="hand2").pack(side="left", padx=4)

    def _show_ctx(self, e):
        item = self._tree.identify_row(e.y)
        if item:
            self._tree.selection_set(item)
            self._ctx.post(e.x_root, e.y_root)

    def _selected_row(self):
        sel = self._tree.selection()
        if not sel:
            return None
        iid = sel[0]
        return next((r for r in self._data if str(r["id"]) == iid), None)

    def _ctx_validate(self):
        row = self._selected_row()
        if row:
            self._bridge.validate_row(row)

    def _ctx_verify(self):
        row = self._selected_row()
        if row:
            # Ask the agent to validate format first, then check online.
            # Do NOT pass the raw phone — let the agent fetch it via get_listings
            # so validate_listing can catch format errors before the web search.
            self._send_msg(
                f"Validate and verify the contact details for '{row['title']}': "
                f"first check if the phone, email and website format is valid, "
                f"then search online to check if the details are current."
            )

    def _ctx_ask(self):
        row = self._selected_row()
        if row:
            self._input.delete(0, "end")
            self._input.insert(0, f"Validate the contact details of {row['title']}")
            self._input.focus()

    # ── CHAT ──────────────────────────────────────────────

    def _send(self, *_):
        txt = self._input.get().strip()
        if txt:
            self._input.delete(0, "end")
            self._send_msg(txt)

    def _send_msg(self, txt: str):
        self._append_chat("You", txt, "you")
        self._append_chat("Agent", "Thinking…", "lbl")
        self._btn_stop.config(state="normal")
        self._bridge.chat(txt)

    def _append_chat(self, sender, text, style):
        self._chat_log.config(state="normal")
        self._chat_log.insert("end", f"{sender}:  ", "lbl")
        self._chat_log.insert("end", text + "\n\n", style)
        self._chat_log.see("end")
        self._chat_log.config(state="disabled")

    def _clear_console(self):
        self._console.config(state="normal")
        self._console.delete("1.0", "end")
        self._console.config(state="disabled")


# ─────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    App().mainloop()
