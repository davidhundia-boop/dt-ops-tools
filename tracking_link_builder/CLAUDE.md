# Agent Instructions — Tracking Link Builder

When the user refers to the **Link Builder**, the **test link builder**, or asks to build/fix/update tracking links, go directly to:

```
tracking_link_builder/builder.py
```

This is the single source of truth for all link-building logic. `main.py` is just a thin CLI wrapper that calls `build_link()` from `builder.py`.

## Key facts

- **MMP detection** — inferred from URL host (AppsFlyer, Adjust, Singular, Kochava, Branch)
- **Unified detection** — presence of `id2` param in the URL
- **id2 values** — `dV9XX0xY` (ODS, `[...]` placeholder style) / `ckFCRVBW` (DSP, `{...}` placeholder style)
- **Kochava hashing** — triggered by `device_id_is_hashed=true` + `device_hash_method=sha1` in the URL params
- **Default click ID** — `DTestDDMM` (date-based, e.g. `DTest2303`), auto-generated at runtime
- **Embedded `[ClickID]` replacement** — applied everywhere across all param values
