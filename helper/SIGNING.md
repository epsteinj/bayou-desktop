# Signing & notarizing the bayou helper (macOS)

The hosted UI is a website — nothing to sign. The **helper** is what ships to
users' machines, and an unsigned thing that auto-starts and runs a bash-capable
agent will be blocked by Gatekeeper and (rightly) distrusted. To distribute it
you must **package → code-sign → notarize → staple**.

> Status: this is the remaining public-launch blocker. It requires an **Apple
> Developer Program** membership ($99/yr) and a build step that can't be done
> from this repo's `install.sh` (which builds a venv on the user's machine —
> fine for dev/internal, not signable). Treat this file as the runbook.

## Prerequisites
- Apple Developer Program membership.
- **Developer ID Application** + **Developer ID Installer** certificates in your
  login keychain (Xcode → Settings → Accounts → Manage Certificates).
- A notarytool credential profile:
  ```bash
  xcrun notarytool store-credentials bayou-notary \
    --apple-id you@example.com --team-id TEAMID --password <app-specific-password>
  ```

## Recommended packaging: freeze → .app → .pkg
The venv-on-install approach isn't signable. Instead freeze the helper into a
self-contained bundle so Python + mlx + the harness are inside one signed app.

1. **Freeze** the helper (PyInstaller; mlx ships Metal libs, so test on a clean
   machine):
   ```bash
   pyinstaller --windowed --name bayou-helper \
     --collect-all mlx --collect-all mlx_lm --collect-all bayou \
     backend/server.py
   # → dist/bayou-helper.app
   ```
2. **Sign** with hardened runtime + the entitlements below:
   ```bash
   codesign --deep --force --options runtime \
     --entitlements helper/entitlements.plist \
     --sign "Developer ID Application: Your Name (TEAMID)" \
     dist/bayou-helper.app
   ```
3. **Wrap in a signed .pkg** (lays down the .app + the LaunchAgent plist):
   ```bash
   pkgbuild --root dist/bayou-helper.app --install-location /Applications/bayou-helper.app \
     --scripts helper/pkg-scripts --identifier com.bayou.helper --version 0.1.0 component.pkg
   productbuild --package component.pkg --sign "Developer ID Installer: Your Name (TEAMID)" bayou-helper.pkg
   ```
4. **Notarize + staple:** `./helper/sign.sh bayou-helper.pkg` (below).

## Entitlements (`helper/entitlements.plist`)
mlx JITs Metal kernels, so the hardened runtime needs:
- `com.apple.security.cs.allow-jit` = true
- `com.apple.security.cs.allow-unsigned-executable-memory` = true
- (avoid `disable-library-validation` unless a dependency forces it)

## Notes
- Alternative: ship the **UI as a Tauri `.app`** (Tauri's bundler signs +
  notarizes via its config) that spawns the signed helper. More moving parts.
- Re-sign + re-notarize on every release; staple so it verifies offline.
- This does NOT remove the security model — keep `BAYOU_ALLOWED_ORIGINS`, the
  pairing token, the approval gates, and the tool sandbox.
