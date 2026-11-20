# Loki

A local literature radar for biological research. It fetches new papers every
day, scores them against your interests, and gives you the top 10 for the day,
the week and the month.

Everything runs on your machine. No account, no API key, no data leaves the
Mac except the queries sent to PubMed and bioRxiv.

New here? Go to [Install](#install).

Once it is set up, **double-click `Loki.app`**. It starts the server, opens the
page, and exits. The server keeps running in the background; stop it with
`bin/loki stop`.

Or from a terminal:

```
bin/loki serve      # then open http://127.0.0.1:8713
```

---

## What it does

1. **Fetches** new papers once a day from PubMed, bioRxiv and medRxiv.
2. **Scores** each one on topic relevance and journal impact.
3. **Ranks** them into three digests: day, week and month, each widened by
   ±10 days so nothing is missed at the edges.
4. **Summarises** each abstract down to its findings.
5. **Serves** them at `http://127.0.0.1:8713`, where you can also edit your topics.

---

## Install

### What you need

| | |
|---|---|
| **Python 3.12** | Pinned in `.python-version`. 3.13 is not supported yet: `pyproject.toml` caps it. |
| **macOS** | For `Loki.app` and the daily `launchd` schedule. The Python side itself is portable, so `bin/loki fetch` and `bin/loki serve` work on Linux too; only the two shell scripts are Mac-only. |
| **~500 MB disk** | 30 MB embedding model, plus the SQLite database as it fills. A year of daily fetches lands around 200 MB. |
| **Network** | Only to reach PubMed and bioRxiv. Everything else is local. |

[uv](https://docs.astral.sh/uv/) is optional but recommended; the installer
falls back to `python3.12 -m venv` when uv is absent.

### Set it up

```bash
git clone https://github.com/GMFranceschini/loki.git
cd loki
scripts/install.sh
```

That creates `.venv`, installs the dependencies, runs the test suite and then
`bin/loki doctor`. The first run downloads the embedding model, which is why it
takes a minute. After that Loki works offline apart from the fetching itself.

The installer is safe to rerun and takes two optional flags:

```bash
scripts/install.sh --schedule   # also install the daily 07:00 fetch
scripts/install.sh --app        # also build Loki.app onto the Desktop
```

### Do it by hand instead

```bash
uv venv --python 3.12 .venv
uv pip install -e ".[dev]"      # or: .venv/bin/pip install -e ".[dev]"
bin/loki doctor
```

### First run

The database starts empty, so fill it before expecting a digest:

```bash
bin/loki fetch --backfill 7     # the last week; 30 also works, it just takes longer
bin/loki top week               # see it in the terminal
bin/loki serve                  # or open http://127.0.0.1:8713
```

Then **set your topics** — this is the step that decides whether Loki is useful
to you. The five shipped in `config/interests.yaml` are an example, not a
default worth keeping. See [Setting your topics](#setting-your-topics).

Finally, if you want it to keep itself current:

```bash
scripts/install_schedule.sh     # daily at 07:00
scripts/build_app.sh --desktop  # the double-clickable launcher
```

### Where to put the folder

Keep it out of `~/Desktop`, `~/Documents` and `~/Downloads`. macOS privacy
protection blocks background jobs from reading those, and the daily fetch then
fails with `Operation not permitted`. `~/Loki` is a good home.

---

## The app

`Loki.app` is a thin launcher around `bin/loki serve`. Double-clicking it:

1. Opens the page if Loki is already running, and does nothing else.
2. Otherwise starts the server detached, then opens your browser once it is up.
3. Exits immediately either way.

```bash
bin/loki status    # is the server running?
bin/loki stop      # stop it
```

Drag it from `/Applications` to the Dock to keep it there.

The daily fetch is separate and runs whether the server is up or not, so your
digests stay current even with everything closed.

> **Why the launcher exits straight away.** A shell script has no Cocoa event
> loop. If it stayed in the foreground it would hold a Dock slot, never answer
> the system's Apple Events, and get marked *not responding* — and macOS would
> then try to re-activate that stuck instance instead of launching, so clicking
> the icon would do nothing at all. Exiting immediately avoids the whole class
> of problem.

> **Replacing an installed copy.** macOS protects existing app bundles, and the
> Desktop on top of that, so the build script cannot overwrite one from a
> terminal. Drag the old `Loki.app` to the Trash in Finder first, then rerun
> `scripts/build_app.sh --desktop`.

Rebuild or reinstall it with:

```bash
scripts/build_app.sh              # rebuild in the project folder
scripts/build_app.sh --desktop    # and put a copy on the Desktop
scripts/build_app.sh --apps       # and put a copy in /Applications
```

> **If you move the `~/Loki` folder, rerun `scripts/build_app.sh --desktop`.**
> The launcher holds the absolute path to the project, so a moved folder leaves
> the old app pointing nowhere. It will tell you so in a dialog rather than
> failing silently.

Problems starting up are written to `data/logs/app.log` and shown in a dialog,
since an app launched from Finder has nowhere else to report them.

## Daily use

```bash
bin/loki serve                  # web UI at http://127.0.0.1:8713
bin/loki top day                # today's top 10 in the terminal
bin/loki top week --limit 20    # top 20 for the last 7 days
bin/loki top month --json       # machine-readable
bin/loki fetch                  # fetch now (the schedule does this for you)
bin/loki fetch --backfill 14    # fill in the last two weeks
bin/loki status                 # is the server running?
bin/loki stop                   # stop the server
bin/loki rescore --summaries    # rebuild summaries after changing summary settings
bin/loki stats                  # what is stored
bin/loki doctor                 # health check
```

### The daily schedule

A `launchd` job runs `bin/loki fetch` every day at 07:00.

```bash
launchctl list | grep com.loki.daily            # is it registered
launchctl kickstart -p gui/$(id -u)/com.loki.daily   # run it right now
tail -f data/logs/launchd.err.log               # watch it work
scripts/install_schedule.sh 6 30                # move it to 06:30
scripts/install_schedule.sh --remove            # turn it off
```

If the Mac is asleep at 07:00, launchd runs the job when it wakes. Each run
covers the last 3 days, because PubMed keeps indexing papers for a while after
they appear. Re-seen papers are updated, never duplicated.

> **Keep this folder out of `~/Desktop`, `~/Documents` and `~/Downloads`.**
> macOS privacy protection blocks background jobs from reading those folders,
> and the daily fetch fails with `Operation not permitted`. That is why Loki
> lives in `~/Loki`.

---

## Setting your topics

This is the part that decides what you see. Two ways to edit it:

**In the web UI** — press **Topics**, or go to
`http://127.0.0.1:8713/#topics`. Edit, then press **Save & rescore**. Your
changes are written to `config/interests.yaml` and every stored paper is
rescored immediately.

**In the file** — edit `config/interests.yaml` directly, then run
`bin/loki rescore`. The UI and the file are the same thing; editing one shows
up in the other. The previous version is kept as `interests.yaml.bak`.

Each topic has four parts:

| Field | What it does |
|---|---|
| `name` | The tag shown on each paper. It also counts towards matching, so keep it about the same subject as the description. |
| `weight` | Relative importance. A topic at `0.5` needs roughly twice the similarity of a topic at `1.0` to rank equally. |
| `prose` | **The important one.** One to three sentences describing the topic in natural language. This is what abstracts are compared against. |
| `pubmed` | A PubMed query that pre-filters what gets downloaded. Optional. |

### Write the prose like you would describe it to a colleague

Relevance is semantic, not keyword matching. Full sentences work much better
than a list of terms, because the comparison is between meanings.

```yaml
# Good — rich, specific, reads like a description
prose: >
  CRISPR-Cas genome editing, base editing and prime editing. Delivery of
  editors into primary human cells and in vivo tissues. Editing efficiency,
  specificity, off-target effects and therapeutic correction of mutations.

# Weak — too sparse to match against
prose: "CRISPR, editing, Cas9"
```

### The PubMed query is a filter, not the ranking

`pubmed` only decides what gets **downloaded**; `prose` decides what gets
**ranked**. Keep the query broad. If it is too narrow, good papers never reach
the scorer.

Preprints work differently: bioRxiv and medRxiv have no keyword search, so Loki
downloads everything posted each day and lets relevance do the filtering. New
topics therefore work on preprints straight away, but only reach PubMed once
you give them a query and run a fetch.

### Keep the name and the description about the same thing

The name is what you see on a paper; the description is what papers are
matched against. If you rename a topic but leave the old description, every
tag becomes wrong — the label says one thing and the matching does another.
`topic_name_weight` (0.35) blends the name into the matching to soften this,
but it cannot rescue a description about a different subject. Rename and
rewrite together.

### After adding a topic

Saving rescores what is already stored. To pull in older papers that match a
new topic, run a backfill:

```bash
bin/loki fetch --backfill 30
```

---

## How ranking works

Each paper gets a score from 0 to 1:

```
score = 0.7 × relevance  +  0.3 × impact
```

Both weights live under `scoring:` in `config/interests.yaml`.

### Relevance

The title and abstract are embedded locally with a small static model
(`model2vec`, 256 dimensions) and compared against your topic prose by cosine
similarity.

Before comparing, Loki subtracts a **background vector**: the mean embedding of
recent literature. This matters more than it sounds. Any two life-science
abstracts are quite similar to each other — they share vocabulary, structure
and hedging — so without this step a paper on knee surgery scores nearly as
high against genome-editing prose as a real genome-editing paper does. On the
test corpus, subtracting the background dropped an off-topic seismology
abstract from 0.28 to 0.09 while on-topic papers stayed near 0.45.

The background is seeded from `config/background.npy` and re-estimated from
your own corpus as it grows. When it changes, every stored paper is rescored
automatically, so all scores stay on one scale.

The result is squashed from `[sim_floor, sim_ceiling]` onto 0–1. Raise
`sim_floor` to be stricter. Papers below `min_relevance` are stored but never
shown.

### Tags

Tags are the topic labels on each paper. A paper can carry several, or none.

- A topic is tagged only when its similarity clears `tag_min_sim` (0.20).
- Further topics are tagged when they reach `tag_relative` (0.80) of the best
  one, up to `max_tags` (3). Set `tag_relative: 1.0` for exactly one tag.
- A paper that matches nothing confidently is shown as **no clear topic**. It
  is still ranked; it is simply not labelled, which is more honest than
  attaching the least-bad topic.

Tags are chosen by **raw** similarity while ranking uses the **weighted**
similarity. So lowering a topic's weight pushes its papers down the list
without renaming what they are about.

### Impact

The journal's impact factor, looked up in `config/journals.json` by title, ISO
abbreviation or ISSN, then log-compressed:

```
impact = log(1 + IF) / log(1 + cap)      # cap defaults to 60
```

Log compression means the gap between IF 2 and IF 10 counts for more than the
gap between IF 50 and IF 90 — which is the right shape, since the difference
between a minor and a solid journal matters more than between two famous ones.

- **Preprints** have no impact factor, so they take a flat `preprint_impact`
  (0.33, about an IF of 6). Raise it to favour preprints, lower it to bury them.
- **Journals not in the table** score `unknown_journal_impact` (0.20).

> **About the impact factors.** `config/journals.json` holds 252 hand-curated
> approximate values for major biology and biomedical journals. Impact factors
> are not available from any free API, and this is not licensed JCR data. The
> numbers are approximate and drift year to year. Treat them as a prestige
> prior, not a measurement. Correct or extend the file freely — add a row, fix
> a number — then run `bin/loki rescore`.

### What gets thrown away

Retractions, errata, corrigenda, editorials, comments and similar notices are
dropped at parse time. They carry a journal's full prestige but no science, so
a retraction notice in a high-impact journal would otherwise outrank real work.

### Summaries

Expanding a card shows a compact version of the abstract — about a quarter of
the length, carrying the findings rather than the scene-setting. Press **Show
full abstract** for the original.

Summaries are **extractive**: the most informative sentences already in the
abstract, put back in their original order. Nothing is paraphrased, reworded or
invented, which matters here — a confidently wrong paraphrase of a result is
worse than no summary at all. No model is called and nothing leaves your Mac.

Two strategies, picked per paper:

- **Labelled abstracts** (BACKGROUND / METHODS / RESULTS / CONCLUSIONS — about
  a quarter of PubMed records) use the conclusions and results directly.
- **Unlabelled abstracts** rank sentences on how central they are to the
  abstract, how much they read like a finding rather than background, and where
  they sit, since the conclusion usually comes last.

An abstract already shorter than `max_chars` is shown unchanged, because
compacting it would only lose information.

```yaml
summary:
  enabled: true        # false turns the feature off entirely
  max_sentences: 3
  max_chars: 420
```

Summaries are computed when papers are scored and stored alongside them, so
expanding a card is instant and works offline. They are written once and then
left alone: a plain `bin/loki rescore` will not touch them, because summarising
re-embeds every sentence of every abstract and the result does not depend on
your topics. After changing the settings above, run `bin/loki rescore
--summaries` to rebuild them.

### The digest windows

Each window is widened by `window_padding_days` (10) on both sides, because a
paper worth reading now was often posted or indexed a few days earlier, and
journals backdate:

| Tab | Base | Actual span with ±10 days |
|---|---|---|
| Day | 1 day | 21 days |
| Week | 7 days | 27 days |
| Month | 30 days | 50 days |

The windows stay nested, so anything in the day list also appears in the week
and month lists. Set `window_padding_days: 0` for strict calendar windows.

### Preprint and published version of the same paper

When a preprint later appears as a journal article with the same DOI, the
preprint record is removed so the digest shows one entry, not two.

---

## Tuning it

Everything below is in `config/interests.yaml`.

| Symptom | What to change |
|---|---|
| Too many loosely related papers | Raise `sim_floor` (0.10 → 0.15) or `min_relevance`. |
| Famous journals crowd out the rest | Lower `impact_weight` (0.3 → 0.2). |
| Too few high-profile papers | Raise `impact_weight`, or raise `impact_factor_cap`. |
| Preprints dominate | Lower `preprint_impact` (0.33 → 0.20). |
| You want preprints first | Raise `preprint_impact` (0.33 → 0.50). |
| One topic swamps the others | Lower its `weight`. |
| A topic returns nothing from PubMed | Its `pubmed` query is too narrow. |
| Tags look wrong or arbitrary | The topic's description probably describes something other than its name. Rewrite them together. |
| Too many papers tagged with everything | Raise `tag_relative` towards 1.0, or raise `tag_min_sim`. |
| Too many papers show "no clear topic" | Lower `tag_min_sim` (0.20 → 0.15). |
| Day list is too wide or too narrow | Change `window_padding_days`. |
| Summaries are too short or too long | Change `summary.max_sentences` / `max_chars`, then `bin/loki rescore --summaries`. A plain `rescore` leaves stored summaries alone, because rebuilding them is far slower than rescoring and they do not depend on your topics. |
| A summary reads like background, not findings | The abstract buried its result mid-text. Raise `max_sentences`, or open the full abstract. |

After any change: `bin/loki rescore` (or **Save & rescore** in the UI).

---

## Privacy and network access

Loki is a local program. It has no account, no telemetry and no cloud component.

**What leaves your Mac.** Only the queries themselves: your PubMed search
strings go to NCBI, and bioRxiv/medRxiv are asked for each day's postings. Your
topic prose, your database and your reading are never uploaded. Scoring,
tagging and summarising all run locally — the embedding model is downloaded
once and then used offline, and no language model is called at any point.

**Your contact details.** `sources.pubmed.contact_email` is blank by default
and Loki sends a generic User-Agent. NCBI asks heavy users for an address so
they can get in touch; fill it in only if you want to be identifiable to them.

**The server listens on loopback only.** `bin/loki serve` binds `127.0.0.1`, so
nothing outside the machine can connect. Loopback alone is not a boundary that
browsers respect, though, so two further checks are enforced on every request:

- **Host header.** A request addressed to anything but a loopback name is
  refused with `421`. This defeats DNS rebinding, where a name the attacker
  controls resolves to `127.0.0.1` and their page becomes same-origin with Loki.
- **Origin header.** A `POST`, `PUT`, `PATCH` or `DELETE` carrying a foreign
  `Origin` is refused with `403`. A cross-site `POST` needs no preflight, so
  without this any page you had open could trigger a fetch or rewrite your topics.

To reach Loki by another name on purpose — a reverse proxy, a container, another
machine on your LAN — name it explicitly, and remember the API has no
authentication of its own:

```bash
LOKI_ALLOWED_HOSTS=loki.lan bin/loki serve --host 0.0.0.0
```

**The API is unauthenticated.** That is deliberate and safe while it is bound to
loopback. Do not expose it to a network you do not trust.

---

## Where the data comes from

| Source | What Loki asks for | Terms |
|---|---|---|
| **PubMed** (NCBI E-utilities) | Title, abstract, journal, authors, DOI for records matching your queries | Public domain records. NCBI asks for ≤3 requests/second without an API key, 10 with one; Loki rate-limits itself to stay under both. |
| **bioRxiv / medRxiv** | Everything posted in the date range | Free API. Preprint abstracts are posted by their authors, usually under a Creative Commons licence. |

Abstracts are stored in your local `data/loki.db` and shown to you, which is
ordinary personal research use. They stay on your machine: `data/` is
git-ignored, so cloning or publishing this repository never carries anyone's
abstracts with it. If you redistribute a database, the abstracts in it are
still the publishers' and authors' material, not yours.

`config/journals.json` holds 252 approximate impact factors, hand-curated for
ranking. Individual impact factors are facts, not anyone's copyright, but the
file is deliberately not a copy of a licensed JCR dataset and must not be
treated as one. It is a prestige prior, nothing more.

The embedding model, `minishlab/potion-base-8M`, is downloaded from Hugging Face
at first use under its own licence. It is not redistributed here.

All Python dependencies are MIT or BSD licensed. The icon in `assets/icon.svg`
was drawn for this project: it is a plain geometric hypercube, and contains no
third-party or emoji artwork.

---

## Layout

```
~/Loki/
├── Loki.app                  double-clickable launcher (generated)
├── assets/                   app icon artwork
├── assets/icon.svg           the app icon; everything else is generated from it
├── bin/loki                  the command you run
├── config/
│   ├── interests.yaml        your topics and all scoring settings
│   ├── journals.json         journal → impact factor table (editable)
│   └── background.npy        seed background vector
├── data/
│   ├── loki.db               SQLite: every paper and score
│   └── logs/                 fetch logs, including the scheduled runs
├── loki/                     the Python package
│   ├── sources/              PubMed and bioRxiv fetchers
│   ├── scoring/              embeddings, impact lookup, combination
│   ├── summarize.py          compacts abstracts down to their findings
│   ├── pipeline.py           fetch → score → store
│   ├── server.py             the JSON API
│   └── cli.py                the command line
├── web/                      the single-page UI
├── scripts/
│   ├── install.sh            one-command setup from a fresh clone
│   ├── install_schedule.sh   the daily launchd job
│   ├── build_app.sh          builds Loki.app
│   ├── make_icon.sh          rebuilds icon.png and loki.icns from icon.svg
│   └── svg2png.swift         the fallback SVG rasteriser make_icon.sh uses
└── tests/                    152 tests: bin/loki is not needed, use pytest
```

Your data is just a SQLite file. Query it directly whenever you want:

```bash
sqlite3 data/loki.db \
  "SELECT score, journal, title FROM papers ORDER BY score DESC LIMIT 10;"
```

---

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/digests?date=&limit=` | All three windows in one call |
| `GET /api/digest?window=day\|week\|month` | One window |
| `GET /api/topics` · `PUT /api/topics` | Read and save the topic profile |
| `POST /api/fetch?backfill=2` | Run a fetch |
| `GET /api/stats` · `GET /api/coverage?days=30` | Storage and per-day counts |
| `GET /api/docs` | Interactive API documentation |

---

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

152 tests, no network needed: the scoring, tagging and summary tests use the
offline hashing embedder and the API tests run against a temporary database.

---

## Troubleshooting

**`bin/loki doctor`** checks everything below first.

| Problem | Cause and fix |
|---|---|
| Scheduled fetch fails, `Operation not permitted` | The folder is under `~/Desktop`, `~/Documents` or `~/Downloads`. Move it to `~/Loki`, then rerun `scripts/install_schedule.sh`. |
| `doctor` says it fell back to the hashing embedder | `model2vec` could not load, so matching is lexical and much weaker. Reinstall it, then `bin/loki rescore`. |
| Digest is empty | Nothing fetched yet for that window. Run `bin/loki fetch --backfill 7`. |
| Everything scores near zero | Your topic prose is too short or too far from the corpus. Write fuller descriptions and rescore. |
| PubMed is slow or rate-limited | Set `NCBI_API_KEY` in your environment. It raises the limit from 3 to 10 requests per second. |
| Port 8713 is busy | `bin/loki serve --port 9000`, or `LOKI_PORT=9000 scripts/build_app.sh --desktop` for the app. |
| The app says it cannot find its Python environment | You moved the project folder. Rerun `scripts/build_app.sh --desktop`. |
| The app seems to do nothing, Finder says "not responding" | An old launcher is stuck. `pkill -f "Loki.app/Contents/MacOS/Loki"`, then rebuild with `scripts/build_app.sh`. |
| The app bounces and quits | Read `data/logs/app.log`. |
| `build_app.sh` cannot replace an installed app | Trash the old one in Finder, then rerun it. |
| The Desktop icon looks generic | Finder caches icons. Log out and back in, or rerun the build script. |

---

## Adding more sources

`loki/sources/` holds one module per source. Each exposes a fetch function that
returns a list of plain dicts with the fields in `loki/db.py`. Add a module,
call it from `run_fetch` in `loki/pipeline.py`, and the scoring, storage,
de-duplication and UI all work unchanged.

---

## Licence

MIT. See [LICENSE](LICENSE).
