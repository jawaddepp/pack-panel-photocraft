# Pack Panel for PhotoCraft

A docked asset-pack panel for [PhotoCraft](https://getartcraft.com) on macOS. Point it at a folder of textures, PNG/JPG/SVG art, PSD templates, brushes and layer styles, and it sits beside PhotoCraft. One click puts an asset into your document.

> Pack Panel does **not** run Photoshop plugins. It does the job a texture-pack plugin does, using files on your Mac (and, optionally, downloads from your shop account).

## Features

**Browsing**
- **Packs:** every folder is a pack, shown as a row with its cover. Rows open as dropdowns, one at a time.
- **Templates:** PSD and PSB files as cover cards.
- **Inspo:** any web page you choose, shown inside the panel.
- Search across every pack, a **★ favorites** filter and a **↺ recent** list.
- **Quick Look preview:** press Space, use ← → to browse, Enter to place and F to favorite.
- Sharp previews on Retina screens. Transparent art sits on a checkerboard that adapts to light or dark art.

**Placing, like Photoshop's Place Embedded**
- Images land as smart objects, centred, just above the selected layer, named after the file.
- Images larger than the canvas are scaled to fit. The smart object keeps the full original resolution.
- Each place is one History step, with an **Undo** button right in the panel.
- Very large templates ask before opening, with an estimate of how much memory they need.

**Brushes, layer styles and actions**
- `.abr`, `.asl` and `.atn` files import with a click and get an "In PhotoCraft" badge. A file is never imported twice by accident.
- **Show me** opens the right PhotoCraft panel (Brushes, Styles and so on) and tells you which group to look in.

**Store sign-in, downloads and updates (optional)**
- Sign in to your shop's download portal with your email and an emailed code, or with your order number.
- Packs you own but haven't downloaded appear with a **Download** button. **Download all** gets everything.
- **Automatic updates:** new and updated packs download one at a time, unpack into your folder and replace the old version. The panel respects the portal's daily download limit.
- Downloaded brushes and styles can be added to PhotoCraft automatically.

**A real Mac panel**
- Native app with a Dock icon. The panel floats beside PhotoCraft, or just inside its panel column when PhotoCraft fills the screen.
- It hides when you switch to another app, and collapses to a draggable floating icon (⌘J).

## Install

1. Download **Pack-Panel-1.6.1-macOS.zip** from [Releases](../../releases) and unzip it.
2. Move **Pack Panel.app** to Applications.
3. The first time, right-click it, then choose **Open**, then **Open**. The app is not notarized by Apple, so macOS asks once.
4. In the panel, click **Choose folder…** and pick your packs folder.
5. Click **Open PhotoCraft**. If PhotoCraft is already open, choose **Reconnect** instead; it asks you to save open work first.

**Requirements**
- macOS 12 or later.
- [PhotoCraft](https://getartcraft.com) in `/Applications`. Tested with 0.23.1.
- Python 3. If you don't have it, run `xcode-select --install`.

## Store downloads

On a fresh install, the welcome screen offers **Sign in and download**: sign in with your email code or order number, then click **Download all**. Packs go to `~/Pictures/Everything Pack` unless you choose another folder. The store address is set by default and can be changed in **Settings › Store download address**.

The client speaks the API used by the Everything Pack download portal: `/api/auth/start`, `/api/auth/verify`, `/api/auth/order`, `/api/packs` and `/api/packs/download`. It only downloads what your own account owns, one file at a time. Your sign-in is stored in `~/Library/Application Support/Pack Panel/store-session.json`, readable only by your user account.

*Pack Panel is an independent project. It is not affiliated with PhotoCraft, Adobe, or any asset shop.*

## How it works

- `packpanel.py` is a local server (Python standard library only) on `127.0.0.1:47821`. Every request needs a per-session token, and requests must come from that local address.
- It launches PhotoCraft with its local control server on `127.0.0.1:47820`, protected by a random key and limited to reading your packs folder.
- It talks to PhotoCraft through `photocraft-cli mcp --bridge`.
- `native/PackPanel.swift` (AppKit and WebKit) hosts the interface in a floating, non-activating panel, docks it beside PhotoCraft's window, and shows the Inspo page in its own web view.
- PhotoCraft 0.23 doesn't accept automated `.pat` pattern imports. For those, the panel opens PhotoCraft's Patterns panel and shows the file in Finder.

## Build from source

```sh
./build_app.sh        # makes "Pack Panel.app" (native panel if Xcode's swiftc is available)
```

Without `swiftc`, the script builds a launcher that opens the panel in Chrome, Brave or your default browser.

| Path | What it is |
|---|---|
| `packpanel.py` | Local server, PhotoCraft link, store client |
| `ui/` | Panel interface (HTML, CSS and JS, no build step) |
| `native/PackPanel.swift` | Native floating panel |
| `build_app.sh` | Builds the app bundle |

## License

MIT
