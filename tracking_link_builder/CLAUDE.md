# Agent Instructions — Tracking Link Builder

## What to do when the user gives you a tracking link

When the user pastes a tracking link (with or without a device ID), **immediately run `builder.py`** — do not ask questions, do not analyse the link, do not describe it.

**Step 1 — Collect inputs:**
- Tracking link → required (user has provided it)
- Device ID → required. If not provided, ask for it once: _"Please share your device ID (GAID/AAID UUID)."_ Nothing else.
- Click ID → optional, default is auto-generated (`DTestDDMM`). Do not ask for it unless user mentions it.

**Step 2 — Run the script:**
```bash
# Always pass --name with the requester's first name — click ID is generated as name+random (e.g. david47)
python builder.py --link "<link>" --device-id "<device_id>" --name "<requester_first_name>"

# Override click ID directly only if explicitly requested:
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
- **Click ID** — generated from requester name + random 2-digit number, e.g. `david47`. Pass via `--name <name>`. Use `--click-id` only to override manually.
- **Embedded `[ClickID]` replacement** — applied everywhere across all param values
