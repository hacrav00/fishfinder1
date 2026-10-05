# FishFinder ROV Mobile App

This project builds a native Android APK for controlling the Raspberry Pi FishFinder ROV system with full-screen live video and touch swipe camera gestures.

---

## Method 1: Instant 1-Tap Install (PWA — No Build Required)
You can install the app directly on your Android phone right now without waiting for compilation:
1. Connect your phone Wi-Fi to your router (`dlink` / `DIR-615-72AB`).
2. Open Chrome on your phone and go to:
   ```
   http://192.168.0.131:8080/
   ```
3. Tap the **three dots menu (⋮)** in Chrome and tap **"Install app"** or **"Add to Home screen"**.
4. An icon named **"FishFinder ROV"** appears on your home screen. When you tap it, it launches in **true full screen** (no browser address bar, locked landscape) and behaves exactly like a native APK!

---

## Method 2: Build Native Android APK using GitHub Actions
1. Create a repository on GitHub (e.g. `fishfinder-app`).
2. Run the included uploader:
   ```bash
   python upload_to_github.py <YOUR_GITHUB_PERSONAL_ACCESS_TOKEN>
   ```
3. GitHub Actions will automatically start building your APK in the cloud.
4. Go to your repository on GitHub:
   - Click on the **Actions** tab.
   - Click on the latest workflow run: **Build FishFinder Android APK**.
   - Under **Artifacts**, download `FishFinder-ROV-App` (`app-debug.apk`).
5. Open and install `app-debug.apk` on your Android phone!
