---
title: "Scoring what to serve: currency as a hard filter"
description: "A small scoring model for context selection — admissibility as a gate, similarity and decay as the ranking, and a half-life you can actually explain to a colleague."
date: 2026-07-18
author: Memseek
tags: ["Agent memory", "Retrieval", "Scoring"]
---

The previous post argued that currency isn't a ranking signal you can tune. It's
a gate. This one writes that down precisely, because "hard filter" turns out to
mean something specific once you try to implement it.

## The shape of the problem

We have a question $q$, a set of candidate facts $F$, and a timestamp $\tau$ —
usually "now," but not always. We want the subset of $F$ that is both
*admissible* at $\tau$ and *relevant* to $q$, ordered so we can cut it off at a
token budget.

Each fact $f$ carries a validity interval $[t_{\text{from}}(f),\, t_{\text{to}}(f))$.
A fact nothing has contradicted has $t_{\text{to}}(f) = \infty$. When a later
fact supersedes it, $t_{\text{to}}$ is set to the moment of supersession —
the fact isn't deleted, it's *closed*.

Admissibility is then just interval membership:

$$
A(f, \tau) = \mathbb{1}\!\left[\, t_{\text{from}}(f) \le \tau < t_{\text{to}}(f) \,\right]
$$

This is the part that has to be a gate rather than a weight. A superseded fact
with a similarity of $0.97$ must lose to a current fact with a similarity of
$0.61$, always — and no coefficient you can pick makes a soft score behave that
way at every ratio.

## Ranking what survives

Among admissible facts, two things matter: how well the fact answers the
question, and how much its age should discount it. Write similarity as
$s(f, q) \in [0, 1]$ and apply exponential decay on the fact's age:

$$
\text{score}(f, q, \tau) \;=\; A(f, \tau) \cdot s(f, q) \cdot e^{-\lambda \,\Delta t},
\qquad \Delta t = \tau - t_{\text{from}}(f)
$$

The decay constant is easier to reason about as a half-life $h$ — the age at
which a fact counts for half of what it did when new:

$$
\lambda = \frac{\ln 2}{h}
$$

Which gives you a knob you can defend in a design review. Set $h$ to 90 days
and a fact from six months ago competes at a quarter of its original weight:

$$
e^{-\lambda \Delta t} \Big|_{\,h = 90,\; \Delta t = 180} = 2^{-180/90} = 0.25
$$

Note that decay applies to *age*, not to *last edit*. A fact asserted in
February and never contradicted decays on a February clock no matter how many
times its source file was touched since — which is exactly the behavior that
recency-weighting gets wrong.

### Why not decay superseded facts instead of gating them?

Because the two mechanisms answer different questions. Decay says *this is old,
weigh it less*. Supersession says *this is wrong, don't send it*. Collapsing
them means a recently-retracted claim — high $s$, tiny $\Delta t$ — scores near
the top, which is the worst possible outcome: the freshest thing you serve is a
statement someone explicitly took back.

Keeping them separate also means the closed facts stay queryable. Set $\tau$ to
March and $A(f, \tau)$ readmits exactly the claims that were live then.

## Selecting under a budget

Ranking gives an order; the budget decides where to stop. Given a token budget
$B$ and per-fact cost $c(f)$, take facts in score order while they fit:

$$
S = \{f_1, \dots, f_m\} \quad \text{s.t.} \quad \sum_{i=1}^{m} c(f_i) \le B
$$

Greedy is the right call here, and it's worth being explicit about why: this
looks like a knapsack problem, but the optimal-packing version optimizes the
wrong objective. Squeezing in two more low-scoring facts to fill the budget is a
regression, not an improvement — the goal is the smallest set that answers the
question, not the largest set that fits.

One refinement does pay for itself: dropping near-duplicates as you go. If $f_j$
is already in $S$ and $\text{sim}(f_i, f_j) > \theta$ for some threshold
$\theta$ (around $0.9$ in practice), $f_i$ adds tokens without adding
information. Skip it and keep going down the list.

## What you can tell the caller

Because every step is a decision about a specific fact, the whole selection is
explainable. For any query you can report what was served, what was withheld,
and under which rule — admissible-but-cut-for-budget, superseded, or
near-duplicate. That report is the difference between a memory layer you can
debug and one you can only re-prompt.

The scoring here is deliberately plain: a gate, a similarity, one decay
constant, one dedup threshold. Most of the leverage isn't in the function. It's
in having the validity intervals at all.
