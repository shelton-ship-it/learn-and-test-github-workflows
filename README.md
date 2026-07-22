# learn-and-test-github-workflows
This workflow runs when a trigger is used, downloading and encoding digital content of your choice.
What it is: a lean variant of process.yml, same functionality, with time/exposure optimizations. Lives at .github/workflows/process-leve.yml, on both accounts.

Flow:

Prepare environment, install FFmpeg + libtorrent.
Resolve credentials — detects which account it's running on, sets the opposite account as the storage target.
Coordinator (series/anime/dorama via torrent only): detects episodes, groups into batches, dispatches one child job per batch to the same repo.
Download the video (torrent or artifact).
Probe the video (detect codec/resolution/fps).
HLS encode: if source is already h264 and no rescale is needed → stream copy (no video re-encode); otherwise, full encode (libx264). Audio always re-encoded to AAC 128k.
Extract thumbnail.
Encrypt segments (ChaCha20) → .bin.
Free disk space.
Pick a storage shard (on the target account) and create the job's exclusive branch.
Rewrite URLs, generate master.m3u8 and cdn_urls.json.
Commit + push the .bin files to the shard, in batches.
Register the content in the backend.
Notify the dispatcher via webhook.
Cleanup.

Key differences from the original process.yml:

Conditional stream copy (avoids unnecessary re-encode).
Dead-torrent timeout: 180s → 45s.
Reduced retries on the coordinator (4→3) and webhook (3→2); kept as-is on git push and content registration.
Cross-account storage: bins always go to the account opposite the one that ran the job.

Deployment prerequisite: secrets STORAGE_ACCOUNT_A_OWNER/TOKEN and STORAGE_ACCOUNT_B_OWNER/TOKEN configured identically on both repos.


