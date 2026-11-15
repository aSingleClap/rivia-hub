# rivia-hub

Server Hub — the Rivia sleek UI for managing the MCP server on a Tab S7+.
A single-page static site served by a Cloudflare Worker with static assets.

## Project structure

```
public/          Static assets served to visitors
  index.html     The full single-page app (HTML + CSS + JS)
wrangler.jsonc   Cloudflare Worker config (name: "rivia-hub")
```

## Deploy

This is a static-assets-only Worker (no build step needed).

```sh
npx wrangler deploy
```

## Connect to GitHub for automatic deploys (Workers Builds)

1. Push this repository to GitHub.
2. In the Cloudflare dashboard: **Workers & Pages** → **rivia-hub** → **Settings** → **Builds** → **Connect**.
3. Select this repository and set:
   - **Production branch:** `main`
   - **Build command:** *(leave empty — no build step)*
   - **Deploy command:** `npx wrangler deploy`
4. Save. Every `git push` to `main` now auto-deploys.

> The `name` in `wrangler.jsonc` is `rivia-hub` and must match the existing Worker,
> or the build will fail.
