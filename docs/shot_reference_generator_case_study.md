# Case Study: `shot_reference_generator.py` — Nine Versions to Get One Script Right

[INSERT IMAGE: title/hero image — an Adobe Premiere timeline with a full-movie edit loaded, scene-marker track visible above the picture tracks]

## The problem

A 1700+ shot feature had been cut as one continuous editorial timeline in Premiere. Every downstream department — CG, roto, comp — needed that timeline broken apart into individual shots: one reference file per shot, pointing at the real native camera file instead of the offline editorial proxy, correctly handling any shot that turned out to actually be two or more overlapping layers once you looked closely (a screen composited into another shot, for instance).

Doing that by hand for 1700+ shots, every time the edit changed, was not an option. This tool is the answer: read the editor's exported timeline once, and generate the full set of per-shot reference XMLs automatically.

What follows isn't the story of writing that tool once. It's the story of writing it nine times — Version 1 through Version 9 — with each version answering a problem the last one didn't know it had yet.

## Version by version

[INSERT IMAGE: a folder listing of the nine archived script versions side by side, to visually convey the iteration count before the reader hits the text below]

**Version 1 — the baseline.** Already doing the core job: reading the scene-marker track, resolving shot boundaries off the timeline, and exploding any shot with more than one overlapping clip into separate per-layer XML files. The plan for what came *after* this script was already written down, as comments at the bottom of the file:

> `# Drop them all into Media Encoder, from there we can set the preset, dump them all into one giant folder`
> `# Later, write a python script to separate them all into folder structure by "Sh01", dump everything into correct folders.`

That plan is worth remembering — it comes back later.

**Version 2.** Fixed a real path bug: Premiere's XML export writes camera paths as `file://localhost//servername/...`, and the raw string needed cleanup before it pointed anywhere real. Also the point where scene-code shot naming first shows up (`T001_SceneName_Sh0001`), so shots could be identified by scene as well as number.

**Version 3.** Two production decisions landed here, not just bug fixes: the frame-handle padding on each shot dropped from 8 frames to 4, and the scene-code prefix changed from a generic `T001_` to the show's real production prefix. This version also fixed the native-camera-file path to match the real on-set XDCAM convention (`FOOTAGE/NATIVE/B047/XDROOT/Clip/B0470022.MXF`) instead of pointing at the editorial proxy. And a function called `create_Reference_Files()` shows up for the first time — as an empty `pass`. The idea existed before the implementation did.

**Version 4.** `create_Reference_Files()` stopped being a stub and became real per-shot reference-XML generation. This version also started handling a genuine FCP7/Premiere XML quirk: several clips can share a single `<file>` node in the export instead of each clip having its own, so a clip's real path sometimes has to be found by tracing back to a *different* clip's file node by ID.

[INSERT IMAGE: a diagram or annotated XML snippet showing two `<clipitem>` nodes pointing at one shared `<file>` node — the actual FCP7 export quirk this version started handling]

**Version 5.** Hardened the shared-file-node lookup from Version 4 with a guard for the case where no matching node is found at all, instead of assuming one always exists. Also where the burn-in graphic layer first gets spliced into each shot's reference XML from a template — so every reference file carries an identifying burn-in, not just the plate.

**Version 6.** No new capability — a consolidation pass. The line count actually *dropped* (453 to 382) even though nothing was removed feature-wise. Worth calling out on its own: not every version in this history is a version where something got added.

**Version 7.** Added `Local_Host_Remove()` — a macOS `sed`-based post-process pass run over every exported XML file, stripping `localhost` and collapsing the triple-slash artifacts Premiere's export kept reintroducing. A blunt instrument, but it worked, and a version of the same idea survives in today's cleaned-up tool (see below).

**Version 8.** Added `create_ShotXML2()`, which generalized the per-layer splitting into its own dedicated pass over the already-written reference files: for any shot with more than one clip, explode it into `_Layer01`, `_Layer02`, etc., strip filters, and — this is the detail worth noticing — check each resulting layer file for the burn-in graphic and delete it if that's all it turned out to contain. The burn-in spliced in back in Version 5 had to be explicitly filtered back *out* of the per-layer split, or it would have counted as its own layer.

**Version 9.** The final version, and one more cleanup pass (491 lines back down to 434). This is the version `shot_reference_generator.py` was actually ported from.

[INSERT IMAGE: a simple line chart of file length across all nine versions — 299 → 306 → 319 → 390 → 453 → 382 → 392 → 491 → 434 — visually making the "not every version adds, some versions pay down" point above]

## A detail worth noticing

Every path in every one of these nine versions is a macOS path — a personal-machine `/Users/.../Desktop/...` path, nothing pointing at studio infrastructure. The studio's actual pipeline ran on Windows, against an internal storage server. This tool was built and iterated on a personal machine, on personal time, before it ever touched the studio's own infrastructure — the `Local_Host_Remove` hostname-cleanup logic exists specifically because the tool had to bridge those two environments. It shipped as a real pipeline tool; it started as one person's side project to solve their own problem.

## What this tool was actually building toward — and why that part didn't ship

The plan written into Version 1's own comments — batch-import every per-shot XML into Adobe Media Encoder, export them all with one preset, then sort the results into folders — was the real target. It's why the tool generates *per-shot, single-clip* XML files instead of just a shot list: those files were meant to be Media Encoder's batch-import queue.

[INSERT IMAGE: Adobe Media Encoder's batch/queue panel, to illustrate what the planned next stage would have looked like]

That stage never shipped. The camera footage was shot in a log color space (S-Log3), which carries a deliberately flat, low-contrast image so there's enough dynamic range left to color-correct later — but Media Encoder's batch export couldn't preserve that latitude. It burned in color that looked plausible at a glance but was actually just the flat log image passed through with no real correction, discarding the dynamic range the whole point of shooting log was to protect. That's a real deliverable-quality problem, not a cosmetic one, and it wasn't discoverable until the plan was tested against real footage. Production pivoted to DaVinci Resolve for that step instead, run manually rather than through this batch path.

Media Encoder wasn't even the only avenue tried. A separate scratch script found in the same archive drives Adobe Premiere directly through Windows COM automation (`win32com.client`), batch-exporting clips on a track straight to EXR without Media Encoder in the loop at all — a third attempt at the same underlying problem, alongside the Media Encoder plan and the eventual Resolve pivot. It never went further than a hardcoded, single-machine experiment, but it's a good data point on its own: this wasn't one plan that failed once, it was a real problem worth trying to automate from more than one angle before landing on the manual Resolve step that actually shipped.

That's the honest framing for this tool in a portfolio context: **built, and technically sound as far as it went — genuinely used, genuinely iterated on nine times, genuinely solving the shot-breakdown problem it was aimed at — but the specific batch-export pipeline it was built to feed into is not what ended up shipping on the real show.** The XML-generation half is real and proven; the Media Encoder half is a plan that ran into a wall outside this tool's control.

## From Version 9 to `shot_reference_generator.py`

Porting the final version into this repo's config-driven `shot_reference_generator.py` surfaced three real bugs that had been sitting in the production script the whole time:

1. **A missing path separator**, responsible for a family of stray `XML_Dumptemp_*.xml-e` files cluttering the output directory — a string built without the separator character needed between two path components.
2. **An undefined-variable typo** — a variable referenced under a name that was never actually assigned, only working by accident in the original because of how the surrounding scope happened to be structured at the point it was called.
3. **A burn-in-layer cleanup check that could never match** — the check for "is this layer file just the burn-in graphic" ran *after* the code that overwrote the very field the check was looking at, so the condition it was testing for had already been erased by the time it ran.

None of these were show-stoppers in production — the pipeline visibly worked, shots visibly got generated — but they're the kind of bug that survives specifically *because* a tool works well enough, most of the time, for nobody to go looking. Porting the tool into a clean, tested version is what actually surfaces them.

One more small one, found while writing this case study rather than during the original port: the cleaned tool's `clean_local_host_paths()` — the direct descendant of `Local_Host_Remove()` from Version 7 — had a leftover `sed` pass that replaced a string with itself, a no-op left behind when a hostname-specific fix was generalized into a broader one on the line above it. Harmless, but dead code, and now removed.

A fourth one surfaced later still, this time not from reading the code but from a studio convention that only came up in conversation: production always leaves numbering gaps when naming scenes and shots — `SHO_010`, `SHO_020`, not `SHO_001`, `SHO_002` — specifically so a pickup or reshoot scene can land as `SHO_015` later without renumbering anything that already shipped. Version 9's numbering never actually did this: scenes and shots both counted up by 1 (`_{val+1:03}`, `Sh{val+1:04}`), and shot numbers ran as one continuous count across the *entire* movie instead of resetting at each new scene the way the studio's own naming spec calls for. Not a crash, not something that would show up in a log — just numbering with no room to insert into, quietly at odds with the convention everything downstream assumed. `shot_reference_generator.py` now takes a configurable `number_step` (default 10) and resets the shot count at every new scene boundary.

## Where it stands today

`shot_reference_generator.py` in this repo is the config-driven, tested descendant of this whole line — same core logic as Version 9, same handling of the shared-`<file>`-node quirk, same burn-in splice and per-layer explosion, same `localhost`/path cleanup, minus the three bugs above, and driven by a YAML config instead of hardcoded personal-machine paths. See the main [README](../README.md#shot_referencegeneratorpy) for how it's actually run.
