---
name: yelp-review
description: Draft a Yelp review in Yu's voice from a short intake (restaurant, city, email, items, stars), then ask targeted follow-ups about food, service, and vibe before producing a paste-ready draft. Use when the user says they just ate somewhere, wants to "write a Yelp review", "review this place", or drops a restaurant name plus a star rating.
---

# Yelp Review Drafter

Turns a few facts about a meal into a finished review that sounds like Yu wrote
it — not like a bot wrote it.

The output is a **draft to paste**, not an auto-post. See [Posting](#5-posting)
for why, and for the fastest path from draft to published.

## Run order

Do not skip steps or reorder them. Step 2 is the one that keeps the voice honest.

### 1. Intake — collect the five required fields

Ask for all five in one message. Do not start drafting until every one is filled.

| Field | Why it's needed |
|---|---|
| **Restaurant** | Exact name as Yelp lists it, so the right page is found |
| **City** | Disambiguates chains and same-name spots; also sets regional context |
| **Email** | The account to look up (see [Email lookup](#email-lookup) — confirm scope first) |
| **Items** | The specific dishes being reviewed — this is what makes a review credible |
| **Stars** | 1–5, integer. Sets the entire tone of the draft |

If a field comes back vague ("we got a few things"), push once for specifics.
"The pork belly bao and the uni pasta" produces a good review; "the food"
produces filler.

**Stars drive tone.** Match the register to the rating — do not write a 5-star
rave and staple a 3-star number on it:

- **5** — enthusiastic, specific, one standout dish named as the reason to come
- **4** — clearly positive, with one honest reservation
- **3** — genuinely mixed; say what worked and what didn't, both concretely
- **2** — disappointed but fair; no personal attacks on staff
- **1** — factual account of what went wrong; no insults, no accusations you
  can't back up, nothing that reads as a threat

### 2. Voice check — load the profile

Read `voice-profile.md` before writing a single sentence.

If that file is still marked **UNVERIFIED**, say so out loud and offer to
calibrate it first (the procedure is in the file). A draft written against an
unverified profile is a guess at the voice, and should be handed over labeled
as one.

### 3. Follow-ups — ask about what the intake always misses

The five fields cover *what* was eaten. They never cover *why it was worth
writing about*. Ask these three, in one message, after intake:

1. **Food** — anything beyond the dishes themselves? Portion, price-to-value,
   how something was cooked, a texture or temperature worth naming, something
   that didn't land?
2. **Service** — pace, attentiveness, how a problem was handled, wait time vs.
   what was quoted?
3. **Vibe** — what was the room actually like? Noise level, lighting, how
   packed it was, the crowd, music, decor, whether it felt like a date spot or
   a get-in-and-out spot. This is the section that tells a reader whether the
   place fits the night they're planning.

Then one open catch-all: **"anything I'm missing?"** — occasion, who was there,
whether it's a first visit or a tenth, parking, wait for a table, and anyone
worth crediting by name (a server who made the meal, the chef, the owner).

Accept "nothing" as an answer. A review with two real details beats one padded
with four invented ones. **Never fabricate a detail to fill a section** — if
service didn't come up, the draft doesn't mention service.

### 4. Draft

Produce **one** draft, not three variants. Structure it per the shape rules in
`voice-profile.md`.

Hard rules regardless of voice:

- Every claim traces to something the user actually said. No invented dishes,
  prices, names, or wait times.
- Name the specific items. Generic praise is what gets reviews filtered.
- No em-dash-heavy AI cadence, no "nestled in the heart of", no "culinary
  journey", no "hidden gem" unless the user said it first.
- Don't open with the restaurant name and city — Yelp already shows both.
- Length: 80–150 words unless the user asks otherwise. Long reviews get skimmed.

Present the draft plainly, then ask: **keep, tweak, or redo?** Revise on
request. This is the loop where the voice actually gets right.

### 5. Posting

**This skill does not submit reviews to Yelp**, and shouldn't be extended to.
Two reasons, both practical:

1. Automated posting violates Yelp's Terms of Service. The exposure is Yu's
   account and its entire review history, not a throwaway.
2. Yelp's recommendation software filters content that looks machine-generated
   or machine-submitted. A review that gets filtered is invisible — the work is
   wasted even when the post technically succeeds.

The supported handoff keeps a human on the submit button:

```
scripts/open-review-page.sh "Restaurant Name" "City"
```

It opens Yelp's search for the business and copies the draft to the clipboard.
Find the place, click **Write a Review**, paste, read it once, post.

Reading the draft before posting is not ceremony — it's the step that catches
the detail the follow-ups got slightly wrong.

## Email lookup

The email field exists so the right Yelp account and the right visit can be
identified. Before searching a mailbox, **confirm what to search for** —
"find my account" and "find the receipt from Saturday" are different asks with
different privacy surfaces.

Useful, with permission:

- **Yelp's own "Thanks for your review" emails** — these quote past reviews
  back in full. This is the email field's main purpose: a fallback source of
  genuine voice samples when the profile page can't be read (see the
  calibration procedure in `voice-profile.md`).
- **The reservation or receipt** — confirms date, party size, and the actual
  items ordered, so the draft doesn't misremember the meal.

Never paste raw email contents into a commit or a review draft.

## Handling a Yelp HAR file

A HAR captured from yelp.com is a **credential**. It contains live session
cookies and auth tokens in cleartext; anyone holding it can act as that Yelp
account.

- Do not commit one to this repo. `.gitignore` blocks `*.har` — leave that rule
  alone.
- Do not paste one into a chat, an issue, or a PR.
- If one was already shared somewhere it shouldn't be: log out of Yelp
  everywhere to invalidate the session, then log back in.

A HAR is genuinely useful for exactly one thing here: reading the business's
Yelp URL or ID out of it, so the write-review page can be linked directly. That
does not require the auth headers, and the auth headers should be stripped
before the file is handled at all.

## Files

- `voice-profile.md` — the style rules and the calibration procedure
- `intake-template.md` — copy-paste intake block
- `examples/mock-review.md` — worked example, intake through final draft
- `scripts/open-review-page.sh` — draft-to-clipboard handoff
