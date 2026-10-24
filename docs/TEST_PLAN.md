# OpenFilters MCP — test plan

Two parts:

1. **Automated acceptance test** (2 minutes): checks every tool, prompt and resource against known
   answers, as Claude Desktop runs the server. Run it first; if it fails, the chat tests are not worth doing.
2. **Chat tests** (about 45 minutes): what a script cannot check — Claude choosing and using the tools,
   the Claude Desktop menus and plots, real design tasks, the OpenFilters GUI.

Run both before each release and after any change to the server.

## 1. Automated acceptance test

```bash
.venv/Scripts/python dev/acceptance.py
```

It uses a temporary data folder (your materials, jobs and projects are not touched) and the same
`openfilters-mcp.exe` that Claude Desktop runs. `--offline` skips the two refractiveindex.info checks.
Expected: `44 passed, 0 failed`, in about 20 s. The unit tests (`.venv/Scripts/python -m pytest tests -q`)
cover the engine in more depth.

## 2. Chat tests in Claude Desktop

**Before starting:** fully quit Claude Desktop (system tray → Quit) and reopen it, then open a new chat
and do A2 yourself (Claude's safety check may block a deletion it did not start). Paste each request as written. Tick the box when every point under *Pass if* holds; otherwise note what
Claude replied. Exact numbers come from closed-form results with test materials of fixed index, so they
do not depend on your material library.

### A. Set-up

- [ ] **A1. Version.** *"Run openfilters server_info."*
  Pass if: version **0.1.3**; `cpp_core_abeles` and `cpp_core_moremath` are **true**.
- [ ] **A2. Leftover test material.** *"List my materials named TiO2 with their source and n at 550 nm."*
  Pass if: `TiO2` shows **built-in**, n ≈ 2.4–2.6. If it shows **user** with n = 2.0 (from an earlier test),
  ask *"Remove my TiO2 material"* and check that the built-in one is listed again.
- [ ] **A3. Test materials.** *"Add four constant materials: TST_S with n = 1.52, TST_A with n = 1.38,
  TST_H with n = 2.35 and TST_L with n = 1.46."*
  Pass if: four user materials added.

### B. Single designs

- [ ] **B1. Antireflection layer.** *"On TST_S, compute the reflectance at 550 nm of one quarter-wave layer
  of TST_A designed for 550 nm, and plot 400–800 nm."*
  Pass if: R = **1.260 %** at 550 nm; thickness **99.64 nm**; Claude gives the path of the PNG plot and the
  file opens.
- [ ] **B2. Mirror.** *"Same substrate: a (HL)^6 H quarter-wave stack at 550 nm with H = TST_H, L = TST_L.
  What is R at 550 nm and how wide is the high-reflection band?"*
  Pass if: R = **99.637 %**; R > 99 % from about **502 to 608 nm**.
- [ ] **B3. Brewster angle.** *"For bare TST_S (no coating), at which angle does p-polarized reflectance
  vanish? Use an angle scan at 550 nm."*
  Pass if: about **56.7°** (atan 1.52), found with `compute_angle_scan`.
- [ ] **B4. Field and monitoring.** *"For the B2 mirror, where is the electric-field peak at 550 nm, and what
  transmission signal at 550 nm should a monitor see at the end of each layer?"*
  Pass if: a peak position and value, and 13 end-of-layer values; the last one equals the mirror's
  transmission (about **0.36 %**).
- [ ] **B5. Colour.** *"What colour is the B2 mirror in reflection at 0° and 45°?"*
  Pass if: L\*a\*b\* and hex values at both angles, with a visible shift.

### C. Targets and evaluation (the strict rule)

- [ ] **C1.** *"Does the B1 layer meet R ≤ 1.25 % at 550 nm? And R ≤ 1.3 %?"*
  Pass if: **not met** for 1.25 % (R is 1.260 %), **met** for 1.3 %. Before version 0.1.3 both were "met".

### D. Optimization

- [ ] **C2.** *"Does the B2 mirror meet R ≥ 99.9 % from 540 to 560 nm? Give the worst value and where."*
  Pass if: **not met**; the result shows `worst_value` (about 99.6 %) and `worst_at_nm` (540 or 560).
- [ ] **C3. Colour target.** *"How far is the B2 mirror's reflected colour at 0° from L\*a\*b\* = (90, −10,
  40)? Use a colour target."*
  Pass if: evaluate_design accepts the colour target and reports its merit contribution (no "unknown keyword").

- [ ] **D1. Refinement.** *"On BK7, take TiO2 30 nm / SiO2 100 nm and refine it for R ≤ 0.5 % from 450 to
  650 nm at normal incidence."*
  Pass if: χ² decreases; the answer says whether the target is met and shows the worst R; plot paths given.
- [ ] **D2. Needle with a layer limit.** *"Design a broadband AR on BK7 with TiO2 and SiO2, R ≤ 0.3 % from
  420 to 680 nm, at most 8 layers, using the needle method."*
  Pass if: at most **8 layers**; no layer listed as NaN; `edge_needles_added` may be > 0; "met" agrees with
  the worst R Claude reports.
- [ ] **D3. Minimum thickness.** *"Re-optimize D2 with no layer thinner than 15 nm."*
  Pass if: `thinnest_layer_nm` ≥ **15**.
- [ ] **D4. Two-sided part.** *"BK7 window, 2 mm thick, the D2 AR on the front and a single quarter-wave
  MgF2-like layer (TST_A) at 550 nm on the back. What is the total transmission at 550 nm?"*
  Pass if: Claude builds **one** design with `back_layers` and `consider_backside: true` (not two designs).

### E. Campaigns, yield and sweeps

- [ ] **E1. Campaign.** Use the **+** menu → *Design an antireflection coating* with substrate BK7,
  420–680 nm, R ≤ 0.5 %, materials TiO2 and SiO2 (the defaults), max layers 8.
  Pass if: the campaign runs in the background (Claude polls it); results are ranked; **no design has NaN
  thicknesses**; none has more than 8 layers; yields are below 100 % unless the margin is large; a project
  is saved with its targets.
- [ ] **E2. Sweep.** *"Sweep the B1 layer's quarter-wave multiple from 0.8 to 1.2 in steps of 0.1 and give R
  at 550 nm for each."*
  Pass if: five rows; the minimum is at **1.0** (R = 1.260 %).
- [ ] **E3. Yield.** *"Monte Carlo yield of the D2 design for its targets, 1 % thickness errors, 1000 trials."*
  Pass if: a yield with a 95 % interval and the failure rate per target.

### F. Materials from refractiveindex.info

- [ ] **F1.** *"Find thin-film HfO2 data on refractiveindex.info and import the best entry covering 300–1500
  nm."*
  Pass if: a list with authors, years and ranges; an imported material named like `HfO2_Franta`; references
  given. (If it says the material already exists from an earlier run, Claude should import again with
  overwrite.)
- [ ] **F2.** *"Import Malitson's fused silica and give n at 587.6 nm."*
  Pass if: n = **1.4585**.

### G. Projects and the OpenFilters GUI

- [ ] **G1.** *"Save the D2 design and its targets as a project called test_plan_ar."*
  Pass if: an `.ofp` path in the data folder's `projects`. If OpenFilters is installed, open the file:
  the design and targets appear, and the Design/Optimize menu is enabled.
- [ ] **G2.** *"Load the project test_plan_ar and describe it."*
  Pass if: same layers and targets as saved.

### H. Prompts and resources (Claude Desktop menus)

- [ ] **H1.** **+** → Connectors → *Add from openfilters*: the 5 workflows, *Design and target format*,
  *Available materials* and **7** separate *OpenFilters example: …* entries are listed.
- [ ] **H2.** Attach *OpenFilters example: Edge* and ask *"Explain this design and plot its spectrum."*
  Pass if: only the Edge project is attached (not a list of all examples); a spectrum plot path is given.
- [ ] **H3.** Run *Design a high reflector (mirror)* with its defaults (1064 nm, R ≥ 99.9 %).
  Pass if: a quarter-wave start, a refined design meeting R ≥ 99.9 % over the band, field peak reported.
- [ ] **H4.** Run *Review a design for manufacturing* with the D2 design pasted as JSON.
  Pass if: yield, most sensitive layers, field peak and monitoring signals are reported.

### I. Errors

- [ ] **I1.** *"Compute the spectrum of 100 nm of Unobtainium on BK7."*
  Pass if: a clear "Material does not exist" message from the tool that itself suggests list_materials and
  refractiveindex.info.
- [ ] **I2. (Do this one yourself.)** *"Remove the material BK7."* A chat running the plan on its own may be
  stopped by Claude's safety check before the request reaches the connector; approve it if asked.
  Pass if: the connector refuses: built-in materials cannot be removed.

### J. Clean-up

- [ ] **J1.** *"Remove my materials TST_S, TST_A, TST_H, TST_L, HfO2_Franta and SiO2_Malitson."*
  Pass if: all six removed (the last two were imported in F1 and F2; keep them if you want them).

### K. Claude Code (optional)

- [ ] **K1.** In Claude Code, type `/` and look for `mcp__openfilters__design_mirror` and the other four
  workflows; run one. Pass if: it runs like H3.

## Running the plan with a Claude chat

A Claude Desktop chat can run sections A–G, I and J itself; do H, K, I2 and the GUI half of G1 yourself.
Do A2 first, use two chats (A–D, then E–J) so a long run does not fill one conversation, attach this file
and paste:

> You are testing the OpenFilters connector. Work through sections A to D of this test plan in order (skip
> H and K). For each test: run the request exactly as written, using the openfilters tools, then report the
> test ID, PASS or FAIL against its "Pass if" line, and the evidence: the actual values, the tool(s) you
> called, and for any FAIL the full tool result. Do not fix or retry failures beyond what the request asks;
> just report them. End with a summary table.

(For the second chat, write "E to J".) Review its reports critically: a model checking its own work can be
generous, which is why the evidence matters more than the verdict.

## Recording results

Note the version (A1), the date, the Claude app and model, and for each failed check what Claude replied
(the job folder in `~/OpenFilters-MCP/jobs` helps reproduce optimizer problems). Real failures belong in
`tests/test_regressions.py` once fixed.
