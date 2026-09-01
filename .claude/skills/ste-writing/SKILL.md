---
name: ste-writing
description: Rewrite prose (docs, READMEs, PR descriptions, error messages, release notes, commit messages, comments — never code) into ASD-STE100 Simplified Technical English to remove "AI slop". Use when asked to make writing not sound like AI, make docs clear or plain, cut the slop, enforce a controlled writing style, or write technical documentation that reads human. Two modes — strict (procedures/safety) and STE-flavored (general prose). Bundles a deterministic linter to score drafts.
---

# ste-writing

Write prose in ASD-STE100 Simplified Technical English. This applies to documentation, READMEs, pull-request text, commit messages, error messages, release notes, and comments. It does not apply to code, identifiers, or command syntax. It is not for marketing copy, essays, or anything that needs a voice — STE strips voice on purpose. If the user wants a voice, tell them this skill does not fit.

## Rules

WORDS
- Use one name for one thing. Do not call the same item by two different names.
- Use the short common word: start (not begin/commence/initiate), use (not utilize/leverage), help (not facilitate), make sure (not ensure), before (not prior to), after (not subsequent to), about (not regarding/concerning), get (not obtain/acquire), show (not demonstrate), also (not additionally/furthermore/moreover).
- Give each word one meaning. "fall" means to move down, not to decrease.
- No marketing adjectives: seamless, robust, powerful, cutting-edge, effortless, world-class, next-generation, revolutionary.
- American spelling.

VERBS
- Active voice. "the parser reads the file", not "the file is read by the parser".
- Use a verb for an action. "analyze the log", not "perform an analysis of the log".
- No stacked auxiliaries. Not "it is important to note that this may help to improve". Write "this improves X".
- No "-ing" main verb where a simple tense works.
- No phrasal verbs. Write "start" not "spin up", "deploy" not "roll out", "ask" not "reach out".

SENTENCES
- One instruction per sentence. Max 20 words (instruction), max 25 (descriptive).
- No contractions. Use articles: a, an, the, this, these.

PUNCTUATION
- No semicolons. Write two sentences.
- No em dash. STE itself does not ban the em dash, but the em dash is a strong slop marker, so remove it here.

STRUCTURE
- One topic per paragraph, max six sentences. For steps, use a numbered vertical list, one action per item, imperative form. Put a condition before its command.

Write only the requested text. No preamble, no summary, no closing remarks.

## Modes

- **strict** — procedures, runbooks, safety text, error messages: apply every rule and both length caps. Aim for a linter score near 0.
- **STE-flavored** — general prose (READMEs, PR descriptions, docs): apply the sentence, paragraph, active-voice, no-phrasal-verb, and no-slop-word discipline. Relax the ~900-word dictionary lockdown so the text keeps enough range to read naturally.

Pick the mode from the text type. When the user does not say, default to **STE-flavored** for READMEs/PRs/docs and **strict** for procedures, errors, and runbooks. State which mode you used.

## Workflow with the linter

The skill bundles a deterministic linter at `scripts/ste-lint.py`. It counts the machine-checkable violations and reports a `total_per100w` score. Lower is cleaner. The score delta between the draft and the rewrite is the signal that the rewrite worked.

Run it two ways:

```bash
# Score a file
python3 scripts/ste-lint.py path/to/draft.md

# Score text on stdin (full JSON breakdown)
echo "your text here" | python3 scripts/ste-lint.py
```

`scripts/` is relative to this skill's directory. Use the absolute path to the skill when the working directory differs.

### Reading the output

The linter prints a JSON breakdown (stdin) or a one-line summary per file. Read it like this:

- **`total_per100w`** is the headline score: violations per 100 words. Lower is cleaner. This is the number to quote and to compare before and after. Use it, not the raw `total`, so texts of different length compare fairly.
- **`violations`** is the per-category count. Each key maps to one rule in the self-lint list below. Use it to find what to fix.
- **`em_dash(slop-marker)`** sits OUTSIDE `total` and does not change the score. STE does not ban the em dash, so the linter only reports it. This skill treats it as a slop marker, so still remove em dashes in a rewrite.
- **`longest_sentence_words`** is the worst single sentence. Split it first.
- **`sample_banned`** and **`sample_marketing`** list up to six offending words so you can find them fast.

Rough scale (from the source experiment, general prose): baseline AI prose scores about **4.4** per 100 words. Orwell's rules get it to about **2.5**. A clean STE rewrite lands near **1.1**, and strict-mode procedures should sit near **0**. Treat about 1 or below as clean.

Two cautions:
- The scale is calibrated on ordinary prose. Dense reference docs (tables, chained pipeline steps, status lines) can score high on sentence length and punctuation while the vocabulary is clean. Read the per-category counts before you call a text "sloppy" — a high score driven only by `long_sentence`, `semicolon`, and `contraction` is a style mismatch, not slop. Real slop shows up in `banned_word`, `marketing_adjective`, `nominalization`, and `modal_hedge`.
- A low score means the FORM is clean, not that the content is true. See Limits.

Standard loop:
1. If a baseline text exists (the user's draft, an existing file), lint it first and note the score.
2. Rewrite the text with the rules above in the chosen mode.
3. Write your rewrite to a temp file and lint it.
4. Read the JSON breakdown. For each remaining violation, fix the specific sentence and lint again.
5. Report the before score and the after score so the user sees the delta.

Do not hand back a rewrite whose linter total is higher than the baseline.

## Self-lint (the checks the linter runs)

The linter flags these. Fix each one:

1. Any sentence over 20 words? Split it.
2. Any semicolon? Replace with a period.
3. Any contraction? Expand it.
4. Any passive voice with a known actor? Make it active.
5. Any "-ing" main verb, nominalization ("perform an analysis"), or phrasal verb ("spin up")? Replace with a plain verb.
6. Any banned slop word or marketing adjective? Replace with the plain word.
7. Any em dash? Rewrite the sentence without it.
8. Same thing named two ways? Pick one name.

## Limits

The mechanical rules above are lintable and are what removes slop. Full STE also needs human judgment: the right technical noun, and whether a sentence "makes good sense". A checker cannot certify that, and it is not what slop is about. This skill fixes the FORM of slop. It cannot make a hollow paragraph true. A low linter score means the prose is clean, not that the content is correct — check the facts yourself.

Free official standard (do not paste it in full; it is copyrighted): https://asd-ste100.org
