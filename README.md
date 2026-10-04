# NFL Sharp Picks

Follows the most profitable Polymarket NFL wallets that have bet every week this
season and recommends three bets where they agree.

## How it runs online

1. **GitHub Actions** runs `nfl_sharps.py` on a schedule (daily, plus before each
   Sunday, Monday and Thursday kickoff window) and saves `results/latest.json`
   back to the repo.
2. **Streamlit Community Cloud** hosts `app.py`, which reads that file. Every
   refresh updates the site automatically.

Both are free. Nothing runs on your computer.

## One-time setup

### Part A: GitHub (about 10 minutes)

1. Sign in at https://github.com (create a free account if needed).
2. Create a new repository at https://github.com/new
   1. Name: `nfl-sharp-picks`
   2. Choose **Private**
   3. Click **Create repository**
3. On the new repo page, click **uploading an existing file**, drag in every file
   from this folder (including the `.github` folder), and click **Commit changes**.
   If the `.github` folder doesn't upload, create the file manually:
   **Add file → Create new file**, name it `.github/workflows/refresh.yml`, and
   paste the contents in.
4. Turn on write access for the workflow:
   **Settings → Actions → General → Workflow permissions →
   Read and write permissions → Save**.
5. Start the first run: **Actions** tab → **Refresh NFL picks** →
   **Run workflow** → **Run workflow**.
   The first run downloads the whole season and can take an hour or more.
   Later runs take a few minutes.

### Part B: Streamlit Community Cloud (about 5 minutes)

1. Go to https://share.streamlit.io and sign in with GitHub.
2. Click **Create app → Deploy a public app from GitHub** (it stays private
   because the repo is private).
3. Repository: `nfl-sharp-picks` · Branch: `main` · Main file: `app.py`
4. Click **Deploy**. Bookmark the URL you get.

### Optional: "Refresh data now" button

In the Streamlit app's **Settings → Secrets**, paste the line below, with
`YOUR-GITHUB-NAME` replaced, and save:

```
REFRESH_URL = "https://github.com/YOUR-GITHUB-NAME/nfl-sharp-picks/actions/workflows/refresh.yml"
```

## Notes

1. Schedule times are in UTC inside `.github/workflows/refresh.yml`. GitHub can
   start scheduled runs 5–30 minutes late at busy times.
2. A private repo gets 2,000 free Actions minutes a month. This schedule uses
   roughly 500.
3. Streamlit's free tier allows one private app at a time. The app sleeps after
   12 hours without visitors; it wakes with one click.
4. To change how many wallets are followed or the volume floor, edit `TOP_N` and
   `MIN_VOLUME` at the top of `nfl_sharps.py` on GitHub. The next refresh uses
   the new values.
