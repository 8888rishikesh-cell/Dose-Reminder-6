# DOSE REMINDER

A beginner-friendly medicine schedule and dose-tracking prototype built with
Streamlit and Supabase. It stores account data in Supabase; it does not use
SQLite.

## Features currently in the code

- Create an account, sign in, and sign out with Supabase Auth.
- Use the interface in English or Hindi. Some labels and messages remain in
  English.
- Add a medicine with one daily scheduled time, optional notes, and an optional
  photo selected from the device or captured with the camera, and select its
  scheduled weekdays.
- View active medicines and deactivate them.
- Create an `Unconfirmed` dose record for each active medicine scheduled for
  the current date's weekday when the app runs, if a matching record does not
  already exist.
- View the medicine image and name together with the schedule, then mark a dose
  `Taken` or `Skipped`.
- View dose records for a selected date.
- Show an in-page reminder at an active medicine's scheduled time while the
  signed-in website is open. The browser checks every five seconds and shows a
  reminder once per medicine, time, and local calendar day. A short sound is
  attempted; use **Enable sound** if the browser blocks automatic playback.

The app tracks schedules and statuses. **Push notifications are not implemented
yet.** Reminders and sounds only work while the website is open in a browser;
there are no service workers or background notifications. Since `scheduled_time`
is a time-only database value with no timezone, reminders use the browser's
current local time. Changing the device timezone changes when that local-clock
schedule and weekday are reached. Today's schedule and dose-log dates use the
server date, while browser reminders use the browser's local date; align the
server and browser timezones for consistent weekday behavior near midnight. The
app does not send SMS, phone calls, or reminders when closed, and it does not
provide medical advice.

## Supabase database setup

The code expects three Supabase tables named `medicines`, `dose_logs`, and
`user_settings`, and uses Supabase Auth for user accounts.

### Columns used by the app

| Table | Columns used |
| --- | --- |
| `medicines` | `id`, `user_id`, `name`, `scheduled_time`, `scheduled_days`, `notes`, `active`, `image_path` |
| `dose_logs` | `id`, `user_id`, `medicine_id`, `scheduled_date`, `scheduled_time`, `status`, `updated_at` |
| `user_settings` | `user_id`, `caretaker_email` |

The app inserts medicine rows with `user_id`, `name`, `scheduled_time`,
`scheduled_days`, `notes`, and `active`, and sets `image_path` when an image is
uploaded. `scheduled_days` is a PostgreSQL `smallint[]` containing ISO weekday
numbers: Monday `0` through Sunday `6`. A `NULL` value means every day, which
preserves the daily schedule behavior of older medicine rows. New medicines
default to all seven days selected. The app inserts dose rows with `user_id`,
`medicine_id`, `scheduled_date`, `scheduled_time`, and `status`. It updates medicine
`active` and dose-log `status` and `updated_at`. It reads the other listed
columns to display schedules and history.

The app stores each signed-in user's caretaker email in `user_settings`.
This setting only saves and loads the address; it does not send emails.

### Caretaker settings database migration

The repository does not include an existing profile/settings table or a
database schema export. Run this SQL once in the Supabase SQL Editor to create
the user-specific settings table and restrict access to each user's own row:

```sql
create table if not exists public.user_settings (
    user_id uuid primary key references auth.users(id) on delete cascade,
    caretaker_email text
);

alter table public.user_settings enable row level security;

grant select, insert, update on table public.user_settings to authenticated;

drop policy if exists "Users can read their own caretaker settings"
    on public.user_settings;
create policy "Users can read their own caretaker settings"
    on public.user_settings for select to authenticated
    using ((select auth.uid()) = user_id);

drop policy if exists "Users can insert their own caretaker settings"
    on public.user_settings;
create policy "Users can insert their own caretaker settings"
    on public.user_settings for insert to authenticated
    with check ((select auth.uid()) = user_id);

drop policy if exists "Users can update their own caretaker settings"
    on public.user_settings;
create policy "Users can update their own caretaker settings"
    on public.user_settings for update to authenticated
    using ((select auth.uid()) = user_id)
    with check ((select auth.uid()) = user_id);
```

The app uses the authenticated Supabase session and upserts only `user_id` and
`caretaker_email`; no other user profile fields are modified. The table grants
API access to the `authenticated` role, while RLS restricts reads and writes to
the row whose `user_id` matches `auth.uid()`.

### Weekday schedule database migration

The existing app code and schema documentation do not include
`medicines.scheduled_days`, and this repository has no database migration or
schema export. Apply this migration to the Supabase project before adding
medicines with weekday selections:

```sql
alter table public.medicines
add column if not exists scheduled_days smallint[];
```

The column is nullable intentionally. Existing rows are left unchanged and are
treated as scheduled every day; no backfill or dose-history changes are needed.
After the migration, new medicine inserts save their selected days in this
column. The code cannot inspect a hosted Supabase schema from this repository,
so confirm the migration succeeded in the target project before deploying.

### Medicine images

Image uploads require a nullable `image_path` column on `medicines`:

```sql
alter table public.medicines
add column if not exists image_path text;
```

The app accepts JPG/JPEG, PNG, and WebP photos up to 5 MB. Store them in a
**private** Supabase Storage bucket named `medicine-images`, configured with a
5 MB file-size limit and allowed MIME types `image/png`, `image/jpeg`, and
`image/webp`. The app stores each file under the signed-in user's ID folder,
saves the path in `medicines.image_path`, and creates short-lived signed URLs
to display images.

If the database column, private bucket, and user-folder policies are already
configured, no additional SQL or Storage setup is required. Otherwise, the
following setup is needed:

```sql
create policy "Users can view their medicine images"
on storage.objects for select to authenticated
using (
  bucket_id = 'medicine-images'
  and (storage.foldername(name))[1] = (select auth.uid()::text)
);

create policy "Users can upload their medicine images"
on storage.objects for insert to authenticated
with check (
  bucket_id = 'medicine-images'
  and (storage.foldername(name))[1] = (select auth.uid()::text)
);

create policy "Users can remove their medicine images"
on storage.objects for delete to authenticated
using (
  bucket_id = 'medicine-images'
  and (storage.foldername(name))[1] = (select auth.uid()::text)
);
```

Medicine records without an image remain supported; in that case the app leaves
`image_path` unset.

**Repository verification limit:** this repository does not include SQL
migrations, a schema export, or RLS policy definitions. The exact column types,
defaults, constraints, foreign keys, and indexes therefore cannot be verified
from the codebase. The table and column names above reflect what the app
references; they are not a database migration. In particular, the code checks
for an existing dose using medicine, date, and time, but the database must
enforce uniqueness on those values (scoped to a user) if duplicate prevention
must also hold during concurrent app requests.

### Enable Row Level Security

Enable Row Level Security (RLS) on all three tables. Add policies for
authenticated users that allow access only when the row's `user_id` equals the
current Supabase user's ID (`auth.uid()`). Apply that condition to each
operation the app needs:

- `medicines`: `SELECT`, `INSERT`, `UPDATE`, and `DELETE` (permanent removal
  of archived medicines)
- `dose_logs`: `SELECT`, `INSERT`, `UPDATE`, and `DELETE` (dose history is
  removed with its archived medicine)
- `user_settings`: `SELECT`, `INSERT`, and `UPDATE` (the caretaker settings
  migration above creates these policies)

For inserts, the policy must check that the new row's `user_id` matches
`auth.uid()`. For reads, updates, and deletes, ensure users can only target
rows whose `user_id` matches. The app filters many queries by `user_id`, but
those client filters are not a security boundary; RLS is essential.

This is a description of the access rules required by the code, not verified SQL
or a claim that policies are already configured. Review and test policies in
your own Supabase project before using real accounts or data. Use fake data
while learning.

## Run locally

1. Install Python and Git if you do not already have them.
2. Clone this repository and open its folder in a terminal:

   ```sh
   git clone https://github.com/khatoonaafiya6-dotcom/smart-medicine-reminder.git
   cd smart-medicine-reminder
   ```

3. (Recommended) Create and activate a virtual environment:

   ```sh
   python -m venv .venv
   ```

   On macOS or Linux:

   ```sh
   source .venv/bin/activate
   ```

   On Windows PowerShell:

   ```powershell
   .venv\Scripts\Activate.ps1
   ```

4. Install the project dependencies:

   ```sh
   python -m pip install -r requirements.txt
   ```

5. Create a local Streamlit secrets file at
   `.streamlit/secrets.toml` with your Supabase project URL and **publishable
   (anon) key**:

   ```toml
   SUPABASE_URL = "https://YOUR-PROJECT.supabase.co"
   SUPABASE_ANON_KEY = "YOUR-PUBLISHABLE-KEY"
   ```

   Get these values from your Supabase project settings. Never use a Supabase
   `service_role` key in this app. Do not commit `secrets.toml` or share its
   contents. Alternatively, the app can read `SUPABASE_URL` and
   `SUPABASE_ANON_KEY` from environment variables.

6. Start the app:

   ```sh
   python -m streamlit run app.py
   ```

7. Open the local address printed by Streamlit, usually
   `http://localhost:8501`.

You need working Supabase credentials, the three tables, and appropriate RLS
policies before the app can load or save data.

## Persistent login

The app stores the Supabase session in an encrypted, secure browser cookie so
login can survive a browser restart. Configure a private Fernet key in
Streamlit Secrets or the `AUTH_COOKIE_ENCRYPTION_KEY` environment variable.
Generate a key locally with:

```sh
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Add the generated key to Streamlit Secrets:

```toml
AUTH_COOKIE_ENCRYPTION_KEY = "YOUR-GENERATED-FERNET-KEY"
```

Keep this key private and stable across app restarts; changing it invalidates
existing login cookies. Do not use the Supabase publishable key or a
`service_role` key as the cookie-encryption key. The cookie has a rolling
400-day browser lifetime and is renewed when the user returns; Supabase still
controls refresh-token validity and can require reauthentication if a session is
revoked or expires under project policy. The app uses Supabase's refresh-token
flow on Streamlit runs so each refreshed token pair is written back encrypted.
Use HTTPS in deployment. Select **Logout** to clear the browser cookie.

## Deploy with Streamlit Community Cloud

1. Push the project to a GitHub repository you control. Do not include local
   secrets or credentials in the repository.
2. In Streamlit Community Cloud, create an app from that repository and select
   `app.py` as the main file.
3. In the app's settings, add `SUPABASE_URL` and `SUPABASE_ANON_KEY` as
   Streamlit secrets, using the Supabase project URL and publishable (anon) key.
   Also add a generated `AUTH_COOKIE_ENCRYPTION_KEY`. Do not add a
   `service_role` key.
4. Deploy and test sign-up, sign-in, adding a medicine, updating a dose, and
   viewing history with fake data. Confirm your Supabase RLS policies isolate
   users before allowing anyone else to use the app.

The app uses the server's current date to create daily dose records when it
runs; there is no user-configurable timezone or background scheduler in the
code.

## Important limitations

This is a basic tracker, not a medical decision system. It does not prescribe,
validate, or change medication instructions. `Unconfirmed` means the app has no
recorded Taken/Skipped choice; it does not establish whether a dose was missed.
The app does not automatically refresh every 15 seconds: Streamlit reruns it
when the user interacts with the page. No push notifications or other
out-of-app reminders are implemented.
