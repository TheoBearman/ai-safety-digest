# AI Safety Weekly Digest

A curated weekly digest of AI safety research — papers, technical posts, and reports from leading labs and research organizations.

**[Read the latest digest](https://sambhav-mahesh.github.io/ai-safety-digest/)**

## Reading the Digest

No setup required. Just bookmark the link above. The digest updates automatically every Monday with the latest AI safety research from the past week.

You can also open `site/index.html` directly in any browser if you clone the repo.

## Sources

The digest aggregates research from 35+ sources across five fetcher types:

### RSS Feeds
| Organization | Feed |
|---|---|
| Google DeepMind | Blog RSS |
| Microsoft Research | Research feed (AI keyword filtered) |
| Anthropic | News + Engineering (via community RSS mirrors) |
| OpenAI | News RSS (Research/Safety/Publication categories) + Alignment Research Blog |
| Redwood Research | Blog RSS |
| Alignment Forum | Feed (research keyword filtered) |
| Dan Hendrycks | ML Safety Newsletter |
| FLI | Blog feed |
| Epoch AI | Blog RSS |

### arXiv
Keyword search across `cs.AI`, `cs.LG`, `cs.CL` for: AI safety, alignment, interpretability, mechanistic interpretability, RLHF, scalable oversight, AI governance, red teaming, AI evaluations. Up to 40 papers per week.

### Web Scrapers
| Organization | URL | Filter |
|---|---|---|
| Anthropic Alignment Science | alignment.anthropic.com | Blog posts with dates |
| METR | metr.org/blog | Links containing `/blog/` |
| Apollo Research | apolloresearch.ai/blog | All blog posts |
| ARC (Alignment Research Center) | alignment.org/blog | Links containing `/blog/` |
| MIRI | intelligence.org/research | Links containing `/research/` |
| CAIS | safe.ai/research | Links to arxiv.org |
| FAR AI | far.ai/blog | Links containing `/news/` |
| UK AISI | aisi.gov.uk/work | Links containing `/blog/` |
| US AISI (NIST) | nist.gov | Links containing `/artificial-intelligence` |
| RAND | rand.org/topics/artificial-intelligence | All items |
| CSET Georgetown | cset.georgetown.edu/publications | All publications |
| GovAI Oxford | governance.ai/research | Links containing `/research-paper/` |
| IAPS | iaps.ai/research | Links containing `/research-paper/` |
| CHAI Berkeley | humancompatible.ai/news | All items |
| MATS | matsprogram.org/research | Links containing `/research/` |
| Paul Christiano | paulfchristiano.com | All items |
| Yoshua Bengio | yoshuabengio.org (AI safety) | All items |
| Lennart Heim | blog.heim.xyz | All items |

### LessWrong
GraphQL API — posts with 150+ karma from the past 7 days.

### Trending
Hacker News (Algolia API, 100+ points) + Reddit (r/aisafety, r/mlsafety, r/ControlProblem) — research-filtered by URL domain and title keywords.

## How It Works

```
config.yaml → fetchers → 7-day date filter → dedup → research filter → enrich → render → GitHub Pages
```

1. **Fetch** — Five fetcher types (RSS, arXiv, scraper, LessWrong, trending) collect papers
2. **Date filter** — Only papers from the last 7 days are kept. Scraped papers without parseable dates are dropped.
3. **Deduplicate** — Exact title match, then fuzzy matching (>85% similarity)
4. **Research filter** — 144-term scoring filter removes news/opinion. arXiv always passes. Known research orgs need score >= 1, others >= 2.
5. **Enrich** — Papers with short/missing abstracts get them fetched from URLs or a synthetic fallback
6. **Render** — Jinja2 template produces a static site with featured section, org filters, and expandable abstracts

## Deployment

A GitHub Actions workflow runs every Monday at 9 AM UTC:

1. Fetches papers from all sources
2. Renders the site
3. Commits updated `data/papers.json` and `site/index.html`
4. Deploys to GitHub Pages

You can also trigger it manually from the Actions tab.

## Development

```bash
# Install dependencies
pip install -r requirements.txt  # Python 3.9+

# Fetch papers and render
python scripts/fetch.py
python scripts/render.py

# Or do both and open in browser
bash scripts/update-and-open.sh
```

### Adding a source

**RSS feed** — Add to `rss_feeds` in `config.yaml`:
```yaml
- name: "Example Lab"
  url: "https://example.com/feed.xml"
  org: "Example Lab"
  keywords: ["research", "paper", "model"]  # optional
  categories: ["Research"]                   # optional, filters by RSS <category> tags
```

**Web scraper** — Add to `scrapers` in `config.yaml`:
```yaml
- name: "Example Org"
  url: "https://example.com/research"
  org: "Example Org"
  link_must_contain: "/research/"  # optional
```

## License

MIT
