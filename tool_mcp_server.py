
from fastmcp import FastMCP
# from context_store import request_context
# from parameter_store import update_required_parameters,session_required_parameters,set_parameter_value,delete_user
import requests
import os
from bs4 import BeautifulSoup
from dotenv import load_dotenv
load_dotenv()

mcp = FastMCP("MCP Tools")


@mcp.tool()
def linkedin_job_search(job_title: str, location: str, date_posted: str = "today"):
    """
    Search for jobs on LinkedIn based on job title and location.
    date_posted options: 'today', 'week', 'month'
    Returns formatted job listings with links.
    """
    num_jobs=15

    time_filter = {
        "today": "r40400",
        "week": "r604800",
        "month": "r2592000"
    }.get(date_posted, "r40400")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        )
    }
    print("--=-==-=-=- this is the details --=-=-=-=",job_title,location,date_posted)
    jobs_list = []
    start = 0

    while len(jobs_list) < num_jobs:
        url = (
            f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
            f"?keywords={requests.utils.quote(job_title)}"
            f"&location={requests.utils.quote(location)}"
            # f"&f_TPR={time_filter}"
            f"&f_TPR={time_filter}"
            f"&start={start}"
        )

        response = requests.get(url, headers=headers)

        if response.status_code != 200:
            break

        soup = BeautifulSoup(response.text, "html.parser")
        job_cards = soup.find_all("li")

        if not job_cards:
            break

        for card in job_cards:
            try:
                title_tag = card.find("h3", class_="base-search-card__title")
                title = title_tag.get_text(strip=True) if title_tag else "N/A"

                company_tag = card.find("h4", class_="base-search-card__subtitle")
                company = company_tag.get_text(strip=True) if company_tag else "N/A"

                location_tag = card.find("span", class_="job-search-card__location")
                job_location = location_tag.get_text(strip=True) if location_tag else "N/A"

                date_tag = card.find("time")
                date = date_tag.get_text(strip=True) if date_tag else "N/A"

                link_tag = card.find("a", class_="base-card__full-link")
                link = link_tag["href"] if link_tag else "N/A"

                if title != "N/A":
                    jobs_list.append({
                        "title": title,
                        "company": company,
                        "location": job_location,
                        "date": date,
                        "link": link
                    })
            except Exception:
                continue

        start += 25
        # time.sleep(1.5)

    jobs_list = jobs_list[:num_jobs]

    # ── Format output as string for the agent ──
    if not jobs_list:
        return "❌ No jobs found. Try different title or location."

    output = f"✅ Found {len(jobs_list)} jobs for '{job_title}' in '{location}':\n\n"
    for i, job in enumerate(jobs_list, 1):
        output += (
            f"#{i}\n"
            f"💼 Title    : {job['title']}\n"
            f"🏢 Company  : {job['company']}\n"
            f"📍 Location : {job['location']}\n"
            f"📅 Posted   : {job['date']}\n"
            f"🔗 Link     : {job['link']}\n"
            f"{'─'*50}\n"
        )

    print(jobs_list)
    return {
        "type": "jobs",           
        "jobs": jobs_list
    }


if __name__ == "__main__":
    print("...Starting MCP Server...")
    mcp.run(transport="streamable-http", port=8004)