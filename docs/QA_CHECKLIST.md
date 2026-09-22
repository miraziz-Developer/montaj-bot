# QA checklist — run through this before showing the bot to real bloggers

## Money (most important — test these first, with a test account)
- [ ] Balance never goes negative no matter what you click.
- [ ] Reserved units are refunded when a job fails (check `unit_ledger`, not just the balance).
- [ ] Reserved units are refunded when you cancel BEFORE analysis starts, and NOT refunded once analysis has started (per policy).
- [ ] Free trial: works once per Telegram account, once per phone number; blocked the second time either way.
- [ ] Buying a tariff, sending a fake receipt, and approving it as admin grants exactly the right number of units, once.
- [ ] A non-admin cannot approve or reject payments, even by replaying an old callback.
- [ ] Two uploads confirmed at almost the same instant with only 1 unit of balance: only one succeeds.

## Upload
- [ ] A 500 MB+ file uploads successfully from a real phone over normal 4G (not just Wi-Fi on a laptop).
- [ ] Killing the app mid-upload and reopening resumes instead of restarting.
- [ ] A non-video file is rejected with a clear Uzbek message before it wastes a unit.
- [ ] A video longer than the configured maximum is rejected before it's queued.
- [ ] A trial user uploading a video longer than 60s is told so before it's queued, not after paying nothing but wasting compute.

## Understanding and planning
- [ ] The plan's Uzbek summary actually matches what got cut (spot-check 5 different real videos across different niches).
- [ ] The hook (first clip) for "dynamic_reels"/"ad_commercial" is a genuinely strong moment, not just clip #1 of the source.
- [ ] Cuts never land mid-word (listen with headphones on at least 5 videos).
- [ ] A silent, low-content video (or one Gemini fails on) still produces SOMETHING via the fallback planner instead of an error.
- [ ] Revision requests in Uzbek, Russian, and mixed language all do something reasonable ("qisqartir", "музыку убери", "3-qismni o'chir").
- [ ] Asking for something out of scope (new footage, translation, voice change) gets a clear "can't do that" instead of a silently wrong plan.

## Render quality
- [ ] Captions are readable on a real phone screen, not just a monitor (check font size, contrast, position on both 9:16 and 16:9).
- [ ] Subtitle text matches spoken Uzbek reasonably well (STT quality check — this is the biggest quality risk in the whole product).
- [ ] Music, when on, doesn't drown out speech (ducking works) and fades out before the video ends, not mid-note.
- [ ] Output plays correctly in Telegram's in-app player AND when downloaded and opened elsewhere (faststart, correct pixel format).
- [ ] Reframed 9:16 output keeps the speaker's face in frame throughout, not just at the start.

## Delivery and reliability
- [ ] A ~1 GB finished video is delivered through the bot itself (not just a link) via the local Bot API server.
- [ ] A 2GB+ video (or any send failure) falls back cleanly to a working 48-hour download link.
- [ ] A job stuck for 30+ minutes (kill the worker mid-render once, on purpose) gets recovered or fails-with-refund, not lost forever.
- [ ] Files actually disappear from Blob after 48 hours (check the lifecycle rule fired, don't just trust the config).

## Cost sanity (before opening this to real users)
- [ ] Run `scripts/cost_report.py` on at least 10 real test jobs across different lengths and niches.
- [ ] Confirm the estimated cost per unit is meaningfully below the tariff price per unit, including revision retries.
- [ ] Confirm a deliberately long video (30+ min) doesn't blow the budget for its unit cost — check light-mode kicked in.

## Uzbek text quality
- [ ] Every user-facing string uses proper `o'`/`g'` (apostrophe character `'` or the dedicated ʻ, pick one and be consistent —
      check it renders correctly on a real phone keyboard/font, not just in your editor).
- [ ] No leftover English debug strings or `{placeholder}` text ever reaches the user.
