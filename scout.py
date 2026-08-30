#!/usr/bin/env python3
"""
internship-scout — fetches fresh internships/entry-level jobs from many sources,
scores them against profile.json, and delivers:

  >= 85  -> individual alert immediately  (subject: "X [NN%] Match - Role at Company")
  70-84  -> consolidated digest (green tier)
  60-69  -> digest as "Stretch Match" with missing skills
  < 60   -> silent, except rare major-company exception (flagged with a reason)

Delivery: email (Gmail app password) and/or Telegram bot. If the primary channel
fails, the other one is used automatically (delivery: "auto").

Zero third-party dependencies. Python 3.8+.

  python scout.py             # normal run: fetch, score, deliver
  python scout.py --no-send   # dry run: print what would be delivered
  python scout.py --digest    # force the digest to be sent this run
"""
import json, re, ssl, sys, time, urllib.request, urllib.error, urllib.parse
import smtplib, html as html_mod
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE_DIR = BASE / "state"
LOG_DIR = BASE / "logs"
STATE_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
SEEN_FILE = STATE_DIR / "seen.json"
ERR_FILE = STATE_DIR / "source_errors.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CTX = ssl.create_default_context()

FIRE = "\U0001F525"   # fire
GOOD = "\U0001F7E2"   # green
STRT = "\U0001F7E1"   # yellow
GEM  = "\U0001F48E"   # gem

# ------------------------------------------------------------------ utils
def log(msg):
    line = time.strftime("[%Y-%m-%d %H:%M:%S] ") + str(msg)
    print(line, flush=True)
    try:
        with open(LOG_DIR / "scout.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass

def http(url, headers=None, data=None, method="GET", timeout=35):
    hdrs = {"User-Agent": UA, "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        hdrs.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode() if isinstance(data, (dict, list)) else data
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.read()

def get_json(url, **kw):
    return json.loads(http(url, **kw).decode("utf-8", "replace"))

def clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default

def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)

def strip_bs(s):
    r"""Strip unicode-escape junk (e.g. backslash-u sequences) from scraped titles."""
    if not s:
        return ""
    s = s.replace("\\/", "/")
    try:
        s = s.encode("latin-1", "ignore").decode("unicode_escape")
    except Exception:
        pass
    return re.sub(r"\s+", " ", s).strip()

def jd(key, *parts):
    return f"https://www.linkedin.com/jobs/view/{key}"

# ---------------------------------------------------------- scraper pieces
JD_LINK_RE = re.compile(
    r'href="(?:https://www\.indeed\.viewjob\?jk=|/rc/clk\?jk=|/viewjob\?jk=)([a-f0-9]{16})"', re.I)
JD_TITLE_RE = re.compile(r'<h2[^>]*class="[^"]*jobTitle[^"]*"[^>]*>(.*?)</h2>', re.S | re.I)
JSON_LD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)
WF_JOB_LINK = re.compile(r'href="(/jobs/(\d{6,10})-([a-z0-9\-]+))"')
WF_JOB_BLOB = re.compile(r'"jobListings":\s*\[\s*\{\s*"id":\s*"?(\d{6,10})"?')
WF_TITLE_RE = re.compile(r'"title":\s*"((?:[^"\\]|\\.){3,120})"')
GO_JOB_RE = re.compile(r'"id"\s*:\s*"(jobs/[\w\[\]-]{5,40})"\s*,\s*'
                       r'"title"\s*:\s*"((?:[^"\\]|\\.){3,120})"')
LN_JOB_RE = re.compile(
    r'<a[^>]*href="((?:https://www\.linkedin\.com)?/jobs/view/[^"]+)"[^>]*>.*?<h3[^>]*>(.*?)</h3>.*?<h4[^>]*>(.*?)</h4>',
    re.S)

def indeed_jsonld(page):
    out = []
    for m in JSON_LD_RE.finditer(page):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        for it in (data if isinstance(data, list) else [data]):
            if not (isinstance(it, dict) and it.get("@type") == "JobPosting"):
                continue
            loc = ""
            la = it.get("jobLocation") or {}
            if isinstance(la, dict):
                ad = la.get("address") or {}
                if isinstance(ad, dict):
                    loc = ", ".join(x for x in (ad.get("addressLocality"),
                                    ad.get("addressRegion"), ad.get("addressCountry")) if x)
            org = it.get("hiringOrganization") or {}
            out.append({
                "id": "indeed:" + re.sub(r"\W", "-",
                      f"{it.get('title','?')}@{org.get('name','?')}")[:80].lower(),
                "title": clean(it.get("title") or "?"),
                "company": clean(org.get("name") or "?"),
                "location": loc or "See posting",
                "url": it.get("url") or it.get("sameAs") or "https://www.indeed.com",
                "desc": clean(re.sub(r"<[^>]+>", " ", it.get("description") or ""))[:4000],
                "date": (it.get("datePosted") or "")[:10],
            })
    return out

# ------------------------------------------------------------ source feeds
def J(src, jid, title, company, loc, url, desc, date=""):
    return {"id": f"{src}:{jid}", "title": clean(str(title)), "company": clean(str(company)),
            "location": clean(str(loc)) or "Not specified", "url": url,
            "desc": clean(str(desc))[:4000], "date": (date or "")[:10], "source": src}

def adp_remotive(p):
    kw = urllib.parse.quote(p["_keyword"])
    d = get_json(f"https://remotive.com/api/remote-jobs?limit=60&category=software-dev&search={kw}")
    return [J("remotive", j.get("id"), j.get("title"), j.get("company_name"), "Remote",
              j.get("url") or f"https://remotive.com/remote-jobs/software-dev/{j.get('slug','')}",
              f"{j.get('description','')} tags: {','.join(j.get('tags') or [])}",
              (j.get("publication_date") or ""))
            for j in d.get("jobs", []) if j.get("title")]

def adp_remoteok(p):
    d = get_json("https://remoteok.com/api")
    out = []
    for j in d:
        if not isinstance(j, dict) or not j.get("position"):
            continue
        out.append(J("remoteok", j.get("id") or j.get("slug"), j.get("position"),
                     j.get("company"), j.get("location") or "Remote",
                     j.get("url") or f"https://remoteok.com/jobs/{j.get('slug','')}",
                     j.get("description", ""), str(j.get("date", ""))))
    return out

def adp_jobicy(p):
    d = get_json("https://jobicy.com/api/v2/remote-jobs?count=50&industry=dev")
    return [J("jobicy", j.get("id"), j.get("jobTitle"), j.get("companyName"),
              j.get("jobGeo") or "Remote",
              j.get("url") or f"https://jobicy.com/jobs/{j.get('id')}",
              f"{j.get('jobExcerpt','')} level: {j.get('jobLevel','')}",
              (j.get("pubDate") or ""))
            for j in d.get("jobs", []) if j.get("jobTitle")]

def adp_arbeitnow(p):
    d = get_json("https://www.arbeitnow.com/api/job-board-api")
    return [J("arbeitnow", j.get("id"), j.get("title"), j.get("company_name"),
              (j.get("location") or "") + (" [Remote]" if j.get("remote") else ""),
              j.get("url") or f"https://www.arbeitnow.com/jobs/{j.get('slug','')}",
              j.get("description", ""), str(j.get("created_at", "")))
            for j in d.get("data", []) if j.get("title")]

def adp_netflix(p):
    d = get_json("https://explore.jobs.netflix.net/api/apply/v2/jobs?domain=netflix.com"
                 "&query=intern&num=30&sort_by=new")
    out = []
    for j in d.get("positions", []):
        locs = ",".join(str(x.get("value", "")) for x in (j.get("metadata") or [])
                        if isinstance(x, dict))
        out.append(J("netflix", j.get("canonicalPositionUuid") or j.get("id"),
                     j.get("name"), "Netflix", (str(j.get("location") or "") + " " + locs)[:140],
                     f"https://explore.jobs.netflix.net/careers/job?domain=netflix.com&pid={j.get('id','')}",
                     str(j.get("description", "")), (j.get("createdDate") or "")))
    return out

def adp_amazon(p):
    d = get_json("https://www.amazon.jobs/en/search.json?base_query=internship"
                 "&result_limit=40&country=IND")
    out = []
    for j in d.get("jobs", []):
        if not j.get("is_intern") and "intern" not in (j.get("title") or "").lower() \
           and "internship" not in (j.get("job_category") or "").lower():
            continue
        posted = j.get("posted_date") or ""
        try:
            posted = time.strftime("%Y-%m-%d", time.strptime(posted.strip(), "%b %d, %Y"))
        except ValueError:
            posted = ""
        out.append(J("amazon", j.get("id") or j.get("id_icims"), j.get("title"),
                     j.get("company_name") or "Amazon",
                     j.get("normalized_location") or j.get("location") or "",
                     "https://www.amazon.jobs" + (j.get("job_path") or ""),
                     f"{j.get('description','')} {j.get('basic_qualifications','')} "
                     f"{j.get('preferred_qualifications','')}", posted))
    return out

def adp_smartrecruiters(p):
    out = []
    for company in p["_sr_companies"]:
        try:
            d = get_json(f"https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=30")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
            log(f"smartrecruiters:{company} unreachable")
            continue
        for j in d.get("content", []):
            name = j.get("name") or ""
            if not is_target_title(name):
                continue
            lo = j.get("location") or {}
            out.append(J("smartrecruiters", j.get("id"), name,
                         (j.get("company") or {}).get("name", company),
                         lo.get("remote") or "/".join(x for x in (lo.get("city"), lo.get("region")) if x),
                         f"https://jobs.smartrecruiters.com/{company}/{j.get('id','')}",
                         f"req: {j.get('requiredQualifications','')} pref: {j.get('preferredQualifications','')}",
                         (j.get("releasedDate") or "")))
    return out

def adp_greenhouse(p):
    out = []
    for company in p["_gh_companies"]:
        try:
            d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
            log(f"greenhouse:{company} unreachable")
            continue
        for j in d.get("jobs", []):
            title = j.get("title", "")
            if not is_target_title(title):
                continue
            loc = ""
            offices = j.get("offices") or []
            if offices:
                loc = ", ".join(o.get("location", "") for o in offices[:2])
            out.append(J("greenhouse", j.get("id"), title, company_map(company), loc,
                         j.get("absolute_url", ""), "", (j.get("updated_at") or "")))
    return out

def adp_lever(p):
    out = []
    for company in p["_lever_companies"]:
        try:
            d = get_json(f"https://api.lever.co/v0/postings/{company}?mode=json")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
            log(f"lever:{company} unreachable")
            continue
        for j in d:
            if not is_target_title(j.get("text", "")):
                continue
            created = j.get("createdAt")
            out.append(J("lever", j.get("id"), j.get("text"), company_map(company),
                         ((j.get("categories") or {}).get("location") or "") + " " +
                         str(j.get("workplaceType") or ""),
                         j.get("hostedUrl") or j.get("applyUrl") or "",
                         clean(f"{j.get('description','')} "
                               f"{json.dumps(j.get('lists') or [])}"),
                         time.strftime("%Y-%m-%d", time.gmtime(created / 1000)) if created else ""))
    return out

def adp_ashby(p):
    out = []
    for company in p["_ashby_companies"]:
        try:
            d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{company}")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
            log(f"ashby:{company} unreachable")
            continue
        for j in d.get("jobs", []):
            if not is_target_title(j.get("title", "")):
                continue
            out.append(J("ashby", j.get("id"), j.get("title"), company_map(company),
                         f"{j.get('location','')} {j.get('workplaceType','')}",
                         j.get("jobUrl") or j.get("applyUrl") or f"https://jobs.ashbyhq.com/{company}",
                         f"{j.get('descriptionHtml','')} {j.get('descriptionPlain','')}",
                         (j.get("publishedAt") or "")))
    return out

def adp_wellfound(p):
    page = http("https://wellfound.com/jobs", headers={"Accept": "text/html"}).decode("utf-8", "replace")
    titles = [strip_bs(t) for t in WF_TITLE_RE.findall(page)]
    out, used = [], set()
    for m in WF_JOB_LINK.finditer(page):
        jid = m.group(2)
        if jid in used:
            continue
        slug = m.group(3)
        best = ""
        # match a nearby title from the embedded payload (heuristic: longest title
        # whose slugified head matches the link slug)
        slug_head = slug.split("-at-")[0]
        for t in titles:
            if slug_head[:28] and re.sub(r"[^a-z0-9]+", "-", t.lower()).startswith(slug_head[:28]):
                best = t
                break
        if not best:
            parts = slug.split("-at-")
            best = (parts[0].replace("-", " ") + " at " +
                    (parts[1].replace("-", " ") if len(parts) > 1 else "startup")).title()
        used.add(jid)
        out.append(J("wellfound", jid, best, "Wellfound startup", "Wellfound (varies)",
                     "https://wellfound.com" + m.group(1), "", ""))
    return out

def adp_indeed(p):
    loc = p.get("_indeed_location", "India")
    page = http(f"https://www.indeed.com/jobs?q={urllib.parse.quote(p['_keyword'])}"
                f"&l={urllib.parse.quote(loc)}&fromage=3",
                headers={"Accept": "text/html"}).decode("utf-8", "replace")
    jobs = indeed_jsonld(page)
    if jobs:
        return jobs
    titles = [clean(t) for t in JD_TITLE_RE.findall(page)]
    seen, out = set(), []
    for i, jk in enumerate(JD_LINK_RE.findall(page)):
        if jk in seen:
            continue
        seen.add(jk)
        out.append(J("indeed", jk, titles[i] if i < len(titles) else "Internship",
                     "Indeed employer", loc, f"https://www.indeed.com/viewjob?jk={jk}", ""))
    return out

def adp_linkedin(p):
    """LinkedIn guest jobs HTML — job cards carry data-entity-urn ids."""
    kw = urllib.parse.quote("internship software AI machine learning")
    loc = urllib.parse.quote(p.get("_indeed_location", "India"))
    page = http("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
                f"?keywords={kw}&location={loc}&f_TPR=r604800&start=0",
                headers={"Accept": "text/html"}).decode("utf-8", "replace")
    ids = re.findall(r'data-entity-urn="urn:li:jobPosting:(\d+)"', page)
    titles = [clean(t) for t in
              re.findall(r'base-search-card__title[^>]*>\s*([^<]{3,120})', page)]
    companies = [clean(c) for c in
                 re.findall(r'base-search-card__subtitle[^>]*>\s*(?:<a[^>]*>)?\s*([^<]{2,80})', page)]
    out, seen = [], set()
    for i, jid in enumerate(ids):
        if jid in seen:
            continue
        seen.add(jid)
        out.append(J("linkedin", jid,
                     titles[i] if i < len(titles) else "Internship",
                     companies[i] if i < len(companies) else "LinkedIn employer",
                     "LinkedIn", f"https://www.linkedin.com/jobs/view/{jid}", ""))
    return out

def adp_google(p):
    """Google careers has no public JSON API; extract internship job ids from
    the HTML app payload ('jobPost:uid:...' style keys)."""
    page = http("https://careers.google.com/jobs/results/?target_level=INTERN&distance=50",
                headers={"Accept": "text/html"}).decode("utf-8", "replace")
    out, seen = [], set()
    for m in re.finditer(r'"(jobPost:uid:[^"]{6,40})"', page):
        uid = m.group(1)
        jid = uid.split(":")[-1]
        if jid in seen:
            continue
        seen.add(jid)
        out.append(J("google", jid, "Google Internship (see posting)", "Google",
                     "Google offices",
                     f"https://careers.google.com/jobs/results/{urllib.parse.quote(jid)}",
                     "", ""))
    return out

SOURCES = [
    ("remotive", adp_remotive), ("remoteok", adp_remoteok), ("jobicy", adp_jobicy),
    ("arbeitnow", adp_arbeitnow), ("netflix", adp_netflix), ("amazon", adp_amazon),
    ("smartrecruiters", adp_smartrecruiters), ("greenhouse", adp_greenhouse),
    ("lever", adp_lever), ("ashby", adp_ashby), ("wellfound", adp_wellfound),
    ("indeed", adp_indeed), ("linkedin", adp_linkedin),
]

# ---------------------------------------------------------------- scoring
SENIOR_BLOCK = ("senior", "sr.", "sr ", "principal", "staff", "manager", "director",
                "lead engineer", "head of", "architect", "vice president", "vp ",
                "distinguished", "ii)", "iii", " iv", "iv)")
ENTRY_KEYS = ("intern", "internship", "graduate", "fresher", "campus", "new grad",
              "early career", "university grad", "university graduate", "trainee",
              "co-op", "coop", "apprentice", "junior", "entry level", "entry-level",
              "sde", "engineer i", "analyst i", "developer i", "research fellow",
              "software engineer, ", "member of technical staff")
FT_TITLE_KEYS = ("graduate", "campus", "new grad", "university", "fresher",
                 "early career", "2027", "2026", "trainee", "entry level", "entry-level")

def is_target_title(title):
    t = (title or "").lower()
    if not t:
        return False
    if any(b in t for b in SENIOR_BLOCK):
        return False
    if "intern" in t or "internship" in t:
        return True
    return any(k in t for k in ENTRY_KEYS)

def word_hit(skill, text):
    return re.search(r"(?<![a-z0-9])" + re.escape(skill.lower()) + r"(?![a-z0-9])", text)

def score_job(j, prof, cfg):
    title = j["title"].lower()
    body = f"{j['title']} {j['desc']} {j['location']}".lower()
    score = 45.0
    reasons = []
    missing = []
    # full-time detection (early-career only; plain senior FT filtered by title)
    is_fulltime = False
    if any(k in title for k in ("full-time", "full time", "fulltime")) and \
       "intern" not in title and any(k in title for k in FT_TITLE_KEYS):
        is_fulltime = True

    # 1. role relevance
    role_hits = [r for r in prof["want_roles"]
                 if all(w in title or w in body[:120] for w in re.split(r"[\s/]+", r.lower()))]
    ai_role = any(k in title for k in ("ai", "machine learning", " ml", "ml ", "data scien",
                                       "deep learning", "nlp", "computer vision", "genai", "llm"))
    if ai_role:
        score += 18
        reasons.append("AI/ML/data role")
    elif role_hits:
        score += 12
        reasons.append(f"target role: {role_hits[0]}")
    else:
        if "engineer" not in title and "developer" not in title and "analyst" not in title \
           and "scientist" not in title and "sde" not in title:
            return 0, [], [], False
        score += 6
        reasons.append("software/engineering role")
    # 2. intern/entry
    if any(k in title for k in ("intern", "internship", "trainee", "co-op", "apprentice")):
        score += 10
        reasons.append("internship position")
    elif any(k in title for k in ("fresher", "graduate", "campus", "new grad", "university",
                                  "early career")) or is_fulltime:
        score += 5
        reasons.append("entry-level / early-career role")
    # 3. skills
    have_hits = [s for s in prof["have_skills"] if word_hit(s, body)]
    missing = [s for s in prof["missing_skills"] if word_hit(s, body)]
    cov = len(have_hits) / max(1, len(prof["have_skills"]))
    score += min(20, round(40 * cov))
    if have_hits:
        reasons.append(f"skills: {', '.join(have_hits[:5])}")
    # 4. remote / flexible
    if "remote" in body or "work from home" in body or "wfh" in body:
        score += 8
        reasons.append("remote friendly")
    elif "hybrid" in body:
        score += 3
        reasons.append("hybrid arrangement")
    # 5. duration / semester compatibility
    if re.search(r"(?:three|four|five|six|[2-6])\s*(?:to\s*[0-9]\s*)?(?:-|to)?\s*(?:months|month)", body) \
       or re.search(r"\b[3-6]\s*month", body):
        score += 8
        reasons.append("3-6 month duration")
    if re.search(r"(summer|winter|spring|fall|autumn)\s*(intern|internship|co.?op|2026|2027)", body):
        score += 6
        reasons.append("seasonal internship")
    if re.search(r"\bflexible\b|\bflex hours\b|own pace|self-paced", body):
        score += 2
    # 6. paid
    if re.search(r"\bstipend\b|paid internship|\$\d|₹\d|usd \d|per month|lpa", body):
        score += 4
        reasons.append("paid / stipend mentioned")
    # 7. location relevance
    if re.search(r"\bindia\b|bengaluru|bangalore|hyderabad|pune|mumbai|delhi|gurgaon|noida|chennai|chennai|remote", body):
        score += 4
    # 8. major company
    major = any(c.lower() in j["company"].lower() or c.lower() == j["source"]
                for c in prof["major_companies"])
    if major:
        score += 6
        reasons.append("major company")
    # 9. freshness
    if j.get("date"):
        try:
            import datetime as dt
            age = (dt.datetime.now(dt.timezone.utc).date() - dt.date.fromisoformat(j["date"][:10])).days
            if age <= 2:
                score += 5
                reasons.append("posted in last 2 days")
            elif age <= 7:
                score += 3
            elif age > 45:
                score -= 10
        except ValueError:
            pass
    score = max(0, min(100, round(score)))
    return score, reasons, missing, is_fulltime

# --------------------------------------------------------------- delivery
def esc(s):
    return html_mod.escape(s or "")

def tg_send(cfg, text_html):
    token, chat = cfg.get("telegram_bot_token"), cfg.get("telegram_chat_id")
    if not token or not chat:
        return False
    ok = True
    for i in range(0, len(text_html), 3800):
        payload = json.dumps({"chat_id": chat, "text": text_html[i:i + 3800],
                              "parse_mode": "HTML",
                              "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
                resp = json.loads(r.read().decode())
                if not resp.get("ok"):
                    log(f"telegram: API said not ok: {str(resp)[:200]}")
                    ok = False
        except Exception as e:
            log(f"telegram send failed: {str(e)[:200]}")
            ok = False
    return ok

def mail_send(cfg, subject, html_body, text_body):
    user, pwd = cfg.get("gmail_user"), cfg.get("gmail_app_password")
    to = cfg.get("notify_email") or user
    if not user or not pwd or not to:
        log("email not sent: missing gmail_user / gmail_app_password / notify_email")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    if cfg.get("cc_emails"):
        msg["Cc"] = ", ".join(cfg["cc_emails"])
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    recipients = [to] + list(cfg.get("cc_emails") or [])
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=CTX, timeout=30) as s:
            s.login(user, pwd)
            s.sendmail(user, recipients, msg.as_string())
        log(f"EMAIL SENT -> {to} :: {subject}")
        return True
    except Exception as e:
        log(f"EMAIL FAILED: {str(e)[:250]}")
        return False

def deliver(cfg, subject, html_body, text_body):
    mode = cfg.get("delivery", "auto")
    log(f"delivery mode={mode} subject={subject[:60]}")
    if mode in ("email", "auto"):
        if mail_send(cfg, subject, html_body, text_body):
            return "email"
        if mode == "auto":
            log("email failed -> falling back to Telegram")
        else:
            return None
    if mode in ("telegram", "auto"):
        if tg_send(cfg, "<b>" + esc(subject) + "</b><br><br>" + html_body):
            return "telegram"
    return None

# ------------------------------------------------------------- formatting
def alert_email(j, sc, reasons, missing, gem):
    tier = GEM if gem else FIRE
    miss_html = ""
    if missing:
        miss_html = ("<h3 style='margin:14px 0 4px'>Watch out for (missing skills)</h3><ul>" +
                     "".join(f"<li>⚠️ {esc(m)}</li>" for m in missing[:6]) + "</ul>")
    why = "".join(f"<li>+ {esc(r)}</li>" for r in reasons)
    return f"""<div style="font-family:Segoe UI,Arial,sans-serif;max-width:640px;line-height:1.5">
  <h2 style="margin:0 0 2px">{tier} Match score {sc}%</h2>
  <p style="font-size:18px;margin:2px 0 12px"><b>{esc(j['title'])}</b> — <b>{esc(j['company'])}</b></p>
  <table style="font-size:14px;color:#333">
    <tr><td style="padding:2px 10px 2px 0"><b>Link</b></td>
        <td><a href="{esc(j['url'])}">{esc(j['url'][:90])}</a></td></tr>
    <tr><td style="padding:2px 10px 2px 0"><b>Source</b></td><td>{esc(j['source'])}</td></tr>
    <tr><td style="padding:2px 10px 2px 0"><b>Location</b></td><td>{esc(j['location'])}</td></tr>
    <tr><td style="padding:2px 10px 2px 0"><b>Posted</b></td><td>{esc(j.get('date') or 'recent')}</td></tr>
  </table>
  <h3 style="margin:14px 0 4px">Why it matches</h3><ul style="font-size:14px">{why}</ul>
  {miss_html}
  <p style="font-size:12px;color:#888">You get this because the score is 85+ (immediate-alert tier).
  Major companies often interview early — apply soon.</p>
</div>"""

def alert_text(j, sc, reasons, missing, gem):
    tier = GEM if gem else FIRE
    lines = [f"{tier} [MATCH SCORE {sc}%] — {j['title']} at {j['company']}", "",
             f"Link:     {j['url']}",
             f"Source:   {j['source']}   Location: {j['location']}",
             f"Posted:   {j.get('date') or 'recent'}", "", "WHY IT MATCHES:"]
    lines += [f"  + {r}" for r in reasons]
    if missing:
        lines += ["", "MISSING SKILLS:"] + [f"  - {m}" for m in missing[:6]]
    return "\n".join(lines)

def alert_tg(j, sc, reasons, missing, gem):
    tier = GEM if gem else FIRE
    s = f"<b>{tier} [{sc}%] {esc(j['title'])}</b>\n<b>{esc(j['company'])}</b>\n\n" \
        f"<a href=\"{esc(j['url'])}\">Open posting</a>\n" \
        f"<i>{esc(j['source'])} · {esc(j['location'])}</i>\n\n" \
        + "\n".join(f"+ {esc(r)}" for r in reasons)
    if missing:
        s += "\n\n" + "\n".join(f"⚠️ missing: {esc(m)}" for m in missing[:5])
    return s

def digest_email(items):
    out = []
    for sc, j, reasons, missing in items:
        color = "#e6a100" if sc < 70 else "#1a7f37"
        tier = STRT if sc < 70 else GOOD
        mis = (f"<div style='color:#a33;font-size:12px;margin-top:3px'>Missing: "
               f"{esc(', '.join(missing[:5]))}</div>") if missing else ""
        out.append(f"""<div style="border:1px solid #ddd;border-radius:8px;padding:10px 14px;margin:10px 0">
  <div style="font-size:15px"><span style="color:{color};font-weight:700">{tier} [{sc}%]</span>
  <b>{esc(j['title'])}</b> — {esc(j['company'])}</div>
  <div style="font-size:13px;color:#555">{esc(j['location'])} · {esc(j['source'])}</div>
  <div style="font-size:13px;margin-top:4px">{esc(' · '.join(reasons[:4]))}{mis}</div>
  <div style="margin-top:6px"><a href="{esc(j['url'])}" style="font-size:13px">View posting →</a></div>
</div>""")
    return "".join(out) + ("<p style='font-size:12px;color:#888'>🟢 strong match (70-84) · "
                           "🟡 stretch match (60-69 — missing skills shown)</p>")

def digest_text(items):
    blocks = []
    for sc, j, reasons, missing in items:
        tier = STRT if sc < 70 else GOOD
        b = f"{tier} [{sc}%] {j['title']} — {j['company']}\n   {j['location']} ({j['source']})\n   {j['url']}\n   + " + "; ".join(reasons[:4])
        if missing:
            b += "\n   Missing: " + ", ".join(missing[:5])
        blocks.append(b)
    return "\n\n".join(blocks)

def digest_tg(items):
    blocks = []
    for sc, j, reasons, missing in items:
        tier = STRT if sc < 70 else GOOD
        b = (f"{tier} <b>[{sc}%] {esc(j['title'])}</b> — {esc(j['company'])}\n"
             f"<a href=\"{esc(j['url'])}\">open</a> · {esc(j['location'])}\n"
             + esc(" · ".join(reasons[:3])))
        if missing:
            b += "\n⚠️ missing: " + esc(", ".join(missing[:4]))
        blocks.append(b)
    return "\n\n".join(blocks)

# ------------------------------------------------------------------- main
def main():
    prof = load_json(BASE / "profile.json", {})
    cfg = load_json(BASE / "email.json", {}) or {}
    for k, v in [("immediate_min_score", 85), ("digest_min_score", 70),
                 ("stretch_min_score", 60), ("delivery", "auto")]:
        cfg.setdefault(k, v)
    dry = "--no-send" in sys.argv
    force_digest = "--digest" in sys.argv
    # helper: python scout.py --telegram-id  (print chat ids after you message the bot)
    if "--telegram-id" in sys.argv:
        token = cfg.get("telegram_bot_token") or input("Paste bot token: ").strip()
        try:
            d = get_json(f"https://api.telegram.org/bot{token}/getUpdates")
            chats = {u.get("message", {}).get("chat", {}).get("id")
                     for u in d.get("result", []) if u.get("message")}
            chats.discard(None)
            if chats:
                print("Your chat_id(s):", ", ".join(str(c) for c in chats))
            else:
                print("No messages found — open your bot in Telegram, press Start, then retry.")
        except Exception as e:
            print("Failed:", e)
        return 0
    if not prof:
        log("FATAL: profile.json missing or invalid")
        return 1
    # environment overrides (used by GitHub Actions secrets; email.json is optional
    # because on Actions credentials come exclusively from secrets)
    import os
    for env_key, cfg_key in [("GMAIL_USER", "gmail_user"),
                             ("GMAIL_APP_PASSWORD", "gmail_app_password"),
                             ("NOTIFY_EMAIL", "notify_email"),
                             ("TELEGRAM_BOT_TOKEN", "telegram_bot_token"),
                             ("TELEGRAM_CHAT_ID", "telegram_chat_id"),
                             ("DELIVERY", "delivery")]:
        if os.environ.get(env_key):
            cfg[cfg_key] = os.environ[env_key]

    prof["_keyword"] = "internship"
    prof["_indeed_location"] = "India"
    prof["_sr_companies"] = ["Visa", "bakerhughes", "Bosch", "Workday", "Stryker",
                             "JuniperNetworks", "adp", "SonyInteractiveEntertainmentGlobal"]
    prof["_gh_companies"] = ["stripe", "databricks", "figma", "cloudflare", "mongodb",
                             "twilio", "robinhood", "reddit", "dropbox", "coinbase",
                             "doordash", "flexport", "benchling", "nuro", "glean"]
    prof["_lever_companies"] = ["spotify", "leverdemo", "netflixtech", "kraken", "smartnews",
                                "esteelauder", "zeuslegion", " Pluto".strip()]
    prof["_ashby_companies"] = ["elevenlabs", "Perplexity", "Cohere", "Runway",
                                "huggingface", "baseten", "modal-labs", "groq",
                                "deepmind", "mistral", "togetherai"]
    SLUGS = {}
    for c in prof["_sr_companies"] + prof["_gh_companies"] + prof["_lever_companies"] + prof["_ashby_companies"]:
        SLUGS[c.lower()] = c.replace("-", " ").title()
    SLUGS.update({"openai": "OpenAI", "mistral": "Mistral AI", "groq": "Groq",
                  "huggingface": "Hugging Face", "deepmind": "DeepMind",
                  "perplexity": "Perplexity", "elevenlabs": "ElevenLabs",
                  "runway": "Runway", "cohere": "Cohere", "togetherai": "Together AI",
                  "modal-labs": "Modal", "baseten": "Baseten", "deepl": "DeepL"})
    globals()["_COMPANY_SLUGS"] = SLUGS

    seen = load_json(SEEN_FILE, {})
    src_err = load_json(ERR_FILE, {})
    all_jobs = []
    for name, fn in SOURCES:
        if src_err.get(name, {}).get("consecutive", 0) >= 8:
            log(f"skip {name}: disabled after 8 consecutive failures")
            continue
        try:
            t0 = time.time()
            jobs = fn(prof) or []
            all_jobs.extend(jobs)
            if name in src_err:
                src_err.pop(name, None)
            log(f"{name}: {len(jobs)} jobs ({time.time()-t0:.1f}s)")
        except Exception as e:
            rec = src_err.setdefault(name, {"consecutive": 0})
            rec["consecutive"] = rec.get("consecutive", 0) + 1
            rec["last_error"] = str(e)[:250]
            log(f"{name} FAILED x{rec['consecutive']}: {str(e)[:160]}")
        time.sleep(1.2)
    save_json(ERR_FILE, src_err)

    # de-dup + filter + score
    seen_ids, scored = set(), []
    now_iso = time.strftime("%Y-%m-%d %H:%M")
    cutoff = prof.get("fulltime_available_from", "2026-12-15")
    for j in all_jobs:
        if not j.get("title") or not j.get("url") or j["id"] in seen_ids:
            continue
        seen_ids.add(j["id"])
        if not is_target_title(j["title"]):
            continue
        sc, reasons, missing, is_ft = score_job(j, prof, cfg)
        if sc <= 0:
            continue
        j["_ft"] = is_ft
        scored.append((sc, j, reasons, missing))
    scored.sort(key=lambda x: -x[0])
    log(f"scored {len(scored)} relevant of {len(all_jobs)} raw")

    fire, digest, stretch = [], [], []
    for sc, j, reasons, missing in scored:
        if j["_ft"]:
            # early-career full-time: only future-joining cohorts pass.
            # Heuristic: allow if a year 2027+ is in the title, or score is very high
            # and no past-date joining signal present; joining-date text is checked
            # in the description when available.
            if not (re.search(r"\b202[7-9]\b", j["title"]) or
                    re.search(r"\b202[7-9]\b", j["desc"][:600])):
                # no explicit future year: allow only if "winter 2026"/"dec" present
                if not re.search(r"(december|dec|winter)\s*2026", j["desc"][:600] + j["title"].lower()):
                    continue
            reasons.append("future cohort / graduate program (fits your Dec 2026 rule)")
        if sc >= cfg["immediate_min_score"]:
            fire.append((sc, j, reasons, missing))
        elif sc >= cfg["digest_min_score"]:
            digest.append((sc, j, reasons, missing))
        elif sc >= cfg["stretch_min_score"]:
            stretch.append((sc, j, reasons, missing))
        else:
            major = any(c.lower() in j["company"].lower() or c.lower() == j["source"]
                        for c in prof["major_companies"])
            preferred = {"aws", "docker", "kubernetes", "spark", "kafka", "golang",
                         "rust", "scala", "langchain", "mlops", "airflow"}
            if major and sc >= 50 and missing and set(m.lower() for m in missing) <= preferred:
                fire.append((sc, j, reasons + ["exception: major company, gaps are only "
                            "in preferred (not mandatory) tech"], missing))

    fire = fire[:6]
    digest_items = digest[:15]
    stretch_items = stretch[:15]
    to_alert = [x for x in fire if x[1]["id"] not in seen]
    to_digest = [x for x in (digest_items + stretch_items) if x[1]["id"] not in seen]

    log(f"to-alert={len(to_alert)} to-digest={len(to_digest)}")

    if dry:
        print("\n===== DRY RUN — what would be delivered =====")
        for sc, j, r, m in to_alert:
            print(alert_text(j, sc, r, m, sc < 60))
            print("-" * 70)
        if to_digest:
            print("DIGEST:")
            print(digest_text(to_digest[:20]))
        if not (to_alert or to_digest):
            print("(nothing new)")
        return 0

    sent = []
    for sc, j, r, m in to_alert:
        gem = sc < 60
        subject = f"{GEM if gem else FIRE} [{sc}%] Match — {j['title']} at {j['company']}"
        via = deliver(cfg, subject, alert_email(j, sc, r, m, gem),
                      alert_text(j, sc, r, m, gem))
        if via:
            sent.append(via)
            seen[j["id"]] = now_iso
        time.sleep(1.5)

    if to_digest and (force_digest or not to_alert or sent):
        subject = f"🟢 Internship Digest — {len(to_digest[:20])} new matches ({now_iso})"
        via = deliver(cfg, subject, digest_email(to_digest[:20]),
                      digest_text(to_digest[:20]))
        if via:
            for sc, j, r, m in to_digest:
                seen[j["id"]] = now_iso

    if len(seen) > 5000:
        for k in sorted(seen, key=seen.get)[:1500]:
            seen.pop(k)
    save_json(SEEN_FILE, seen)
    log(f"done: seen={len(seen)} alerts={len(to_alert)} digest={len(to_digest)}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
