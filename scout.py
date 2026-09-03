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
    import html as _html
    s = _html.unescape(s or "")          # &amp; &#8211; etc. before tag stripping
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def unescape_html(s):
    """Greenhouse returns double-escaped HTML (&lt;p&gt;...); unescape then strip."""
    import html as _html
    return _html.unescape(_html.unescape(s or ""))

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
            "desc": clean(str(desc))[:8000], "date": (date or "")[:10], "source": src}

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
    # NOTE: adding country=IND makes amazon.jobs silently ignore base_query
    # (verified live: returns senior roles matching 'international' in text,
    # zero real internships). Global query returns real internships; the
    # location gate drops onsite-abroad afterwards.
    d = get_json("https://www.amazon.jobs/en/search.json?base_query=internship"
                 "&result_limit=100")
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
    """Two-phase: cheap list for title+location filtering, then hydrate the
    descriptions of survivors only (?content=true on the whole board is ~4.5 MB
    per company)."""
    out = []
    hydrate = []
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
            # the board list API returns offices[].location as null; the real
            # value lives in location.name (verified live across 16 boards)
            loc = (j.get("location") or {}).get("name") or ""
            if loc.strip().lower() in ("", "n/a", "na", "not specified"):
                # multi-location reqs leave location blank but name the office region
                offices = j.get("offices") or []
                alt = ", ".join(o.get("name") or o.get("location") or "" for o in offices[:2])
                loc = alt.strip(", ") or loc
            job = J("greenhouse", j.get("id"), title, company_map(company), loc,
                    j.get("absolute_url", ""), "", (j.get("updated_at") or ""))
            out.append(job)
            ok, _ = location_gate(job)
            if ok and j.get("id"):
                hydrate.append((company, j.get("id"), job))
    for company, jid, job in hydrate[:70]:
        try:
            one = get_json(f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs/{jid}")
            job["desc"] = clean(unescape_html(str(one.get("content") or "")))[:8000]
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
            continue
    log(f"greenhouse: hydrated {min(len(hydrate), 70)} descriptions")
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
            # location + workplaceType (workplaceType can be None — never print 'None')
            loc = " ".join(x for x in (str(j.get("location") or ""),
                                       str(j.get("workplaceType") or "")) if x and x != "None")
            # employmentType gives reliable intern/full-time signal the scorer can use
            et = {"intern": "internship", "fulltime": "full-time"}.get(
                str(j.get("employmentType") or "").lower().replace("_", ""), "")
            desc = f"{j.get('descriptionPlain') or j.get('descriptionHtml') or ''} {et}"
            out.append(J("ashby", j.get("id"), j.get("title"), company_map(company), loc,
                         j.get("jobUrl") or j.get("applyUrl") or f"https://jobs.ashbyhq.com/{company}",
                         desc, (j.get("publishedAt") or "")))
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
    locations = [clean(x) for x in
                 re.findall(r'job-search-card__location[^>]*>\s*([^<]{2,80})', page)]
    for i, jid in enumerate(ids):
        if jid in seen:
            continue
        seen.add(jid)
        out.append(J("linkedin", jid,
                     titles[i] if i < len(titles) else "Internship",
                     companies[i] if i < len(companies) else "LinkedIn employer",
                     # the search is already location-scoped to India; never emit
                     # "LinkedIn" as a location — the gate reads it as onsite-abroad
                     locations[i] if i < len(locations) else p.get("_indeed_location", "India"),
                     f"https://www.linkedin.com/jobs/view/{jid}", ""))
    return out

def adp_google(p):
    """Google careers: the results HTML embeds job ids and titles (no public API)."""
    out, seen = [], set()
    for q in ("software+engineering+intern", "machine+learning+intern"):
        try:
            page = http("https://www.google.com/about/careers/applications/jobs/results/"
                        f"?q={q}&location=India", headers={"Accept": "text/html"}
                        ).decode("utf-8", "replace")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            log(f"google:{q} unreachable")
            continue
        # links look like /about/careers/applications/jobs/results/<id>-<slug>
        for jid, slug in re.findall(r'jobs/results/(\d{8,})-([a-z0-9\-]{5,80})', page):
            if jid in seen:
                continue
            seen.add(jid)
            title = slug.replace("-", " ").title()
            if not is_target_title(title):
                continue
            out.append(J("google", jid, title, "Google", "India",
                         "https://www.google.com/about/careers/applications/jobs/results/"
                         f"{jid}-{slug}", "", ""))
        time.sleep(0.8)
    return out

def adp_workday(p):
    """Workday-hosted career sites (Nvidia, Adobe, Salesforce, Micron, HPE, ...).
    Two-phase: search list, then hydrate descriptions of gate-passing jobs."""
    out, hydrate = [], []
    for label, host, tenant, site in p["_workday_tenants"]:
        base = f"https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
        for query in ("intern", "graduate"):
            try:
                raw = http(f"{base}/jobs", method="POST",
                           data={"appliedFacets": {}, "limit": 20, "offset": 0,
                                 "searchText": query},
                           headers={"Content-Type": "application/json",
                                    "Accept": "application/json"})
                d = json.loads(raw.decode("utf-8", "replace"))
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
                log(f"workday:{label}:{query} unreachable")
                continue
            for j in d.get("jobPostings") or []:
                title = j.get("title") or ""
                if not is_target_title(title):
                    continue
                path = j.get("externalPath") or ""
                loc = j.get("locationsText") or ""
                job = J("workday", f"{label}:{path[-40:]}", title, company_map(label), loc,
                        f"https://{host}.myworkdayjobs.com/en-US/{site}{path}", "", "")
                out.append(job)
                ok, _ = location_gate(job)
                if ok and path:
                    hydrate.append((base, path, job))
            time.sleep(0.25)
    for base, path, job in hydrate[:24]:
        try:
            one = get_json(base + path)
            info = one.get("jobPostingInfo") or {}
            job["desc"] = clean(str(info.get("jobDescription") or ""))[:8000]
            job["location"] = (info.get("location") or job["location"])
            posted = str(info.get("startDate") or "")[:10]
            if re.match(r"\d{4}-\d{2}-\d{2}", posted):
                job["date"] = posted
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
            continue
    log(f"workday: hydrated {min(len(hydrate), 24)} descriptions")
    return out

def adp_yc(p):
    """Y Combinator's own job board (higher weight — YC startups hire juniors and
    remote). Role pages are server-rendered links to /companies/<co>/jobs/<id>-<slug>;
    each job page carries a schema.org JobPosting block with the real details."""
    out, seen = [], set()
    for role in ("eng", "data-science", "ml", "engineering"):
        try:
            page = http(f"https://www.ycombinator.com/jobs/role/{role}",
                        headers={"Accept": "text/html"}).decode("utf-8", "replace")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            log(f"yc:{role} unreachable")
            continue
        for path, comp_slug, job_slug in re.findall(
                r'href="(/companies/([a-z0-9\-]+)/jobs/([^"]+))"', page):
            if path in seen:
                continue
            seen.add(path)
            title = re.sub(r"^[A-Za-z0-9_\-]{5,10}-", "", job_slug).replace("-", " ").title()
            if not is_target_title(title):
                continue
            out.append(J("yc", path[-60:], title,
                         comp_slug.replace("-", " ").title() + " (YC)",
                         "", "https://www.ycombinator.com" + path, "", ""))
        time.sleep(0.8)
    # hydrate the JobPosting JSON-LD for location, description, pay and title
    for job in out[:25]:
        try:
            page = http(job["url"], headers={"Accept": "text/html"}).decode("utf-8", "replace")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            continue
        m = re.search(r'<script type="application/ld\+json">\s*(\{.*?\})\s*</script>', page, re.S)
        if not m:
            continue
        try:
            ld = json.loads(m.group(1))
        except ValueError:
            continue
        job["title"] = ld.get("title") or job["title"]
        job["desc"] = clean(str(ld.get("description") or ""))[:8000]
        loc = ld.get("jobLocation")
        parts = []
        for entry in (loc if isinstance(loc, list) else [loc]):
            addr = (entry or {}).get("address") or {} if isinstance(entry, dict) else {}
            parts += [str(addr.get(k)) for k in ("addressLocality", "addressRegion",
                                                 "addressCountry") if addr.get(k)]
        if str(ld.get("jobLocationType") or "").lower().startswith("tele"):
            parts.append("Remote")
        job["location"] = ", ".join(dict.fromkeys(parts))[:120]
        sal = ld.get("baseSalary") or {}
        val = (sal.get("value") or {}) if isinstance(sal, dict) else {}
        lo, hi = val.get("minValue"), val.get("maxValue")
        if lo or hi:
            cur = sal.get("currency") or "USD"
            unit = str(val.get("unitText") or "YEAR").lower()
            # YC's feed labels Indian salaries as USD (e.g. Bengaluru role listed as
            # "USD 2000000-5000000/year"). Trust magnitude over the currency field.
            try:
                if ("in" in job["location"].lower() or "india" in job["location"].lower()) \
                   and float(lo or hi) >= 300000 and unit.startswith("year"):
                    cur = "INR"
            except (TypeError, ValueError):
                pass
            job["desc"] += f" Compensation: {cur} {lo or ''}-{hi or ''} per {unit}."
        posted = str(ld.get("datePosted") or "")[:10]
        if re.match(r"\d{4}-\d{2}-\d{2}", posted):
            job["date"] = posted
        time.sleep(0.3)
    return out

def adp_oracle(p):
    """Oracle Cloud recruiting API (Oracle's own openings)."""
    out = []
    url = ("https://eeho.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/"
           "recruitingCEJobRequisitions?onlyData=true&expand=requisitionList"
           "&finder=findReqs;siteNumber=CX_1001,keyword=intern,limit=25")
    try:
        d = get_json(url)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
        log("oracle unreachable")
        return out
    for item in d.get("items") or []:
        for j in item.get("requisitionList") or []:
            title = j.get("Title") or ""
            if not is_target_title(title):
                continue
            locs = j.get("PrimaryLocation") or ""
            out.append(J("oracle", j.get("Id"), title, "Oracle", locs,
                         "https://careers.oracle.com/jobs/#en/sites/jobsearch/job/"
                         + str(j.get("Id") or ""),
                         str(j.get("ShortDescriptionStr") or ""),
                         str(j.get("PostedDate") or "")[:10]))
    return out

def adp_internshala(p):
    """Internshala — demoted to a single highest-signal page (ML); low-signal
    keyword scrapes removed. Its score floor is raised separately in main()."""
    out = []
    seen = set()
    for kw in ("machine%20learning",):
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
            # keep the word "Internship" in the title: the slug drops it, and without
            # it the title gate rejected real technical roles ("Data Engineer",
            # "Applied Scientist", "Nlp Scientist", ...)
            if "intern" not in role.lower():
                role = role + " Internship"
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
        # \uXXXX escapes come back as surrogate pairs / mojibake unless the
        # unicode_escape pass is re-encoded as latin-1 first
        def _dec(s):
            try:
                return s.encode("latin-1", "backslashreplace").decode("unicode_escape")
            except (UnicodeDecodeError, UnicodeEncodeError):
                return s
        titles = [_dec(t) for t in
                  re.findall(r'"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.){5,100})"', page)]
        chans = [_dec(c) for c in
                 re.findall(r'"ownerText":\{"runs":\[\{"text":"((?:[^"\\]|\\.){2,60})"', page)]
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
    ("lever", adp_lever), ("ashby", adp_ashby), ("workday", adp_workday),
    ("oracle", adp_oracle), ("google", adp_google), ("yc", adp_yc),
    ("wellfound", adp_wellfound),
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
              "sde", "engineer i", "analyst i", "developer i",
              "research fellow", "member of technical staff", "associate software",
              "graduate software", "junior ml", "junior ai", "python developer",
              "backend developer", "data science", "genai", "llm", "machine learning",
              "ai engineer", "ml engineer", "ai/ml", "artificial intelligence",
              "software developer", "software engineer", "backend engineer",
              "full stack", "full-stack", "fullstack",
              # domain engineer/developer/scientist titles that are routinely
              # entry-level-open and were being dropped by the old allow-list
              "data scientist", "data analyst", "data engineer", "research engineer",
              "applied scientist", "research scientist", "nlp", "computer vision",
              "frontend engineer", "front-end engineer", "frontend developer",
              "web developer", "devops engineer", "platform engineer",
              "site reliability engineer", "cloud engineer", "qa engineer",
              "quality assurance engineer", "test engineer", "automation engineer",
              "support engineer", "technical support", "analytics engineer",
              "database engineer", "python development", "ai research",
              "ai developer", "ml developer", "mlops", "prompt engineer",
              "technical staff", "product analytics", "quantitative")
FT_TITLE_KEYS = ("graduate", "campus", "new grad", "university", "fresher",
                 "early career", "2027", "2026", "trainee", "entry level", "entry-level")

def is_target_title(title):
    t = (title or "").lower()
    if not t:
        return False
    # 'staff' alone signals senior, but 'member of (the) technical staff' is entry-level
    if re.search(r"member of (?:the )?technical staff", t):
        return True
    if any(b in t for b in SENIOR_BLOCK):
        return False
    # non-tech internships are out of scope regardless of 'intern' in the title.
    # 'ai'/'ml' must be whole words here — bare substrings matched "Retail Intern".
    if "intern" in t or "internship" in t:
        if any(k in t for k in ("machine learning", "data scien", "data analy",
                                "software", "developer", "engineer", "python",
                                "programming", "backend", "frontend", "full stack",
                                "full-stack", "fullstack", "sde", "tech", "automation",
                                "computer", "research", "scien", "genai", "llm",
                                "cloud", "coding", "technical", "robotics", "security",
                                "platform", "infrastructure", "devops", "mlops",
                                "quantitative", "analytics", "web", "mobile",
                                "android", "ios", "database", "data")):
            return True
        return bool(re.search(r"\b(?:ai|ml|swe|nlp|cv|qa)\b", t))
    return any(k in t for k in ENTRY_KEYS)

def word_hit(skill, text):
    s = skill.lower()
    if len(s) <= 2:
        # 1-2 char skills (C, R, Go) need tight context: a bare "c" otherwise matches
        # C++, "(c) 2026" and "Option C", producing "required skills matched: c"
        return re.search(r"(?<![a-z0-9+#(])" + re.escape(s) + r"(?![a-z0-9+#])", text)
    return re.search(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9])", text)

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
            r"(?:/?\s?(?:per\s)?(?:month|mo|yr|year|annum|lpa|pa|hour|hr|week))?",
            r"(?:₹|\$|rs\.?|inr|usd)\s?[\d,.]+\s?(?:k|lakh|lpa)?\s?"
            r"(?:/?\s?(?:per\s)?(?:month(?:ly)?|mo|yr|year|annum|pa|hour|hr|week)|annually)",
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
            # a pay string with no number is boilerplate ("compensation, benefits,
            # promotions, transfers" from EEO paragraphs), not a stipend
            if not re.search(r"\d", txt) and not re.search(r"unpaid|not paid", txt, re.I):
                continue
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
    m = re.search(r"(?:pursuing|enrolled (?:in|with)|currently (?:in|pursuing)|"
                  r"students?\s+(?:in|pursuing)|"
                  r"b\.?tech|b\.\s?e\.?\b|b\.?sc\b|bca\b|bachelor'?s?(?: degree)?"
                  r"|master'?s?(?: degree)?|ph\.?d)"
                  r"[^.|;\n]{0,100}", body, re.I)
    if not m:
        return None
    txt = clean(m.group(0))
    # cut at a clause boundary so the card never shows a run-on fragment
    txt = re.split(r"\s+(?:and|with|plus)\s+(?:experience|demonstrated|proven)", txt)[0]
    if len(txt) > 120:
        txt = txt[:120].rsplit(" ", 1)[0] + "…"
    return txt or None

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
    if re.search(r"\bph\.?d\b", title) or re.search(r"\bmaster'?s?\b\s+(?:intern|student)", title):
        v.append("❌ HARD ELIGIBILITY ISSUE: PhD/Master's-level posting — you are a BCA undergraduate")
    elif re.search(r"(?:require|must have|requires)[^.]{0,60}\b(master'?s(?: degree)?|m\.?tech|ph\.?d|msc\b)[^.]{0,40}\b(degree|in\b)", body) \
       or re.search(r"\b(master'?s degree|ph\.?d|msc|m\.?tech)\s+(?:is )?(?:required|mandatory|must)\b", body) \
       or re.search(r"\b(?:ph\.?d|master'?s)\s+(?:student|candidate)s?\s+(?:doing|pursuing|only|preferred|enrolled)", body) \
       or re.search(r"^(?:requirements?|minimum qualifications?)[\s\S]{0,200}?(?:master'|ph\.?d)", desc, re.M):
        if "bca" not in body and "bachelor" not in body and "any degree" not in body \
           and "or equivalent" not in body and "currently pursuing" not in body:
            v.append("❌ HARD ELIGIBILITY ISSUE: requires Master's/PhD — you are a BCA undergraduate")

    # 2. experience requirements
    m = re.search(r"(\d{1,2})\+?\s*(?:-\s*\d{1,2}\s*)?(?:years?|yrs?)[^.;\n]{0,45}?experience", body)
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

# ------------------------------------------------------------- location gate
# matched as whole words, so "indiana"/"indianapolis" cannot pass as "india"
INDIA_CITIES = ("noida", "greater noida", "delhi", "new delhi", "gurgaon", "gurugram",
                "india", "bengaluru", "bangalore", "hyderabad", "pune", "mumbai",
                "navi mumbai", "thane", "chennai", "kolkata", "ahmedabad", "jaipur",
                "indore", "kochi", "coimbatore", "bhopal", "nagpur", "lucknow",
                "chandigarh", "mohali", "dehradun", "trivandrum", "thiruvananthapuram",
                "visakhapatnam", "vizag", "mysuru", "mysore", "bhubaneswar", "surat",
                "vadodara", "gandhinagar", "ghaziabad", "faridabad", "goa",
                "karnataka", "telangana", "maharashtra", "tamil nadu", "kerala",
                "gujarat", "haryana", "uttar pradesh", "delhi ncr")
INDIA_RE = re.compile(r"(?<![a-z])(" + "|".join(INDIA_CITIES).replace(" ", r"\s") +
                      r")(?![a-z])")
REMOTE_ONLY_SOURCES = ("remotive", "remoteok", "jobicy")
LOC_UNKNOWN = ("", "not specified", "n/a", "na", "varies", "multiple locations",
               "see posting", "wellfound (varies)", "linkedin", "youtube announcement")
# "2 Locations", "5 Locations" — Workday's placeholder for multi-site reqs
LOC_MULTI_RE = re.compile(r"^\d+\s+locations?$|^multiple\b|^various\b")
# "Remote - US" / "Canada Remote" / "Spain Remote" mean remote WITHIN that country:
# he would need work authorization there, so they are not opportunities he can take.
FOREIGN_REMOTE = ("usa", "u.s.", "united states", "us only", "remote - us", "us remote",
                  "us - remote", "us-remote", "us based", "u.s. remote",
                  "canada", "canadian", "uk", "united kingdom", "england", "ireland",
                  "germany", "deutschland", "france", "spain", "italy", "netherlands",
                  "sweden", "norway", "denmark", "finland", "poland", "portugal",
                  "switzerland", "austria", "belgium", "czech", "romania", "greece",
                  "australia", "new zealand", "japan", "korea", "singapore", "china",
                  "brazil", "mexico", "argentina", "colombia", "chile", "israel",
                  "uae", "dubai", "saudi", "nigeria", "kenya", "south africa",
                  "emea", "apac", "latam", "europe", "eu only", "north america",
                  # foreign cities that appear as "<city> Remote" (remote-from-there)
                  "toronto", "vancouver", "montreal", "san francisco", "new york",
                  "seattle", "austin", "boston", "chicago", "los angeles", "denver",
                  "london", "berlin", "munich", "paris", "amsterdam", "dublin",
                  "barcelona", "madrid", "lisbon", "warsaw", "stockholm", "zurich",
                  "tokyo", "sydney", "melbourne", "singapore", "tel aviv", "sao paulo")
GLOBAL_REMOTE = ("worldwide", "anywhere", "global", "any location", "fully remote",
                 "work from anywhere", "remote (global)", "international")
# short country tokens need word boundaries: "Remote, US" / "Remote - UK" / "EU remote"
FOREIGN_TOKEN_RE = re.compile(r"(?<![a-z])(us|usa|u\.s\.?|uk|eu|uae|apac|emea|latam)(?![a-z])")

def location_gate(j):
    """Remote is fine when he can actually take it from India: globally-remote or
    India-remote. Remote locked to another country needs work authorization he does
    not hold, so it is treated like onsite abroad. Onsite is fine only in India.
    An uninformative location is delivered with a verify-flag rather than dropped,
    because dropping it silently wipes out whole sources. Returns (ok, tag)."""
    loc = (j.get("location") or "").lower().strip()
    head = (j.get("desc") or "")[:600].lower()
    in_india = bool(INDIA_RE.search(loc)) or bool(INDIA_RE.search(head[:400]))
    is_remote = ("remote" in loc or "work from home" in loc or "wfh" in loc
                 or "anywhere" in loc or "hybrid-remote" in loc or "distributed" in loc
                 or "telecommut" in loc or "home-based" in loc or "home based" in loc
                 or "virtual" in loc)
    if not is_remote:
        # some boards put remote info only in the description head
        is_remote = bool(re.search(r"\bremote\b|work from home|wfh|work anywhere"
                                   r"|fully distributed|telecommut", head))
    if j.get("source") in REMOTE_ONLY_SOURCES:
        is_remote = True  # these APIs only carry remote listings
    if in_india:
        return True, ("Remote India" if is_remote else "India onsite")
    if is_remote:
        if any(g in loc for g in GLOBAL_REMOTE) or any(g in head for g in GLOBAL_REMOTE):
            return True, "Remote worldwide"
        foreign = [c for c in FOREIGN_REMOTE if c in loc]
        tok = FOREIGN_TOKEN_RE.search(loc)
        if foreign or tok:
            where = foreign[0] if foreign else tok.group(1)
            return False, f"remote locked to {where} — work authorization needed"
        return True, None  # plain "Remote" with no country named
    if loc in LOC_UNKNOWN or len(loc) < 3 or LOC_MULTI_RE.match(loc):
        return True, "location unverified"
    if "hybrid" in loc:
        return False, "hybrid abroad"  # hybrid abroad means regular office presence
    return False, "onsite abroad"

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
    # Fee scams demand money FROM the candidate. A bare "processing fee"/"deposit of"
    # substring also appears in legitimate fintech JDs (Stripe, Ramp, Block, Coinbase),
    # so the fee must be tied to an applicant-pays cue before blocking.
    fee_pat = (r"(?:registration|security|training|refundable|processing|application)\s"
               r"(?:fee|deposit)[^.]{0,50}"
               r"(?:required|mandatory|payable|to apply|before (?:joining|interview|start)"
               r"|to (?:join|secure|confirm|register))"
               r"|(?:you|candidates?|applicants?|students?|interns?)\s"
               r"(?:must\s|have to\s|need to\s|will\s|should\s)?"
               r"(?:pay|deposit|submit|transfer)\s[^.]{0,40}"
               r"(?:fee|deposit|charge|registration|amount)"
               r"|(?:pay|deposit|transfer)\s(?:₹|rs\.?|inr)\s?[\d,]+"
               r"|pay to apply|refundable (?:fee|deposit|amount)"
               r"|(?:fee|deposit) of (?:₹|rs\.?|inr)\s?[\d,]+")
    if re.search(fee_pat, body):
        issues.append("pay-to-apply scheme")
    if any(k in body for k in ("commission only", "commission-only", "no fixed salary")):
        issues.append("commission-only role")
    if re.search(r"earn\s+(?:up to\s*)?(?:₹|rs\.?|inr)?\s*[\d,]+\s*(?:per|/)\s*(?:day|week|task|assignment)", body):
        issues.append("earn-per-task scheme")
    if re.search(r"training program[^.]{0,50}(?:fee|charge|paid)|paid training program", body):
        issues.append("training program disguised as job")

    # company identifiable
    comp = (j.get("company") or "").strip().lower()
    if not comp or comp in ("?", "unknown", "confidential company", "private limited 123",
                            "wellfound startup", "indeed employer", "linkedin employer") \
       or len(comp) < 2:
        issues.append("company not identifiable")

    # Staleness is NOT a scam signal and must not block ATS boards: Ashby/Greenhouse/
    # Lever only publish live postings, and `date` there is req-creation, so evergreen
    # reqs look "old" while still hiring (this rule alone was silently killing 148
    # jobs/run, incl. Cohere's Winter-2027 SWE internship). Only aggregator copies,
    # where a stale date really does mean a dead link, are dropped — and only after
    # two years.
    date_s = j.get("date") or ""
    if date_s and j.get("source") not in ("ashby", "greenhouse", "lever",
                                          "smartrecruiters", "netflix", "amazon"):
        try:
            import datetime as dt
            age = (dt.datetime.now(dt.timezone.utc).date() - dt.date.fromisoformat(date_s[:10])).days
            if age > 730:
                issues.append(f"posted {age} days ago — link almost certainly dead")
        except ValueError:
            pass

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
    # A JD too short to describe anything (aggregator placeholders like
    # "Python Development internship via Internshala") cannot prove a match:
    # naming one technology used to buy a perfect 35/35, beating real JDs.
    thin_jd = len(desc) < 250 and not re.search(
        r"requirement|qualification|responsibilit|you will|what you|must have|preferred",
        desc, re.I)
    have_hits = [s for s in have if word_hit(s, req_blob)]
    if thin_jd:
        breakdown["required"] = 20  # neutral: unproven either way
        reasons.append("⚠ Job description not available from this source — "
                       "match estimated from the title; verify requirements yourself")
    else:
        # numerator and denominator must be measured over the SAME text, else a
        # long JD mentioning extra tech elsewhere drags the ratio down
        tech_present = [s for s in (prof.get("have_skills", []) + prof.get("missing_skills", []))
                        if word_hit(s.lower(), req_blob)]
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
        # a match must never score below silence (7): baseline + per-hit credit
        breakdown["preferred"] = min(15, 7 + 3 * len(pref_hits)) if pref_hits else 6
        if pref_hits:
            reasons.append(f"preferred skills you have: {', '.join(pref_hits[:4])}")
    else:
        breakdown["preferred"] = 7  # nothing stated as preferred: neutral
        missing = [s.title() for s in prof.get("missing_skills", []) if word_hit(s, body)]

    # missing skills = JD-named tech you don't have
    missing = [s.title() for s in prof.get("missing_skills", [])
               if word_hit(s, body)] or missing

    # ---- education & eligibility (15)
    # A JD that never states a degree bar is not a rejection — silence gets the same
    # credit as an explicit welcome, and only an explicit bar he cannot meet loses it.
    edu = 0
    hard_bar = re.search(r"\b(?:phd|ph\.d|doctoral|master'?s?\s+degree|m\.?tech|m\.?s\.?\s+in|mba)\b"
                         r"[^.]{0,40}(?:required|must|mandatory)"
                         r"|(?:must|required to) (?:be enrolled in|hold) a (?:master|phd)", body)
    explicit_ok = any(k in body for k in ("bca", "any graduate", "any degree", "bachelor",
                                          "b.tech", "btech", "b.sc", "bsc", "undergraduate",
                                          "all majors", "any discipline"))
    states_degree = bool(re.search(r"\b(?:degree|bachelor|master|phd|b\.?tech|bca|bsc|mca|graduat)", body))
    if hard_bar:
        edu += 0
    elif explicit_ok or not states_degree:
        edu += 8
    else:
        edu += 4  # degree mentioned in terms that neither include nor exclude him
    if any(k in body for k in ("fresher", "student", "pursuing", "no experience",
                               "0-1 years", "entry level", "entry-level", "final year",
                               "new grad", "recent graduate")):
        edu += 4
    if "intern" in title or "internship" in title or "intern" in body[:600]:
        edu += 3
    elif any(k in title for k in ("fresher", "graduate", "campus", "new grad", "university", "trainee")):
        edu += 3
    breakdown["education"] = min(15, edu)

    # ---- projects (15): his two shipped projects are resume facts — credit them by
    # role family, then add JD-stack overlap on top. A JD that simply doesn't list his
    # exact tools does not erase the projects.
    proj_stacks = []
    for p in prof.get("projects", []):
        proj_stacks += [s.lower() for s in p.get("stack", [])]
    proj_hits = [s for s in set(proj_stacks) if word_hit(s, body)]
    ml_role = any(k in title for k in ("ml", "machine learning", "ai", "data scien",
                                       "data analy", "deep learning", "nlp", "llm",
                                       "genai", "computer vision", "research"))
    swe_role = any(k in title for k in ("software", "developer", "engineer", "sde",
                                        "backend", "full stack", "fullstack", "python",
                                        "api", "platform", "technical staff", "swe"))
    base = 10 if ml_role else (8 if swe_role else 4)
    breakdown["projects"] = min(15, base + 2 * len(set(proj_hits)))
    if ml_role:
        reasons.append("your Cricket Performance ML project (49% → 73% accuracy) is direct evidence")
    elif swe_role:
        reasons.append("your FastAPI Student Performance Analyser is direct evidence")
    if proj_hits:
        reasons.append(f"project stack overlap: {', '.join(sorted(set(proj_hits))[:5])}")

    # ---- practical experience (10): project experience, honestly labelled — he has no
    # professional employment yet, so this component never reaches full marks on that
    # basis alone.
    exp = 4 if (ml_role or swe_role) else 2
    if word_hit("fastapi", body) or word_hit("rest api", body):
        exp += 2
        reasons.append("your FastAPI/REST work maps directly")
    if word_hit("scikit-learn", body) or word_hit("pandas", body) or word_hit("numpy", body):
        exp += 2
        reasons.append("your ML/data workflow experience applies")
    if any(k in body for k in ("no experience", "fresher", "0-1 years", "entry level",
                               "entry-level", "student")):
        exp += 2
    if re.search(r"\b([3-9]|1\d)\+?\s*years", body):
        exp = min(exp, 3)  # JD wants real seniority: don't pretend
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
    season = find_season(j)
    if season:
        reasons.append(f"🎯 {season} cohort — applications open ahead of the start date")
    # unusually high pay is a priority signal, not a score bonus
    pay_emoji, pay_note = pay_flag(j)
    if pay_note:
        reasons.insert(0, pay_note)

    # hours/week: college semester allows 15-20; heavier must be flagged not hidden
    hours = find_hours(body)
    if hours is not None:
        if hours <= 20:
            reasons.append(f"{hours} h/week — fits college (you can give ~15-20)")
        elif hours <= 25:
            reasons.append(f"{hours} h/week — slightly above your 15-20 h target")
        else:
            reasons.append(f"⚠ {hours}+ h/week during semester — heavy; decide if feasible")

    # A role he is structurally ineligible for is a WORSE match, and the score has to
    # say so — otherwise mid-level reqs ("Software Engineer L4", "5+ years") outrank
    # real internships. Ceilings, not deductions, so the reasons stay truthful.
    ceiling = 100
    yrs_m = re.search(r"(\d{1,2})\+?\s*(?:-\s*\d{1,2}\s*)?(?:years?|yrs?)"
                      r"[^.;\n]{0,45}?experience", body)
    if yrs_m and not re.search(r"years? of (?:college|university|education|study)", body):
        yrs = int(yrs_m.group(1))
        if yrs >= 4:
            ceiling = min(ceiling, 45)
        elif yrs >= 2:
            ceiling = min(ceiling, 55)
    if re.search(r"\b(?:l[3-9]|level [3-9]|iii|iv|v)\b", title) or \
       re.search(r"\b(?:engineer|developer|scientist|analyst)\s*[3-9]\b", title):
        ceiling = min(ceiling, 55)  # numbered mid-level ladder rung
    if re.search(r"\bph\.?d\b", title) or \
       re.search(r"\b(?:ph\.?d|master'?s)\s+(?:student|candidate)s?\s+"
                 r"(?:doing|pursuing|only|preferred|enrolled)", body):
        ceiling = min(ceiling, 50)  # postgraduate-only posting
    score = max(0, min(ceiling, min(100, sum(breakdown.values()) + adj)))

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
    # stipend keeps original casing so the card reads "Rs 25,000", not "rs 25,000"
    return {
        "stipend": find_stipend(f"{j['title']} {j['desc']}"),
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
    # company marketing ("more than 50 million registered users", "backed by ...")
    # crowds out the actual role; prefer sentences that describe the job itself
    role_cue = re.compile(r"(?i)\b(you(?:'ll| will)?|we(?:'re| are) (?:looking|hiring|seeking)"
                          r"|this (?:role|internship|position)|the (?:role|intern)"
                          r"|responsib|requirement|qualifi|experience with|skills?"
                          r"|intern(?:ship)?\b|stipend|duration|join)")
    ranked = [s for s in sents if role_cue.search(s)] or sents
    pick, total = [], 0
    for s in ranked:
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
        s = re.sub(r"^(?:what you['\u2019]?ll (?:do|work on)|responsibilit(?:y|ies)|"
                   r"key (?:duties|tasks)|you will|your day(?: to day)?)\s*[:\u2013-]?\s*",
                   "", s, flags=re.I)
        s = re.sub(r"^[-–—•\s]+", "", s)
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
    pay_emoji, pay_note = pay_flag(j)
    season = find_season(j)
    L = []
    add = L.append
    add(f"{pay_emoji or '🔥'} {j['title']} — {j['company']}")
    add("")
    add(f"📍 {j['location'] or 'Not specified'}")
    pay_line = f['stipend'] or 'Not disclosed'
    if pay_note:
        # show the normalised monthly figure — raw feed strings drop the period
        pay_line = pay_note.split("— ", 1)[-1]
    add(f"💰 {pay_line}{'  ' + pay_emoji if pay_emoji else ''}")
    add(f"🎓 {f['eligibility'] or 'Not disclosed'}")
    add(f"📅 Posted: {j.get('date') or 'Not specified'}")
    if season:
        add(f"🗓 Cohort: {season}")
    join_line = f['joining'] or ("Joining date unclear — verify before applying"
                                 if j.get('_ft') else "Not specified")
    add(f"🚀 Joining: {join_line}")
    add(f"⭐ Resume Match: {sc}/100 — {match_tier(sc)}")
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
    add(f"👉 {j['url']}")
    add(f"🔎 Source: {j.get('source', 'Not specified')}")
    return "\n".join(L)

def find_season(j):
    """Named internship cohort ('Summer 2027', 'Winter 2026', 'Fall 2027').
    These open months ahead of the start date, so they are a priority signal."""
    body = f"{j.get('title') or ''} {(j.get('desc') or '')[:2500]}"
    m = re.search(r"\b(summer|winter|fall|autumn|spring|monsoon)\s*[-/ ]?\s*(20(?:2[6-9]))\b",
                  body, re.I)
    if m:
        return f"{m.group(1).title()} {m.group(2)}"
    m = re.search(r"\b(20(?:2[6-9]))\s+(summer|winter|fall|autumn|spring)\b", body, re.I)
    if m:
        return f"{m.group(2).title()} {m.group(1)}"
    # "2027 Intern - Machine Learning Engineer" (Adobe/Nvidia style) — cohort year only
    m = re.search(r"\b(20(?:2[6-9]))\s+(?:intern|new college grad|graduate)", body, re.I)
    if m:
        return f"{m.group(1)} cohort"
    return None

# monthly-INR equivalents; USD converted at a deliberately conservative 85/USD
HIGH_PAY_MONTHLY_INR = 60000
EXCEPTIONAL_PAY_MONTHLY_INR = 150000

def pay_monthly_inr(text):
    """Best-effort monthly INR value of a pay string. Returns None when unparseable —
    never guesses, because a wrong number here would fire a false priority alert."""
    if not text:
        return None
    t = text.lower().replace(",", "")
    usd = bool(re.search(r"\$|usd", t))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|to|–)\s*(\d+(?:\.\d+)?)", t)
    num = None
    if m:                                  # take the low end of a range
        num = float(m.group(1))
    else:
        m = re.search(r"(\d+(?:\.\d+)?)", t)
        if m:
            num = float(m.group(1))
    if num is None:
        return None
    if re.search(r"\bk\b|k\s*(?:/|per)", t) and num < 1000:
        num *= 1000
    if re.search(r"lpa|lakhs?|lac\b", t):
        return num * 100000 / 12
    per_year = bool(re.search(r"year|annum|annual|/yr|per yr|pa\b", t))
    per_hour = bool(re.search(r"hour|/hr|hourly", t))
    per_week = bool(re.search(r"week|/wk", t))
    if usd:
        num *= 85
    if per_hour:
        val = num * 160
    elif per_week:
        val = num * 4.3
    elif per_year:
        val = num / 12
    else:
        val = num                          # default: already monthly
    # A monthly figure this large means the currency or period was misread
    # (job feeds mislabel INR as USD). Refuse rather than fire a false alert.
    return None if val > 5000000 else val

def pay_flag(j):
    """(emoji, note) when the stated pay is unusually high, else (None, None)."""
    desc = j.get("desc") or ""
    stipend = find_stipend(desc)
    if not stipend or re.search(r"unpaid|not paid", stipend, re.I):
        return None, None
    ctx = stipend
    if not re.search(r"month|year|annum|annual|hour|week|lpa|lakh|/yr|/hr|pa\b", stipend, re.I):
        # the snippet may have been cut before its period unit ("USD 175000" from
        # "USD 175000-380000 per year") — read a little further in the JD
        at = desc.find(stipend[:20])
        if at >= 0:
            ctx = desc[at:at + len(stipend) + 60]
    val = pay_monthly_inr(ctx)
    if not val:
        return None, None
    if val >= EXCEPTIONAL_PAY_MONTHLY_INR:
        return "💎💎", (f"💎💎 EXCEPTIONAL PAY — {stipend} "
                       f"(~₹{int(val):,}/month equivalent)")
    if val >= HIGH_PAY_MONTHLY_INR:
        return "💎", f"💎 HIGH PAY — {stipend} (~₹{int(val):,}/month equivalent)"
    return None, None

def split_full_rest(to_send, cfg):
    """Top N jobs get the full card; everything else goes in the compact digest.
    High-pay finds are promoted into the full-card tier regardless of rank — the
    user asked to be told about those immediately."""
    n = int(cfg.get("full_cards_per_run", 3))
    priority = [x for x in to_send if any("💎" in r for r in x[2])]
    rest = [x for x in to_send if x not in priority]
    top = priority + rest[:max(0, n - len(priority))]
    return top, [x for x in to_send if x not in top]

def compact_line(sc, j, r, m):
    """2-line compact entry for the digest message."""
    pay_emoji, _ = pay_flag(j)
    badge = pay_emoji or ("🔥" if sc >= 85 else "🟢" if sc >= 70 else "🟡")
    tags = []
    season = find_season(j)
    if season:
        tags.append("🗓 " + season)
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
    if join and len(tags) < 3:
        tags.append("🚀 " + join)
    # essentials so a digest line alone is actionable (user: don't be so quick
    # that essentials are lost) — stipend + deadline before cosmetic tags
    stp = find_stipend(j.get("desc") or "")
    if stp:
        stp = ("⚠ UNPAID" if re.search(r"unpaid|not paid", stp, re.I) else "💰 " + stp)
        if stp not in tags and len(tags) < 4:
            tags.append(stp[:40])
    dl = find_deadline(j)
    if dl and len(tags) < 5:
        tags.append("📅 " + dl[:18])
    if any(x in (j.get("eligibility") or "") for x in ("BCA", "bca", "B.Sc", "Bachelors")):
        if len(tags) < 5:
            tags.append("🎓 BCA-friendly")
    loc = j.get("location", "") or ""
    tag_txt = (" · " + " · ".join(t for t in tags if t)) if tags else ""
    line1 = f"{badge} [{sc}%] {j['title']} — {j['company']}{tag_txt}"
    line2 = f"   {loc} · {j['url']}" if loc else f"   {j['url']}"
    return line1[:220] + "\n" + line2[:200]

def compact_digest(items):
    """One compact list: 2 lines per job, ~150 chars each."""
    return "\n\n".join(compact_line(sc, j, r, m) for sc, j, r, m in items)

# ------------------------------------------------------------------- main
def main():
    prof = load_json(BASE / "profile.json", {})
    cfg = load_json(BASE / "telegram.json", {}) or {}  # legacy email.json also still works
    # Credentials are normally empty here (they live in GitHub Secrets); that
    # must NOT discard the non-credential knobs in telegram.json (min_score,
    # full_cards_per_run, internshala_min_score). Merge instead of replace.
    tg_token = cfg.get("telegram_bot_token") or ""
    tg_chat = cfg.get("telegram_chat_id") or ""
    if not tg_token or not tg_chat:
        legacy = load_json(BASE / "email.json", {}) or {}
        tg_token = tg_token or legacy.get("telegram_bot_token") or ""
        tg_chat = tg_chat or legacy.get("telegram_chat_id") or ""
        for k, v in legacy.items():
            if k not in cfg:
                cfg[k] = v
    cfg["telegram_bot_token"], cfg["telegram_chat_id"] = tg_token, tg_chat
    cfg.setdefault("full_cards_per_run", 3)
    cfg.setdefault("min_score", 60)
    # Internshala is demoted (per user): needs a higher bar to earn an alert.
    cfg.setdefault("internshala_min_score", 70)
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
    # US giants/startups plus boards that actually post in India (verified live)
    prof["_gh_companies"] = ["stripe", "databricks", "figma", "cloudflare", "mongodb",
                             "twilio", "robinhood", "reddit", "dropbox", "coinbase",
                             "nuro", "roblox", "samsara", "flexport", "block",
                             "gitlab", "netradyne", "postman"]
    prof["_lever_companies"] = ["spotify", "zeta", "paytm", "mindtickle"]
    prof["_ashby_companies"] = ["elevenlabs", "Perplexity", "Cohere", "Runway",
                                "baseten", "openai", "notion", "ramp", "modal",
                                "sarvam", "composio", "spotdraft"]
    # Workday-hosted big-tech career sites (label, host, tenant, site) — all verified live
    prof["_workday_tenants"] = [
        ("nvidia", "nvidia.wd5", "nvidia", "NVIDIAExternalCareerSite"),
        ("adobe", "adobe.wd5", "adobe", "external_experienced"),
        ("salesforce", "salesforce.wd12", "salesforce", "External_Career_Site"),
        ("micron", "micron.wd1", "micron", "External"),
        ("hpe", "hpe.wd5", "hpe", "Jobsathpe"),
        ("autodesk", "autodesk.wd1", "autodesk", "uni"),
        ("paypal", "paypal.wd1", "paypal", "jobs"),
        ("ebay", "ebay.wd5", "ebay", "apply"),
    ]
    SLUGS = {}
    for c in prof["_sr_companies"] + prof["_gh_companies"] + prof["_lever_companies"] + prof["_ashby_companies"]:
        SLUGS[c.lower()] = c.replace("-", " ").title()
    SLUGS.update({"openai": "OpenAI", "huggingface": "Hugging Face", "deepmind": "DeepMind",
                  "perplexity": "Perplexity", "elevenlabs": "ElevenLabs",
                  "runway": "Runway", "cohere": "Cohere", "modal": "Modal",
                  "baseten": "Baseten", "deepl": "DeepL", "notion": "Notion",
                  "ramp": "Ramp", "gitlab": "GitLab", "netradyne": "Netradyne",
                  "postman": "Postman", "sarvam": "Sarvam AI", "composio": "Composio",
                  "spotdraft": "SpotDraft", "zeta": "Zeta", "paytm": "Paytm",
                  "mindtickle": "Mindtickle", "nvidia": "NVIDIA", "adobe": "Adobe",
                  "salesforce": "Salesforce", "micron": "Micron", "hpe": "HPE",
                  "autodesk": "Autodesk", "paypal": "PayPal", "ebay": "eBay"})
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

    # de-dup + score (score floor 60; location gate; scam filter)
    seen_ids, scored = [], []
    now_iso = time.strftime("%Y-%m-%d %H:%M")
    n_qual = n_hard = n_loc = n_score = 0
    # cross-source dedup + persistent fingerprint (company+role normalized);
    # prefer official ATS/company URLs over aggregator copies.
    def dedup_key(j):
        role = re.sub(r"[^a-z0-9 ]", "", j["title"].lower())
        role = " ".join(w for w in role.split() if w not in
                        ("intern", "internship", "remote", "part", "time", "full"))
        comp = re.sub(r"[^a-z0-9 ]", "", j["company"].lower())
        return (comp, role[:60])
    ATS_PRIORITY = ("ashby", "greenhouse", "lever", "workday", "smartrecruiters",
                    "netflix", "amazon", "google", "oracle", "yc", "remotive",
                    "remoteok", "jobicy", "arbeitnow", "internshala", "wellfound",
                    "linkedin", "indeed", "youtube")

    def ats_rank(src):
        # unknown sources sort last instead of crashing the run
        return ATS_PRIORITY.index(src) if src in ATS_PRIORITY else len(ATS_PRIORITY)
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
            if ats_rank(j["source"]) < ats_rank(kept["source"]):
                by_key[k] = j
            continue
        by_key[k] = j
    for j in by_key.values():
        # Scams/clearly-fake filter — these are never delivered.
        q = quality_check(j, prof)
        if q:
            n_qual += 1
            continue
        # Location gate: remote always OK; onsite OK only in India; onsite abroad skip.
        ok, _tag = location_gate(j)
        if not ok:
            n_loc += 1
            continue
        sc, reasons, missing, is_ft = score_job(j, prof, cfg)
        # Score floor: below 60% is not worth your attention (per user rule).
        # Internshala needs a higher bar (demoted source).
        floor = int(cfg.get("internshala_min_score", 70)) if j["source"] == "internshala" \
            else int(cfg.get("min_score", 60))
        if sc < floor:
            n_score += 1
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
    log(f"scored {len(scored)} deliverable of {len(all_jobs)} raw "
        f"(blocked: {n_qual} scam/fake, {n_loc} onsite-abroad, {n_score} below-score)")

    # No score-tier splitting: every fresh job is delivered.
    # Persistent fingerprints stop Internshala reposts (same role+company, new URL
    # timestamp) from re-alerting across runs. fps keys are "company|role" strings.
    fps = seen.get("fps", {}) or {}

    def fp_of(j):
        c, r = dedup_key(j)
        return f"{c}|{r}"

    to_send = []
    for x in scored:
        sc, j, r, m = x
        if j["id"] in seen:
            continue
        if fp_of(j) in fps:
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
            pe, _ = pay_flag(j)
            head = f"{pe} HIGH PAY" if pe else "🔥"
            subj = f"{head} [{sc}%] {j['title']} at {j['company']}"
            if deliver(cfg, subj, alert_text(j, sc, r, m)):
                sent += 1
                seen[j["id"]] = now_iso
                fps[fp_of(j)] = now_iso
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
                fps[fp_of(j)] = now_iso
        seen["fps"] = fps
        log(f"delivered {sent} opportunities ({len(top)} full cards + {len(rest)} compact)")

    # prune old entries so seen.json stays small. NOTE: 'fps' holds a dict,
    # so it must be pruned separately — sorting mixed str/dict values crashes.
    if len(seen) > 5000:
        fps_rec = seen.pop("fps", {})
        for k in sorted(seen, key=seen.get)[:1500]:
            seen.pop(k)
        if len(fps_rec) > 3000:
            for k in sorted(fps_rec, key=fps_rec.get)[:1000]:
                fps_rec.pop(k)
        seen["fps"] = fps_rec
    save_json(SEEN_FILE, seen)
    # a silent exit code keeps the Actions run green; delivery failures are already logged
    log(f"done: seen={len(seen)} sent={sent} scored={len(scored)}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
