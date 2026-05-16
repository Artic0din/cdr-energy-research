# Brand Assets — Logos + Org Metadata

189 logos cached locally (117 retailers + 72 brokers/third parties), 2.7 MB total. Source: Energy Made Easy `refdata2` API.

## Layout

```
data/logos/
├── _manifest.json                 # cdrCode/orgId → file mapping
├── 0082c19f38cbb...png            # hash-named PNG (~14 KB avg)
└── ... 188 more
```

## How to look up a brand's logo

```python
import json

manifest = json.load(open("data/logos/_manifest.json"))

# By cdrCode (retailer)
agl = next(m for m in manifest if m.get("cdrCode") == "agl")
logo_path = f"data/logos/{agl['file']}"
# → /static/organisations/logos/04406045549ba2ea3773d3ea0ef06f89.png on EME

# By trading name
amber = next(m for m in manifest if "amber" in (m.get("name") or "").lower())
```

## Manifest schema

```json
{
  "orgId": "1559",
  "cdrCode": "ergon",
  "name": "Ergon Energy",
  "logo": "/static/organisations/logos/abc123.png",   // EME path
  "url": "https://www.energymadeeasy.gov.au/static/...",  // resolved URL
  "file": "abc123.png",                                 // local filename
  "status": "ok(14523)",                                // bytes downloaded
  "thirdParty": true                                    // optional, present for brokers
}
```

## URL resolution

EME serves logos at:
- `https://www.energymadeeasy.gov.au/static/organisations/logos/<hash>.png`

The `logo` field in `refdata2` is the path component starting with `/`.

## Refresh

```bash
# Re-fetch refdata + logos (idempotent, skips cached)
python3 -c "
import urllib.request, json, os
ua = 'Mozilla/5.0'
req = urllib.request.Request('https://api.energymadeeasy.gov.au/refdata2?keys=organisations,thirdParties', headers={'User-Agent': ua})
with urllib.request.urlopen(req, timeout=30) as r:
    refdata = json.loads(r.read())
json.dump(refdata, open('data/eme-refdata.json','w'), indent=2)
out = 'data/logos'
os.makedirs(out, exist_ok=True)
for src in (refdata['data']['organisations'].values(),
            refdata['data']['thirdParties'].values()):
    for o in src:
        if not o.get('logo'): continue
        url = 'https://www.energymadeeasy.gov.au' + o['logo']
        fname = os.path.basename(o['logo'])
        path = os.path.join(out, fname)
        if os.path.exists(path) and os.path.getsize(path): continue
        try:
            req = urllib.request.Request(url, headers={'User-Agent': ua})
            with urllib.request.urlopen(req, timeout=15) as r:
                open(path,'wb').write(r.read())
        except Exception as e:
            print(f'FAIL {fname}: {e}')
"
```

## Notes

- Filenames are content-addressed hashes (PNG content). If a retailer rebrands, the hash changes; the manifest tracks the latest.
- All 189 fetched cleanly — no failures.
- Average size 14 KB; all PNG; dimensions vary (typically 100-300px wide).
- Suitable for direct embed in HA Lovelace cards.

## Org metadata (beyond logo)

The `eme-refdata.json` also carries per-retailer:

| Field | Use |
|---|---|
| `tradingName` | Display name for plan picker |
| `orgName` | Legal entity name |
| `cdrCode` / `cdrBrand` | API endpoint key |
| `abn` | Australian Business Number |
| `websiteURL` | Retailer's main site |
| `electricityBillURL` | Direct link to retailer's bill explainer |
| `gasBillURL` | Same for gas |
| `retailerCode` | AEMO/AER classification code (e.g. "ERG") |
| `residentialContact` | Customer phone |
| `smallBusinessContact` | Business phone |
| `orgStatus` | Active/inactive flag |
