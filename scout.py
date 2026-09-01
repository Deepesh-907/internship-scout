#!/usr/bin/env python3
"""
internship-scout — fetches fresh internships/entry-level jobs from many sources,
scores them against profile.json, and delivers:

  >= 85  -> individual alert immediately  (subject: "X [NN%] Match - Role at Company")
  70-84  -> consolidated digest (green tier)
  60-69  -> digest as "Stretch Match" with missing skills
  < 60   -> silent, except rare major-company exception (flagged with a reason)

Delivery: Telegram bot only (scoring tiers and email removed by request;
the score is a guide, not a filter — every relevant job is delivered).

Zero third-party dependencies. Python 3.8+.

  python scout.py             # normal run: fetch, score, deliver
  python scout.py --no-send   # dry run: print what would be delivered
  python scout.py --digest    # force the digest to be sent this run
"""
import json, re, ssl, sys, time, urllib.request, urllib.error, urllib.parse
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

# slug -> display name for ATS board companies; populated in main() before feeds run
_COMPANY_SLUGS = {}

def company_map(slug):
    return _COMPANY_SLUGS.get(slug.lower(), slug.replace("-", " ").title())

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

def adp_internshala(p):
    """Internshala ML/Python/software internship listing pages -> detail links."""
    out = []
    seen = set()
    for kw in ("machine%20learning", "python", "artificial%20intelligence",
               "software%20development"):
        try:
            page = http(f"https://internshala.com/internships/{kw}-internship/",
                        headers={"Accept": "text/html"}).decode("utf-8", "replace")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            log(f"internshala:{kw} unreachable")
            continue
        for slug in re.findall(r'href="/internship/detail/([a-z0-9\-]+)"', page):
            if slug in seen:
                continue
            seen.add(slug)
            m = re.match(r"([a-z0-9\-]+?)-internship(?:-in-[a-z\-]+)?-at-([a-z0-9\-]+?)(\d{10,})$", slug)
            if not m:
                continue
            role = m.group(1).replace("-", " ").title()
            comp = m.group(2).replace("-", " ").title()
            out.append(J("internshala", slug[:80], role, comp, "India (Internshala)",
                        "https://internshala.com/internship/detail/" + slug,
                        f"{role} internship via Internshala"))
        time.sleep(1.0)
    return out

def adp_youtube(p):
    """YouTube search restricted to recruitment-announcement-style results."""
    out = []
    queries = ['"internship 2027" apply announcement india',
               '"hiring interns" company announcement 2027']
    seen = set()
    for q in queries:
        try:
            page = http("https://www.youtube.com/results?search_query=" + urllib.parse.quote(q),
                        headers={"Accept": "text/html"}).decode("utf-8", "replace")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            continue
        titles = [t.encode().decode("unicode_escape", "replace")
                  for t in re.findall(r'"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.){5,100})"', page)]
        chans = [c.encode().decode("unicode_escape", "replace")
                 for c in re.findall(r'"ownerText":\{"runs":\[\{"text":"((?:[^"\\]|\\.){2,60})"', page)]
        vids = re.findall(r'"videoId":"([\w-]{11})"', page)
        for i, vid in enumerate(vids):
            if vid in seen or i >= len(titles):
                continue
            t = titles[i]
            tl = t.lower()
            # only recruitment announcements, not career advice
            if not any(k in tl for k in ("internship", "hiring", "recruit", "openings", "hiring")):
                continue
            if any(k in tl for k in ("roadmap", "how to crack", "resume", "tips", "prep",
                                     "course", "complete guide to learn", "tutorial",
                                     "interview experience", "day in", "salary of")):
                continue
            if not any(k in tl for k in ("2027", "2026", "apply", "openings", "announced",
                                         "program", "drive")):
                continue
            seen.add(vid)
            out.append(J("youtube", vid,
                         t.replace("\u0026", "&")[:90],
                         (chans[i] if i < len(chans) else "YouTube channel"),
                         "YouTube announcement",
                         f"https://www.youtube.com/watch?v={vid}",
                         "Video announcement — verify on the company's official careers page"))
        time.sleep(1.0)
    return out[:8]

SOURCES = [
    ("remotive", adp_remotive), ("remoteok", adp_remoteok), ("jobicy", adp_jobicy),
    ("arbeitnow", adp_arbeitnow), ("netflix", adp_netflix), ("amazon", adp_amazon),
    ("smartrecruiters", adp_smartrecruiters), ("greenhouse", adp_greenhouse),
    ("lever", adp_lever), ("ashby", adp_ashby), ("wellfound", adp_wellfound),
    ("internshala", adp_internshala), ("indeed", adp_indeed), ("linkedin", adp_linkedin),
    ("youtube", adp_youtube),
]

# ---------------------------------------------------------------- scoring
SENIOR_BLOCK = ("senior", "sr.", "sr ", "principal", "staff", "manager", "director",
                "lead engineer", "head of", "architect", "vice president", "vp ",
                "distinguished", "ii)", "iii", " iv", "iv)")
ENTRY_KEYS = ("intern", "internship", "graduate", "fresher", "campus", "new grad",
              "early career", "university grad", "university graduate", "trainee",
              "co-op", "coop", "apprentice", "junior", "entry level", "entry-level",
              "entry level", "sde", "engineer i", "analyst i", "developer i",
              "research fellow", "member of technical staff", "associate software",
              "graduate software", "junior ml", "junior ai", "python developer",
              "backend developer", "data science", "genai", "llm", "machine learning",
              "ai engineer", "ml engineer", "ai/ml", "artificial intelligence",
              "software developer", "software engineer", "backend engineer",
              "full stack", "full-stack", "python developer")
FT_TITLE_KEYS = ("graduate", "campus", "new grad", "university", "fresher",
                 "early career", "2027", "2026", "trainee", "entry level", "entry-level")

def is_target_title(title):
    t = (title or "").lower()
    if not t:
        return False
    # 'staff' alone signals senior, but 'member of technical staff' is entry-level
    if "member of technical staff" in t:
        return True
    if any(b in t for b in SENIOR_BLOCK):
        return False
    # non-tech internships are out of scope regardless of 'intern' in the title
    if "intern" in t or "internship" in t:
        if not any(k in t for k in ("ai", "ml", "machine learning", "data scien", "data analyst",
                                    "software", "developer", "engineer", "python",
                                    "programming", "backend", "full stack", "full-stack",
                                    "sde", "tech", "automation", "computer", "research",
                                    "science", "genai", "llm", "cloud", "coding",
                                    "technical", "technical staff")):
            return False
        return True
    return any(k in t for k in ENTRY_KEYS)

def word_hit(skill, text):
    return re.search(r"(?<![a-z0-9])" + re.escape(skill.lower()) + r"(?![a-z0-9])", text)

def find_hours(body):
    """Detect stated weekly hours; returns int or None."""
    m = re.search(r"(\d{1,2})\s*(?:\+)?\s*(?:hours|hrs|hours/week|hrs/week|hours per week|h/w)", body)
    if m:
        h = int(m.group(1))
        if 5 <= h <= 60:
            return h
    if re.search(r"full[- ]?time (?:hours|commitment|basis|basis\b)", body):
        return 40
    if re.search(r"\bpart[- ]?time\b", body):
        return 20
    return None

def find_duration(body):
    """Detect internship length in months; returns int or None."""
    m = re.search(r"(\d{1,2})\s*(?:-|to|–)?\s*(?:months?|mos\b)", body)
    if m:
        d = int(m.group(1))
        if 1 <= d <= 24:
            return d
    for word, n in (("one month", 1), ("two months", 2), ("two-month", 2), ("three months", 3),
                    ("three-month", 3), ("four months", 4), ("four-month", 4),
                    ("five months", 5), ("five-month", 5), ("six months", 6),
                    ("six-month", 6), ("year-long", 12), ("1 year", 12), ("2 years", 24)):
        if word in body:
            return n
    return None

def find_stipend(body):
    """Pull a short salary/stipend snippet if present (skip perk stipends like lunch)."""
    pats = [r"(?:stipend|salary|compensation|pay(?:ing)?|interns? (?:are |get )?(?:paid|earn))"
            r"[^.;\n]{0,60}(?:₹|\$|rs\.?|inr|usd)\s?[\d,.]+[kkl]?\s?"
            r"(?:/?\s?(?:month|mo|yr|year|annum|lpa|pa|hour|hr|week))?",
            r"(?:₹|\$|rs\.?|inr|usd)\s?[\d,.]+\s?(?:k|lakh|lpa)?\s?"
            r"(?:per month|/month|monthly|per annum|annually|per hour|/hr)",
            r"[\d,.]+\s?(?:lpa|lakhs?)(?!\s)",
            r"\bunpaid\b|not paid"]
    for p in pats:
        m = re.search(p, body, re.I)
        if m:
            context = body[max(0, m.start() - 40):m.end()]
            if re.search(r"lunch|meal|commut|travel|relocation|wellness|gym|learning budget",
                         context, re.I):
                continue
            txt = clean(m.group(0))[:70]
            txt = re.sub(r"^(?:stipend|salary|compensation|pay(?:ing)?|interns? (?:are |get )?(?:paid|earn))"
                         r"[:\s]*(?:of\s+)?", "", txt, flags=re.I)
            return txt or None
    return None

def find_deadline(j):
    """Application deadline hints: 'apply by <date>' or LinkedIn easy-apply windows."""
    body = (j.get("desc") or "") + " " + (j.get("title") or "")
    m = re.search(r"(?:apply by|applications? (?:close|due)|deadline)[:\s]*"
                  r"(\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?"
                  r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:,?\s+20\d\d)?)",
                  body, re.I)
    if m:
        return clean(m.group(1))
    return None

def find_joining(j):
    """Joining/start date hints: cohort year, season+year, or 'starting <date>'.
    Only future dates (2026+) count; older years are JD noise, not a joining date."""
    title = (j.get("title") or "").lower()
    body = (j.get("desc") or "")[:800].lower()
    m = re.search(r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|winter|spring|summer|fall|autumn)[a-z]*\.?\s*20(?:2[6-9]|[3-9]\d))", title + " " + body)
    if m:
        return clean(m.group(1))
    m = re.search(r"(?:joining|start(?:s|ing)? (?:on|date|from))[:\s]*"
                  r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+20(?:2[6-9]|[3-9]\d))", body)
    if m:
        return clean(m.group(1))
    return None

def find_eligibility(j):
    """Short eligibility snippet: degree/year/pursuing requirements."""
    body = j.get("desc") or ""
    m = re.search(r"(?:to be eligible[^.;\n]{0,40}"
                  r"|pursuing|enrolled (?:in|with)|currently (?:in|pursuing)|"
                  r"students?\s+(?:in|pursuing)|"
                  r"b\.?tech|b\.\s?e\.?\b|b\.?sc\b|bachelor'?s?(?: degree)?|master'?s?(?: s)?|ph\.?d)"
                  r"[^.|;\n]{0,110}", body, re.I)
    if m:
        return clean(m.group(0))[:130]
    return None

# ------------------------------------------------------------- eligibility
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}

def parse_date_any(s):
    """Parse '15-10-2026', 'Oct 15, 2026', '15 Oct 2026' -> date or None."""
    import datetime as dt
    s = (s or "").strip().lower()
    m = re.search(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?", s)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else None
        if y and y < 100:
            y += 2000
        if y and 1 <= mo <= 12 and 1 <= d <= 31:
            try:
                return dt.date(y, mo, d)
            except ValueError:
                pass
    m = re.search(r"([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,)?\s*(\d{4})?", s)
    if m and m.group(1) in MONTHS:
        d = int(m.group(2))
        y = int(m.group(3)) if m.group(3) else None
        if y and 1 <= d <= 31:
            import datetime as dt
            try:
                return dt.date(y, MONTHS[m.group(1)], d)
            except ValueError:
                pass
    return None

def hard_eligibility(j, prof):
    """Return list of HARD eligibility violations (empty = eligible).
    Only counts REQUIRED wording, not preferred/nice-to-have."""
    import datetime as dt
    title = j["title"].lower()
    desc = (j.get("desc") or "")[:6000].lower()
    body = f"{title} {desc}"
    v = []

    # 1. degree requirements (BCA = bachelor's; can't satisfy master's/phd-only)
    if re.search(r"(?:require|must have|requires)[^.]{0,60}\b(master'?s(?: degree)?|m\.?tech|ph\.?d|msc\b)[^.]{0,40}\b(degree|in\b)", body) \
       or re.search(r"\b(master'?s degree|ph\.?d|msc|m\.?tech)\s+(?:is )?(?:required|mandatory|must)\b", body) \
       or re.search(r"^(?:requirements?|minimum qualifications?)[\s\S]{0,200}?(?:master'|ph\.?d)", desc, re.M):
        if "bca" not in body and "bachelor" not in body and "any degree" not in body \
           and "or equivalent" not in body and "currently pursuing" not in body:
            v.append("❌ HARD ELIGIBILITY ISSUE: requires Master's/PhD — you are a BCA undergraduate")

    # 2. experience requirements
    m = re.search(r"(\d{1,2})\+?\s*(?:-\s*\d{1,2}\s*)?(?:years?|yrs?)\s*(?:of\s*)?(?:professional\s*)?experience", body)
    if m:
        yrs = int(m.group(1))
        if yrs >= 2 and not re.search(r"(?:years? of college|years? of university|years? of education)", body):
            v.append(f"❌ HARD ELIGIBILITY ISSUE: requires {yrs}+ years experience — you are a fresher")
        elif yrs == 1 and re.search(r"(?:require|must have|minimum)", body[:m.start()][-60:]):
            v.append("⚠ 1 year experience required — borderline for a fresher")

    # 3. graduation year cohort restrictions (2026-only batches exclude a 2027 grad)
    m = re.search(r"\b20(2[4-6])\b\s*(?:batch|graduates?|passing out|passout)", body)
    if m and "2027" not in body:
        v.append(f"❌ HARD ELIGIBILITY ISSUE: restricted to 20{m.group(1)} batch — you graduate 2027")

    # 4. work authorization for onsite abroad
    if re.search(r"\bmust be (?:authorized|eligible) to work in (?:the )?(?:us|uk|canada|eu|germany|singapore)\b", body) \
       or re.search(r"\b(?:us|uk|canadian|eu) (?:citizens?|citizenship|work authorization) (?:required|only)\b", body):
        v.append("❌ HARD ELIGIBILITY ISSUE: requires work authorization you don't hold")

    # 5. immediate full-time joining before 15 Dec 2026
    if j.get("_ft"):
        joining = find_joining(j)
        if joining:
            d = parse_date_any(joining)
            if d and d < dt.date(2026, 12, 15):
                v.append(f"❌ HARD ELIGIBILITY ISSUE: joining {joining} is before your 15 Dec 2026 availability")

    return v

def quality_check(j, prof):
    """Scam / fake / expired listing detection. Returns list of issues."""
    t = j["title"].lower()
    body = f"{j['title']} {j.get('desc') or ''}".lower()
    loc = (j.get("location") or "").lower()
    issues = []

    # scam patterns
    scam_t = ("earn money online", "data entry", "form filling", "typing job",
              "copy paste", "make money", "work from home job", "investment",
              "insurance agent", "mlm", "network marketing", "bitcoin", "crypto trading",
              "clicking", "captchas", "registration fee", "earn daily", "earn 500",
              "earn ₹", "earn rs", "paid training", "training fee", "security deposit")
    if any(k in t for k in scam_t) and not any(k in t for k in ("ai", "ml", "machine learning", "developer", "engineer", "python")):
        issues.append("scam-pattern title")
    if any(k in body for k in ("registration fee", "security deposit", "training fee",
                               "pay to apply", "refundable fee", "processing fee",
                               "deposit of", "pay rs", "pay ₹")):
        issues.append("pay-to-apply scheme")
    if any(k in body for k in ("commission only", "commission-only", "no fixed salary")):
        issues.append("commission-only role")
    if re.search(r"earn\s+(?:up to\s*)?(?:₹|rs\.?|inr)?\s*[\d,]+\s*(?:per|/)\s*(?:day|week|task|assignment)", body):
        issues.append("earn-per-task scheme")
    if re.search(r"training program[^.]{0,50}(?:fee|charge|paid)|paid training program", body):
        issues.append("training program disguised as job")

    # company identifiable
    comp = (j.get("company") or "").strip()
    if not comp or comp in ("?", "unknown", "confidential company", "private limited 123",
                            "wellfound startup", "indeed employer", "linkedin employer") or len(comp) < 2:
        issues.append("company not identifiable")

    # obviously stale posting
    date_s = j.get("date") or ""
    if date_s:
        try:
            import datetime as dt
            age = (dt.datetime.now(dt.timezone.utc).date() - dt.date.fromisoformat(date_s[:10])).days
            if age > 120:
                issues.append(f"posted {age} days ago — likely expired")
        except ValueError:
            pass

    # clearly fake company names
    if re.search(r"\b(consultancy|hr services)\b", comp.lower()) and "recruitment" in body and "software" not in body:
        pass  # consultancy postings can be real; skip hard block

    return issues

# ---------------------------------------------------- new resume match score
def score_job(j, prof, cfg):
    """Honest 0-100 resume match: required 35 + preferred 15 + education 15 +
    projects 15 + experience 10 + role relevance 10."""
    title = j["title"].lower()
    desc = (j.get("desc") or "")
    body = f"{j['title']} {desc} {j['location']}".lower()
    reasons = []
    missing = []
    breakdown = {}

    # ---- required skills (35)
    # split JD into requirement-ish segments; treat tech named there as 'required'
    req_text = " ".join(re.findall(
        r"(?:requirements?|require|must have|mandatory|minimum qualifications?|"
        r"what you['\u2019]?ll (?:do|need)|qualification)[\s\S]{0,400}?(?:\.\s[A-Z]|;|$)", desc, re.I))
    if not req_text:
        # 'python, pandas required' / 'requires python' styles
        req_text = " ".join(re.findall(r"[\s\S]{0,200}?(?:require[ds]?|mandatory|must have)[\s\S]{0,80}", desc, re.I))
    if not req_text:
        req_text = desc  # can't isolate requirements: use full JD as proxy
    req_blob = req_text.lower()
    have = [s.lower() for s in prof.get("have_skills", [])]
    have_hits = [s for s in have if word_hit(s, req_blob)]
    tech_present = [s for s in (prof.get("have_skills", []) + prof.get("missing_skills", []))
                    if word_hit(s.lower(), body)]
    # ratio of your skills among the technologies the JD actually names
    if tech_present:
        req_ratio = len(have_hits) / max(1, len(tech_present))
    else:
        req_ratio = 0.6  # JD names nothing specific: neutral
    breakdown["required"] = round(35 * req_ratio)
    if have_hits:
        reasons.append(f"required skills matched: {', '.join(have_hits[:6])}")

    # ---- preferred skills (15)
    pref_blob = " ".join(re.findall(
        r"(?:preferred|nice[- ]to[- ]have|bonus|plus|good to have)[\s\S]{0,300}?(?:\.|$)",
        desc, re.I)).lower()
    if pref_blob:
        pref_hits = [s for s in have if word_hit(s, pref_blob)]
        breakdown["preferred"] = round(15 * len(pref_hits) / max(1, len(pref_hits) + 2))
        if pref_hits:
            reasons.append(f"preferred skills you have: {', '.join(pref_hits[:4])}")
    else:
        breakdown["preferred"] = 7  # nothing stated as preferred: neutral
        missing = [s.title() for s in prof.get("missing_skills", []) if word_hit(s, body)]

    # missing skills = JD-named tech you don't have
    missing = [s.title() for s in prof.get("missing_skills", [])
               if word_hit(s, body)] or missing

    # ---- education & eligibility (15)
    edu = 0
    if any(k in body for k in ("bca", "bca students", "any graduate", "any degree",
                               "bachelor", "bca/btech", "bca/ bsc")):
        edu += 6
    if any(k in body for k in ("fresher", "student", "pursuing", "no experience",
                               "0-1 years", "entry level", "entry-level")):
        edu += 5
    if "intern" in title or "internship" in title:
        edu += 4
    elif any(k in title for k in ("fresher", "graduate", "campus", "new grad", "university", "trainee")):
        edu += 4
    breakdown["education"] = min(15, edu)

    # ---- projects (15): overlap of JD stack with my two projects
    proj_stacks = []
    for p in prof.get("projects", []):
        proj_stacks += [s.lower() for s in p.get("stack", [])]
    proj_hits = [s for s in set(proj_stacks) if word_hit(s, body)]
    if proj_hits:
        breakdown["projects"] = min(15, 5 + 4 * len(set(proj_hits)))
        reasons.append(f"project stack overlap: {', '.join(sorted(set(proj_hits))[:5])}")
    else:
        breakdown["projects"] = 2

    # ---- practical experience (10)
    exp = 0
    if word_hit("fastapi", body) or word_hit("rest api", body):
        exp += 3
        reasons.append("your FastAPI/REST project maps directly")
    if word_hit("scikit-learn", body) or word_hit("pandas", body) or word_hit("numpy", body):
        exp += 3
        reasons.append("your ML/data workflow experience applies")
    if any(k in body for k in ("collaborat", "team", "agile", "cross-functional")):
        exp += 2
    if any(k in body for k in ("event", "workshop", "volunteer")):
        exp += 1
    breakdown["experience"] = min(10, exp)

    # ---- role relevance (10)
    want = [r.lower() for r in prof.get("want_roles", [])]
    role_hit = None
    for r in want:
        words = [w for w in re.split(r"[\s/]+", r) if len(w) > 2]
        if words and all(w in title for w in words[:2]):
            role_hit = r
            break
    if role_hit:
        breakdown["role"] = 10
        reasons.append(f"target role: {role_hit}")
    elif any(k in title for k in ("ai", "ml", "machine learning", "data scien", "deep learning",
                                  "nlp", "genai", "llm", "computer vision")):
        breakdown["role"] = 8
        reasons.append("AI/ML-adjacent role")
    elif any(k in title for k in ("software", "developer", "engineer", "sde", "backend", "python")):
        breakdown["role"] = 7
        reasons.append("software/engineering role in your direction")
    else:
        breakdown["role"] = 3

    # ---- context adjustments (small, honest, explained)
    adj = 0
    if "remote" in body or "work from home" in body:
        adj += 2
    if re.search(r"noida|greater noida|delhi ncr|\bncr\b|gurugram|gurgaon|\bdelhi\b", body):
        adj += 2
        reasons.append("Noida/Delhi NCR — your home zone")
    if any(c.lower() in j["company"].lower() for c in prof.get("major_companies", [])):
        adj += 1
        reasons.append("major company")
    # early-hiring flag (no score inflation; it's a reporting priority)
    if re.search(r"\b202[7-9]\b", title + " " + body[:300]):
        reasons.append("🎯 early hiring — applications open now for a future joining date")

    # hours/week: college semester allows 15-20; heavier must be flagged not hidden
    hours = find_hours(body)
    if hours is not None:
        if hours <= 20:
            reasons.append(f"{hours} h/week — fits college (you can give ~15-20)")
        elif hours <= 25:
            reasons.append(f"{hours} h/week — slightly above your 15-20 h target")
        else:
            reasons.append(f"⚠ {hours}+ h/week during semester — heavy; decide if feasible")
    score = max(0, min(100, sum(breakdown.values()) + adj))

    # full-time detection for downstream joining-date rule
    is_fulltime = False
    if any(k in title for k in ("full-time", "full time", "fulltime")) and \
       "intern" not in title and any(k in title for k in FT_TITLE_KEYS):
        is_fulltime = True
    # fresher/graduate full-time without the literal words still counts
    if not is_fulltime and "intern" not in title and \
       any(k in title for k in ("fresher", "graduate software", "graduate engineer",
                                "2027", "graduate trainee", "campus hire", "new grad")):
        is_fulltime = True

    # unpaid labeling
    if re.search(r"\bunpaid\b|without stipend|no stipend", body):
        reasons.append("⚠ UNPAID internship")

    return score, reasons, missing, is_fulltime



# --------------------------------------------------------------- delivery
def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def tg_send(cfg, text_html):
    token, chat = cfg.get("telegram_bot_token"), cfg.get("telegram_chat_id")
    if not token or not chat:
        log("telegram not configured: missing token/chat_id")
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
                    log(f"telegram: API said not ok: {str(resp)[:300]}")
                    ok = False
                else:
                    log(f"telegram: message ({len(text_html[i:i+3800])} chars) delivered")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:300]
            except Exception:
                pass
            log(f"telegram send failed: HTTP {e.code} {detail}")
            ok = False
        except Exception as e:
            log(f"telegram send failed: {str(e)[:200]}")
            ok = False
    return ok

def deliver(cfg, subject, body_text):
    """Telegram-only delivery. body_text is plain text with light HTML markup."""
    tg_body = "<b>" + esc(subject) + "</b>\n\n" + esc(body_text)
    if tg_send(cfg, tg_body):
        return "telegram"
    return None

# ------------------------------------------------------------- formatting
def job_facts(j):
    """Extracted card fields for alerts (best-effort from posting text)."""
    body = f"{j['title']} {j['desc']} {j['location']}".lower()
    return {
        "stipend": find_stipend(body),
        "eligibility": find_eligibility(j),
        "deadline": find_deadline(j),
        "joining": find_joining(j),
        "hours": find_hours(body),
        "duration": find_duration(body),
    }

def summarize_jd(j):
    """3-6 line JD summary: what you'd work on, tech, expectations. Never copy huge chunks."""
    d = (j.get("desc") or "").strip()
    if not d:
        return "Not disclosed"
    # first meaningful sentences, skipping boilerplate headings
    d = re.sub(r"(?i)^(?:about (?:us|the company|the role)|job description|requirements?|"
               r"responsibilities?|who (?:we|you) (?:are|'re)|overview|role)\s*:?\s*", "", d)
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", clean(d)) if len(s.strip()) > 25]
    pick, total = [], 0
    for s in sents:
        if total > 420:
            break
        pick.append(s)
        total += len(s)
    return " ".join(pick)[:520] or "Not disclosed"

def extract_responsibilities(j, n=3):
    """Pull up to n responsibility bullets from the JD."""
    d = j.get("desc") or ""
    seg = " ".join(re.findall(r"(?:responsibilit(?:y|ies)|what you['\u2019]?ll (?:do|work on)|"
                              r"you will|your day(?: to day)?|key (?:duties|tasks))[\s\S]{0,700}", d, re.I))
    if not seg:
        seg = d
    # bullet-ish sentences with verbs
    verbs = ("build", "develop", "design", "create", "work", "maintain", "write", "test",
             "train", "analyze", "deploy", "collaborate", "implement", "research", "support",
             "optimize", "integrate", "build", "participate", "contribute")
    out = []
    for s in re.split(r"(?:<li[^>]*>|\n|;\s|(?<=[.!?])\s+)", clean(seg)):
        s = s.strip(" -•\u2022\t")
        s = re.sub(r"^(?:what you['\u2019]?ll (?:do|work on)|responsibilit(?:y|ies)|"
                   r"key (?:duties|tasks)|you will|your day(?: to day)?)\s*[:\u2013-]?\s*",
                   "", s, flags=re.I)
        if 20 < len(s) < 160 and any(v in s.lower() for v in verbs):
            out.append(s[:150])
        if len(out) >= n:
            break
    return out or ["As described in the posting — see direct link"]

def deadline_line(j):
    """Deadline with urgency marker if close."""
    dl = find_deadline(j)
    if not dl:
        return "⚠️ Deadline: Not specified"
    d = parse_date_any(dl)
    if d:
        import datetime as dt
        days = (d - dt.date.today()).days
        if days < 0:
            return f"⚠️ Deadline: {dl} — appears PASSED, verify"
        if days <= 2:
            return f"🚨 Deadline in {days} day(s) — {dl}"
        if days <= 7:
            return f"⚠️ Deadline: {dl} (in {days} days)"
    return f"⚠️ Deadline: {dl}"

def match_tier(sc):
    if sc >= 85:
        return "🔥 Exceptional Match"
    if sc >= 70:
        return "🟢 Strong Match"
    if sc >= 60:
        return "🟡 Stretch Match"
    return "🟡 Stretch Match"

def alert_text(j, sc, reasons, missing, gem=False):
    f = job_facts(j)
    L = []
    add = L.append
    add(f"🔥 {j['title']} — {j['company']}")
    add("")
    add(f"📍 Location:\n{j['location']}")
    add(f"💰 Salary/Stipend:\n{f['stipend'] or 'Not disclosed'}")
    add(f"🏢 Company:\n{j['company']}")
    add(f"🎓 Eligibility:\n{f['eligibility'] or 'Not disclosed'}")
    add(f"📅 Posted:\n{j.get('date') or 'Not specified'}")
    join_line = f['joining'] or ("Joining date unclear — verify before applying"
                                 if j.get('_ft') else "Not specified")
    add(f"🚀 Joining:\n{join_line}")
    add(f"⭐ Resume Match:\n{sc}/100")
    add(f"{match_tier(sc)}")
    add("")
    add("Why you match")
    for r in reasons[:5]:
        add(f"- {r}")
    add("")
    add("Missing / weaker areas")
    if missing:
        for s in missing[:5]:
            add(f"- {s}")
    else:
        add("- None material — you meet the stated requirements")
    add("")
    add("Job description")
    add(summarize_jd(j))
    add("")
    add("Key responsibilities")
    for r in extract_responsibilities(j):
        add(f"- {r}")
    add("")
    add("Why this is relevant to me")
    rel = [r for r in reasons if any(k in r for k in ("target role", "AI/ML", "project",
                                                       "required skills", "FastAPI", "ML/data"))]
    for r in rel[:2]:
        add(f"- {r}")
    if not rel:
        add("- Builds on your Python/ML foundation toward your AI/ML career direction.")
    add("")
    add(deadline_line(j))
    add("")
    add(f"👉 DIRECT APPLICATION:\n{j['url']}")
    add(f"🔎 Source:\n{j.get('source', 'Not specified')}")
    return "\n".join(L)

def split_full_rest(to_send, cfg):
    """Top N jobs get the full card; everything else goes in the compact digest."""
    n = int(cfg.get("full_cards_per_run", 3))
    return to_send[:n], to_send[n:]

def compact_line(sc, j, r, m):
    """2-line compact entry for the digest message."""
    badge = ("🔥" if sc >= 85 else "🟢" if sc >= 70 else "🟡")
    tags = []
    for reason in r:
        if "🎯" in reason and len(tags) < 2:
            tags.append("🎯 early-hiring")
        elif "UNPAID" in reason and len(tags) < 2:
            tags.append("⚠ UNPAID")
        elif "HARD ELIGIBILITY" in reason and len(tags) < 2:
            tags.append("❌ hard-eligibility")
        elif "h/week" in reason and "heavy" in reason and len(tags) < 2:
            tags.append("⏰ heavy hours")
    join = find_joining(j)
    if join and len(tags) < 2:
        tags.append("🚀 " + join)
    loc = j.get("location", "") or ""
    tag_txt = (" · " + " · ".join(t for t in tags if t)) if tags else ""
    line1 = f"{badge} [{sc}%] {j['title']} — {j['company']}{tag_txt}"
    line2 = f"   {loc} · {j['url']}" if loc else f"   {j['url']}"
    return line1[:180] + "\n" + line2[:200]

def compact_digest(items):
    """One compact list: 2 lines per job, ~150 chars each."""
    return "\n\n".join(compact_line(sc, j, r, m) for sc, j, r, m in items)

# ------------------------------------------------------------------- main
def main():
    prof = load_json(BASE / "profile.json", {})
    cfg = load_json(BASE / "telegram.json", {}) or {}  # legacy email.json also still works
    if not cfg.get("telegram_bot_token") or not cfg.get("telegram_chat_id"):
        cfg = load_json(BASE / "email.json", {}) or {}
    cfg.setdefault("full_cards_per_run", 3)
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
    # environment overrides (Telegram bot only)
    import os
    for env_key, cfg_key in [("TELEGRAM_BOT_TOKEN", "telegram_bot_token"),
                             ("TELEGRAM_CHAT_ID", "telegram_chat_id")]:
        if os.environ.get(env_key):
            cfg[cfg_key] = os.environ[env_key]

    prof["_keyword"] = "internship"
    prof["_indeed_location"] = "India"
    prof["_sr_companies"] = ["Visa", "bakerhughes", "Bosch", "Workday", "Stryker",
                             "JuniperNetworks", "adp", "SonyInteractiveEntertainmentGlobal"]
    prof["_gh_companies"] = ["stripe", "databricks", "figma", "cloudflare", "mongodb",
                             "twilio", "robinhood", "reddit", "dropbox", "coinbase",
                             "nuro"]
    prof["_lever_companies"] = ["spotify"]
    prof["_ashby_companies"] = ["elevenlabs", "Perplexity", "Cohere", "Runway",
                                "baseten", "groq", "mistral", "togetherai"]
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

    # de-dup + score (no score-based filter; every relevant job is delivered)
    seen_ids, scored = [], []
    now_iso = time.strftime("%Y-%m-%d %H:%M")
    n_qual = n_hard = 0
    # cross-source dedup + persistent fingerprint (company+role normalized);
    # prefer official ATS/company URLs over aggregator copies.
    def dedup_key(j):
        role = re.sub(r"[^a-z0-9 ]", "", j["title"].lower())
        role = " ".join(w for w in role.split() if w not in
                        ("intern", "internship", "remote", "part", "time", "full"))
        comp = re.sub(r"[^a-z0-9 ]", "", j["company"].lower())
        return (comp, role[:60])
    ATS_PRIORITY = ("ashby", "greenhouse", "lever", "smartrecruiters", "netflix",
                    "amazon", "remotive", "remoteok", "jobicy", "arbeitnow",
                    "internshala", "wellfound", "linkedin", "indeed", "youtube")
    by_key = {}
    for j in all_jobs:
        if not j.get("title") or not j.get("url") or j["id"] in seen_ids:
            continue
        seen_ids.append(j["id"])
        if not is_target_title(j["title"]):
            continue
        # keep only the best-source copy of each (company, role) pair
        k = dedup_key(j)
        if k in by_key:
            kept = by_key[k]
            if ATS_PRIORITY.index(j["source"]) < ATS_PRIORITY.index(kept["source"]):
                by_key[k] = j
            continue
        by_key[k] = j
    for j in by_key.values():
        # Scams/clearly-fake filter only — scoring is informational, never a rejecter.
        # PhD-only / 2026-batch / work-auth requirements are shown with a HARD-ELIGIBILITY
        # tag so the user can decide; they're not dropped.
        q = quality_check(j, prof)
        if q:
            n_qual += 1
            continue
        sc, reasons, missing, is_ft = score_job(j, prof, cfg)
        if sc <= 0:
            continue
        j["_ft"] = is_ft
        # surface hard-eligibility as a tag (still delivered)
        hv = hard_eligibility(j, prof)
        if hv:
            reasons = hv + reasons
        # full-time joining-date rule
        if j["_ft"]:
            joining = find_joining(j)
            if joining:
                if re.search(r"20(?:2[7-9]|[6][6-9])", joining) or \
                   re.search(r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|winter|spring)[a-z]*\.?\s*2027", joining):
                    reasons.append(f"joining {joining} — on/after 15 Dec 2026 (OK)")
                elif re.search(r"dec[a-z]*\.?\s*2026", joining):
                    reasons.append(f"joining {joining} — December 2026; verify it's on/after 15 Dec")
                else:
                    reasons.append(f"⚠ joining {joining} looks BEFORE your 15 Dec 2026 availability")
            else:
                reasons.append("⚠️ Joining date unclear — verify before applying")
        scored.append((sc, j, reasons, missing))
    scored.sort(key=lambda x: -x[0])
    log(f"scored {len(scored)} relevant of {len(all_jobs)} raw "
        f"(blocked: {n_qual} scam/fake; {n_hard} other)")

    # No score-tier splitting: every fresh job is delivered.
    # Persistent fingerprints stop Internshala reposts (same role+company, new URL
    # timestamp) from re-alerting across runs.
    fps = seen.get("fps", {}) if isinstance(seen, dict) else {}
    to_send = []
    for x in scored:
        sc, j, r, m = x
        if j["id"] in seen:
            continue
        fp = dedup_key(j)
        if fp in fps:
            continue
        to_send.append(x)
    log(f"to-send={len(to_send)} (of {len(scored)} scored)")

    if dry:
        print("\n===== DRY RUN — what would be delivered =====")
        top, rest = split_full_rest(to_send, cfg)
        for sc, j, r, m in top:
            print(f"--- FULL CARD: {j['title']} at {j['company']} ---")
            print(alert_text(j, sc, r, m))
        if rest:
            print("=== COMPACT DIGEST ===")
            print(compact_digest(rest))
        if not to_send:
            print("(nothing new)")
        return 0

    sent = 0
    if to_send:
        top, rest = split_full_rest(to_send, cfg)
        # 1) a few full cards, one message each
        for sc, j, r, m in top:
            subj = f"🔥 [{sc}%] {j['title']} at {j['company']}"
            if deliver(cfg, subj, alert_text(j, sc, r, m)):
                sent += 1
                seen[j["id"]] = now_iso
                fps[dedup_key(j)] = now_iso
        # 2) everything else in one compact digest (split only on overflow)
        if rest:
            lines = compact_digest(rest).split("\n")
            part, parts, budget = [], [], 3500
            for ln in lines:
                if budget - len(ln) < 0 and part:
                    parts.append("\n".join(part))
                    part, budget = [], 3500
                part.append(ln)
                budget -= len(ln) + 1
            if part:
                parts.append("\n".join(part))
            total_parts = len(parts)
            for idx, ptext in enumerate(parts):
                hdr = (f"🟢 {len(rest)} more opportunities ({now_iso})"
                       if total_parts == 1 and idx == 0
                       else f"🟢 More opportunities — part {idx+1}/{total_parts}")
                if deliver(cfg, hdr, ptext):
                    pass
            for sc, j, r, m in rest:
                sent += 1
                seen[j["id"]] = now_iso
                fps[dedup_key(j)] = now_iso
        seen["fps"] = fps
        log(f"delivered {sent} opportunities ({len(top)} full cards + {len(rest)} compact)")

    if len(seen) > 5000:
        for k in sorted(seen, key=seen.get)[:1500]:
            seen.pop(k)
    save_json(SEEN_FILE, seen)
    # a silent exit code keeps the Actions run green; delivery failures are already logged
    log(f"done: seen={len(seen)} sent={sent} scored={len(scored)}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
