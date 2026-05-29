
from fastmcp import FastMCP
# from context_store import request_context
# from parameter_store import update_required_parameters,session_required_parameters,set_parameter_value,delete_user
import requests
import os
from bs4 import BeautifulSoup
from dotenv import load_dotenv
load_dotenv()
import time
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



from pathlib import Path
from langchain_community.document_loaders import PyPDFLoader, TextLoader, Docx2txtLoader
from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage

UPLOAD_DIR = Path("uploads")

# ── shared: load file text from uploads folder ────────────────────────────────
def _load_file_text(filename: str) -> str:
    time.sleep(1)
    path = UPLOAD_DIR / filename
    if not path.exists():
        return ""
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            loader = PyPDFLoader(str(path))
        elif ext == ".txt":
            loader = TextLoader(str(path), encoding="utf-8")
        elif ext in (".docx", ".doc"):
            loader = Docx2txtLoader(str(path))
        else:
            return ""
        docs = loader.load()
        print("-=-=-=-==docs=--=-=-=-=",docs)
        return "\n".join(d.page_content for d in docs).strip()
    except Exception as e:
        return f"Error reading file: {e}"


def _call_llm(prompt: str) -> str:
    llm = ChatGroq(model="meta-llama/llama-4-scout-17b-16e-instruct")
    response = llm.invoke([HumanMessage(content=prompt)])
    return response.content


# ── TOOL 1: ATS Resume Checker ────────────────────────────────────────────────
@mcp.tool()
def ats_checker_resume(query: str, file_names: list[str]) -> str:
    """
    Check the ATS score of a resume against a job description.
    Extracts resume content from uploaded files and scores it.
    
    Args:
        query: User query
        file_names: List filenames (e.g. ['resume.pdf'])
    
    Returns:
        ATS score, matching keywords, missing keywords, and improvement tips
    """
    if not file_names:
        return "No file provided. Please upload a resume file."

    resume_text = ""
    for fname in file_names:
        text = _load_file_text(fname)
        if text:
            resume_text += f"\n{text}"

    if not resume_text.strip():
        return "Could not extract text from the uploaded file."
    print("-=-=-=-=-==--=-=-==-",resume_text)

    prompt = f"""You are an expert ATS (Applicant Tracking System) analyzer.

                USER REQUEST: {query}

                RESUME CONTENT:
                {resume_text[:5000]}

                TASK:
                Analyze this resume and provide a detailed ATS evaluation.

                Return your response in this exact format:

                ## ATS Score: [X/100]
                ## ✅ Matching Keywords Found
                - List all keywords/skills found in the resume

                ## ❌ Missing Important Keywords  
                - List important keywords missing based on the role/JD mentioned

                ## 📊 Section Analysis
                - Contact Info: [present/missing]
                - Summary/Objective: [present/missing]  
                - Work Experience: [present/missing]
                - Skills Section: [present/missing]
                - Education: [present/missing]
                - Certifications: [present/missing]

                ## 💡 Improvement Suggestions
                - Give 3-5 specific actionable tips to improve ATS score

                ## Overall Assessment
                Brief 2-3 line summary of the resume quality.
                """
    response=_call_llm(prompt)
    print("-=-=-=-=-=-=-=-= response =-=-=-=-=-=-=",response)
    return response


# ── TOOL 2: File Summary ──────────────────────────────────────────────────────
@mcp.tool()
def file_summary(query: str, file_names: list[str]) -> str:
    """
    Summarise the content of an uploaded file based on user query.
    Works for resumes, documents, PDFs, text files.
    
    Args:
        query: What the user wants to know or summarise
        file_names: List of uploaded filenames
    
    Returns:
        A clear structured summary of the file content
    """
    if not file_names:
        return "No file provided. Please upload a file to summarise."

    all_text = ""
    for fname in file_names:
        text = _load_file_text(fname)
        if text:
            all_text += f"\n=== {fname} ===\n{text}"

    if not all_text.strip():
        return "Could not extract text from the uploaded file."

    prompt = f"""You are a professional document analyst.

            USER REQUEST: {query}

            DOCUMENT CONTENT:
            {all_text[:5000]}

            TASK:
            Provide a clear, structured summary based on what the user is asking.

            If this is a RESUME, include:
            - Candidate Name & Contact
            - Current/Target Role  
            - Total Experience
            - Key Skills & Technologies
            - Education
            - Recent Work Experience highlights
            - Certifications

            If this is a general DOCUMENT, include:
            - Main topic/purpose
            - Key points (bullet list)
            - Important data or findings
            - Conclusion or recommendations

            Keep the summary concise but comprehensive.
            """
    response=_call_llm(prompt)
    print("-=-=-=-=-=-=-=-= response =-=-=-=-=-=-=",response)
    return response


# ── TOOL 3: Job Search From File ──────────────────────────────────────────────
@mcp.tool()
def file_job_search(query: str, file_names: list[str]) -> dict:
    """
    Extract job role, skills, and location from an uploaded resume
    and search for matching LinkedIn jobs
    Args:
        query: User query — may include preferred location or role preference
        file_names: List of uploaded resume filename
    Returns:
        Job listings matching the resume profile
    """
    if not file_names:
        return {"type": "text", "text": "No resume file provided. Please upload your resume."}

    resume_text = ""
    for fname in file_names:
        text = _load_file_text(fname)
        if text:
            resume_text += f"\n{text}"

    if not resume_text.strip():
        return {"type": "text", "text": "Could not extract text from resume."}

    # Step 1: extract role + location from resume using LLM
    extract_prompt = f"""Extract job search information from this resume.

            USER REQUEST: {query}

            RESUME:
            {resume_text[:4000]}
            Step 1 — Extract Details:
                - Convert position or keywords into OR-based keywords forr Job Title:
                - Extract location
                - Extract date_posted (day/week/month), default = today
            Return ONLY a JSON object with these fields, nothing else:
            {{
            "job_title": Example: "ai ml engineer and genai and llm" into → "AI OR ML OR Engineer OR GenAI OR LLM",
            "location": "city or location from resume or user query, default to 'india' if not found",
            "date_posted": "today"
            }}
            """
    try:
        extracted_raw = _call_llm(extract_prompt)
        import json, re
        match = re.search(r'\{.*\}', extracted_raw, re.DOTALL)
        extracted = json.loads(match.group()) if match else {}
    except Exception:
        extracted = {}

    job_title = extracted.get("job_title", "Software Engineer")
    location  = extracted.get("location", "India")
    date_posted  = extracted.get("date_posted", "today")

    # Step 2: call your existing linkedin_job_search tool
    # (it's already registered in MCP — call it directly)
    try:
        results = linkedin_job_search(
            job_title=job_title,
            location=location,
            date_posted=date_posted,
        )
        return results
    except Exception as e:
        return {"type": "text", "text": f"Job search failed"}


if __name__ == "__main__":
    print("...Starting MCP Server...")
    mcp.run(transport="streamable-http", port=8004)