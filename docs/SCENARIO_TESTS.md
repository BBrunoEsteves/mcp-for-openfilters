# OpenFilters MCP — scenario tests

Realistic coating tasks for a Claude chat to run on its own and report on. Where a result is known
(closed forms, or values computed with the connector's engine from built-in materials), it is given as a
**reference**; elsewhere the check is a physical or behavioural one.

Five batches, one chat each (a batch takes 20–40 minutes):

| Batch | Theme | Scenarios |
|---|---|---|
| 1 | Fundamentals with known answers | S1.1–S1.8 |
| 2 | Applications (design tasks) | S2.1–S2.11 |
| 3 | Analysis and manufacturing | S3.1–S3.9 |
| 4 | Robustness: bad inputs, limits, edge cases | S4.1–S4.14 |
| 5 | Recheck of the Batch 3 and 4 fixes, plus spot checks | S5.1–S5.15 |

## How to run a batch

1. Fully quit and reopen Claude Desktop, then open a new chat (Chat tab).
2. Attach this file (**+** → *Upload or add files*) and paste:

> You are testing the OpenFilters connector. Run every scenario of **Batch N** of this file in order, using
> the openfilters tools. For each scenario, report:
> - the scenario ID and a verdict: **PASS** (meets every check), **FAIL** (a check fails because of the
>   connector or its results), **LIMIT** (the request is physically impossible or out of reach and the
>   connector says so correctly), or **BLOCKED** (you could not run it, e.g. a permission prompt);
> - the evidence: the values you got next to the reference values, the tools you called, and every error
>   message **verbatim**;
> - anything surprising, slow (over 1 minute) or unclear in the connector's behaviour.
>
> Do not retry a failing call more than once and do not work around failures: report them. Name every
> material you create with the prefix SC_, and remove the SC_ materials and any material you imported
> when the batch is done. End with a summary table and a list of suggested connector improvements,
> separating real problems from cosmetic ones.

(Write the batch number in place of **N**.) Then paste the chat's whole report into the Claude Code
session for evaluation.

Conventions used below: R, T, A in %, wavelengths in nm, layers listed from the substrate outward.

---

## Batch 1 — Fundamentals with known answers

**S1.1 Uncoated glass.** *Compute R of bare BK7 (one face only) at 550 nm, then T of a 1 mm BK7 plate with
both faces counted.*
Reference: R = **4.239 %** (one face); T = **91.87 %**, R = 8.13 % (both faces, incoherent substrate).
Check: the second calculation uses consider_backside.

**S1.2 Brewster angle.** *At what angle does p-polarized reflection vanish on bare BK7 at 550 nm? Use an
angle scan.*
Reference: **56.63°** (atan n, n = 1.5185).

**S1.3 Metal mirrors.** *Reflectance at 400, 550 and 1064 nm of 200 nm Ag, 200 nm Au, 200 nm Cu and 100 nm
Al, each on BK7.*
Reference (built-in data):

| Metal | 400 nm | 550 nm | 1064 nm |
|---|---|---|---|
| Ag 200 nm | 86.6 | 96.0 | 98.3 |
| Au 200 nm | 39.1 | 84.8 | 98.0 |
| Cu 200 nm | 51.0 | 63.6 | 98.5 |
| Al 100 nm | 92.4 | 92.1 | 95.7 |

Check: the trends are explained (gold and copper are poor in the blue).

**S1.4 Single-layer antireflection.** *Quarter-wave SiO2 at 550 nm on BK7: R at 550 nm, and why it is a poor
AR. Then import MgF2 from refractiveindex.info (a thin-film entry if available) and repeat.*
Reference: SiO2 gives R = **3.39 %** (its index is close to BK7's). MgF2 should give roughly 1.2–1.4 %
depending on the data chosen.

**S1.5 Laser mirror.** *(HL)^15 H quarter-wave stack at 1064 nm, H = TiO2, L = SiO2, on FusedSilica: R and
optical density at 1064 nm, the band with R > 99.9 %, and R at 45° for s and p.*
Reference: R = **99.99955 %**, OD = **5.35**, R > 99.9 % from **957 to 1197 nm**; at 45°: Rs = 99.99968 %,
Rp = **99.751 %**.

**S1.6 Fabry–Perot band-pass.** *(HL)^5 HH (LH)^5 at 532 nm, H = TiO2, L = SiO2, on FusedSilica: peak
transmission, its wavelength and the FWHM.*
Reference: peak T = **96.3 %** at **532.0 nm**, FWHM ≈ **1.1 nm**. Check: the answer explains why the peak
is below 100 % (the substrate and air differ, so the cavity's mirrors are not matched).

**S1.7 Silicon wafer.** *R of a bare Si wafer at 550 nm.*
Reference: R = **36.8 %**.

**S1.8 Energy balance.** *For 50 nm of Ag on BK7 at 550 nm, give R, T and A from the front and from the
substrate side.*
Reference: front R = 92.1 %, T = 3.48 %, A = 4.42 %; from the substrate side R = 90.3 %, T = 3.48 % (equal),
A = 6.19 %. Check: R + T + A = 100 % both ways, T the same both ways, R and A different.

---

## Batch 2 — Applications

These are design tasks with no single right answer. Each check is about honest, physically sensible
results; when the connector says a target is met, verify it with evaluate_design.

**S2.1 Broadband visible AR.** *AR coating on BK7, R ≤ 0.5 % from 430 to 670 nm at normal incidence, TiO2
and SiO2, at most 6 layers, no layer thinner than 10 nm. Use a campaign with a yield stage.*
Check: designs respect 6 layers and 10 nm; ranking is by yield; "met" agrees with the worst R.

**S2.2 V-coat for a laser line.** *Two-layer AR on FusedSilica with R < 0.05 % at 1064 nm, TiO2 and SiO2.*
Check: a two-layer solution exists (for example about 35 nm TiO2 and 245 nm SiO2; other thickness pairs also
work); R at 1064 nm < 0.05 %; the V-shaped curve around 1064 nm is described.

**S2.3 High-power laser mirror.** *Start from S1.5's mirror, add a half-wave SiO2 overcoat at 1064 nm, and
compare the electric-field peak (value and position) with and without it.*
Check: with TiO2 outermost the surface is already a field node, so the overcoat's effect is to move the |E|²
peak away from the outer TiO2/SiO2 interface into the SiO2 (where it reaches about 4/n_L², ≈ 1.9); R at 1064 nm
stays ≥ 99.99 %.

**S2.4 45° dichroic beam combiner.** *On FusedSilica at 45°: R ≥ 99 % at 532 nm for both s and p, and
T ≥ 95 % at 1064 nm for both s and p, TiO2/SiO2, at most 30 layers.*
Check: separate s and p targets are used; p is the harder polarization; the result is honest about which
targets are met.

**S2.5 Plate polarizer.** *Thin-film plate polarizer at 56° incidence at 1064 nm on FusedSilica: Tp ≥ 95 %
and Rs ≥ 99 %.*
Check: s and p targets; the design exploits the s/p band-width difference; the polarization extinction
(Tp/Ts) is reported.

**S2.6 Hot mirror.** *T ≥ 90 % from 420 to 650 nm and R ≥ 95 % from 750 to 1100 nm at normal incidence on
BK7, TiO2/SiO2.*
Check: a quarter-wave start near 900 nm is refined or needle-optimized; pass-band ripple is reported.

**S2.7 Narrow band-pass.** *Band-pass at 532 nm, FWHM 2 to 3 nm, peak T ≥ 90 %, on FusedSilica.*
Check: the FWHM achieved is measured from the spectrum; the angle shift at 10° is reported (the peak moves
to shorter wavelengths).

**S2.8 Infrared AR on germanium.** *AR for a Ge window over 8–12 µm. First try with the built-in Ge, then
import Ge and ZnS (or YbF3) data covering 8–12 µm from refractiveindex.info and design a 2–4 layer AR with
R ≤ 1 %.*
Check: the built-in Ge is **refused** (its data end at 1.77 µm; the connector reports an unphysical index or
an extrapolation warning, not a silent result); with imported data a design is found.

**S2.9 Dispersive mirror.** *Mirror for 760–840 nm, s-polarized, R ≥ 99.5 %, with GDD = −20 ± 5 fs² across
the band, starting from (HL)^10 H at 800 nm (TiO2/SiO2, FusedSilica).*
Check: GD/GDD are reported in fs and fs² (a few fs and fs², not 1e-15 or 1e-30); the start's GDD, computed
with steps of 5 nm or less, is about +4 fs² at 760 nm and −4 fs² at 840 nm; the optimizer moves the GDD
towards −20 fs²; R ≥ 99.5 % is kept or the trade-off is stated. (GD and GDD are numerical derivatives
over the wavelengths requested; a coarse grid gives a warning.)

**S2.10 Decorative colour.** *A coating on BK7 that reflects a blue colour L\*a\*b\* ≈ (50, 0, −40) at 0°
(D65), TiO2/SiO2, at most 5 layers. Then report how the colour changes at 30° and 60°.*
Check: a colour target is used; the result's colour distance (ΔE\*ab) is reported and confirmed by
compute_color.

**S2.11 Protected silver mirror.** *150 nm Ag on BK7 with a protective SiO2 layer: compare R at 450, 550 and
650 nm for no overcoat, a half-wave overcoat at 550 nm, and a quarter-wave one.*
Check: the half-wave overcoat leaves R at 550 nm almost unchanged; the quarter-wave lowers it; the
dispersion of the effect over wavelength is noted.

---

## Batch 3 — Analysis and manufacturing

**S3.1 Sensitive layers.** *For S1.5's mirror and the target R ≥ 99.9 % from 1000 to 1130 nm, which layers
are most sensitive (layer_sensitivity)?*
Check: one row per layer (31); the merit stays 0 (the band has room to spare), so the layers are ranked by
worst margin (most_sensitive, with a note); the most sensitive layers are named and the result is interpreted.

**S3.2 Production yield.** *Yield of that mirror for R ≥ 99.9 % from 1000 to 1130 nm with 1 % random thickness
errors, 1000 trials; then add a 0.5 % index error.*
Check: a yield with a 95 % interval; adding the index error does not raise the yield.

**S3.3 Optical monitoring.** *Transmission monitoring curves at 1064 nm for S1.5's mirror: the signal at the
end of each layer.*
Check: 31 end-of-layer values; quarter-wave layers end at turning points (alternating extremes of the
signal), and the last value equals the mirror's final transmission (about 0.00045 %).

**S3.4 Angle shift.** *Sweep S1.6's band-pass from 0° to 30° in 5° steps (s and p) and give the peak
wavelength at each angle.*
Check: the peak moves monotonically to shorter wavelengths; s and p split increasingly with angle.

**S3.5 Parameter sweep.** *Batch sweep of the number of pairs p in (HL)^p H at 1064 nm (p = 4 to 14): R at
1064 nm for each.*
Check: R increases monotonically with p; the values match the quarter-wave formula's trend.

**S3.6 Campaign with yield for a hot mirror.** *S2.6's specification as a campaign of 30 starts, at most 20
layers, minimum thickness 10 nm, with a yield stage.*
Check: distinct designs in the top list (duplicates marked); ranking by yield; no layer below 10 nm.

**S3.7 OpenFilters example project.** *Load the OpenFilters "Edge" example (resource
openfilters://examples/Edge or load_project("examples/Edge")), describe it, re-optimize it with the needle method, save it
as SC_edge_reopt with its targets and load it back.*
Check: the re-optimized design keeps the example's targets; save/load round-trips.

**S3.8 Two-sided window.** *2 mm BK7 window with a quarter-wave SiO2 AR at 550 nm on both faces: total T at
550 nm, compared with the uncoated plate.*
Reference: coated T = **93.44 %**, uncoated **91.87 %**. Check: one design with back_layers and
consider_backside.

**S3.9 Graded index (step method).** *Start from one 100 nm layer of the mixture SiO2TiO2 (index 1.9) on BK7
and optimize R ≤ 0.5 % from 450 to 650 nm with the step method.*
Check: the step method runs and the merit decreases. From this thin start it adds no steps (one layer at the
mixture's lowest index); the result warns that this is a local optimum. Restarting from a thicker layer
(e.g. 300-500 nm) adds steps and reaches R ≤ 0.5 % (reference: worst R 0.500 %).

---

## Batch 4 — Robustness

Each item should give a clear, correct message or a sensible result, never a crash, a hang or a silent
wrong answer.

**S4.1 Percent instead of fraction.** *Evaluate an R target with value 99.5 (meaning 99.5 %).*
Expected: an error saying R values are fractions between 0 and 1.

**S4.2 Grazing angle.** *Compute a spectrum at 90°.*
Expected: an error (angles must be below 90°).

**S4.3 GDD without polarization.** *An R_GDD target with polarization unpolarized.*
Expected: an error saying GDD targets need s or p.

**S4.4 Negative thickness.** *A layer of −5 nm.*
Expected: an error.

**S4.5 Malformed formula.** *Stack formula "(HL)^ H".*
Expected: a clear formula error with an example of valid syntax.

**S4.6 Unknown names.** *A target of kind "Q", a quantity "Z", and a material "Unobtainium".*
Expected: each error lists or points to the valid choices.

**S4.7 Out-of-range wavelengths.** *Spectrum of 500 nm Ge on Si from 8 to 12 µm with built-in materials;
then 50 nm Ge on BK7 at 1500 and 2500 nm.*
Expected: the first is refused (unphysical index); the second works with a warning that Ge is extrapolated
beyond 1771 nm.

**S4.8 Empty design.** *Evaluate bare BK7 (no layers) against R ≤ 5 % at 550 nm; then try to optimize it.*
Expected: the evaluation works (R = 4.24 %, met); the optimization explains that it needs a starting layer.

**S4.9 Very large stack.** *(HL)^60 H at 800 nm: describe it and compute R from 400 to 1200 nm in 1 nm steps.*
Expected: 121 layers, quick (seconds), peak R ≈ 100 %.

**S4.10 Time limit.** *Optimize a (HL)^20 H start for R ≥ 99.9 % over 600–1000 nm with the needle method and
time_limit 2 s.*
Expected: stops after about 2 s with stop_reason time_limit and a plain explanation.

**S4.11 Long campaign.** *A campaign of 80 starts (any AR specification) — then cancel it after the first
poll, and get the partial results.*
Expected: a job_id comes back at once (no tool timeout), job_status shows progress, cancel_job stops it
(tasks_done + tasks_still_running + tasks_cancelled = 80; later only the running ones finish), get_results
returns the finished part with provisional ranks. Use a heavy specification (e.g. R <= 0.2 % from 400 to
900 nm, 1 nm steps, max_layers 40, time_limit 60) or the campaign finishes before the first poll.

**S4.12 Material housekeeping.** *Add SC_test (constant n = 1.7); add it again without overwrite; again with
overwrite n = 1.8; remove it; remove it again.*
Expected: the second add is refused, the third replaces it, the first removal works, the second says it
does not exist.

**S4.13 refractiveindex.info edge cases.** *Search for "zzzzqqq"; import the page "main/Nope/nk/Missing".*
Expected: zero results, gracefully; a clear "cannot download" error.

**S4.14 Concurrent jobs.** *Start two small campaigns back to back, then list_jobs.*
Expected: both run and both appear in list_jobs.

---

## Batch 5 — Recheck of the Batch 3 and 4 fixes

Run after the fixes of 2026-10-24 (commits aed1a33 and ec00130). S5.1–S5.12 recheck what failed or
changed; S5.13–S5.15 are passing scenarios that go through code the fixes touched. Throughout, note any
call that is clearly slower than in earlier batches (evaluate_design and optimize_design now always
re-check targets on a 4x finer grid).

**S5.1 Sensitivity of a design that meets its targets** (was S3.1). *S1.5's mirror, target R ≥ 99.9 % from
1000 to 1130 nm (step 5, tolerance 0.001): layer_sensitivity.*
Reference: base_chi2 0; base_worst_margin **0.981**; most_sensitive **front #17, #15, #16, #19, #18**, with a
note that layers are ranked by worst margin. Check: 31 rows, every delta_worst_margin negative.

**S5.2 Monitoring precision** (was S3.3). *Transmission monitoring at 1064 nm for S1.5's mirror.*
Reference: first values 0.685145, 0.918413, 0.396379, 0.660786; the last value **4.45431e-06**, equal to the
mirror's T at 1064 nm from compute_spectrum. Check: no end-of-layer value prints as 0.

**S5.3 Angle plot** (was S3.4). *S1.6's band-pass from 0° to 30° in 5° steps, s and p, one compute_spectrum
call with a plot.*
Check: the plot draws all 14 curves (one colour per angle, dashed s, dash-dot p) with no "more series not
drawn" note; peak wavelengths as in Batch 3 (532.00 nm at 0°, about 515.4 nm s / 515.05 nm p at 30°).

**S5.4 Campaign duplicates and notes** (was S3.6). *Hot mirror campaign: T ≥ 90 % from 420 to 650 nm and
R ≥ 95 % from 750 to 1100 nm on BK7; 30 random starts (TiO2/SiO2, 10–20 layers, 20–200 nm, center wavelength
900, seed 1); needle, max_layers 20, min_thickness 10; tolerance stage 200 trials at 1 % thickness error.*
Check: the call returns a **warning** that starts with up to 20 layers leave the needle method little room
(max_layers 20); no two top rows have merits equal to 6 figures (copies are marked duplicate_of); if every
yield is 0 the summary has a note saying the ranking is by merit; no layer below 10 nm. Then rerun with
10–14 starting layers and max_layers 30: no warning.

**S5.5 Example projects without resources** (was S3.7). *load_project("examples/Edge"); describe it;
re-optimize it with the needle method; save it as SC_edge_reopt with its targets and load it back. Then try
load_project("Edge") and save_project("examples/Edge", ...).*
Reference: Edge has 1 design of **17 layers** and 2 targets (R from 500 to 600 nm, T from 650 to 1000 nm).
Check: the round trip keeps both targets; load_project("Edge") fails with a message that names
examples/<name> and lists the examples; saving to examples/Edge is refused as read-only.
(SC_edge_reopt.ofp stays in the projects folder; it is a file, not a material.)

**S5.6 Step method diagnosis** (was S3.9). *One 100 nm layer of SiO2TiO2 (index 1.9) on BK7, R ≤ 0.5 % from 450
to 650 nm (step 5), step method; then the same from a 500 nm layer.*
Reference: the thin start ends as one layer at n = 1.4847 with a **warning** that the step method added no
steps (layer 1 at the lowest index of SiO2TiO2) and that this is a local optimum, not a limit of the
materials. The 500 nm start ends with several layers and worst R **0.500 %**, with no such warning.

**S5.7 Extrapolation warning** (was S4.7). *50 nm Ge on BK7 at 1500 and 2500 nm.*
Reference: R = 61.22 % / 24.64 %; the warning names only **1771-2500 nm** as extrapolated.

**S5.8 Refused optimization** (was S4.8). *list_jobs; optimize bare BK7 (no layers) for R ≤ 5 % at 550 nm;
optimize S1.6's band-pass with method "annealing"; list_jobs again.*
Check: both calls are refused with clear messages (needs a front layer; method must be refine, needle or
step) and **no new job** appears in list_jobs.

**S5.9 Long stack description** (was S4.9). *describe_design of (HL)^60 H at 800 nm.*
Reference: 121 layers, total 13 575.572 nm. Check: the layer table is there but the normalized design JSON is
replaced by a design_note; describing (HL)^2 at 550 nm (4 layers) still returns the design JSON.

**S5.10 Misses between grid points** (was S4.10). *Evaluate (HL)^4 HH (LH)^4 at 532 nm (TiO2/SiO2, BK7) against
T ≤ 5 % from 500 to 560 nm in 5 nm steps. Then rerun S4.10 (needle, (HL)^20 H start, R ≥ 99.9 % over
600–1000 nm, step 5, time_limit 2 s).*
Reference: not met; worst T on the grid **0.335966 at 530 nm**; a **warning** that between the grid points
the worst is **0.956897 at 532.0 nm** (the peak itself, refined beyond the 4x grid). For the S4.10 rerun: if the plot shows a dip between grid points, the
result carries the same kind of warning (the exact values depend on how far 2 s gets).

**S5.11 Cancel accounting** (was S4.11). *Campaign of 80 starts: R ≤ 0.2 % from 400 to 900 nm in 1 nm steps on
BK7, TiO2/SiO2 random starts, needle, max_layers 40, time_limit 60. Cancel right after the first job_status;
get_results at once, after about 75 s and after about 3 minutes; then cancel again.*
Check: cancel_job reports tasks_done + tasks_still_running + tasks_cancelled = **80**; the rows returned later
never exceed tasks_done + tasks_still_running (queued tasks no longer run); partial rows carry provisional
ranks 1, 2, 3, ...; the second cancel says the job is already cancelled and cancels nothing. Also cancel a
job that has already finished (e.g. S5.4's): it stays done.

**S5.12 Ranking of zero merits** (was S4.14). *Campaign: two-layer V-coat R ≤ 0.05 % at 1064 nm on FusedSilica,
TiO2/SiO2, 12 random starts of 2–4 layers, needle, max_layers 6.*
Check: among designs that meet the target with merit below 1e-9, fewer layers rank first, then the thinner
total; a design stopped by max_layers below the limit is explained (a needle adds 2 layers).

**S5.13 Spot check: laser mirror** (S1.5). *R and OD at 1064 nm and the R > 99.9 % band of S1.5's mirror.*
Reference: R = 99.99955 %, OD = 5.35, band 957–1197 nm.

**S5.14 Spot check: dispersive mirror** (S2.9). *As S2.9: compute the start's GDD at 760 and 840 nm (5 nm steps
or finer), then optimize towards GDD −20 ± 5 fs² with R ≥ 99.5 %, s-polarized.*
Check: start about +4 fs² at 760 nm and −4 fs² at 840 nm; the optimizer moves the GDD towards −20 fs²; if the
result fails its targets, any between-grid warning is consistent with its plot.

**S5.15 Spot check: AR campaign with yield** (S2.1). *As S2.1: R ≤ 0.5 % from 430 to 670 nm on BK7, TiO2/SiO2,
at most 6 layers, min_thickness 10, campaign with a yield stage.*
Check: ranking by yield; designs respect 6 layers and 10 nm; "met" agrees with the worst R; duplicates marked.
