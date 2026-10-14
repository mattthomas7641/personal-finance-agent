# Using Northstar on your phone

Northstar runs on your Mac. Your iPhone reaches it privately over [Tailscale](https://tailscale.com) (free), and you add it to the home screen like an app.

1. Install Tailscale on the Mac and the iPhone, and sign in to the same account on both.
2. On the Mac, open Northstar → **Privacy & security** and set a passcode (10+ characters). Phones need it; the Mac doesn't.
3. On the Mac, run once in Terminal:

   ```bash
   tailscale serve --bg 8766
   ```

   If it asks, allow HTTPS certificates for your tailnet. This gives Northstar a private HTTPS address that only your devices can reach.
4. On the iPhone, open Safari to the address shown on the Privacy & security page (`https://<your-mac>.<tailnet>.ts.net`), sign in, then **Share → Add to Home Screen**.

Pages you open are saved on the phone for up to 7 days, so you can see your last-synced numbers when the Mac is off. Signing out deletes them.
