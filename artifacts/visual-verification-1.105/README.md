# Motion verification — 1.105

Captured from the running app with Playwright/Chromium at 1440×1000.

- `01-picker-motion.png` — preset dialog with content-pane motion.
- `02-picker-streams-hover.png` — family switching and hover feedback.
- `03-rapid-family-switch.png` — after repeated Aim/Streams switches.
- `04-picker-reduced-motion.png` — `prefers-reduced-motion: reduce`; content and interactive motion are disabled.

Computed styles during verification:

```text
content animation: osu-soft-in 0.16s
dialog animation: 0.16s
button transition: 0.12s
button hover: translateY(-1px)
reduced-motion content animation: none
reduced-motion button transition: 0s
reduced-motion hover transform: none
```
