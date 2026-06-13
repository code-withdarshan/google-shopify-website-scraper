# Shopify / Tech-Stack Vertical Scraper

A single-file Windows app that:

1. Searches **Google** (through your real local Chrome browser, in parallel tabs)
2. Detects the **tech stack** of every result (Shopify, WordPress, Wix, Webflow, Squarespace, GoHighLevel, BigCommerce, Magento, Drupal, Joomla, Ghost, Framer, Duda, Weebly, ClickFunnels, Kajabi, Cargo, Next.js, React, WooCommerce)
3. Optionally verifies each site against a **custom niche** you describe in plain English, using NVIDIA's reasoning LLMs (DeepSeek R1, Nemotron, QwQ, etc.)
4. Filters the results and exports a CSV named after your search

---

## Requirements

- Windows 10 or 11 (64-bit)
- **Google Chrome** installed in the standard location
- A free **NVIDIA Build** API key — get one in 60 seconds at <https://build.nvidia.com> → click any model → "Get API Key"
- Internet connection

No Python, no installer, no admin rights needed.

---

## Running

1. Double-click **`ShopifyVerticalScraper.exe`**
2. A console window opens (leave it running) and your browser opens to `http://127.0.0.1:5000`
3. In the **NVIDIA Build API** card on the right:
   - Paste your key
   - Pick a model (default `deepseek-ai/deepseek-r1` is the strongest)
   - Click **Save**
4. Click **Open Google search tab** — a separate Chrome window appears with `google.com` open. If Google shows a consent or login wall, complete it once in that tab. You won't need to do this again until you delete the `.chrome-debug-profile` folder.
5. Fill the form:
   - **Niche keywords** — comma-separated (e.g. `jewelry, engagement rings`)
   - **Niche to verify** — free text, e.g. `dental clinic` or `vegan skincare` (optional)
   - **Country / State / City / Area** — any combination
   - **Tech stacks to keep** — check the platforms you care about
   - **Max results** — typically 25–100
6. Click **Start scraping**

You'll see three phases run live:

- **Collect** — Google queries fired through your real Chrome tab
- **Verify** — each URL is fetched once and fingerprinted for tech stack, then sent to NVIDIA for niche verification
- **Filter** — only sites matching your tech stack + niche are kept

Click **Download CSV** to export. The filename includes your keywords, location, and selected stacks.

---

## How it talks to your browser

The app launches Chrome with the debugger flag in an isolated profile (`.chrome-debug-profile/` next to the exe). It never touches your normal Chrome data. To wipe state (e.g. log out of Google in the scraper's Chrome), delete that folder.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Browser doesn't open | Manually visit `http://127.0.0.1:5000` |
| Status shows `chrome.exe NOT found` | Install Chrome from <https://google.com/chrome> |
| Google returns 0 results in every tab | Log into Google in the opened tab; consent walls block scraping |
| "NVIDIA error: 401" | Wrong/missing API key — paste it in the NVIDIA card and Save |
| App won't start, says "port in use" | Something else is using 5000 — close it or restart the PC |
| Want to factory-reset | Delete `config.json` and `.chrome-debug-profile/` next to the exe |

To stop the app, close the console window.

---

## Data & privacy

- The NVIDIA API key is saved locally in `config.json` next to the exe. Never commit or share that file.
- The app sends each candidate page's text + URL to NVIDIA's API for classification. Don't run it on sites that should not be exposed to a third-party LLM.
- Google sees your queries because they're executed through your real Chrome — same as if you typed them yourself.
