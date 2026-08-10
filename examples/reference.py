"""memseek reference implementation — the whole system's SEMANTICS in one file.

Normative for semantics: latest-per-key beliefs, watermark accumulators, fused
ranking, provenance cascade behave EXACTLY as here (parity tests enforce it).
Deliberately NON-normative for architecture: this file is synchronous, SQLite,
seam-less, single-process. Part TWO of the spec defines the real architecture.

Run me:  python examples/reference.py
"""
import sqlite3, json, math, random, hashlib, uuid, datetime as dt

DB = sqlite3.connect(":memory:")
DB.row_factory = sqlite3.Row
DB.executescript("""
create table record(
  seq integer primary key autoincrement,          -- total system order
  id text unique, workspace text, collection text, entity text,
  key text,                                       -- NULL = event; set = belief
  type text, status text default 'active',        -- active | draft
  text text, embedding text, scores text default '{}',
  enriched_at text, run_id text, derived_from text default '[]',
  created_at text, last_accessed text);
""")

now = lambda: dt.datetime.now(dt.timezone.utc).isoformat()
JOBS: list[tuple[str, str]] = []          # pending (derivation, entity) — deduped
LLM_QUEUE: list[str] = []                 # canned derivation outputs (the "fake provider")

# ---------- tiny fake LLM + embeddings (deterministic) ------------------------
def embed(text: str) -> list[float]:
    rnd = random.Random(int(hashlib.sha256(text.encode()).hexdigest(), 16))
    v = [rnd.uniform(-1, 1) for _ in range(8)]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]

def cosine(a, b): return sum(x * y for x, y in zip(a, b))

def fake_llm(prompt: str) -> str:
    if prompt.startswith("SCORER: importance"):   # batch scoring, honors [importance=N]
        out = []
        for line in prompt.splitlines():
            if line[:2].strip().rstrip(".").isdigit():
                out.append(int(line.split("[importance=")[1].split("]")[0])
                           if "[importance=" in line else 5)
        return json.dumps(out)
    return LLM_QUEUE.pop(0) if LLM_QUEUE else "{}"

# ---------- supply side: ingest → enrich → accumulate → derive ----------------
DERIVATIONS = {"profile": dict(counter=("importance", 20),   # product default: 100
                               input_types=("event", "chat"), output_type="fact")}

def ingest(entity, type_, text, key=None, derived_from=(), status="active", run_id=None):
    rid = str(uuid.uuid4())
    DB.execute("insert into record(id,workspace,collection,entity,key,type,status,text,"
               "run_id,derived_from,created_at,last_accessed) values(?,?,?,?,?,?,?,?,?,?,?,?)",
               (rid, "ws", "main", entity, key, type_, status, text, run_id,
                json.dumps(list(derived_from)), now(), now()))
    return rid

def watermark(deriv, entity):                     # max high_seq of ok|noop runs
    r = DB.execute("select max(json_extract(text,'$.high_seq')) m from record "
                   "where type='run' and entity=? and json_extract(text,'$.derivation')=? "
                   "and json_extract(text,'$.status') in ('ok','noop')",
                   (entity, deriv)).fetchone()
    return r["m"] or 0

def enrich():
    """Embed + score pending rows, then check accumulators (never reset a counter:
    the pending aggregate is derived from the watermark, Section 10.4)."""
    rows = DB.execute("select * from record where enriched_at is null "
                      "and type not in ('run','contradiction') order by seq").fetchall()
    if not rows: return
    items = "\n".join(f"{i+1}. {r['text']}" for i, r in enumerate(rows))
    scores = json.loads(fake_llm(f"SCORER: importance\nRate 1-10.\nITEMS:\n{items}"))
    for r, s in zip(rows, scores):
        DB.execute("update record set embedding=?, scores=?, enriched_at=? where seq=?",
                   (json.dumps(embed(r["text"])), json.dumps({"importance": s}), now(), r["seq"]))
    for name, d in DERIVATIONS.items():           # accumulator check per touched entity
        scorer, threshold = d["counter"]
        for ent in {r["entity"] for r in rows}:
            wm = watermark(name, ent)
            pend = DB.execute(
                "select coalesce(sum(json_extract(scores,'$.'||?)),0) s from record "
                "where entity=? and seq>? and enriched_at is not null and type in "
                f"({','.join('?'*len(d['input_types']))})",
                (scorer, ent, wm, *d["input_types"])).fetchone()["s"]
            print(f"  accumulator[{name}/{ent}] pending Σ{scorer}={pend} "
                  f"(threshold {threshold}, wm={wm})")
            if pend >= threshold and (name, ent) not in JOBS:
                JOBS.append((name, ent))          # dedupe index in the real system

def run_derivation(name, entity):
    d, wm, run_id = DERIVATIONS[name], watermark(name, entity), str(uuid.uuid4())
    new = DB.execute("select * from record where entity=? and seq>? and type in "
                     f"({','.join('?'*len(d['input_types']))}) order by seq",
                     (entity, wm, *d["input_types"])).fetchall()
    if not new:
        ingest(entity, "run", json.dumps({"derivation": name, "status": "noop", "high_seq": wm}))
        return print(f"  run[{name}/{entity}] noop (nothing past wm={wm})")
    latest = {b['key']: b['text'] for b in beliefs(entity)}
    prompt = (f"Maintain beliefs about {entity}.\nCURRENT: {latest}\nNEW:\n"
              + "\n".join(f"[{r['id'][:8]}] {r['text']}" for r in new) + "\n# OUTPUT: keys")
    out = json.loads(fake_llm(prompt))            # {"records":[{key,text,citations[]}]}
    for u in out.get("records", []):
        cites = [c for c in u.get("citations", []) if DB.execute(
                 "select 1 from record where id=?", (c,)).fetchone()]  # drop unknown
        ingest(entity, d["output_type"], u["text"], key=u["key"],
               derived_from=cites, run_id=run_id)
    ingest(entity, "run", json.dumps({"derivation": name, "status": "ok",
           "high_seq": new[-1]["seq"], "resolved_models": {"final": "fake:fake"}}))
    print(f"  run[{name}/{entity}] ok → {len(out.get('records', []))} beliefs, "
          f"wm {wm}→{new[-1]['seq']}")

# ---------- read side: document, history, fused search, provenance ------------
def beliefs(entity):                              # latest ACTIVE row per key
    return DB.execute("""select * from record r where entity=? and key is not null
      and status='active' and seq=(select max(seq) from record r2 where r2.entity=r.entity
      and r2.key=r.key and r2.status='active')""", (entity,)).fetchall()

def history(entity, key):
    return DB.execute("select * from record where entity=? and key=? order by seq desc",
                      (entity, key)).fetchall()

def search(q, k=3, w=dict(rec=0.5, rel=3.0, imp=2.0)):
    """score = Σ wᵢ · minmax(signalᵢ) — the Generative-Agents fusion (default rank)."""
    qv, t = embed(q), dt.datetime.now(dt.timezone.utc)
    cands = DB.execute("select * from record where embedding is not null").fetchall()
    sig = {}
    for r in cands:
        hrs = (t - dt.datetime.fromisoformat(r["last_accessed"])).total_seconds() / 3600
        sig[r["seq"]] = dict(rel=cosine(qv, json.loads(r["embedding"])),
                             imp=json.loads(r["scores"]).get("importance", 5) / 10,
                             rec=72 / (hrs + 72))            # decay, midpoint 72h
    def norm(name):
        vals = [s[name] for s in sig.values()]; lo, hi = min(vals), max(vals)
        return {k2: 0.5 if hi == lo else (s[name] - lo) / (hi - lo) for k2, s in sig.items()}
    n = {name: norm(name) for name in ("rec", "rel", "imp")}
    ranked = sorted(cands, key=lambda r: -sum(w[x] * n[x][r["seq"]] for x in w))[:k]
    DB.executemany("update record set last_accessed=? where seq=?",
                   [(now(), r["seq"]) for r in ranked])       # touch-on-read
    return ranked

def erase(ids):
    """Provenance cascade: deleting evidence deletes everything derived from it."""
    doomed, frontier = set(ids), set(ids)
    while frontier:
        nxt = {r["id"] for r in DB.execute("select id, derived_from from record")
               if set(json.loads(r["derived_from"])) & frontier} - doomed
        doomed |= nxt; frontier = nxt
    DB.executemany("delete from record where id=?", [(i,) for i in doomed])
    return doomed

# ---------- demo trace ---------------------------------------------------------
if __name__ == "__main__":
    print("1· ingest three observations about maria")
    a = ingest("maria", "event", "Kickoff: Maria leads the platform team [importance=6]")
    b = ingest("maria", "event", "Maria confirmed Q3 budget of $40k [importance=9]")
    c = ingest("maria", "chat",  "Maria prefers async updates over meetings [importance=7]")

    print("2· enrich → accumulator fires (Σ 22 ≥ threshold 20)"); enrich()

    print("3· drain job → profile derivation writes cited beliefs")
    LLM_QUEUE.append(json.dumps({"records": [
        {"key": "role", "text": "Leads the platform team.", "citations": [a]},
        {"key": "commitments", "text": "Q3 budget of $40k confirmed.", "citations": [b]},
        {"key": "preferences", "text": "Prefers async updates.", "citations": [c]}]}))
    while JOBS: run_derivation(*JOBS.pop(0))

    print("4· /document — latest active belief per key")
    for r in beliefs("maria"): print(f"   {r['key']}: {r['text']}")

    print("5· fused search for the exact budget sentence ranks it first")
    top = search("Maria confirmed Q3 budget of $40k [importance=9]", k=1)[0]
    print(f"   top hit: {top['text']}")

    print("6· supersession — a new event revises the role belief (new row, old kept)")
    d2 = ingest("maria", "event", "Org update: Maria promoted to CTO [importance=9]"); enrich()
    LLM_QUEUE.append(json.dumps({"records": [
        {"key": "role", "text": "CTO.", "citations": [d2]}]}))
    run_derivation("profile", "maria")
    print("   history(role):", [r["text"] for r in history("maria", "role")])

    print("7· erasure cascade — deleting the budget event removes the belief citing it")
    erase([b])
    print("   beliefs now:", {r["key"]: r["text"] for r in beliefs("maria")})
    print("done — every mechanism above has a 1:1 section in Part TWO.")
