# EDL review: clip.mp4

- **Source:** `clip.mp4`
- **Duration:** 00:12.00 → 00:06.10 (cut 5.9s, 49.2%)
- **3 cuts** by category: 1 silence, 1 retake, 1 filler
- **Filtered to:** `retake` only

## Cut #2 — 00:07.00 → 00:08.70 (1.7s)
**Reason:** retake: 'wait, let me start over' [frame ok]

```
  [   0.2] Hello and welcome to Kalmströ
✂ [   7.0] wait, let me start over.    ←
  [  10.0] is running now.
```

---

To restore a cut, edit the EDL JSON and flip its `keep` from `false` to `true`.
Then re-run `apply.py`. The EDL is the source of truth — `review.py` is read-only.
