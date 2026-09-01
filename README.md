# Media Monitor

Tracks news coverage of a subject across Marathi, Hindi and English publishers.

Articles are discovered from each publisher's **news sitemap** — the feed
publishers maintain for machine consumption — then filtered to a date window and
keyword-matched. This replaces the earlier Selenium/Google-SERP notebooks.

## Files

| File | Purpose |
|---|---|
| `news_monitor.py` | Fetcher, sitemap parser, matcher, discovery pipeline |
| `corpus.py` | Full-text extraction, boilerplate stripping, near-duplicate removal |
| `relevance.py` | Cross-encoder scoring of each article against the subject |
| `report_llm.py` | Prompt, LangChain chain, Gemini call — the written report |
| `profiles.py` | Entity profiles — subject, search terms, per-entity system prompt |
| `profiles.json` | Where those profiles live, briefs included (seeded once, then yours) |
| `store.py` | Run storage under `runs/` |
| `server.py` | FastAPI app: the UI, the run endpoints, the profile CRUD |
| `static/index.html` | The whole frontend — one file, no build step |
| `run_pipeline.py` | CLI entry point for the same pipeline |
| `news_monitor.ipynb` | Driver notebook, for poking at discovery directly |

Everything else the project writes — `runs/`, `.cache/`, logs, spreadsheets — is
generated and git-ignored. `.env` holds `GEMINI_API_KEY` and is ignored too;
`profiles.json` is deliberately **not** ignored, since the entities you monitor
and their briefs are configuration worth keeping.

## Install

    pip install -r requirements.txt

`requirements.txt` deliberately leaves out torch: the PyPI wheel pulls ~2.5 GB
of CUDA libraries that are dead weight on a CPU box. Install the CPU build
first if you want the small one:

    pip install --index-url https://download.pytorch.org/whl/cpu torch==2.10.0

Then put your key in `.env` at the project root:

    GEMINI_API_KEY=...

Run the UI with `python3 server.py` and open <http://127.0.0.1:8000>.

Note that `server.py` imports `news_monitor`, `corpus` and `relevance` once at
first use and Python caches them, so **edits to those modules need a server
restart** to take effect.

## Use

```python
import news_monitor as nm

df = nm.run(
    keywords   = ["Devendra Fadnavis", "Fadnavis", "देवेंद्र फडणवीस", "फडणवीस"],
    start_date = "08/30/2026",
    end_date   = "08/31/2026",
    user_agent = "MediaMonitor/1.0 (+contact: you@example.com)",
)
df.to_excel("out.xlsx", index=False)
```

## How it fetches

- **robots.txt is enforced** for every URL, using `protego`. The stdlib
  `RobotFileParser` is not used: it treats a leading `*` in a Disallow path as
  matching every URL, which turns a rule like `Disallow: */reporter/*` into a
  site-wide block and silently drops legitimate sources.
- **Rate limited per domain** — `delay` seconds plus jitter between hits, and a
  site's own `Crawl-delay` wins if it is longer.
- **Backs off** on 429/5xx with exponential delay; gives up on 403/404 rather
  than retrying.
- **Caches** every response under `.cache/pages`, so re-runs cost nothing.
  Delete that directory to force a refresh.

## Matching

Two stages, to keep fetch volume low:

1. **Shallow** — match against the sitemap title, `news:keywords`, and the URL
   slug. Free; catches articles where the subject is the headline.
2. **Deep** (`deep=True`) — fetch the remaining candidates and match against
   body text. This is what catches passing mentions, and it is the reason the
   monitor beats headline-only matching. `deep_limit` caps pages per run.

Latin terms match on word boundaries; Devanagari matches on substring, since it
has no case or word-boundary conventions to rely on.

## Source coverage

| Source | Lang | Sitemap reach |
|---|---|---|
| ABP Live | hi/en | **~30 days**, sharded by date |
| ABP Majha | mr | current window |
| Maharashtra Times | mr | last 48h |
| Loksatta | mr | last 48h |
| eSakal | mr | last 48h |
| Lokmat | mr | ~100 most recent articles |
| Sarkarnama | mr | last 48h |
| Saam TV | mr | last 48h |
| TV9 Marathi | mr | last 48h |
| News18 Marathi | mr | last 48h |
| Pudhari | mr | **daily shards**, backfills a past window |
| Navarashtra | mr | ~350 most recent articles (~4 weeks) |
| Lokshahi | mr | ~200 most recent articles (~3 weeks) |
| Tarun Bharat | mr | ~1000-article shards, backfills months |
| Navbharat Times | hi | last 48h |
| Times Now | en | last 48h |
| NDTV / NDTV Marathi | en/mr | **blocked** — see below |

**This is the main constraint.** Only ABP Live, Pudhari and Tarun Bharat can
backfill a past week. Most other sources expose roughly the last 48 hours, so a
query for a window more than two days old returns nothing from them —
correctly, not as a bug.

NDTV's Akamai edge answers 403 to every request from this host, `robots.txt`
included, whatever the User-Agent. Both NDTV entries stay in `SOURCES` because
they work from networks the edge does not block; from here they simply
contribute nothing.

The User-Agent matters. It is `nm.UA`, a `Mozilla/5.0 (compatible; ...)` string
— the conventional bot shape. A bare `MediaMonitor/1.0 ... python-requests`
gets a 403 from some WAFs (Tarun Bharat's, for one) while the compatible form
passes; the MediaMonitor token and contact address still identify the crawler
to `robots.txt`.

To build history, **run daily and append** (see the last notebook cell).

## Adding a source

Append a `Source` to `SOURCES` in `news_monitor.py`. Find the sitemap via the
`Sitemap:` lines in the site's `robots.txt`. Use `dated_sitemap` with a
`strftime` template if the site shards by date, otherwise `sitemap_index`.
`sections` optionally restricts candidates to URL paths containing those
substrings, which cuts deep-fetch volume a lot. `nested_include` restricts which
nested sitemaps an index is followed into — needed for Yoast/Rank Math indexes
that mix article shards with tag-archive shards stamped with a fresh `lastmod`.

Two traps worth knowing: an index whose `<lastmod>` values run days behind the
articles inside it (Lokshahi) makes `discover()` skip every shard, so point the
source straight at the live shard; and a site whose own `sitemap.xml` paths 404
may still publish one on a feed host (News18 Marathi's index points at
`news18marathi.com`).

---

# Stage 2 — text, dedupe, relevance, report

`news_monitor` finds links. These modules turn links into a written report.

    python3 run_pipeline.py --start 08/30/2026 --end 08/31/2026 --report

| File | Purpose |
|---|---|
| `corpus.py` | Fetch article text, strip boilerplate, drop near-duplicates, write `corpus.txt` |
| `relevance.py` | Cross-encoder relevance scoring |
| `report_llm.py` | Send the corpus to Gemini (LangChain) and get the written report |
| `run_pipeline.py` | All of the above, end to end |

## Text extraction

Paragraph-level assembly (`<p>`, `<h2>`, `<h3>` — deliberately **not** `<li>`,
which is where nav menus live), then a **cross-document boilerplate filter**:
any line appearing in ≥30% of a source's articles is removed. Publishers mark
nav and standing footers up as ordinary paragraphs, so tag-based cleaning misses
them — but they repeat across articles and real prose does not. This needs no
per-site selectors and adapts when a site redesigns.

Measured effect: eSakal articles went from ~100 words (60 of them nav) to clean
text; typical extractions now run 200–900 words.

**Paywalls are respected.** eSakal serves only the lede to anonymous readers.
Those articles are kept, flagged `partial`, and labelled in the corpus so the
model does not read a first paragraph as if it were full coverage. Nothing
attempts to get past the login.

## Duplicate removal

Two layers:

1. **Exact** — URL de-duplication, in `news_monitor`.
2. **Near-duplicate** — hashed 5-gram shingles compared by Jaccard similarity,
   threshold 0.55, keeping the longest copy of each cluster. This catches the
   case URL dedupe cannot: the same PTI/ANI wire copy republished by three
   mastheads under different URLs and reworded headlines.

Word-level shingles work for Devanagari and Latin alike. Run the boilerplate
filter **before** deduping — shared nav text alone pushed unrelated eSakal
articles to 0.30 similarity, and would produce false positives at a lower
threshold.

## Relevance — cross-encoder

**`BAAI/bge-reranker-v2-m3`** (default). XLM-RoBERTa-large based, genuinely
multilingual, and one of the few rerankers that handles Marathi and Hindi as
well as English. It reads query and article *together*, which is what separates
an article **about** the subject from one that mentions him in a list of names —
a distinction keyword matching cannot make.

| Model | Params | Speed (CPU) | Notes |
|---|---|---|---|
| `BAAI/bge-reranker-v2-m3` | 568M | ~3.5 s/article | Default. Best Indic quality |
| `Alibaba-NLP/gte-multilingual-reranker-base` | 306M | ~1.5 s/article | ~2× faster, some quality loss |
| `cross-encoder/ms-marco-MiniLM-L-6-v2` | 22M | very fast | **Do not use** — English-only; scores Devanagari at random |

Measured on this project's 48 articles, CPU-only. A GPU cuts this ~20×.

Scores are logits — pass through `relevance.sigmoid()` for a 0–1 value.
Observed distribution was strongly bimodal (28 articles above 0.01, 20 at
essentially zero), so the cutoff is not delicate. **Default threshold: 0.1.**
Use 0.3–0.5 for a tight report, 0.05 to keep marginal mentions.

Caveat: the reranker sees less text for paywalled articles, which depresses
their scores (eSakal median 0.18). Don't read a low score on a `partial`
article as evidence of irrelevance.

## The report

`report_llm.py` sends the numbered corpus to **Gemini** through LangChain. A
`ChatPromptTemplate` holds the analyst brief and the report comes back **in
Marathi** — headings, tables and analysis all in Devanagari, with Hindi and
English coverage translated in. Sections, in order: परिचय, प्रमुख घडामोडी
(ठळक / मर्यादित प्रसिद्धीतील, split positive-negative), लेखांमधील प्रमुख नेते
(ranked mention table), प्रसिद्धीची कारणे, एकूण विश्लेषण आणि निष्कर्ष with 3-4
actionable recommendations, स्रोत, and any further evidence-backed sections.
Every claim is cited by article number, names are written in full with `श्री.` /
`श्रीमती.`, and nothing outside the supplied articles may be used. The chain is
the standard three-part one:

```python
chain = PROMPT | llm | StrOutputParser()
report = "".join(chain.stream({"subject": ..., "window": ..., "corpus": ...}))
```

Streamed, because a long corpus plus a long report can exceed the
non-streaming HTTP timeout. Swapping providers means swapping the chat model
object in `build_llm()`; the prompt and both callers stay unchanged.

The corpus goes in as a template **value**, not as template text, so stray `{`
or `}` in scraped prose cannot break formatting — worth keeping in mind if you
ever switch to f-string interpolation.

`corpus_facts()` counts the articles and tallies the outlets in Python and
passes both into the prompt, so the स्रोत section and the article count in
परिचय are exact rather than the model's own tally of a few hundred delimiters.

**The social media section is conditional.** The prompt asks for Facebook type
and engagement analysis *only if* such data is actually present in the input.
This pipeline feeds it news articles and nothing else, so the model is told to
state that social data is absent rather than invent numbers — which is what it
does. Wire Facebook posts into the corpus and that section starts producing
real analysis.

**Credentials required** — put `GEMINI_API_KEY=...` in `.env` at the project
root; `report_llm` loads it with `python-dotenv`. Nothing else in the pipeline
needs an API key. The model defaults to `gemini-3.1-pro-preview` and is
overridable with `GEMINI_MODEL`; `gemini-2.5-pro` is the stable fallback if a
preview id is ever withdrawn.

### Reading and downloading it

The web UI renders the returned Markdown with a small escape-then-format helper
in `static/index.html` (`mdToHtml`) — headings, lists, bold and **pipe tables**,
escaped before formatting so nothing in the model output can inject markup. The
report pane is set in Noto Sans Devanagari at a line height Marathi can breathe
in.

The Report tab carries three buttons:

| Button | What it does |
|---|---|
| Download .md | `GET /api/runs/{id}/report.md`, served as an attachment named `media-report-<run id>.md` |
| Download .html | A self-contained styled HTML file — inline CSS, no dependencies, safe to email or archive |
| Save as PDF | Opens that same document in a print window and calls `print()`; choose "Save as PDF" |

The PDF path waits ~600ms for the Devanagari webfont before printing, or the
report prints in a fallback face.

## How much to scrape

The old notebook looped `for page in range(1, MAX_PAGES + 1)` with
`MAX_PAGES = 8`, so it made 79 × 8 = **632** Google News SERP requests per run.
(The per-keyword `30` in `keywords_with_pages` was unpacked as `num_pages` and
then never used — that setting had no effect.) In the sitemap architecture there
is no pagination at all: a date window replaces it, and one sitemap fetch
enumerates a whole day.

The remaining cost knob is `--deep`, which fetches candidate articles to match
keywords in body text. **It is off by default, on evidence:**

| Matched via | Articles | Median relevance | Cleared 0.1 |
|---|---|---|---|
| Title / URL slug | 35 | 0.371 | 27 |
| Body text | 13 | 0.0003 | **0** |

Every body match scored ~0.001. Not one survived the relevance floor, and
producing them cost 200 extra page fetches. For entity monitoring, if the
subject is not in the headline or slug, the article is almost never about them.

Sample is one 2-day window (n=13 body matches) — re-check on your own data
before treating it as settled. If you do want body matches, keep `--deep-limit`
near 200 and let the reranker filter them.

**Recommended:** date window of 1–2 days, run daily, `--deep` off,
`--min-relevance 0.02`. Measured on a 2-day window: 10 sitemap fetches + 6
robots.txt enumerate 1,653 articles, then 54 article fetches — **70 requests
against 632**, for strictly better precision.

## How the cross-encoder is used (and how not to)

**It scores one canonical entity query against every article** — the `SUBJECT`
string in `run_pipeline.py`, not the keyword that matched. That is deliberate.

**Why not use the matched keyword as the query.** Measured against real article
text, **76 of the 79 keywords never match anything.** They were written as
Google query strings — "Devendra Fadnavis BJP" is what you type into a search
box, not a phrase a journalist writes. The 3 that do fire are bare name
variants, so keyword-as-query degenerates into name-as-query. And it would not
matter much anyway: canonical, English name, Marathi name and matched keyword
all rank the same articles (Spearman ρ = 0.93–0.94).

**What the cross-encoder is good for here:** the relevance gate. The score
distribution is strongly bimodal, so the 0.1 cutoff is not delicate, and it
correctly discards every passing-mention article that keyword matching let in.

**What it is not good for:** topic classification. It scores query-answer fit,
not aboutness. Speech-act facets (opposition, reaction,
statement) score 0.74–0.96; subject-matter facets (government, party, Nagpur,
elections) top out at 0.21. Let the LLM name storylines instead.

## Choosing the relevance threshold

Scores on the 48-article sample are sharply bimodal. **The 0.05–0.1 band is
empty**; the cliff falls between 0.0389 and 0.0013 — a 30× drop in one step.

| Threshold | Articles kept |
|---|---|
| 0.01 | 28 |
| **0.02 (default)** | **28** |
| 0.05 | 27 |
| 0.1 | 27 |
| 0.3 | 18 |
| 0.5 | 13 |

The cliff is real, not an artifact. Above it: median 5.5 mentions per article,
first mention 9% into the text, 13 of 28 name him in the headline. Below it:
median 0 mentions, first mention 72% in, none in a headline, and **16 of 20
never mention him in the article prose at all**.

Those 16 were flagged because the deep pass matched *site chrome* — their raw
pages carry 22–40 mentions in sidebars, related-article widgets and tag clouds.
`extract()` now assembles body text paragraph-wise instead of flat-dumping the
page, which cut false body matches from 8/8 to 2/8 on a spot check; the two
survivors are genuine passing mentions that the reranker scores near zero.

**Default is 0.02** — inside the empty gap, with margin on both sides. It also
recovers one paywalled article scoring 0.0389, whose score is depressed by
lede-only truncation rather than irrelevance. For a tight editorial set, use
**0.3** (18 articles); 0.5 (13) is the clearly-headline-subject core.

---

# Frontend

    python3 server.py        # http://127.0.0.1:8000

A local FastAPI app over the same modules. Two things it does:

**Browse past runs.** Every run writes a self-contained directory under `runs/`
(`meta.json`, `articles.jsonl` with relevance scores, `corpus.txt`, `report.md`).
The sidebar lists them; selecting one loads its articles and report. This
replaces the old dashboard's hard-coded spreadsheet names.

**Start a run.** Set subject, terms, date range and relevance floor, then watch
progress stream stage by stage over SSE. A run takes minutes — mostly the
cross-encoder at ~3.5 s/article — so it runs on a worker thread rather than
blocking the request. One run at a time; a second returns 409.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | the UI |
| GET | `/api/runs` | list runs, newest first |
| GET | `/api/runs/{id}` | meta + articles + report |
| POST | `/api/run` | start a run, returns `run_id` |
| GET | `/api/jobs/{id}/events` | SSE progress stream |
| GET | `/api/status` | which run is active, if any |
| GET | `/api/runs/{id}/report.md` | the report as a downloadable file |
| GET | `/api/profiles` | all entity profiles |
| POST | `/api/profiles` | create or update one (by `id`, else slug of `name`) |
| DELETE | `/api/profiles/{id}` | remove one |
| GET | `/api/default-prompt` | the stock Marathi brief, for the UI's "Load default" |

## Entity profiles

The sidebar's **Profiles** box holds one entry per person being monitored. Pick
one and the run form fills in — subject line, search terms, and that entity's
report brief. `New` starts a blank one, `Save` writes the current form back,
`Delete` removes it. Past runs are never touched by any of this.

Twelve are seeded on first launch: Devendra Fadnavis, Ravindra Chavan,
Chandrashekhar Bawankule, Murlidhar Mohol, Atul Bhosale, Siddharth Shirole,
Hemant Rasane, Ranajagjitsinha Patil, Pratap Adsad, Kunal Patil, Rahul Kalate,
Pankaj Bhoyar. They are seeded **once**; after that `profiles.json` is yours and
edits stick.

### Writing search terms

The seeded term sets follow three rules, each learned from a failed search:

1. **Every name needs its Devanagari form.** Marathi matching is substring-based
   and outlets disagree on spelling, so variants each need their own line —
   `रवींद्र चव्हाण` *and* `रविंद्र चव्हाण`, `बावनकुळे` *and* `बावनकुले`.
2. **A bare surname is only safe when it is distinctive.** `बावनकुळे`, `अडसड`,
   `रासने`, `शिरोळे` and `कलाटे` are safe and materially raise recall for
   low-profile figures. `पाटील`, `चव्हाण` and `भोसले` are not — tested on live
   sitemaps, a bare `चव्हाण` returned 26 articles of which most were Prithviraj
   Chavan, a Satara murder case and a Kolhapur college dispute.
3. **Watch for surnames that are also words or places.** `मोहोळ` is a Solapur
   taluka as well as a surname, so Murlidhar Mohol is full-name-only.

Relevance scoring is the second filter, so a slightly loose term set costs
precision, not correctness. A term set that is too tight costs you the article
entirely.

### Per-entity report brief

**⚙ Settings**, top right, opens the brief for whichever entity is selected. It
is the full system prompt, editable in place. `Reset to default` reloads the
stock Marathi brief; `Save brief` writes it to that profile.

Every profile stores its own brief in full — there is no fallback and nothing is
resolved at send time. What the dialog shows is exactly the system prompt that
goes to Gemini for that entity, and the run request carries it explicitly. All
twelve seeded profiles start from the same stock brief and diverge only when you
edit them.

`report_llm.build_prompt(system_prompt)` builds the chain's prompt from the text
it is handed and nothing else; an empty brief raises rather than quietly
substituting one. A brief is template *text*, not a value, so its braces are
doubled before reaching LangChain — otherwise a `{` someone typed would be read
as a `{variable}` and fail at format time.

`POST /api/run` therefore expects `system_prompt` whenever `report` is true. An
API caller that omits it gets a clear failure on the report step — the run still
completes and the articles are kept — rather than a report written to some other
entity's brief. `report_llm.build_report()` keeps the stock brief as its default
argument for the CLI path only.

# Deployment

Live at <https://media-monitoring.duckdns.org>, behind HTTP basic auth. The box
is a 2 vCPU / 3.7 GB EC2 instance shared with other services, which shapes most
of the decisions below.

## Shape of it

    GitHub push to main
      -> Actions builds the image, pushes to ghcr.io
      -> Actions SSHes to EC2, pulls, restarts the container
    nginx (host, :443, TLS + basic auth)
      -> container on 127.0.0.1:8010

The container is bound to loopback. nginx on the host terminates TLS and is the
only thing that can reach it; port 8000 was already taken by another service on
the same box, hence 8010.

## Files

| File | What it is |
|---|---|
| `Dockerfile` | CPU-only image. torch comes from the PyTorch CPU index in its own layer, since it is ~800 MB and changes far less often than the app |
| `docker-entrypoint.sh` | Seeds `profiles.json` into the data volume on first boot |
| `docker-compose.yml` | What runs on the box. CI copies this over on every deploy, so edit it here |
| `.github/workflows/deploy.yml` | Build, push, deploy |
| `deploy/remote-deploy.sh` | The half that runs on the box: login, pull, `up -d`, prune |
| `deploy/wait-healthy.sh` | Post-deploy check; the container answers on loopback, so it has to run there |
| `deploy/prewarm-model.sh` | Pulls the 2.3 GB reranker into the cache volume. Run once |
| `deploy/nginx-media-monitoring.conf` | A copy of the live nginx site, certbot's edits included |

## Volumes

Four, all named, all surviving redeploys:

| Volume | Holds | Why it cannot live in the image |
|---|---|---|
| `runs` | Every past run | The whole point of the run list |
| `data` | `profiles.json` | Edited from the UI |
| `page-cache` | Scraped pages | Makes a re-run of the same window free |
| `hf-cache` | Reranker weights, ~2.3 GB | Too large to bake; downloaded once |

`profiles.json` is both checked into the repo and mounted from a volume. The
image copy is a seed: the entrypoint installs it the first time only, so a
redeploy never overwrites profiles edited in the UI. To push a repo-side change
to the live set, delete the volume copy and restart.

## Environment

`.env` on the box holds `GEMINI_API_KEY` and `APP_PORT`. Everything else has a
working default; see `.env.example`.

| Variable | Default | Notes |
|---|---|---|
| `GEMINI_API_KEY` | — | Required for the report step |
| `APP_PORT` | 8010 | Host port nginx proxies to |
| `RERANKER_MODEL` | `BAAI/bge-reranker-v2-m3` | Set to `Alibaba-NLP/gte-multilingual-reranker-base` on a memory-tight host |
| `RERANKER_BATCH_SIZE` | 8 | Drop to 4 if the host swaps |
| `RUNS_DIR`, `PROFILES_PATH`, `PAGE_CACHE_DIR` | repo-relative | Set by the image to the volume paths |
| `HOST`, `PORT` | `127.0.0.1:8000` | The image sets `HOST=0.0.0.0`; a bare `python3 server.py` still binds loopback |

## Memory

The 568M-parameter cross-encoder was the thing expected to break this box,
which has 3.7 GB shared with Grafana, Prometheus and another service. Measured
on the instance, it is not close: the model loads in ~4s from the warm cache
and the container sits at **694 MiB** with it resident and scoring — the
safetensors weights are memory-mapped rather than read into a 2.3 GB fp32
buffer. The 3 GB cap in `docker-compose.yml` stays as a guard rail, not because
the ceiling is near.

Throughput, not memory, is the real limit: ~3-4s per article on 2 vCPU, so a
200-article window is a 10-minute run. If that is too slow, `RERANKER_MODEL`
takes the gte reranker — 306M parameters, about twice as fast, at some cost to
Marathi ranking quality. `RERANKER_BATCH_SIZE` is there for the same reason.

## SSE through nginx

Run progress is Server-Sent Events, and the default proxy settings break them
in two ways: `proxy_buffering` holds events until the response ends, which for
a run means until it finishes, and the 60s read timeout kills a connection that
sits quiet inside the cross-encoder. The `/api/jobs/` location turns buffering
off and raises the timeout to an hour. The app also sends `X-Accel-Buffering:
no`, which covers the same ground; both are here because the header is easy to
lose in a refactor.

## First-time setup

On the box:

    mkdir -p ~/media_monitoring
    # .env with GEMINI_API_KEY and APP_PORT=8010
    # docker-compose.yml and deploy/*.sh arrive with the first deploy

In the repo settings, four secrets:

| Secret | Value |
|---|---|
| `EC2_HOST` | the instance hostname |
| `EC2_USER` | `ubuntu` |
| `EC2_SSH_KEY` | the private key, whole file including header and footer |

`GITHUB_TOKEN` is not one of them — Actions provides it, and the deploy uses it
to log the box into GHCR for the length of the run and logs out afterwards. No
long-lived registry credential sits on the instance.

nginx and the certificate are already in place. To rebuild them from scratch:

    sudo cp deploy/nginx-media-monitoring.conf /etc/nginx/sites-available/media-monitoring
    sudo ln -s /etc/nginx/sites-available/media-monitoring /etc/nginx/sites-enabled/
    sudo htpasswd -c /etc/nginx/.htpasswd.media-monitoring <user>
    sudo nginx -t && sudo systemctl reload nginx
    sudo certbot --nginx -d media-monitoring.duckdns.org --redirect

The certbot renewal timer is already installed. The ACME challenge location is
exempted from basic auth explicitly — without that, renewal fails ninety days
later with nobody watching.

## Operating it

    docker compose logs -f media-monitoring     # tail
    docker compose ps                           # what is running
    cat ~/media_monitoring/DEPLOYED             # which commit that is
    docker stats media-monitoring               # memory, during a run

Rolling back is a pinned pull, since every commit is tagged:

    IMAGE=ghcr.io/<owner>/media_monitoring:<sha> docker compose up -d

## Notes

The articles table stores **every** extracted article with its score, not just
the ones above the floor. The table opens at the run's own floor, and the slider
goes lower so you can inspect what a run discarded — useful for sanity-checking
the threshold rather than trusting it blind.

The trend chart from the old static dashboard has not been ported — the frontend
shows one run at a time, not a time series across runs.

The Selenium/Google-SERP scrapers, the static dashboard, and their spreadsheets
and screenshots have been removed; everything they did is covered by the sitemap
pipeline and the web UI.
