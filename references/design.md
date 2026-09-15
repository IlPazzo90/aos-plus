# Design — the UI/UX role

## 1. When this applies

Load this when the work produces something a person looks at: a page, a component,
a dashboard, a slide deck, a banner, an email template, a video frame. Not for a
script, a migration, a cron job or an API with no interface.

A one-line copy change or a single CSS value stays T0 and skips this file entirely —
the gate included. The role is not a ceremony to perform on every commit; it is what
stops a build from reading as assembled instead of designed.

**Adopting it on something that already exists.** Most surfaces were built without a
written direction, so "write the direction first" cannot be satisfied retroactively
and must not be read as a reason to stop. On an existing surface, stage 1 is to write
down the direction the shipped result already implies — the palette actually in use,
the type actually in use, the radius actually in use — and to name what departs from
it. That document is the direction from then on. Adopt it for the part you are
touching; do not restyle the rest of the product because the role woke up.

**Surfaces that are not web pages.** A native app, a slide deck, a PDF or a video
frame is in scope, but the browser-specific checks in §5 are not. Substitute the
equivalent for that medium — simulator or device at the OS text sizes for native,
the export at final size for print and video — and use `ios-design-review` where it
applies. A web replica of a native screen verifies the replica, not the product.

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
| 0 | **Plan review** | The plan reviewed by a designer's eye before anything is built | `plan-design-review` |
| 1 | **Direction** | A short `DIRECTION.md` (or a section in the plan): who looks at it, the one adjective it must earn, two visual references, the palette, the type pairing, the motion budget | `design-consultation`, `design-taste-frontend`, `high-end-visual-design`, and one style skill — `minimalist-ui`, `industrial-brutalist-ui`, `apple-design` |
| 2 | **Tokens** | Tokens in code, three layers: primitive → semantic → component. No raw hex in a component | `design-system`, `stitch-design-taste`; `brand` / `brandkit` when an identity is in play |
| 3 | **Library** | A named choice per need, with the reason in one line | `pick-ui-library` (user-invoked), `ui-styling` for shadcn/Tailwind, Context7 for the actual API |
| 4 | **Variants** | Two or three genuinely different builds of the hard screen, seen side by side | `prototype` (user-invoked), `design-shotgun` |
| 5 | **Motion** | A motion spec: what animates, why, which property, which curve, how long | `animate`, `apple-design`, `emil-design-eng`, `animation-vocabulary`, `gsap-*` for GSAP work |
| 6 | **Review** | A findings list, not an approval | `web-design-guidelines` (Web Interface Guidelines, a11y), `ui-ux-pro-max`, `design-review`, `review-animations` (user-invoked) |

Stages 1 and 2 are the ones that get skipped and the ones that decide the result.
Skipping 4 is normal on a small change; skipping 6 is not.

Three of these carry `disable-model-invocation: true` in their frontmatter —
`prototype`, `pick-ui-library`, `review-animations`, with `wayfinder` alongside them.
The gstack skills below do not. **Suggest the blocked ones and let the user run
the slash command**; an agent cannot invoke them, and reporting a stage as done
through a skill that never ran is a false claim. Read the frontmatter of anything you
plan to route to before promising it, because the list changes with every install.

`plan-design-review`, `design-consultation`, `design-shotgun`, `design-review` and
`design-html` come from gstack, which installs them **under different names per host**:
as written here on Claude, prefixed `gstack-` under `~/.codex/skills` on Codex. Empty
directories of the same name may also sit in the shared skills root — inert leftovers,
not the install. Resolve the name on the host you are actually running on.

## 4. The default direction

A direction chosen deliberately overrides this section. A direction *not* chosen is
exactly how generic happens, so in its absence use this one. It is one coherent look,
derived from interfaces that hold up, not a list of options.

- **One canvas, one accent.** The canvas is near-white or near-black, never pure
  `#fff` / `#000`. Every other surface is a step from it. Exactly one saturated
  colour carries meaning; a second saturated colour means one of them is decoration.
  Semantic colours (success, warning, danger) are not the accent.
- **Hierarchy by size and weight, not by colour.** The number is large and takes the
  highest-contrast foreground the canvas allows — near-black on a light canvas,
  near-white on a dark one. Its label is small, muted, and uppercase or plain — never
  both. Muted means a step toward the canvas that still clears the contrast floor in
  §5, not a grey chosen because it looked calm. When everything is emphasised nothing is.
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

- [ ] A written direction exists and the result matches it. On new work it was written
      before the first component; on an existing surface it is the one reconstructed in
      §1, and the departures from it are named
- [ ] Tokens exist and components consume them — `rg` finds no raw hex outside the
      token file
- [ ] The accent count matches the direction. Count the saturated colours actually
      rendered, **excluding the semantic ones** (success, warning, danger, info) —
      a red error next to a blue button is one accent, not two. The default direction
      allows one, and a direction that allows more says which and why; an unexplained
      second accent fails either way
- [ ] Type scale declared and respected, and the family count matches the direction;
      the default allows two
- [ ] **Both themes, if the product has two.** Light and dark are not the same design
      with the lightness flipped, and the one nobody works in is the one that fails.
      On the first product this gate was run against, five contrast failures existed
      **only in the light theme**, which had never once been measured
- [ ] Body contrast ≥ 4.5:1, UI and large text ≥ 3:1 — measured, not judged by eye,
      and measured against the surface the element actually sits on: a chip inside a
      card inside a page has three candidate backgrounds and only one of them is right
- [ ] Touch targets ≥ 44×44px **on both axes**, measured on the element holding the
      shortest real content. Height usually comes from padding and passes; width comes
      from the text and does not. A row link on a two-letter surname measured 11×44 and
      passed a height-only check twice
- [ ] Any input the user types into has ≥ 16px font wherever a finger can reach it, or
      mobile Safari zooms the page on focus. `any-pointer`, not `pointer`: `pointer`
      describes the *primary* pointer, so a tablet with a keyboard reports fine
- [ ] Every animation honours `prefers-reduced-motion`
- [ ] Focus is visible on every interactive element, and the keyboard reaches all of
      them in a sensible order
- [ ] Icons come from one declared set; no emoji standing in for an icon
- [ ] Web: checked at 375px, 768px and 1440px, and at 200% browser zoom. Native: on
      the device or simulator at the smallest and largest OS text size. Print and
      video: on the export, at final size
- [ ] Empty, loading and error states exist and were looked at, not just the happy one
- [ ] Long content, the longest real name, and the largest real number were pasted in
      and nothing broke

**Measure it where it actually runs** — a browser you drive for the web, the simulator
or device for native, the exported file for print and video. A page that renders broken
still returns a clean scan if nothing is measured on it. Screenshot it, read the
computed values, then judge.

### Prove the detector before you believe its zeros

A measurement that finds nothing and a measurement that cannot find anything produce the
same output. This is the empty-round trap from `quality-gates.md` §3, one level down:
there an unanswered round looks like a PASS, here a broken probe looks like a clean page.
So before reporting a zero, **plant the defect the probe is meant to catch and confirm it
screams** — then remove it and confirm the page is byte-identical. Four traps that each
return a confident zero:

- **A rect is not a touch target.** `getBoundingClientRect()` describes the element; what
  the finger hits is whatever `document.elementFromPoint` returns. Grow from the centre
  until it stops answering, and scroll the element into view first — in a background tab
  the rect is all zeros.
- **Colours do not come back as you wrote them.** `getComputedStyle` hands back `oklch`
  or `lab`, so a contrast routine expecting `rgb()` silently reads nothing. Paint the
  value into a 1×1 canvas and read the pixel.
- **A rule can be present and never apply.** Walk the CSSOM recursively: a top-level pass
  misses everything nested inside `@layer` and `@media`, and reports zero matches for a
  rule that is right there.
- **The tool's viewport is not the user's, and it will not tell you.** The danger is not
  a resize that refuses — it is one that reports success and changes nothing. Measured on
  2026-09-15: `resize_window` answered `Successfully resized window ... to 375x812`, and
  `window.innerWidth` stayed **1920**; repeated at 600×800 after a three-second wait, same
  declared success, same 1920. A responsive check trusting that reply would have reported
  "verified at 375px" while measuring the desktop. So **read `window.innerWidth` back
  after every resize and compare it to the target**; if it diverges, the check did not
  run. When the viewport genuinely cannot be reached — and page zoom usually cannot —
  apply by hand the declarations that breakpoint computes, measure that, and **say in the
  report that it is a substitution**. A box ticked by a check that could not run is worse
  than an empty box.

## 6. What it costs and what it sends

Image and asset generation is where a design pipeline quietly spends money. Measured,
not assumed: the `design` skill reads `GEMINI_API_KEY` / `GOOGLE_API_KEY`,
`ATLASCLOUD_API_KEY` and `MUAPI_API_KEY` — `os.environ.get` in `scripts/logo/generate.py`
and `scripts/cip/generate.py`, an operational read, not a line of setup documentation.
The image-direction skills (`imagegen-frontend-web`, `imagegen-frontend-mobile`,
`banner-design`, `image-to-code`) name no backend of their own and will use whatever
generator the host provides.

**A grep hit is not a read.** The first version of this section also claimed
`ui-ux-pro-max` reads `GOOGLE_FONTS_API_KEY`. It does not: the only two occurrences in
the installed copy are inside a test that *unsets* the variable and asserts an error
message, and the script that would read it is not shipped. Finding a name is evidence
that somebody typed it. Before naming a key, find the line that reads it.

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
