# Design — the UI/UX role

## 1. When this applies

Load this when the work produces something a person looks at: a page, a component,
a dashboard, a slide deck, a banner, an email template, a video frame. Not for a
script, a migration, a cron job or an API with no interface.

A one-line copy change or a single CSS value stays T0 and skips this file. The role
is not a ceremony to perform on every commit; it is what stops a build from reading
as assembled instead of designed.

## 2. What the role owns

The designer owns **the direction and the acceptance bar**, not the pixels of every
commit. It asks the one question no other role asks:

> Would someone who designs for a living recognise this as designed, or as assembled?

The gap between the two is rarely talent. It is that nobody wrote the direction down
before the first component existed, so every later decision was taken locally, each
one defensible, and the sum reads as generic. Design debt is decided at the start and
paid at the end.

`UX/Product` in `quality-gates.md` §4 asks whether the thing solves the user's
problem. That is a different question and both are needed.

## 3. The stages

Each stage produces an artifact someone else can read. A stage with no artifact did
not run — "I kept it in mind" is not an output. This is the ICM stage contract
applied to design; see `references/orchestration.md` §ICM.

| # | Stage | The artifact that must exist | Skills to draw on |
|---|-------|------------------------------|-------------------|
| 1 | **Direction** | A short `DIRECTION.md` (or a section in the plan): who looks at it, the one adjective it must earn, two visual references, the palette, the type pairing, the motion budget | `design-taste-frontend`, `high-end-visual-design`, and one style skill — `minimalist-ui`, `industrial-brutalist-ui`, `apple-design` |
| 2 | **Tokens** | Tokens in code, three layers: primitive → semantic → component. No raw hex in a component | `design-system`, `stitch-design-taste`; `brand` / `brandkit` when an identity is in play |
| 3 | **Library** | A named choice per need, with the reason in one line | `pick-ui-library` (user-invoked), `ui-styling` for shadcn/Tailwind, Context7 for the actual API |
| 4 | **Variants** | Two or three genuinely different builds of the hard screen, seen side by side | `prototype` (user-invoked) |
| 5 | **Motion** | A motion spec: what animates, why, which property, which curve, how long | `animate`, `apple-design`, `emil-design-eng`, `animation-vocabulary`, `gsap-*` for GSAP work |
| 6 | **Review** | A findings list, not an approval | `web-design-guidelines` (Web Interface Guidelines, a11y), `ui-ux-pro-max`, `review-animations` (user-invoked) |

Stages 1 and 2 are the ones that get skipped and the ones that decide the result.
Skipping 4 is normal on a small change; skipping 6 is not.

Four of these skills carry `disable-model-invocation: true` — `prototype`,
`pick-ui-library`, `review-animations`, and `wayfinder` alongside them. **Suggest
them and let the user run the slash command**; an agent cannot invoke them, and
reporting a stage as done through a skill that never ran is a false claim.

## 4. The default direction

A direction chosen deliberately overrides this section. A direction *not* chosen is
exactly how generic happens, so in its absence use this one. It is one coherent look,
derived from interfaces that hold up, not a list of options.

- **One canvas, one accent.** The canvas is near-white or near-black, never pure
  `#fff` / `#000`. Every other surface is a step from it. Exactly one saturated
  colour carries meaning; a second saturated colour means one of them is decoration.
  Semantic colours (success, warning, danger) are not the accent.
- **Hierarchy by size and weight, not by colour.** The number is large and near-black;
  its label is small, muted, and uppercase or plain — never both. When everything is
  emphasised nothing is.
- **Cards, not boxes.** Radius 16–24px on containers, 8–12px on controls. One soft,
  wide, low-opacity shadow; a visible border *and* a shadow on the same element is a
  decision made twice.
- **Controls are pills.** Segmented navigation, filter chips, status badges. The
  active state is a filled pill that **moves** between positions rather than
  appearing in place — the movement is what tells the eye where the state went.
- **Motion is functional.** Animate a state change, a position change or an entrance
  under the user's finger. Nothing loops, nothing animates to be noticed. UI
  transitions land in 120–260ms; anything dragged or dismissed uses a spring, not a
  duration.
- **Density without noise.** Dense data is fine; dense *chrome* is not. Remove
  dividers the whitespace already draws, remove labels the value already implies.
  The bar is "make complex data feel simple", and simple here means fewer lines on
  screen, not less information.
- **Type.** At most two families, one of which may be a mono for figures. A declared
  scale (e.g. 12/14/16/20/28/40/56), never an arbitrary size. Headline tracking
  slightly negative, body never justified.

## 5. Acceptance — the design gate

Check these against the rendered thing, not the source. Reading the CSS tells you
what you wrote; it does not tell you what shipped.

- [ ] The direction was written **before** the first component, and the result still
      matches it
- [ ] Tokens exist and components consume them — `rg` finds no raw hex outside the
      token file
- [ ] One accent. Count the saturated colours actually rendered
- [ ] Type scale declared and respected; at most two families
- [ ] Body contrast ≥ 4.5:1, UI and large text ≥ 3:1 — measured, not judged by eye
- [ ] Touch targets ≥ 44×44px; any input the user types into has ≥ 16px font, or
      mobile Safari zooms the page on focus
- [ ] Every animation honours `prefers-reduced-motion`
- [ ] Focus is visible on every interactive element, and the keyboard reaches all of
      them in a sensible order
- [ ] Icons come from one declared set; no emoji standing in for an icon
- [ ] Checked at 375px, 768px and 1440px — and at 200% browser zoom
- [ ] Empty, loading and error states exist and were looked at, not just the happy one
- [ ] Long content, the longest real name, and the largest real number were pasted in
      and nothing broke

**Measure it in a browser you drive.** A page that renders broken still returns a
clean scan if nothing is measured on it. Screenshot the viewport, read the computed
values, then judge.

## 6. What it costs and what it sends

Image and asset generation is where a design pipeline quietly spends money. Measured,
not assumed: the `design` skill reads `GEMINI_API_KEY`, `GOOGLE_API_KEY`,
`ATLASCLOUD_API_KEY` and `MUAPI_API_KEY` for logo and identity generation;
`ui-ux-pro-max` reads `GOOGLE_FONTS_API_KEY`. The image-direction skills
(`imagegen-frontend-web`, `imagegen-frontend-mobile`, `banner-design`, `image-to-code`)
name no backend of their own and will use whatever generator the host provides.

So: **never run an asset-generating stage unattended, and never in a loop.** The first
unattended run spends real money. Before relying on any of them the first time, apply
`references/quality-gates.md` §7 — what it executes, what it asks for, where it sends.

## 7. Failure modes

- **Polishing instead of directing.** Ten rounds of spacing tweaks on a layout whose
  direction was never chosen. Stop and write stage 1.
- **The style skill as a costume.** Loading `industrial-brutalist-ui` over an existing
  product does not make it coherent; it makes two products in one.
- **Approval-shaped review.** Stage 6 returns findings. A review that returns "looks
  great" attacked nothing — the same trap as an empty verification round.
- **Design where none was asked.** An internal tool that works does not need a
  redesign because a skill for it is installed. No unrequested scope, here as anywhere.
