# Go-to-market: lighthouse or landgrab

Conclusions from applying a16z's [Lighthouse or Landgrab: How to Pick](https://www.a16z.news/p/lighthouse-or-landgrab-how-to-pick)
to memseek. Written 2026-07-28. Companion to [PRODUCT.md](../PRODUCT.md) (what we may claim) and
[DESIGN.md](../DESIGN.md) (how surfaces look).

## The framework, compressed

> "Your buyer doesn't purchase the future; they purchase either proof or math."

Two strategies, and the article's point is that the market picks for you:

| | **Lighthouse** | **Landgrab** |
| --- | --- | --- |
| Currency | Borrowed credibility — someone respected went first | Demonstrated ROI, fast |
| Motion | Founder-led, high-touch, custom POC work | Demo-driven, standardized, repeatable |
| Cycle | 3–6+ months | Days to weeks |
| Wins | Few, large, slow | Many, smaller, fast |
| Buyer question | "Is this safe? Who went first?" | "What's the payback, and how fast?" |

Two questions decide it:

1. **Buyer exposure.** How much personal career risk does saying yes create? High exposure
   (regulated work, system-of-record replacement, customer-facing output) → lighthouse. Recoverable
   mistakes and internal tools → landgrab.
2. **Does social proof travel?** Concentrated, status-conscious markets (law, finance) → proof
   travels → lighthouse. Fragmented markets (mid-market AR, support) → proof doesn't travel →
   landgrab, because "the vast majority of revenue sits in companies no one's heard of."

Supporting signals: cycles over 60 days plus custom POCs plus "is this safe?" questions mean
lighthouse. An existing allocated budget line means landgrab — *if* exposure is low.

## Where memseek lands

**Lighthouse, and not marginally.**

**Exposure is high on every axis the article names.** memseek is a system of record — records are
immutable, provenance is structural, and once agent memory sits on it, it is architecture rather
than a tool you swap out next quarter. It touches PII with real teeth: recursive-closure erase,
right-to-be-forgotten, retention jobs. Its output is customer-facing by construction, because the
whole point is what the agent says next. And adopting it means authoring a YAML catalog — a
migration, not an install.

**The feature list is itself the tell.** Look at what memseek leads with: citations or rejection,
point-in-time belief reconstruction, contradiction detection, bounded derivations so a runaway model
is a config error rather than a bill, `LLM_FAKE=1` emitting nothing rather than inventing uncited
content. Every one of those is a *risk-reduction* feature. You do not build a risk-reduction feature
set for a buyer whose mistakes are cheap. The roadmap has already answered question 1.

**Proof travels in this market, but it isn't a logo.** Developer infrastructure is concentrated and
status-conscious in its own way — proof moves through reproducible repos, benchmark tables, HN, "who
runs this in production," and an engineering post from a team people respect. It travels. It just
travels as *technical* proof rather than a customer wordmark.

## The move: a lighthouse without a logo

Here is the honest constraint, straight from PRODUCT.md's Evidence on Hand: **no customer names,
logos, testimonials, case studies, or pricing exist.** A classic lighthouse strategy starts by
signing Allen & Overy. memseek cannot, and must not imply otherwise.

So borrow credibility from a recognizable *artifact* instead of a recognizable buyer. memseek already
has three, and they are more valuable than the site currently treats them:

1. **The gbrain catalog** (`examples/gbrain_catalog/`, `gbrain@0.13.0`) — a complete
   re-expression of Garry Tan's open-source gbrain on memseek's substrate: 9 collections, 8
   derivations, 3 views, 1 artifact, 6 MCP tools, 1 retention job. This is the closest thing memseek
   has to a lighthouse. It says *a memory design people already recognize compiles down to our
   primitives, and here is the YAML.* Borrowed credibility, no logo required.
2. **The Generative Agents reproduction** (Park et al., UIST '23) — a cited paper, reproduced on the
   shipped catalog. Academic proof travels in exactly the concentrated market we're aiming at.
3. **The benchmarks** — LongMemEval-S, MuSiQue, 2WikiMultiHopQA, and the token-reduction figure.
   Third-party datasets are proof a prospect can re-run rather than trust.

**The lighthouse arc, in order:**

- Lead with the reproduction, not the substrate. "Here is a memory system you've read about, and
  here is it declared as YAML" beats "here are eight primitives" for a skeptical reader, because it
  answers *who went first* using published work instead of a customer.
- Publish reproducibility, not assertion. A prospect who can re-run the benchmark gets the whole
  benefit of a reference customer without one existing. This is also the only version of the claim
  PRODUCT.md permits.
- Recruit 2–3 **named design partners** in one high-exposure vertical, on the explicit trade that
  they get founder-led integration and we get a publishable case study. That trade is the standard
  lighthouse deal, and the article's warning applies: price the custom work in, since lighthouse
  tolerates it and landgrab does not.
- Keep the guarantee lists. The M1–M7 guarantee sections in the README read as overkill for a
  landgrab buyer and as exactly right for a high-exposure one. A buyer asking "is this safe?" reads
  "delta visibility filters are canonicalized into a `scope_hash`" as the answer to their actual
  question. Don't sand that off.

## Which vertical

The article's sequencing advice: win a bellwether in one vertical, dominate it, then move to adjacent
verticals with similar risk/proof dynamics (Affirm → Casper → other big-ticket financed categories).

Pick the vertical where **"cites its evidence or it is rejected" is a purchase requirement rather
than a nice-to-have**. Candidates, in rough order of fit:

- **Legal / compliance agents** — provenance and point-in-time reconstruction are the product.
  Proof travels intensely. Harvey's own market, one layer down the stack.
- **Clinical and healthcare agents** — erasure, retention, and audit are regulatory, not
  aspirational.
- **Financial research and advisory** — Hebbia's market; contradiction detection over documents that
  disagree is the core job.
- **Regulated CRM / customer records** — memseek's shipped CRM-augmentation pattern already targets
  this, and PostgreSQL-canonical answers the "where does our data actually live" question.

Explicitly *not* first: coding agents, personal assistants, consumer companions. Exposure there is
low, mistakes are recoverable, and the market is fragmented — the article's textbook landgrab, which
is exactly why the fast-and-cheap memory layers compete there. Fighting on their currency means
fighting on speed-to-first-value while carrying a system-of-record's weight. That's a losing trade
today.

## Where the current site is mismatched

Applying the framework surfaces a real inconsistency worth fixing.

The landing page's second heading is **"Four commands, then it's your data."** That is landgrab
copy — speed, ease, low friction, minutes to value. But the substance underneath it is entirely
lighthouse: immutable records, guarantee lists, provenance, erasure, bounded derivations. The page is
selling on one currency and delivering on the other.

The article's framing makes the fix clear: **lead with the currency the buyer is actually paying
in.** For a high-exposure buyer, "four commands" is not reassuring — it's mildly alarming, because
it implies the system is shallow. The four-command quickstart should stay (it kills the "will this
take a quarter to evaluate" objection *after* trust exists), but it should not carry the second
screen.

Sharper ordering for the page:

1. The problem, in their language — an agent that quotes a stale fact, and nobody can find out why.
2. **The lighthouse:** a memory design you recognize, rebuilt as a catalog you can read.
3. **The proof:** benchmarks on third-party datasets, framed as re-runnable.
4. **The guarantees:** citations or rejection, point-in-time reconstruction, erasure, bounded cost.
5. *Then* four commands — "and evaluating it takes an afternoon."

The token-reduction number is the one genuinely landgrab-shaped asset. Hold it in reserve for the
transition below rather than spending it as the lead.

## Traps to watch

The article's trap lists map cleanly:

- **Hostage to the logo** → over-fitting the catalog to one design partner's schema. memseek's whole
  claim is that capability arrives as YAML over a generic substrate. A partner request that requires
  a bespoke endpoint violates Product Principle 2 and must be refused or generalized, not shipped as
  a special case.
- **Pilot purgatory** → a design partner who authors a catalog and never puts it in the request path.
  Define the exit criterion up front: production traffic, or a published post, or the engagement ends.
- **Prestige without repeatability** → a marquee partner whose integration teaches nothing reusable.
  The test is whether their catalog becomes a shippable template.
- **Dying of indigestion** (later, on landgrab) → onboarding teams who need catalog authoring help we
  can't staff. Until the catalog is a template library rather than an authoring exercise, volume is
  the wrong goal.

## Credibility hygiene

A lighthouse strategy runs entirely on trust, so the evidence discipline in PRODUCT.md stops being
housekeeping and becomes the strategy's load-bearing wall. Two concrete items:

- **`index-v3.html` states both "30× fewer tokens" and "32× fewer tokens."** PRODUCT.md names that
  file as the source of truth for any benchmark number reused elsewhere, so it cannot disagree with
  itself. Pick one, verify it against the run, and propagate. A high-exposure buyer who catches a
  wobbling headline number stops reading — and that number is precisely the one they'd check.
- **Illustrative model output must stay labeled illustrative.** No captured real-provider run for the
  gbrain showcase exists yet. Presenting representative output as a captured result would be the one
  unrecoverable error in a proof-based strategy.

## When to switch to landgrab

The article gives a clean trigger, and it translates directly:

> When buyers approach you with allocated budgets asking for demos rather than asking "who went
> first," the lighthouse has succeeded.

For memseek, the developer-infrastructure version of that signal:

- Inbound questions shift from *"does this actually work / who runs it in production"* to
  *"how does this behave at our scale / what's the operational story."*
- Prospects arrive having already read the catalog and want to discuss their schema, not the premise.
- A vertical produces the second and third deal without founder-led integration.

At that point the currency flips to math, and the token-reduction figure plus a same-day catalog
template becomes the lead. Two things must be true before that switch is safe: the catalog is a
template library rather than an authoring exercise, and deployment is genuinely under a week without
a founder in the room. Neither is true today, which is the real argument for lighthouse first — not
just buyer psychology, but product readiness. The article is explicit that landgrab requires
standardization; memseek is at M6 plus an M7 slice, with hardening still open.

## One-line summary

memseek sells to a high-exposure buyer in a market where proof travels, so it must sell proof — but
having no customers to borrow proof from, it borrows from recognizable *work* instead: a reproduced
memory design, a cited paper, and re-runnable benchmarks. Lead with that, win one regulated vertical
founder-led, and hold the ROI math until the catalog is a template rather than a project.
