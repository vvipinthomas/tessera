# Deploy Tessera to Cloudflare from your phone and test it on Android

You can do everything here in Chrome on an Android phone, without a computer. GitHub builds Tessera and deploys it
to your Cloudflare account. You then open it on the phone like an app.

**Time:** about 30 minutes. Most of it is waiting for the first build.
**Cost:** Cloudflare's **Workers Paid** plan ($5 a month, required for Containers), plus any container usage above what
the plan includes. Check Cloudflare's Containers pricing; [Lower the cost](#lower-the-cost) shows how to save.

> Tip: Cloudflare's and GitHub's settings pages fit better in Chrome with **⋮ → Desktop site** switched on.

---

## Part 1: Put the code on GitHub

1. In Chrome go to **github.com/new**, or open the GitHub app and tap **+ → Create repository**.
2. Name it `tessera`. Leave README, .gitignore and licence **unticked** so the repository is empty. Tap **Create**.
3. Send Claude the message **"done"** and Claude pushes the code. If you are pushing it yourself, push the `main`
   branch with its tags.

## Part 2: Prepare Cloudflare

Sign in at **dash.cloudflare.com**.

| # | Do this | Keep |
|---|---|---|
| 1 | **Workers & Pages → Plans**, then choose **Workers Paid** | — |
| 2 | **Workers & Pages → Overview**: copy the **Account ID** shown on the right (on a phone, scroll down) | Account ID |
| 3 | **R2 Object Storage → Create bucket**, name it `tessera-data`, then **Create** | — |
| 4 | **R2 → Manage API tokens → Create API token**. Permission **Object Read & Write**, applied to the `tessera-data` bucket. **Create**, then copy both keys. The secret is shown only once | R2 Access Key ID, R2 Secret Access Key |
| 5 | **My Profile → API Tokens → Create Token → Edit Cloudflare Workers** (Use template). Under Permissions, add **Account · Workers R2 Storage · Edit** and **Account · Containers · Edit**. Set Account Resources to your account. **Continue → Create Token**, then copy it | Cloudflare API token |

About step 5: Cloudflare's permission names change from time to time. If the deploy later fails with an
*Authentication error*, edit the token and add any Containers or Cloudchamber permission the list offers.

### Two values you make yourself

- **Master key.** It encrypts saved connection passwords and must be exactly 32 random bytes in base64.
  - On Android, install **Termux** and run `openssl rand -base64 32` (if `openssl` is missing, run `pkg install openssl` first).
  - Or ask Claude to generate one.
  - Save it in your password manager. If you lose it, a restored Tessera cannot read the passwords it saved.
- **Setup code.** Any phrase of 8 or more characters. You type it once to create the admin account.

## Part 3: Give GitHub the secrets and deploy

1. In your `tessera` repository go to **Settings → Secrets and variables → Actions → New repository secret**.
2. Add these six secrets. Names must match exactly:

   | Name | Value |
   |---|---|
   | `CLOUDFLARE_API_TOKEN` | the Cloudflare API token from step 5 |
   | `CLOUDFLARE_ACCOUNT_ID` | the Account ID from step 2 |
   | `TESSERA_MASTER_KEY` | your master key |
   | `TESSERA_SETUP_CODE` | your setup code |
   | `TESSERA_CLOUD_ACCESS_KEY_ID` | the R2 Access Key ID from step 4 |
   | `TESSERA_CLOUD_SECRET_ACCESS_KEY` | the R2 Secret Access Key from step 4 |

3. Open the **Actions** tab. The first time, tap **I understand my workflows, go ahead and enable them**.
4. Tap **Deploy to Cloudflare → Run workflow → Run workflow** (branch `main`).
5. Wait for the green tick. The first run takes 10 to 15 minutes while it builds the container image.
6. Open the run, then the **Deploy** step. Near the end it shows your address, for example
   `https://tessera.<your-subdomain>.workers.dev`. You can also find it at **Cloudflare → Workers & Pages → tessera → Visit**.

The workflow creates the bucket if it is missing, deploys, and copies the four `TESSERA_*` secrets into the Worker.
Later runs only update the code.

## Part 4: Test it on Android

### First start

1. Open the address in Chrome. You will see **Tessera is starting** for up to a minute, or a few minutes on the very
   first deploy while Cloudflare provisions the container. It refreshes by itself.
2. To check on it, open `https://<your address>/__tessera/status`. When it shows `"container": "healthy"`, Tessera is up.
3. On the setup screen:
   - Enter your **setup code**.
   - Create the admin account (email, name, a strong password).
   - Leave **Start in demo mode** ticked to get sample data, dashboards and pipelines.
4. In Chrome tap **⋮ → Add to Home screen**. Tessera then opens from its own icon.

### What to try

| Area | Test | You should see |
|---|---|---|
| Layout | Use it upright, then turn the phone sideways | Pages fit the screen; the sections are an icon strip along the top that you can swipe |
| Executive brief | Open **Brief** | Headline numbers, forecasts and plain-language notes |
| Dashboards | Open a demo dashboard and tap a chart | Values on tap; tapping a bar filters the other charts |
| Catalog | **Catalog → a serving dataset → Preview, Profile, Lineage** | Data, column statistics and the lineage graph |
| Upload from the phone | **Catalog → Upload**, pick a CSV from Downloads or Drive | A new `raw.` table with its row count |
| SQL | **SQL**, run `select * from serving.<a demo table> limit 20` | Results in a scrollable table |
| Pipelines | **Pipelines → a demo pipeline → Run** | Status changes to *success*, with the step log |
| Quality and privacy | Dataset → **Quality → Run**; **Scan for personal data** | Rule results; personal-data columns flagged and masked |
| Streams | **Streams** (demo) | Live events counting up |
| Dark mode | Switch Android to dark theme | Tessera follows it |
| Backups | **Admin → Operations → Cloud backups → Back up now** | *Last backup* updates. In Cloudflare, **R2 → tessera-data → tessera/backups/** shows the file |
| AI (optional) | **Admin → AI providers**, add a key (Anthropic, OpenAI or Cloudflare Workers AI), then ask a question in **Agents** | An answer with the SQL it ran |

### Check that your data survives a restart

1. Make a change, for example upload a CSV.
2. In GitHub run **Deploy to Cloudflare** again. This replaces the container.
3. Wait for the starting page to clear, then sign in. Your upload is still there, restored from R2.

## If something goes wrong

| Symptom | Fix |
|---|---|
| Workflow fails at **Deploy** with *Authentication error* | The API token is missing a permission. Add the Containers and R2 Storage edit permissions (Part 2, step 5) and run again |
| Workflow says Containers are not available | The account is not on **Workers Paid** yet |
| Stuck on *Tessera is starting* for more than 10 minutes | **Cloudflare → Workers & Pages → tessera → Logs** (container logs are under **Containers**). The usual cause is a missing or wrongly formatted `TESSERA_MASTER_KEY`: fix the GitHub secret and run the workflow again |
| Setup code is rejected | Check `TESSERA_SETUP_CODE` in GitHub, run the workflow again, then retry |
| *Back up now* shows an error | The R2 key pair is wrong or not allowed on `tessera-data`. Make a new R2 token, update the two `TESSERA_CLOUD_*` secrets, run the workflow |
| Everything is slow after a quiet period | The container was sleeping. Keep `KEEP_WARM` at `true` (the default) |

## Lower the cost

By default, a cron job wakes Tessera every 5 minutes, so schedules and streams run on time. That keeps the container
running all day. To let it sleep when nobody uses it, set `"KEEP_WARM": "false"` in `cloudflare/wrangler.jsonc`.
You can edit the file in GitHub's web editor (the pencil icon), commit, then run the workflow. With `KEEP_WARM` off,
scheduled work waits until the next visit.

## Use your own domain (optional)

1. In Cloudflare, go to **Workers & Pages → tessera → Settings → Domains & Routes → Add → Custom domain**, for example
   `data.example.com`.
2. In `cloudflare/wrangler.jsonc`, add `"TESSERA_BASE_URL": "https://data.example.com"` under `vars`.
3. Commit, then run the workflow.

More detail, including Cloudflare Access sign-in and your lakehouse on R2, is in [CLOUDFLARE.md](CLOUDFLARE.md).
