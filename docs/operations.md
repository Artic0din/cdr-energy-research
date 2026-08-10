# Operations Runbook

How to refresh data, re-run sweeps, and validate output.

## Refresh — quick (~5 min, incremental)

```bash
# 1. Re-fetch EME refdata2 (registry source-of-truth for metadata)
python3 -c "
import urllib.request, json
req = urllib.request.Request('https://api.energymadeeasy.gov.au/refdata2?keys=organisations,thirdParties', headers={'User-Agent':'Mozilla/5.0'})
with urllib.request.urlopen(req, timeout=30) as r:
    json.dump(json.loads(r.read()), open('data/eme-refdata.json','w'), indent=2)
"

# 2. Incremental plan sync — only plans changed since last sweep
LAST_SYNC=$(date -u -v-7d +%Y-%m-%dT%H:%M:%S+10:00)  # 7 days ago Melbourne time
# (TODO: enhance scripts/cdr_full_sweep_v2.py to support --updated-since flag)
```

## Refresh — full (~33 min)

```bash
# 1. Refresh registry
# (as above)

# 2. Re-download AER PDF if a new monthly version exists
# Visit: https://www.aer.gov.au/documents/consumer-data-right-energy-retailer-base-uris-and-cdr-brands
# Save to data/aer-base-uris-<MMM-YYYY>.pdf

# 3. Run the full sweep
python3 scripts/cdr_full_sweep_v2.py
# Output:
# - docs/shape-catalog-v2.md
# - docs/enums-reference.md
# - data/registry-comparison.json
# - data/retailer-index.json

# 4. Sanity check
ls -la docs/shape-catalog-v2.md  # should be ~2 MB
grep -c "^### SIG_" docs/shape-catalog-v2.md  # should be > 1500 unique sigs
```

## Cache layout

```
/tmp/cdr-cache/
├── _registry.json              # GitHub jxeeno cached (legacy, v1 only)
├── _progress_v2.json           # checkpoint state
├── _failed_v2.jsonl            # per-plan failures
└── <slug>/
    ├── _planlist.json          # full plan list for retailer
    ├── _planlist_<brand>.json  # branded list when shared base URI
    └── <planId>.json           # individual plan details
```

149 MB after v1 sweep, ~150-200 MB expected after v2. Symlinked into the repo at `cache/v1`.

## Validation checks

```bash
# Count cached plans
find /tmp/cdr-cache -name '*.json' -not -name '_*' | wc -l
# Expected: ~10,266+ (depending on retailer additions)

# Count unique brand IDs from EME
python3 -c "import json; d=json.load(open('data/eme-refdata.json')); print(sum(1 for o in d['data']['organisations'].values() if o.get('cdrCode')))"
# Expected: 117

# Sanity: every cached plan parses
python3 -c "
import json, os
ok = bad = 0
for d in os.listdir('/tmp/cdr-cache'):
    if d.startswith('_'): continue
    for f in os.listdir(f'/tmp/cdr-cache/{d}'):
        if f.startswith('_') or not f.endswith('.json'): continue
        try: json.load(open(f'/tmp/cdr-cache/{d}/{f}')); ok += 1
        except: bad += 1
print(f'OK: {ok}, broken: {bad}')
"
```

## Watching upstream

```bash
# Open Energy issues sorted by recent activity
gh issue list --repo ConsumerDataStandardsAustralia/standards-maintenance \
  --label Energy --state open --search "sort:updated-desc" --limit 20

# Watch the SSO landing
gh issue view 719 --repo ConsumerDataStandardsAustralia/standards-maintenance --comments

# Watch the EV-overlay pricingModel issue
gh issue view 710 --repo ConsumerDataStandardsAustralia/standards-maintenance --comments
```

## Troubleshooting

### Sweep hangs on one retailer
- Check `/tmp/cdr_v2.stderr` for the last `[N/117]` line
- That retailer is likely rate-limiting
- Solution: lower `PARALLEL_RETAILERS` in the script, or add backoff to that specific slug

### 404s on plan detail despite being listed
- Already-known issue: in v1 sweep, **0/10,266 plans 404'd** — reliability is excellent
- If you see new 404s, check `/tmp/cdr-cache/_failed_v2.jsonl`
- Could indicate a retailer mid-deprecation; cross-check `lastUpdated` and `effectiveTo`

### Cache disk full
- v1 cache: 149 MB. v2 expected ~200 MB
- If full: `rm -rf /tmp/cdr-cache/<slug>` for retailers you don't need

### EME refdata2 returns 5xx
- AER hosts EME so outages are rare but possible
- Fall back to last-cached `data/eme-refdata.json`
- Or fall back to AER PDF as primary registry

## Polite usage

- 1 request per second per base URI maximum
- 12-way parallelism across distinct base URIs is fine; brands on one shared
  base URI must use the same serialized per-second budget
- AER doesn't publish formal rate limits but responds cleanly under that ceiling
- Add `?updated-since=` for incremental sync to avoid re-fetching unchanged plans

## Privacy / legal note

- API is **public** (no auth required) per AER's CDR designation
- Data is publicly published energy plan information
- BUT: this private repo is the right place for ops scripts, internal notes, and
  unpublished analysis. Public release would need legal sign-off.
