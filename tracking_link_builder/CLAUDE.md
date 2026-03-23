# Agent Instructions — Tracking Link Builder

## What to do when the user gives you a tracking link

When the user pastes a tracking link (with or without a device ID), **immediately run `builder.py`** — do not ask questions, do not analyse the link, do not describe it.

**Step 1 — Collect inputs:**
- Tracking link → required (user has provided it)
- Device ID → required. If not provided, ask for it once: _"Please share your device ID (GAID/AAID UUID)."_ Nothing else.
- Click ID → optional, default is auto-generated (`DTestDDMM`). Do not ask for it unless user mentions it.

**Step 2 — Run the script:**
```bash
python builder.py --link "<link>" --device-id "<device_id>"
# Or with a custom click ID:
python builder.py --link "<link>" --device-id "<device_id>" --click-id "<click_id>"
```

**Step 3 — Show the output directly.** No commentary, no questions, no suggestions.

---

## Key facts about builder.py

- **Entry point:** `tracking_link_builder/builder.py` — all logic lives here. `main.py` is a thin wrapper.
- **MMP detection** — inferred from URL host (AppsFlyer, Adjust, Singular, Kochava, Branch)
- **Unified detection** — presence of `id2` param in the URL
- **id2 values** — `dV9XX0xY` (ODS, `[...]` placeholder style) / `ckFCRVBW` (DSP, `{...}` placeholder style)
- **Kochava hashing** — triggered by `device_id_is_hashed=true` + `device_hash_method=sha1` in the URL params
- **Default click ID** — `DTestDDMM` (date-based, e.g. `DTest2303`), auto-generated at runtime
- **Embedded `[ClickID]` replacement** — applied everywhere across all param values
