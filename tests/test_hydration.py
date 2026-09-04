"""Detail-page hydration tests.

Four adapters (linkedin, wellfound, internshala, netflix) emit rows whose `desc`
is empty or a synthetic one-liner. Every alert-card fact is regex-extracted from
`desc` in job_facts(), so those rows print "Not disclosed" on every line -- and
score_job's thin_jd penalty quietly suppresses them as well.

Fixtures under tests/fixtures/ are real slices captured live from each source, so
these tests exercise the same markup the adapters meet in production.

Run: python -m unittest discover -s tests -v
"""
import datetime as dt
import importlib.util
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FX = pathlib.Path(__file__).resolve().parent / "fixtures"

_spec = importlib.util.spec_from_file_location("scout", ROOT / "scout.py")
scout = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scout)


def bare(src="linkedin", title="AI/ML Intern", company="Fairdeal.Market",
         loc="Gurgaon, India"):
    """A row exactly as the un-hydrated adapters emit it: no desc, no date."""
    return scout.J(src, "1", title, company, loc,
                   "https://www.linkedin.com/jobs/view/1", "", "")


class TestTheBug(unittest.TestCase):
    """Reproduction: this is what the user sees on Telegram today."""

    def test_empty_desc_blanks_every_card_fact_at_once(self):
        j = bare()
        facts = scout.job_facts(j)
        for key in ("stipend", "eligibility", "deadline", "joining", "hours", "duration"):
            self.assertIsNone(facts[key], "%s should be unavailable with no desc" % key)
        self.assertEqual(scout.summarize_jd(j), "Not disclosed")
        self.assertEqual(scout.deadline_line(j), "⚠️ Deadline: Not specified")
        self.assertEqual(j["date"], "")


class TestRelativeDate(unittest.TestCase):
    """LinkedIn only ever gives a relative posted date."""

    def setUp(self):
        self.today = dt.date(2026, 9, 4)

    def test_hours_and_days(self):
        f = scout.relative_date_to_iso
        self.assertEqual(f("5 hours ago", self.today), "2026-09-04")
        self.assertEqual(f("1 hour ago", self.today), "2026-09-04")
        self.assertEqual(f("2 days ago", self.today), "2026-09-02")
        self.assertEqual(f("30+ days ago", self.today), "2026-08-05")

    def test_weeks_and_months(self):
        f = scout.relative_date_to_iso
        self.assertEqual(f("3 weeks ago", self.today), "2026-08-14")
        self.assertEqual(f("1 month ago", self.today), "2026-08-05")

    def test_noise_returns_empty_not_a_crash(self):
        f = scout.relative_date_to_iso
        for junk in ("", None, "Be an early applicant", "yesterday-ish"):
            self.assertEqual(f(junk, self.today), "")


class TestLinkedInDetail(unittest.TestCase):
    def setUp(self):
        self.page = (FX / "linkedin_detail.html").read_text(encoding="utf-8")

    def test_parses_desc_posted_and_criteria(self):
        got = scout.parse_linkedin_detail(self.page)
        self.assertGreater(len(got["desc"]), 2000)
        self.assertIn("Fairdeal", got["desc"])
        self.assertNotIn("<", got["desc"], "markup must be stripped")
        self.assertRegex(got["date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertIn("full-time", got["employment_type"].lower())
        self.assertIn("engineering", got["criteria"].lower())

    def test_criteria_deduplicated(self):
        """The guest page renders the criteria list twice (mobile + desktop)."""
        crit = scout.parse_linkedin_detail(self.page)["criteria"]
        self.assertEqual(crit.lower().count("entry level"), 1)

    def test_missing_markup_is_not_fatal(self):
        got = scout.parse_linkedin_detail("<html><body>nothing here</body></html>")
        self.assertEqual(got["desc"], "")
        self.assertEqual(got["date"], "")


class TestLinkedInHydratedRow(unittest.TestCase):
    def test_real_card_facts_appear(self):
        j = bare()
        got = scout.parse_linkedin_detail((FX / "linkedin_detail.html").read_text(encoding="utf-8"))
        j["desc"], j["date"] = got["desc"], got["date"]
        facts = scout.job_facts(j)
        self.assertIsNotNone(facts["eligibility"])
        self.assertIn("pursuing", facts["eligibility"].lower())
        self.assertNotEqual(scout.summarize_jd(j), "Not disclosed")
        self.assertNotEqual(j["date"], "")

    def test_absent_stipend_stays_absent(self):
        """This JD genuinely names no pay and no deadline. Honest absence is not
        the bug -- fabricating a number would be worse than 'Not disclosed'."""
        j = bare()
        got = scout.parse_linkedin_detail((FX / "linkedin_detail.html").read_text(encoding="utf-8"))
        j["desc"] = got["desc"]
        facts = scout.job_facts(j)
        self.assertIsNone(facts["stipend"])
        self.assertIsNone(facts["deadline"])


class TestJsonLdJobPosting(unittest.TestCase):
    def test_finds_posting_in_internshala_page(self):
        ld = scout.jsonld_jobposting((FX / "internshala_detail.html").read_text(encoding="utf-8"))
        self.assertIsNotNone(ld)
        self.assertEqual(ld.get("@type"), "JobPosting")

    def test_finds_posting_in_wellfound_page(self):
        ld = scout.jsonld_jobposting((FX / "wellfound_detail.html").read_text(encoding="utf-8"))
        self.assertIsNotNone(ld)
        self.assertEqual(ld.get("@type"), "JobPosting")

    def test_handles_graph_wrapping_and_skips_malformed(self):
        page = ('<script type="application/ld+json">{not json</script>'
                '<script type="application/ld+json">{"@graph":[{"@type":"WebSite"},'
                '{"@type":"JobPosting","title":"Deep"}]}</script>')
        self.assertEqual(scout.jsonld_jobposting(page)["title"], "Deep")

    def test_handles_top_level_array(self):
        page = ('<script type="application/ld+json">'
                '[{"@type":"Organization"},{"@type":["JobPosting"],"title":"Arr"}]</script>')
        self.assertEqual(scout.jsonld_jobposting(page)["title"], "Arr")

    def test_no_posting_returns_none(self):
        self.assertIsNone(scout.jsonld_jobposting("<html>no ld+json at all</html>"))


class TestApplyJobPosting(unittest.TestCase):
    def internshala(self):
        j = bare(src="internshala", title="Ai Automation Developer Intern",
                 company="Perform Digital", loc="India (Internshala)")
        ld = scout.jsonld_jobposting((FX / "internshala_detail.html").read_text(encoding="utf-8"))
        scout.apply_jobposting(j, ld)
        return j

    def test_internshala_fills_desc_and_date(self):
        j = self.internshala()
        self.assertGreater(len(j["desc"]), 1500)
        self.assertLessEqual(len(j["desc"]), 8000)
        self.assertEqual(j["date"], "2026-08-10")

    def test_internshala_structured_salary_reaches_the_card(self):
        facts = scout.job_facts(self.internshala())
        self.assertIsNotNone(facts["stipend"], "INR 6000-10000/MONTH baseSalary must surface")
        self.assertIn("6000", facts["stipend"].replace(",", ""))

    def test_internshala_stipend_keeps_the_whole_range(self):
        """"Stipend: INR 6,000 - 10,000 /month" was cut to "INR 6,000": the amount
        pattern had no range branch, so it stopped at the lower bound and lost the
        period too. A rate with no period is not a rate."""
        facts = scout.job_facts(self.internshala())
        flat = facts["stipend"].replace(",", "").replace(" ", "").lower()
        self.assertIn("6000", flat)
        self.assertIn("10000", flat, "the upper bound is half the offer")
        self.assertIn("month", flat, "the card must say what the amount is per")

    def test_bare_currency_range_keeps_its_upper_bound(self):
        """The keyword-less amount pattern truncated ranges the same way."""
        got = scout.find_stipend("Interns get ₹20,000 - 35,000 per month here.")
        self.assertIsNotNone(got)
        self.assertIn("35,000", got)

    def test_internshala_validthrough_reaches_the_card(self):
        j = self.internshala()
        self.assertIsNotNone(scout.find_deadline(j), "validThrough must surface")
        self.assertNotEqual(scout.deadline_line(j), "⚠️ Deadline: Not specified")

    def test_internshala_card_has_no_not_disclosed_left(self):
        j = self.internshala()
        self.assertNotEqual(scout.summarize_jd(j), "Not disclosed")
        card = scout.alert_text(j, 72, ["skills match"], [])
        self.assertNotIn("Not disclosed", card)

    def test_internshala_start_window_reaches_the_card(self):
        """"can start the internship between 7th Aug'26 and 11th Sep'26" is a real
        joining date. It is day-first with an apostrophe year, so neither the
        cohort pattern (needs a literal 2026) nor the 'starting <Mon DD, YYYY>'
        pattern ever matched it, and the card printed "Not specified"."""
        facts = scout.job_facts(self.internshala())
        self.assertIsNotNone(facts["joining"], "start window must surface")
        self.assertIn("aug", facts["joining"].lower())
        self.assertIn("Aug", facts["joining"],
                      "the card quotes the JD, so keep the JD's capitalisation")

    def test_internshala_eligibility_is_a_clean_phrase(self):
        """This JD is one punctuation-free run of requirements, so a trailing
        100-char window swallows the next two fields whole and the card shows
        "...students Stipend: INR 6,000 ... Deadline: 2026-09-09"."""
        facts = scout.job_facts(self.internshala())
        self.assertIn("computer science", facts["eligibility"].lower())
        for leaked in ("stipend", "deadline", "interests"):
            self.assertNotIn(leaked, facts["eligibility"].lower(),
                             "%r is a different field, not eligibility" % leaked)

    def wellfound(self):
        j = bare(src="wellfound", title="Software Engineer Clone At Swara",
                 company="Wellfound startup", loc="Wellfound (varies)")
        ld = scout.jsonld_jobposting((FX / "wellfound_detail.html").read_text(encoding="utf-8"))
        scout.apply_jobposting(j, ld)
        return j

    def test_wellfound_replaces_invented_company_and_location(self):
        j = self.wellfound()
        self.assertEqual(j["company"], "SWARA")
        self.assertGreater(len(j["desc"]), 2000)
        self.assertEqual(j["date"], "2026-09-03")
        self.assertNotEqual(j["location"].lower(), "wellfound (varies)")

    def test_empty_posting_leaves_row_untouched(self):
        j = bare()
        scout.apply_jobposting(j, {})
        self.assertEqual(j["desc"], "")
        self.assertEqual(j["company"], "Fairdeal.Market")


class TestGateStaysHonest(unittest.TestCase):
    """Hydration must add information, never lower a bar."""

    def test_country_locked_remote_is_no_longer_laundered(self):
        j = bare(src="wellfound", loc="Wellfound (varies)")
        ok_before, _ = scout.location_gate(j)
        self.assertTrue(ok_before, "placeholder currently passes as location-unverified")
        ld = scout.jsonld_jobposting((FX / "wellfound_detail.html").read_text(encoding="utf-8"))
        scout.apply_jobposting(j, ld)
        self.assertNotIn(j["location"].lower(), scout.LOC_UNKNOWN)
        ok_after, tag = scout.location_gate(j)
        self.assertFalse(ok_after, "US/AU/CA-locked remote must be blocked, got %r" % (tag,))


class TestNetflixDetail(unittest.TestCase):
    def test_detail_payload_yields_description(self):
        payload = json.loads((FX / "netflix_detail.json").read_text(encoding="utf-8"))
        desc = scout.netflix_desc(payload)
        self.assertGreater(len(desc), 1000)
        self.assertLessEqual(len(desc), 8000)
        self.assertNotIn("<", desc)

    def test_missing_fields_yield_empty_string(self):
        self.assertEqual(scout.netflix_desc({}), "")
        self.assertEqual(scout.netflix_desc({"job_description": None}), "")


class TestHydrateJobs(unittest.TestCase):
    def test_respects_cap(self):
        jobs = [bare() for _ in range(10)]
        seen = []
        scout.hydrate_jobs(jobs, seen.append, cap=4, label="t", pause=0)
        self.assertEqual(len(seen), 4)

    def test_one_dead_page_does_not_abort_the_source(self):
        jobs = [bare() for _ in range(5)]
        hit = []

        def fetch(j):
            hit.append(j)
            if len(hit) == 2:
                raise TimeoutError("flaky detail page")
            j["desc"] = "filled"

        scout.hydrate_jobs(jobs, fetch, cap=5, label="t", pause=0)
        self.assertEqual(len(hit), 5, "remaining jobs must still be fetched")
        self.assertEqual([bool(j["desc"]) for j in jobs], [True, False, True, True, True])

    def test_returns_attempted_count(self):
        jobs = [bare() for _ in range(3)]
        self.assertEqual(scout.hydrate_jobs(jobs, lambda j: None, cap=99, label="t", pause=0), 3)


class TestJdTextKeepsListBoundaries(unittest.TestCase):
    """clean() replaced every tag with a single space, so a bulleted JD arrived
    downstream as one unpunctuated run. Every card-fact regex bounds itself on
    `.`, `;`, `|` or a newline -- with none of those left, find_eligibility's
    100-char tail ran straight into the next bullet and was cut mid-word, and
    extract_responsibilities had no sentence boundary to split on at all."""

    def linkedin(self):
        j = bare()
        j["desc"] = scout.parse_linkedin_detail(
            (FX / "linkedin_detail.html").read_text(encoding="utf-8"))["desc"]
        return j

    def test_block_tags_become_sentence_boundaries(self):
        got = scout.clean_jd("<ul><li>First item</li><li>Second item</li></ul>")
        self.assertIn("First item.", got)
        self.assertNotIn("item Second", got, "two bullets must not merge")

    def test_existing_punctuation_is_not_doubled(self):
        got = scout.clean_jd("<p>Ends already.</p><p>Next one!</p><li>Or here?</li>")
        self.assertNotIn("..", got)
        self.assertNotIn("!.", got)
        self.assertNotIn("?.", got)

    def test_inline_tags_still_collapse_to_nothing(self):
        """Only block-level boundaries are sentences; <strong> inside a phrase
        is not, and must not chop the phrase in half."""
        got = scout.clean_jd("Needs <strong>Python</strong> and <em>SQL</em>")
        self.assertIn("Needs Python and SQL", got)

    def test_eligibility_stops_at_its_own_bullet(self):
        got = scout.find_eligibility(self.linkedin())
        self.assertIsNotNone(got)
        self.assertIn("related field", got)
        self.assertNotIn("self-directed", got.lower(),
                         "that is the next bullet, not an eligibility requirement")
        self.assertFalse(got.endswith("…"),
                         "a requirement that fits in the line needs no ellipsis")

    def test_phrase_split_across_a_list_close_is_not_a_sentence(self):
        """LinkedIn closes the list mid-phrase and drops the remainder outside it:
        "framework (PyTorch /<br><br></li></ul>TensorFlow)<br><br><ul><li> Comfort".
        A block boundary is only a sentence boundary when the text before it could
        end a sentence -- "/" cannot, so this one is intra-sentential."""
        got = scout.clean_jd("<ul><li>one framework (PyTorch /<br><br></li></ul>"
                             "TensorFlow)<br><br><ul><li> Comfort with LLMs</li></ul>")
        self.assertIn("(PyTorch / TensorFlow)", got)
        self.assertNotIn("PyTorch /.", got)
        self.assertIn("TensorFlow). Comfort", got, "the next bullet is still a bullet")

    def test_dangling_connective_does_not_end_a_sentence(self):
        """Same markup shape, but the break lands after a word: "...and when not"
        + "to use ML". No English sentence ends in "not", and the continuation
        starts lower-case, so this is one clause, not two."""
        got = scout.clean_jd("<ul><li>evaluation design, and when not"
                             "<br><br></li></ul>to use ML<br><br>"
                             "<ul><li> Applied experience with pandas</li></ul>")
        self.assertIn("when not to use ML", got)
        self.assertNotIn("when not.", got)

    def test_lowercase_bullets_still_get_their_boundary(self):
        """Internshala writes every requirement as a lower-case fragment. Those are
        real bullets, so "starts lower-case" alone must not suppress a boundary --
        only a dangling connective before it does."""
        got = scout.clean_jd("<li>are available for duration of 6 months</li>"
                             "<li>have relevant skills and interests</li>")
        self.assertIn("6 months. have relevant skills", got)

    def test_responsibilities_are_real_bullets(self):
        got = scout.extract_responsibilities(self.linkedin())
        self.assertNotIn("As described in the posting — see direct link", got,
                         "the JD ships 27 <li> bullets; the fallback is a bug")
        self.assertGreaterEqual(len(got), 2)


class TestStartWindowPhrasings(unittest.TestCase):
    """The start-window pattern was written against the fixture's "can start the
    internship between 7th Aug'26", so it only allowed one word between "start" and
    the date preposition. Live Internshala names the whole engagement there -- "can
    start the work from home job/internship between 3rd Sep'26 and 8th Oct'26" -- and
    every such row printed "Joining: Not specified" while the fixture test passed."""

    def joining(self, sentence):
        j = bare(src="internshala")
        j["desc"] = "About the internship. Who can apply: " + sentence
        return scout.find_joining(j)

    def test_work_from_home_phrasing(self):
        got = self.joining("can start the work from home job/internship between "
                           "3rd Sep'26 and 8th Oct'26.")
        self.assertIsNotNone(got, "the live phrasing must resolve")
        self.assertIn("Sep", got)
        self.assertIn("Oct", got, "the window's far end is half the answer")

    def test_fixture_phrasing_still_works(self):
        got = self.joining("can start the internship between 7th Aug'26 and 11th Sep'26.")
        self.assertIsNotNone(got)
        self.assertIn("Aug", got)

    def test_in_office_phrasing(self):
        got = self.joining("can start the full time (in-office) internship between "
                           "1st Oct'26 and 5th Nov'26.")
        self.assertIsNotNone(got)
        self.assertIn("Oct", got)

    def test_it_does_not_cross_a_sentence_boundary(self):
        """"start" in one sentence and a date in the next are unrelated; matching
        across the full stop would invent a joining date the JD never gave."""
        self.assertIsNone(self.joining("You will start soon. Apply between 3rd Sep'26 "
                                       "and 8th Oct'26."))

    def test_it_does_not_cross_a_clause_boundary(self):
        """A connective opens a new clause, so a date after it belongs to that clause
        and not to "start". Without this guard the application-open date was reported
        as the joining date."""
        self.assertIsNone(self.joining("The programme starts and applications are "
                                       "open from 1st Jan'27."))
        self.assertIsNone(self.joining("The role starts, and the deadline is from "
                                       "2nd Feb'27."))

    def test_a_real_start_after_a_sentence_break_still_resolves(self):
        """Guarding the clause must not cost us the plain phrasing."""
        got = self.joining("Applications are open. You will start on 12th Dec'26.")
        self.assertIsNotNone(got)
        self.assertIn("Dec", got)


class TestResponsibilitiesEndCleanly(unittest.TestCase):
    """The 700-char cue window and the 150-char bullet cap both cut mid-word, so the
    live YC card printed "...tool use, TTS, and full-conversation " and "Design voice
    agent behavio" as two of its three bullets. A bullet that stops mid-word reads as
    broken data, which is the complaint this work started from.

    The desc is sized to land both cuts: the first bullet is 154 chars (over the 150
    cap, under the 160 reject that would otherwise drop it), and the cue window's
    700th character falls inside "behaviour"."""

    CAP = ("Building and maintaining the eval framework that scores voice agent quality "
           "across transcription, LLM reasoning, tool use, TTS and full-conversation flow.")

    def bullets(self):
        j = bare()
        j["desc"] = ("About us. Responsibilities: " + self.CAP + " "
                     + "Traces get reviewed weekly. " * 18 + "We demo too. "
                     + "Design voice agent behaviour end to end with the research team."
                     + " We also ship dashboards.")
        return scout.extract_responsibilities(j)

    def test_no_bullet_stops_mid_word(self):
        for b in self.bullets():
            self.assertRegex(b, r"(?:[.!?\u2026]|[a-z]\))$",
                             "bullet ends mid-word: %r" % b)

    def test_the_window_tail_is_not_shipped_as_a_bullet(self):
        """"Design voice agent behavio" is the cue window's own truncated tail, not a
        sentence the JD wrote. Trimming the window back to its last terminator drops
        the fragment instead of printing it."""
        for b in self.bullets():
            self.assertFalse(b.endswith("behavio"),
                             "window tail shipped as a bullet: %r" % b)

    def test_a_capped_bullet_says_it_was_cut(self):
        long_one = [b for b in self.bullets() if len(b) > 120]
        self.assertTrue(long_one, "the 154-char bullet must still be reported")
        self.assertTrue(long_one[0].endswith("\u2026"),
                        "a cut bullet must show it was cut, got %r" % long_one[0])
        self.assertFalse(long_one[0].endswith(" \u2026"))
        self.assertNotIn("conversation f", long_one[0],
                         "the cap must not stop inside a word")

    def test_short_whole_bullets_are_untouched(self):
        j = bare()
        j["desc"] = ("Responsibilities: Build data pipelines for the research team. "
                     "Develop dashboards that surface model drift.")
        got = scout.extract_responsibilities(j)
        self.assertIn("Build data pipelines for the research team.", got)

    def test_no_cue_scans_the_opening_not_the_whole_body(self):
        """With no cue word there is no window, and falling back to the entire desc
        pulls "bullets" out of benefits and legal boilerplate far below the role. The
        fallback reads the opening only, where the role is actually described."""
        j = bare()
        j["desc"] = ("We build developer tools for teams that ship fast. " * 3
                     + "Filler that says nothing. " * 40
                     + "We will not tolerate discrimination and we support equal "
                       "opportunity for every applicant.")
        got = scout.extract_responsibilities(j)
        self.assertTrue(all("discrimination" not in b for b in got),
                        "boilerplate from the JD's tail became a bullet: %r" % got)
        for b in got:
            self.assertNotIn("\u2026", b)


if __name__ == "__main__":
    unittest.main()
