---
title: "The Loop Has Always Been There"
description: "Why verifiability, world models, and memory may matter more than the next model"
date: 2026-08-12
author: Juan Diego Caballero (jdc@darma.ai)
tags: ["Agent architecture", "Verifiability", "World models", "Memory"]
presentation: loop-essay
---

Watch a capable coding agent for long enough and a pattern appears. The impressive part is rarely its first answer. The impressive part is what happens next.

It writes a patch. The tests fail. It reads the failure, changes the patch, runs the tests again, and keeps going until the result works.

The same pattern appears in systems that seem, on the surface, to have little in common:

```text
generate → evaluate → select → update → repeat
```

AlphaGo used it to choose moves. FunSearch used it to discover programs. Coding agents use it to repair software. Karpathy's `autoresearch` uses it to run experiments.

This suggests a shift in what we should be paying attention to. The question is no longer only:

> How intelligent is the model?

It is increasingly:

> **What happens when a sufficiently good generator is placed inside a sufficiently good feedback loop?**

Here is the bottom line:

**The model is not the system anymore. The loop around the model is the system.**

That frame explains why coding agents work unusually well, why verifiability matters so much, why agents need world models as they leave software, why memory and metacognition become architectural concerns, and how expensive search can eventually become something that looks like intuition.

The surprising part is that the loop is not new.

It has always been there.

We are simply automating more of it.

---

## We used to be the loop

A few years ago, the ordinary LLM workflow looked like this:

```text
human → model → human judges → human changes prompt → model
```

We called it prompting. Architecturally, it was already a feedback loop.

The model generated candidates. The human inspected them, compared them with an internal model of the task, supplied missing information, rejected bad answers, remembered previous failures, chose the next attempt, and decided when the result was good enough.

The division of labor was roughly:

```text
generator       → model
verifier        → human
memory          → human
planner         → human
world model     → human
stopping rule   → human
```

In Yoko Li's *Knowing When to Stop*, this surrounding machinery becomes explicit. Once an agent operates autonomously, convergence can no longer remain an implicit judgment supplied by a person; it has to be engineered into the system. That requires an observable state, a target state, actions that move the system toward it, a way to verify progress, and a rule for deciding when to stop. ([a16z.news](https://www.a16z.news/p/knowing-when-to-stop-the-art-of-making))

Seen this way, AI engineering is partly the process of **moving pieces of cognition out of the human and into the machine**.

The important unit is no longer just the model.

It is the loop.

---

## AlphaGo showed the architecture early

AlphaGo is usually remembered as a neural-network breakthrough. But the network alone was not the system.

In AlphaGo Zero, the network supplied useful priors and value estimates. Monte Carlo Tree Search explored the alternatives. Self-play produced outcomes, and those outcomes became training data that improved the network. ([deepmind.google](https://deepmind.google/blog/alphago-zero-starting-from-scratch/))

Two kinds of computation were working together:

> What looks promising?

and:

> What still looks promising after I investigate the consequences?

The first is cheap intuition. The second is expensive search. The familiar **exploration–exploitation tradeoff** lives inside that split: exploit moves the policy already considers promising, but explore enough alternatives to discover when its prior is wrong.

That distinction has proved remarkably durable.

FunSearch replaced Go moves with programs. An LLM generated program mutations; execution scored them; the best candidates survived into later generations. The model did not need to produce the optimal program in one shot. It only needed to propose mutations that were better than blind search. ([deepmind.google](https://deepmind.google/blog/funsearch-making-new-discoveries-in-mathematical-sciences-using-large-language-models/))

AlphaEvolve extended the same idea. Gemini models generate and revise programs, automated evaluators score them, and an evolutionary database chooses which candidates become parents of future attempts. DeepMind has used the resulting algorithms in mathematical discovery and in parts of Google's infrastructure. ([deepmind.google](https://deepmind.google/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/))

A coding agent has the same shape:

```text
LLM → patch → tests → failure → new patch ↺
```

The domain changes. The architecture keeps returning.

A useful engineering mnemonic is:

$$
\text{Capability}
\approx
\text{Generation}
\times
\text{Search}
\times
\text{Verification}
$$

This is not a law of intelligence. It is a way to see the bottlenecks.

A perfect verifier with a weak generator leaves you brute-forcing an enormous space. A brilliant generator without verification produces plausible nonsense. A generator and verifier without search may accept the first candidate that happens to work.

Combine all three, however, and the requirement placed on the model becomes much weaker:

> **The model does not have to know the answer. It has to generate candidates from a distribution better than blind search while the surrounding system removes the bad ones.**

Weaker requirements tend to produce more general architectures.

---

## LLMs may be best at the fuzzy part

The pattern becomes clearer if we separate induction from deduction.

Induction moves from observations toward a possible rule. Deduction applies a rule to derive a consequence.

The paper *Inductive or Deductive? Rethinking the Fundamental Reasoning Abilities of LLMs* tries to pull those operations apart experimentally. Its SolverLearner architecture gives an LLM examples and asks it to infer the underlying function. Instead of asking the model to execute that function reliably too, the proposed function is handed to an external interpreter:

```text
examples
   ↓
LLM infers a possible rule
   ↓
Python executes the rule
   ↓
result
```

In the authors' experiments, this separation exposed a meaningful difference: SolverLearner showed strong inductive performance, while pure deductive execution was weaker, especially on counterfactual variants.

The result has important limits. The examples must constrain the possible function sufficiently. The hypothesis space cannot be arbitrarily open. Performance also depends heavily on the underlying foundation model. This is not evidence that “LLMs solve induction.”

It does support a useful architectural stance:

**Use the LLM for fuzzy induction and candidate generation. Delegate exact operations whenever another mechanism can perform them better.**

Let the LLM hypothesize. Let Python execute. Let Lean prove. Let the compiler reject. Let the database retrieve. Let the simulator predict. Let the environment answer. Let reality decide.

Tool use, from this perspective, is not an accessory attached to an LLM. It is a division of cognitive labor.

---

## Verifiability explains the jagged frontier

Andrej Karpathy's framing of **verifiability** helps explain why these loops work brilliantly in some domains and poorly in others.

Software 1.0 is strongest where we can specify a procedure directly. AI increasingly lets us automate tasks where the procedure is difficult to specify but the result is easier to judge. For autonomous iteration to work, the environment should ideally be repeatable, efficient, and automatically rewardable. ([karpathy.bearblog.dev](https://karpathy.bearblog.dev/verifiability/))

Code has exactly this asymmetry:

```text
write the implementation      hard
compile it                    cheap

find the bug                  hard
run the test                  cheap

invent an optimization        hard
benchmark it                  cheap
```

Formal mathematics often has it too: discovering a proof is hard, while checking a formal proof is easier. Games are the cleanest case of all: discovering a winning strategy is hard; determining who won is trivial.

These domains satisfy an important inequality:

$$
\text{cost of generation}
>
\text{cost of verification}
$$

When that inequality holds, an imperfect generator can become surprisingly powerful.

Karpathy's `autoresearch` is a particularly clean demonstration. The agent may modify a constrained part of a training setup. It runs an experiment for a fixed budget, receives a metric, keeps useful changes, discards bad ones, and repeats. ([github.com](https://github.com/karpathy/autoresearch))

```text
hypothesis → code change → experiment → metric → keep or discard ↺
```

The clever part is not simply that the agent is autonomous. The research problem has been transformed into a shape that autonomy can exploit.

**The environment has been engineered for feedback.**

That gives us another useful mnemonic:

$$
\text{Rate of improvement}
\propto
\text{feedback quality}
\times
\text{affordable attempts}
$$

An agent receiving reliable feedback every five minutes can accumulate far more useful search than one whose actions can only be judged six months later. That can matter as much as the raw capability of the underlying model.

---

## The verifier is both the power and the failure mode

The verifier defines the hill the system climbs.

That is the source of the loop's power, and also its central danger.

A coding agent can make every visible test pass while still producing the wrong program:

```text
tests pass ≠ software is correct
```

An agent may converge according to the observable evaluator while still failing held-out criteria that better represent the intended task.

As generators become stronger, the gap between proxy and intent matters more, not less. A weak optimizer may satisfy your proxy by accident. A strong optimizer may discover precisely how the proxy differs from what you meant.

$$
\text{strong generator}
+
\text{weak verifier}
\neq
\text{strong system}
$$

It may instead produce a strong capability for exploiting the verifier.

This reframes an important part of the frontier. We spend enormous effort improving models. For autonomous systems, the harder problem may become constructing feedback that remains trustworthy as the optimizer gets better at exploiting it.

Verification is not merely another module.

It determines what the system learns to become.

---

## Then we run out of tests

Code is unusually friendly. Reality is not.

“Should this service be rewritten?” at least admits measurements of correctness, performance, migration cost, and reliability.

“Should this company enter Brazil?” does not.

Neither does “Which research direction should a lab pursue for the next three years?” or “What should a home robot do in a situation its designers never anticipated?”

There is no clean:

```python
score(decision)
```

Reality has delayed outcomes, hidden variables, irreversible actions, adversaries, contradictory objectives, and expensive experiments. The verifier cannot execute the world ten thousand times.

This is where the next component becomes necessary.

The loop needs an imagination.

It needs a **world model**.

---

## A world model lets the loop run before reality does

AlphaZero could plan because Go supplied a perfect simulator. Try a move, explore its consequences, and search deeper. The world was cheap to copy.

MuZero took a more interesting step. It was not given the environment's complete rules. Instead, it learned an internal representation sufficient to predict the quantities needed for planning, then searched through that learned model. ([deepmind.google](https://deepmind.google/blog/muzero-mastering-go-chess-shogi-and-atari-without-rules/))

Without a world model, the loop is:

```text
idea → act → find out
```

With one, it becomes:

```text
generate many ideas
        ↓
imagine consequences
        ↓
discard most
        ↓
act on a few
```

World models let an agent **buy simulated experience when real experience is expensive**.

A robot cannot crash a thousand real cars while deciding how to drive. A company cannot enter and exit the same market ten thousand times. A scientist cannot run every conceivable experiment.

Agents operating outside software must evaluate not only candidate actions, but predicted futures. Once we add that requirement, the simple generate-and-test loop becomes a more complete cognitive architecture.

---

## The emerging reference architecture

A general agent begins to look less like a model with tools and more like a collection of cooperating systems:

```text
                         GOAL
                          │
                          ▼
                 ┌─────────────────┐
                 │ META-CONTROLLER │
                 └────────┬────────┘
                          │
          ┌───────────────┼────────────────┐
          │               │                │
          ▼               ▼                ▼
     GENERATOR         MEMORY         WORLD MODEL
   what might work?   what happened?   what happens?
          │               │                │
          └───────────────┼────────────────┘
                          ▼
                     SEARCH / PLAN
                          │
                          ▼
                        ACTION
                          │
                          ▼
                     ENVIRONMENT
                          │
                          ▼
                       CRITICS
               tests / rules / humans /
                metrics / observations
                          │
                          ▼
                        MEMORY
                          │
                   ┌──────┴──────┐
                   ▼             ▼
                 RETRY         LEARN
                   │             │
                   └──────► GENERATOR
```

Each component has a distinct job.

The **generator** handles the combinatorial explosion of possible ideas. **Search** decides which possibilities deserve more computation. The **world model** investigates futures without always paying the cost of reality. **Critics and verifiers** prevent plausibility from being mistaken for success. **Memory** allows experience to accumulate instead of evaporating at the end of a context window.

The **meta-controller** handles a problem that is easy to ignore when a human is still in charge:

> Cognition itself costs time and money.

Should the system answer now or think for another minute? Retrieve memory or generate alternatives? Run an experiment or simulate one? Ask another agent? Escalate to a human? Stop?

This is not merely reasoning. It is **reasoning about how to reason**.

And the architecture contains one more important dynamic: the loop does not only solve problems. It can change the generator itself.

---

## Search can become intuition

This may be the deepest lesson in AlphaGo.

Search produces decisions better than the neural network could produce immediately. But running a large tree search forever is expensive. The results of that expensive search can therefore become training signal. In reinforcement-learning terms, this is **policy improvement**: search finds behavior better than the current policy can produce alone, and learning distills that behavior back into the policy.

$$
\text{expensive cognition}
\xrightarrow{\text{learning}}
\text{cheap cognition}
$$

An agent's sequence of states, actions, tool results, and corrections is a **trajectory**; imagined continuations through a simulator are **rollouts**. Today's agent may need twenty tool calls, twelve errors, three searches, and a simulation to solve a class of problem. If successful trajectories and useful rollouts are stored, abstracted, turned into tools, used for retrieval, or incorporated into training, tomorrow's agent may simply recognize the pattern and act.

Search has become intuition.

A learning agent therefore operates on two timescales:

```text
within a task:
search → verify → improve

across tasks:
experience → learn → change the prior
```

The first loop solves the problem.

The second loop changes the solver.

---

## Psychology already has a vocabulary for this

The obvious analogy is System 1 and System 2: fast intuition and slow deliberation. It is useful, but too coarse.

Reinforcement learning, psychology, and computational neuroscience use a more precise distinction: **model-free** versus **model-based** control. In RL terms, model-free control relies on cached values or a learned policy rather than simulating the environment. It is fast. Model-based control uses a model of the environment's dynamics to evaluate possible futures. It is flexible, but expensive. Research in humans suggests that both contribute to behavior and interact rather than operating as isolated systems. ([pmc.ncbi.nlm.nih.gov](https://pmc.ncbi.nlm.nih.gov/articles/PMC2895323/))

The analogy to agents is deliberate, though not exact: a learned policy or model prior supplies the fast response, while world-model search performs the slower planning.

Then there is **metacognition**. Humans do not merely think. We make judgments about our thinking:

> I'm probably right. I should check this. I don't remember, so I'll look it up. This decision matters enough to spend more time on.

Studies of cognitive offloading show that confidence influences whether people use external aids such as reminders instead of relying on unaided memory. ([pmc.ncbi.nlm.nih.gov](https://pmc.ncbi.nlm.nih.gov/articles/PMC7584199/))

An agent needs the same kind of control policy:

```text
confident              → act
uncertain              → think
missing information    → retrieve
uncertain consequence  → simulate
high stakes            → verify
still uncertain        → ask
```

Intelligence is not only producing an answer. It includes knowing when not to trust the first answer, and deciding how much cognition a problem deserves.

---

## The model is one term in a larger equation

We can now expand the earlier mnemonic:

$$
\boxed{
I \approx
G
\times
S
\times
V
\times
W
\times
M
\times
C
}
$$

where:

```text
G = generation
S = search
V = verification
W = world model
M = memory / learning
C = cognitive control
```

Again, this is not an empirical formula for intelligence. Its value is diagnostic.

If generation is weak, the search space contains nothing interesting. If verification is weak, hallucinations survive. If search is weak, the first plausible answer wins. If the world model is weak, long-horizon planning fails. If memory is weak, experience does not compound. If cognitive control is weak, the agent may spend five hundred iterations polishing something already solved—or stop confidently when it should have checked.

The foundation model matters enormously.

It is simply no longer the whole equation.

---

## A different roadmap for building agents

This perspective changes the roadmap. Progress is not just a sequence of better models. It is a stack of overlapping layers:

```text
prompt engineering
       ↓
tool engineering
       ↓
agent engineering
       ↓
loop engineering
       ↓
verifier / environment engineering
       ↓
world-model engineering
       ↓
memory + learning
       ↓
cognitive architecture
```

These are not historical eras. They are layers of leverage, and we are building several of them at once.

For practitioners, the frame produces a more useful set of questions.

**What should the model generate?** Give it work where induction, synthesis, pattern recognition, ambiguity, or creativity are useful.

**What can be removed from the model and verified externally?** Tests, types, compilers, query engines, formal solvers, simulators, physical sensors, and benchmarks. Before adding another LLM-as-judge prompt, ask whether you can build an actual test.

**Can the representation change?** Sometimes the hardest part is not reasoning over an artifact, but turning it into state that can be observed, edited, compared, and scored. Better representations create better loops.

**Where should search happen?** Generate ten independent ideas or refine one? Branch or backtrack? Spend another dollar of inference?

**What deserves memory?** Not the entire transcript. Preserve failures that changed the strategy, successful abstractions, reusable tools, and facts likely to matter again.

**Where is reality too expensive?** That is where a world model or simulator earns its place.

**Who controls the budget?** A loop is not merely:

```python
while not done:
    call_llm()
```

A useful loop needs progress estimates, uncertainty, budgets, escalation paths, and stopping rules.

The most interesting question may be the last one:

**Can the loop teach itself not to need the loop next time?**

A successful trajectory can become memory. Memory can become a reusable skill. A reusable skill can become training data. Training can move yesterday's search into tomorrow's intuition.

---


## Beyond loops: self-similar systems

It is tempting to say that graphs are the architectural stage after loops. Graphs are useful: they represent branching workflows, parallel workers, debates, supervisors, specialists, dependencies, and multiple feedback paths.

But a graph is a topology. It does not introduce a new principle of intelligence by itself.

A node in the graph may contain a loop. A team of agents may form another loop. The whole system may then participate in a larger loop with its environment.

Zoom in, and an agent generates, evaluates, and updates. Zoom out, and a team proposes, coordinates, evaluates, and reorganizes. Zoom out again, and an organization observes its environment, allocates agents, acts, measures consequences, and adapts.

The same computational motif appears at several scales.

A verifier may itself be an agent with its own generator, search process, memory, and verifier. A planner may call a collection of specialist loops. A multi-agent system may become one component inside the world model or controller of a still larger system.

The progression may therefore be less like:

```text
loop → graph → more advanced graph
```

and more like:

```text
loop
  ↓
loops composed from loops
  ↓
coordination between loops
  ↓
self-similar cognitive systems
```

At that point, **coordination** becomes a first-class problem.

Who gets which task? Who owns shared state? Which conclusions propagate? Who can overrule whom? What should happen in parallel? When should agents communicate? How do we prevent duplicated work? How do we stop one agent's error from becoming everyone else's premise? How does a collection of individually competent systems become a competent whole?

These no longer sound like prompting questions. They sound like problems from distributed systems, operating systems, markets, organizations, biology, control theory, and social systems.

The loop has always been there.

What comes next may be learning how loops become societies.

But that is another article.

---

## References

1. Andrej Karpathy, *Verifiability*. ([karpathy.bearblog.dev](https://karpathy.bearblog.dev/verifiability/))
2. Andrej Karpathy, *autoresearch*. ([github.com](https://github.com/karpathy/autoresearch))
3. DeepMind, *AlphaGo Zero: Starting from Scratch*. ([deepmind.google](https://deepmind.google/blog/alphago-zero-starting-from-scratch/))
4. DeepMind, *FunSearch: Making New Discoveries in Mathematical Sciences Using Large Language Models*. ([deepmind.google](https://deepmind.google/blog/funsearch-making-new-discoveries-in-mathematical-sciences-using-large-language-models/))
5. DeepMind, *AlphaEvolve: A Gemini-powered Coding Agent for Designing Advanced Algorithms*. ([deepmind.google](https://deepmind.google/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/))
6. DeepMind, *MuZero: Mastering Go, Chess, Shogi and Atari Without Rules*. ([deepmind.google](https://deepmind.google/blog/muzero-mastering-go-chess-shogi-and-atari-without-rules/))
7. Cheng et al., *Inductive or Deductive? Rethinking the Fundamental Reasoning Abilities of LLMs*.
8. Gläscher et al., *States versus Rewards: Dissociable Neural Prediction Error Signals Underlying Model-Based and Model-Free Reinforcement Learning*. ([pmc.ncbi.nlm.nih.gov](https://pmc.ncbi.nlm.nih.gov/articles/PMC2895323/))
9. Engeler & Gilbert, *The Effect of Metacognitive Training on Confidence and Strategic Reminder Setting*. ([pmc.ncbi.nlm.nih.gov](https://pmc.ncbi.nlm.nih.gov/articles/PMC7584199/))
