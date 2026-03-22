# Push these repos to GitHub

Remotes are already configured (`origin` → `https://github.com/davidhundia-boop/dt-ops-tools.git` and `dt-ops-streamlit.git`). **The repos must exist on GitHub first** (empty — no README, no license, or push will be rejected).

## Step 1 — Create the two repos (GitHub website)

1. Open **[New repository](https://github.com/new)**.
2. **Repository name:** `dt-ops-tools` → **Public** → **do not** check “Add a README” → **Create repository**.
3. Repeat for **`dt-ops-streamlit`** (also empty).

## Step 2 — Push from this PC

**Option A — PowerShell (both repos):**

```powershell
cd "d:\AI Stuff\dt-ops-tools"
.\push-to-github.ps1
```

**Option B — Manual:**

```powershell
cd "d:\AI Stuff\dt-ops-tools"
git push -u origin main

cd "d:\AI Stuff\dt-ops-streamlit"
git push -u origin main
```

Sign in when Git prompts (browser or Git Credential Manager).

## Step 3 — Archive old repos (optional)

After you verify the new repos: **Settings → Archive this repository** for `AdOps`, `QA-Agent`, `AI-Stuff` (read-only backup; do not delete).
