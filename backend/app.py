from flask import Flask, request, jsonify
import os
import fitz  
import google.generativeai as genai
from dotenv import load_dotenv
from flask_cors import CORS
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse
import smtplib
from email.mime.text import MIMEText
import email.utils

load_dotenv()

# Gemini API configuration
api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise ValueError("Missing GEMINI_API_KEY environment variable")

genai.configure(api_key=api_key)

# SMTP configuration
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")

app = Flask(__name__)
CORS(app)  

def is_url(text):
    """Check if the provided text is a URL"""
    try:
        result = urlparse(text)
        return all([result.scheme, result.netloc])
    except:
        return False

def extract_email_from_resume(resume_text):
    """Extract email address from resume text"""
    email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    match = re.search(email_pattern, resume_text)
    return match.group(0) if match else None

def extract_company_email(url):
    """Extract company email from website"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Look for common contact email patterns
        email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
        emails = re.findall(email_pattern, soup.get_text())
        
        # Filter for likely company emails (avoid generic ones)
        for email in emails:
            if not any(x in email.lower() for x in ['gmail', 'yahoo', 'hotmail']):
                return email
        return None
    except Exception as e:
        print(f"Error extracting company email: {e}")
        return None

def scrape_job_description(url):
    """Scrape job description and attempt to extract company name from a URL"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Try to extract company name from meta tags, title, or content
        company_name = None
        meta_tags = soup.find_all('meta', {'name': ['og:site_name', 'application-name']})
        for tag in meta_tags:
            if tag.get('content'):
                company_name = tag.get('content')
                break
        if not company_name:
            title = soup.find('title')
            if title and title.text:
                company_name = title.text.split('|')[0].strip()
        if not company_name:
            # Fallback: Extract from URL domain
            parsed_url = urlparse(url)
            company_name = parsed_url.netloc.replace('www.', '').split('.')[0].capitalize()
        
        for script in soup(['script', 'style', 'header', 'footer', 'nav']):
            script.decompose()
            
        page_text = soup.get_text(separator=' ', strip=True)
        
        description = extract_job_description_with_gemini(page_text, url)
        return description, company_name
    except Exception as e:
        print(f"Error scraping job description: {e}")
        return None, None

def extract_job_description_with_gemini(page_content, url):
    """Use Gemini to extract the job description from page content"""
    prompt = f"""
You are a job description extraction assistant.

I have a web page from the following URL: {url}

Please extract ONLY the job description from this page content. Include job requirements, responsibilities, qualifications, and skills.
Format this as a clean job description without any HTML or extraneous website content.
Focus only on the actual job description text.

Page content:
{page_content[:10000]}  # Limiting content length
"""
    
    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        print(f"Error extracting job description: {e}")
        return None
        
def extract_text_from_pdf(file):
    try:
        doc = fitz.open(stream=file.read(), filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text()
        return text
    except Exception as e:
        print(f"Error extracting text from PDF: {e}")
        return ""

def parse_gemini_response(response_text):
    print("=== FULL GEMINI RESPONSE ===")
    print(response_text)
    print("===========================")
    
    result = {
        "skills": [],
        "cold_emails": [],
        "reasons": [],
        "improvement_suggestions": [],
        "candidate_email": None,
        "company_email": None
    }
    
    # Extract skills
    skills_section = re.search(r'key skills:?\s*(.*?)(?:\n\n|\n[A-Z]|$)', response_text, re.IGNORECASE | re.DOTALL)
    if skills_section:
        skills_text = skills_section.group(1)
        if '-' in skills_text:
            skills = [s.strip().strip('- ') for s in skills_text.split('\n') if s.strip()]
        else:
            skills = [s.strip() for s in skills_text.split(',')]
        result["skills"] = [s for s in skills if s]
        print(f"Extracted skills: {result['skills']}")
    
    # Extract email
    email_section = re.search(r'Cold Email:?\s*(.*?)(?:\n\nImprovement Suggestions:|\n\n4\. Improvement Suggestions:|$)', 
                           response_text, re.IGNORECASE | re.DOTALL)
    
    if email_section:
        email_content = email_section.group(1).strip()
        print("=== EMAIL SECTION FOUND ===")
        print(email_content)
        print("==========================")
        
        if email_content and len(email_content.strip()) > 20:
            result["cold_emails"] = [email_content]
            print(f"Found 1 email template: {email_content[:50]}...")
        else:
            print("Email content too short or empty")
    
    # Extract reasons (updated regex to match "Match Analysis" or "Reasons")
    reasons_section = re.search(r'(match analysis|reasons|good match|bad match):?\s*(.*?)(?:\n\n|\n[A-Z]|$)', 
                             response_text, re.IGNORECASE | re.DOTALL)
    if reasons_section:
        reasons_text = reasons_section.group(2).strip()
        print("=== REASONS SECTION FOUND ===")
        print(reasons_text)
        print("==========================")
        if '-' in reasons_text:
            reasons = [r.strip().strip('- ') for r in reasons_text.split('\n') if r.strip()]
        else:
            reasons = [reasons_text.strip()]
        result["reasons"] = [r for r in reasons if r]
        print(f"Extracted reasons: {result['reasons']}")
    
    # Extract improvement suggestions
    improvements_section = re.search(r'(improvement suggestions|resume suggestions|suggestions for improvement):?\s*(.*?)(?:\n\n|\n[A-Z]|$)', 
                                  response_text, re.IGNORECASE | re.DOTALL)
    if improvements_section:
        improvements_text = improvements_section.group(2)
        if '-' in improvements_text:
            improvements = [i.strip().strip('- ') for i in improvements_text.split('\n') if i.strip()]
        else:
            improvements = [improvements_text.strip()]
        result["improvement_suggestions"] = [i for i in improvements if i]
        print(f"Extracted improvement suggestions: {result['improvement_suggestions']}")
    
    print(f"Found {len(result['cold_emails'])} email templates")
    
    return result

def analyze_resume_with_gemini(resume_text, job_desc, company_name=None):
    prompt = f"""
You are an AI career assistant analyzing a resume against a job description to generate a fully personalized, ready-to-send cold email with no placeholders.

CRITICAL FORMATTING REQUIREMENTS:
- Do NOT use asterisks (*) ANYWHERE in your response
- Use plain text formatting with hyphens (-) for bullet points
- Generate EXACTLY 1 cold email that is complete, personalized, and ready to send
- Do NOT include placeholders like [Name], [Company], or [Position]
- Use the candidate's full name (Rohana Mahimkar) for the email signature
- Extract the EXACT company name and position title from the job description
- If company name is provided separately ({company_name if company_name else 'not provided'}), use it; otherwise, extract from the job description or infer from the URL context (e.g., domain name)
- If position title cannot be extracted, use "Software Engineer" as a fallback
- If company name cannot be extracted, use the provided company name or infer from the job description context; avoid generic terms like "Your Company"
- Use specific skills (e.g., React, Node.js, Next.js, Flask, FastAPI, AI/ML, GenAI, LLMs) and project details (e.g., Innovatrix, a developer utility hub) from the resume
- Reference specific qualifications and achievements that match the job description
- Ensure the email is professional, concise (150-200 words), and includes enthusiasm for the role and company
- Include a call to action requesting an interview
- Start the email section with "Cold Email:"
- Start the match analysis section with "Match Analysis:"

Analyze the resume against the job description and provide:

1. Key Skills:
   - List key skills from the resume using hyphens
   - Focus on technical skills (e.g., React, Node.js, Next.js, Flask, FastAPI, AI/ML, GenAI, LLMs)

2. Match Analysis:
   - Reason 1 why the candidate is a good fit for this role
   - Reason 2 why the candidate is a good fit for this role
   - Any potential gaps or mismatches between the resume and job requirements

3. Cold Email:
   Subject: Application for [exact position title] at [exact company name]
   
   Dear Hiring Manager,
   
   I am excited to apply for the [exact position title] at [exact company name], as advertised. My expertise in [2-3 specific skills from resume matching job description, e.g., React, Node.js, and AI/ML] aligns closely with your requirements. Notably, my project Innovatrix, a developer utility hub built with Next.js, Flask, and FastAPI, demonstrates my ability to deliver high-performance applications using [mention specific technologies or features, e.g., GenAI and LLMs]. This project enhanced [specific outcome, e.g., developer productivity or user experience] through [specific feature, e.g., intuitive UI/UX and efficient backend]. I am enthusiastic about [specific company aspect, e.g., its innovative AI solutions or mission], and I am eager to contribute my skills to your team. I would welcome the opportunity to discuss how my experience can support your goals in an interview.
   
   Sincerely,
   [full name from resume]

4. Improvement Suggestions:
   - Suggestion 1 to improve resume for this job
   - Suggestion 2 to improve resume for this job
   - Suggestion 3 to improve resume for this job

Resume:
{resume_text}

Job Description:
{job_desc}
"""
    
    model = genai.GenerativeModel(
        'gemini-1.5-flash', 
        generation_config=genai.GenerationConfig(temperature=0.0)  # Strict adherence
    )
    response = model.generate_content(prompt)
    return response.text

@app.route('/analyze', methods=['POST'])
def analyze():
    if 'resume' not in request.files:
        return jsonify({"error": "No resume file provided"}), 400
    
    resume_file = request.files['resume']
    job_url = request.form.get('job_description', '')
    manual_company_email = request.form.get('company_email', '')
    
    if not job_url:
        return jsonify({"error": "No job posting URL provided"}), 400
    
    if resume_file.filename == '':
        return jsonify({"error": "No file selected"}), 400
    
    if not resume_file.filename.lower().endswith('.pdf'):
        return jsonify({"error": "Only PDF files are supported"}), 400
    
    if not is_url(job_url):
        return jsonify({"error": "Please provide a valid URL"}), 400
    
    try:
        resume_text = extract_text_from_pdf(resume_file)
        if not resume_text:
            return jsonify({"error": "Could not extract text from PDF"}), 400
        
        scraped_job_desc, company_name = scrape_job_description(job_url)
        if not scraped_job_desc:
            return jsonify({"error": "Could not extract job description from the provided URL"}), 400
        
        candidate_email = extract_email_from_resume(resume_text)
        company_email = manual_company_email if manual_company_email else extract_company_email(job_url)
        
        analysis_text = analyze_resume_with_gemini(resume_text, scraped_job_desc, company_name)
        structured_result = parse_gemini_response(analysis_text)
        
        structured_result["candidate_email"] = candidate_email
        structured_result["company_email"] = company_email
        
        return jsonify(structured_result)
    
    except Exception as e:
        print(f"Error processing request: {e}")
        return jsonify({"error": "An error occurred while processing your request"}), 500

@app.route('/send_email', methods=['POST'])
def send_email():
    try:
        data = request.get_json()
        candidate_email = data.get('candidate_email')
        company_email = data.get('company_email')
        email_content = data.get('email_content')
        
        if not all([candidate_email, company_email, email_content]):
            return jsonify({"error": "Missing required email parameters"}), 400
        
        # Parse subject and body from email content
        subject_match = re.search(r'Subject: (.*?)\n\n', email_content)
        subject = subject_match.group(1) if subject_match else "Job Application"
        body = re.sub(r'Subject:.*?\n\n', '', email_content, 1)
        
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = email.utils.formataddr(("Job Applicant", candidate_email))
        msg['To'] = company_email
        msg['Date'] = email.utils.formatdate(localtime=True)
        
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        
        return jsonify({"message": "Email sent successfully"})
    
    except Exception as e:
        print(f"Error sending email: {e}")
        return jsonify({"error": f"Failed to send email: {str(e)}"}), 500

if __name__ == '__main__':
    app.run(debug=True)
