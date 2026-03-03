import os
import requests
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv
from datetime import datetime, timedelta

load_dotenv()
mcp = FastMCP("Directory-Manager")

WP_URL = os.getenv("WP_URL")
AUTH = (os.getenv("WP_USERNAME"), os.getenv("WP_APP_PASSWORD"))
API_ENDPOINT = f"{WP_URL}/index.php?rest_route=/wp/v2/listdom-listing&per_page=30"

print(f"Debug: Connecting to {API_ENDPOINT}")

# Tool for fetching data from Wordpress site
@mcp.tool()
def get_listings(query: str = None):
    """
    Fetch diectory listings from WordpPress.
    Use 'query' to search for a specific name (e.g., 'Bayaudiology').
    """

    url = API_ENDPOINT
    if query:
        url += f"&search={query}"
    else:
        url += "&per_page=50"

    print(f"Attempting to fetch from:{API_ENDPOINT}")
    
    try:

        response = requests.get(API_ENDPOINT, auth=AUTH, timeout=10)

        if response.status_code == 200:
            data = response.json()

            if not isinstance(data, list):
                return f"Unexpected response format: {str(data)[:100]}"

            return [
                {
                "id": i["id"], 
                "title": i["title"]["rendered"], 
                "last_updated": i.get("modified"),
                "email": i.get("wp_metadata", {}).get("email", "N/A"),
                "website": i.get("wp_metadata", {}).get("website", "N/A"),
                "phone": i.get("wp_metadata", {}).get("phone", "N/A"),

                } 
                for i in data]
        else:
            return "Failed to connect to WordPress. Status: {response.status_code}. Details: {response.text[:100]}"

    except Exception as e:
        return f"Connection Error: {str(e)}"



# Tools for compairing email and website domain
@mcp.tool()
def compare_domains(email: str, website: str):
    """Compare email domain vs website domain to find inconsistencies."""
    if not email or not website:
        return "Incomplete data"
    
    email_domain = email.split('@')[-1].lower()

    web_domain = website.replace('https://', '').replace('http://', '').split('/')[0].lower()

    if email_domain == web_domain:
        return "Match"
    return f"Mismatch: Email ({email_domain}) vs Website ({web_domain})"

# Tools for updating meta
@mcp.tool()
def update_listing_meta (listing_id: int, field: str, value: str):
    """Update lsd_phone, lsd_email, or lsd_website in WordPress."""

    key_map = {"email": "lsd_email", "phone": "lsd_phone", "website": "lsd_website"}

    payload = {"meta": {key_map[field]: value}}
    url = f"{WP_URL}/index.php?rest_route=/wp/v2/listdom-listing/{listing_id}"

    r = requests.post(url, json=payload, auth=AUTH)
    return "Update Successful" if r.status_code == 200 else "Update Failed"

# # Web Verification Tool
# @mcp.tool()
# def verify_listing_details(name: str, current_website: str, current_phone: str, current_email: str):
#     """
#     Search the web for a service to see if the current database details are still accurate.
#     Returns any differences found.
#     """
#     search_query = f"official contact details for {name} Hawke's Bay New Zealand website phone number and email"

#     response = requests.post("https://api.tavily.com/search", json={
#         "api_key": os.getenv("TAVILY_API_KEY"),
#         "query": search_query,
#         "search_depth": "basic",
#         "include_domains": [".nz"]
#     })
    
#     search_results = response.json()

#     # Send back to Ollama to 'verify' if they match
#     return {
#         "search_context": [r['content'] for r in search_results.get('results', [])[:3]],
#         "instruction": f"Compare these results with Website: {current_website}, Phone: {current_phone} and Email: {current_email}. Report any discrepancies."
#     }

@mcp.tool()
def verify_listing_details(name: str, current_website: str, current_phone: str, current_email: str):
    """
    Search Google for a service to see if the current database details are still accurate.
    """
    # Refined query for NZ context
    search_query = f"official contact details for {name} Hawke's Bay New Zealand"
    
    # Google Custom Search API Endpoint
    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": os.getenv("GOOGLE_API_KEY"),
        "cx": os.getenv("GOOGLE_CSE_ID"),
        "q": search_query,
        "num": 3  # Number of results to return
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        search_results = response.json()

        # Google calls the snippets 'snippet' instead of 'content'
        # We use .get() to avoid crashes if no results are found
        items = search_results.get('items', [])
        context = [item.get('snippet', '') for item in items]

        return {
            "search_context": context,
            "instruction": f"Compare these Google results with Website: {current_website}, Phone: {current_phone} and Email: {current_email}. Report any discrepancies found for this Hawke's Bay organization."
        }
    except Exception as e:
        return f"Google Search Error: {str(e)}"
    
from datetime import datetime, timedelta

# @mcp.tool()
# def audit_and_notify_outdated_listings():
#     """
#     Finds listings older than 1 year and sends a reminder email to the owner.
#     """
#     listings = get_listings() # Uses your existing function
#     one_year_ago = datetime.now() - timedelta(days=365)
#     notifications_sent = 0

#     for item in listings:
#         last_updated_str = item.get("last_updated")
#         if not last_updated_str or last_updated_str == "N/A":
#             continue

#         last_updated = datetime.strptime(last_updated_str, "%b %d, %Y")
        
#         if last_updated < one_year_ago:
#             # Trigger the WordPress Email API we just built
#             endpoint = f"https://your-site.local/wp-json/directory/v1/remind-owner/{item['id']}"
#             response = requests.post(endpoint, auth=AUTH)
            
#             if response.status_code == 200:
#                 notifications_sent += 1

#     return f"Audit complete. Sent {notifications_sent} reminder emails to outdated listings."    

# @mcp.tool()
# def audit_notified_and_report_to_admin():
#     """
#     Audits listings, emails owners, and sends a summary report to the Admin.
#     """
#     listings = get_listings()
#     one_year_ago = datetime.now() - timedelta(days=365)
#     flagged_names = []

#     for item in listings:
#         # ... (previous logic to check dates) ...
#         if last_updated < one_year_ago:
#             # Notify Owner (as done before)
#             requests.post(f"https://your-site.local/wp-json/directory/v1/remind-owner/{item['id']}", auth=AUTH)
#             flagged_names.append(item['title'])

#     # Prepare the Admin Report
#     if flagged_names:
#         report_text = "The following listings were identified as outdated (>1 year) and notified:\n\n- " + "\n- ".join(flagged_names)
        
#         requests.post(
#             "https://your-site.local/wp-json/directory/v1/admin-audit-report",
#             auth=AUTH,
#             json={"report": report_text}
#         )

#     return f"Audit finished. {len(flagged_names)} owners notified and Admin summary sent."

if __name__ == "__main__":
    mcp.run()