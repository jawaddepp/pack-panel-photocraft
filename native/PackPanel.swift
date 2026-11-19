// Pack Panel: native panel host for PhotoCraft.
//
// Runs the bundled Python server (packpanel.py), shows its interface in a
// floating, non-activating panel that snaps to the side of PhotoCraft's window
// like a docked panel, hides when you switch to another app, and shows the
// Inspo page in a native web view (that page refuses to load inside frames).

import AppKit
import WebKit

let panelPort = 47821
let panelURL = URL(string: "http://127.0.0.1:\(panelPort)/")!
let photoCraftBundleID = "ai.storyteller.photocraft"

/// The floating icon the panel collapses into. Click to bring the panel back; drag to move it.
final class BubbleView: NSView {
    var onClick: (() -> Void)?
    var onMoved: ((NSPoint) -> Void)?
    var badge = false { didSet { needsDisplay = true } }
    private var startMouse: NSPoint = .zero
    private var startOrigin: NSPoint = .zero
    private var dragged = false

    override init(frame: NSRect) {
        super.init(frame: frame)
        setAccessibilityElement(true)
        setAccessibilityRole(.button)
        setAccessibilityLabel("Show Pack Panel")
        toolTip = "Show Pack Panel"
    }
    required init?(coder: NSCoder) { fatalError() }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        let box = bounds.insetBy(dx: 6, dy: 6)
        let shape = NSBezierPath(roundedRect: box, xRadius: 14, yRadius: 14)
        NSColor(srgbRed: 0.17, green: 0.17, blue: 0.185, alpha: 0.97).setFill()
        shape.fill()
        NSColor(white: 1, alpha: 0.14).setStroke()
        shape.lineWidth = 1
        shape.stroke()
        // the tape strip from the app icon
        NSGraphicsContext.saveGraphicsState()
        let t = NSAffineTransform()
        t.translateX(by: bounds.midX, yBy: bounds.midY + 5)
        t.rotate(byDegrees: 4)
        t.concat()
        NSColor(srgbRed: 0.96, green: 0.77, blue: 0.32, alpha: 1).setFill()
        NSBezierPath(rect: NSRect(x: -15, y: -4, width: 30, height: 8)).fill()
        NSGraphicsContext.restoreGraphicsState()
        NSColor(white: 0.58, alpha: 1).setFill()
        for i in 0..<2 {
            NSBezierPath(roundedRect: NSRect(x: bounds.midX - 14 + CGFloat(i) * 15, y: bounds.midY - 14, width: 13, height: 10),
                         xRadius: 2, yRadius: 2).fill()
        }
        if badge {
            let dot = NSRect(x: box.maxX - 11, y: box.maxY - 11, width: 9, height: 9)
            NSColor(srgbRed: 0.96, green: 0.77, blue: 0.32, alpha: 1).setFill()
            NSBezierPath(ovalIn: dot).fill()
        }
    }

    override func mouseDown(with event: NSEvent) {
        startMouse = NSEvent.mouseLocation
        startOrigin = window?.frame.origin ?? .zero
        dragged = false
    }

    override func mouseDragged(with event: NSEvent) {
        let now = NSEvent.mouseLocation
        let dx = now.x - startMouse.x, dy = now.y - startMouse.y
        if abs(dx) + abs(dy) > 3 { dragged = true }
        if dragged { window?.setFrameOrigin(NSPoint(x: startOrigin.x + dx, y: startOrigin.y + dy)) }
    }

    override func mouseUp(with event: NSEvent) {
        if dragged { onMoved?(window?.frame.origin ?? .zero) } else { onClick?() }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKScriptMessageHandler,
                         WKNavigationDelegate, WKUIDelegate {
    var panel: NSPanel!
    var webView: WKWebView!
    var inspoView: WKWebView?
    var server: Process?
    var followTimer: Timer?
    var docked = UserDefaults.standard.object(forKey: "docked") as? Bool ?? true
    var movingProgrammatically = false
    var lastDockFrame: NSRect = .zero
    var inspoGeneration = 0
    var lastInspoURL = ""
    var quitting = false
    var userHidden = false   // closed with the red button: stays hidden until reopened from the Dock
    var collapsed = false    // shrunk to the floating icon
    var bubble: NSPanel!
    var bubbleView: BubbleView!

    // MARK: lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        // One panel only: if another copy is running (e.g. one in Applications and one
        // in a build folder), bring that one forward and quit this one.
        let me = ProcessInfo.processInfo.processIdentifier
        if let other = NSRunningApplication.runningApplications(withBundleIdentifier: Bundle.main.bundleIdentifier ?? "local.packpanel")
            .first(where: { $0.processIdentifier != me }) {
            other.activate()
            NSApp.terminate(nil)
            return
        }
        buildMenu()
        buildPanel()
        buildBubble()
        guard startServer() else { return }
        waitForServer { [weak self] ok in
            guard let self else { return }
            if ok {
                var url = panelURL
                if let tab = UserDefaults.standard.string(forKey: "startTab"), !tab.isEmpty,
                   let withTab = URL(string: panelURL.absoluteString + "#" + tab) { url = withTab }
                self.webView.load(URLRequest(url: url))
                // Like a docked panel: open on top of PhotoCraft when it is running.
                if let pc = NSRunningApplication.runningApplications(withBundleIdentifier: photoCraftBundleID).first {
                    pc.activate()
                    self.follow()
                }
                if ProcessInfo.processInfo.environment["PACKPANEL_START_COLLAPSED"] == "1" {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { self.collapseToIcon() }
                }
            } else {
                self.fail("Pack Panel couldn't start its local service. Make sure Python 3 is installed (run xcode-select --install in Terminal), then open Pack Panel again.")
            }
        }
        panel.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        NSWorkspace.shared.notificationCenter.addObserver(
            self, selector: #selector(appActivated(_:)),
            name: NSWorkspace.didActivateApplicationNotification, object: nil)
        followTimer = Timer.scheduledTimer(withTimeInterval: 0.35, repeats: true) { [weak self] _ in self?.follow() }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showPanel()
        return true
    }

    // Closing the panel hides it; Pack Panel keeps running so automatic pack updates continue.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        userHidden = true
        panel.orderOut(nil)
        return false
    }

    @objc func showPanel() {
        userHidden = false
        collapsed = false
        bubble?.orderOut(nil)
        panel.makeKeyAndOrderFront(nil)
        follow()
    }

    func buildBubble() {
        bubble = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 64, height: 64),
                         styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        bubble.isFloatingPanel = true
        bubble.level = .floating
        bubble.backgroundColor = .clear
        bubble.isOpaque = false
        bubble.hasShadow = true
        bubble.hidesOnDeactivate = false
        bubble.collectionBehavior = [.fullScreenAuxiliary, .moveToActiveSpace]
        bubbleView = BubbleView(frame: NSRect(x: 0, y: 0, width: 64, height: 64))
        bubbleView.onClick = { [weak self] in self?.showPanel() }
        bubbleView.onMoved = { origin in
            UserDefaults.standard.set([Double(origin.x), Double(origin.y)], forKey: "bubbleOrigin")
        }
        bubble.contentView = bubbleView
    }

    @objc func collapseToIcon() {
        collapsed = true
        var origin = NSPoint(x: panel.frame.maxX - 72, y: panel.frame.maxY - 72)
        if let saved = UserDefaults.standard.array(forKey: "bubbleOrigin") as? [Double], saved.count == 2 {
            let p = NSPoint(x: saved[0], y: saved[1])
            if NSScreen.screens.contains(where: { $0.visibleFrame.insetBy(dx: -20, dy: -20).contains(p) }) { origin = p }
        }
        bubble.setFrameOrigin(origin)
        panel.orderOut(nil)
        bubble.orderFront(nil)
    }

    func applicationDockMenu(_ sender: NSApplication) -> NSMenu? {
        let menu = NSMenu()
        menu.addItem(withTitle: "Show Pack Panel", action: #selector(showPanel), keyEquivalent: "")
        menu.addItem(withTitle: "Snap to PhotoCraft", action: #selector(snapToPhotoCraft), keyEquivalent: "")
        menu.addItem(withTitle: "Hide to Floating Icon", action: #selector(collapseToIcon), keyEquivalent: "")
        return menu
    }

    func applicationWillTerminate(_ notification: Notification) {
        quitting = true
        server?.terminate()
    }

    // MARK: server

    func pythonPath() -> String? {
        ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
            .first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    func startServer() -> Bool {
        guard let script = Bundle.main.path(forResource: "packpanel", ofType: "py") else {
            fail("Pack Panel is missing packpanel.py. Rebuild the app with build_app.sh.")
            return false
        }
        guard let python = pythonPath() else {
            fail("Pack Panel needs Python 3. Open Terminal and run: xcode-select --install")
            return false
        }
        // Stop any older Pack Panel service so this app always talks to its own version.
        let pkill = Process()
        pkill.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        pkill.arguments = ["-f", "Resources/packpanel.py"]
        try? pkill.run()
        pkill.waitUntilExit()
        Thread.sleep(forTimeInterval: 0.3)

        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = ["-I", script, "--no-window", "--stay"]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        p.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                guard let self, !self.quitting else { return }
                // The service stopped unexpectedly: start it again and reload the panel.
                DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
                    if self.startServer() {
                        self.waitForServer { ok in if ok { self.webView.reload() } }
                    }
                }
            }
        }
        do { try p.run() } catch {
            fail("Pack Panel couldn't start Python: \(error.localizedDescription)")
            return false
        }
        server = p
        return true
    }

    func waitForServer(_ done: @escaping (Bool) -> Void) {
        var tries = 0
        func attempt() {
            tries += 1
            var req = URLRequest(url: URL(string: "http://127.0.0.1:\(panelPort)/api/ping")!)
            req.timeoutInterval = 1
            URLSession.shared.dataTask(with: req) { data, _, _ in
                DispatchQueue.main.async {
                    if data != nil { done(true) }
                    else if tries < 40 { DispatchQueue.main.asyncAfter(deadline: .now() + 0.25, execute: attempt) }
                    else { done(false) }
                }
            }.resume()
        }
        attempt()
    }

    func fail(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "Pack Panel can't start"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.runModal()
        NSApp.terminate(nil)
    }

    // MARK: window

    func buildPanel() {
        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 440, height: 860),
            styleMask: [.titled, .closable, .resizable, .utilityWindow, .nonactivatingPanel],
            backing: .buffered, defer: false)
        panel.title = "Pack Panel"
        panel.titleVisibility = .hidden            // the panel's own header shows the name
        panel.titlebarAppearsTransparent = true
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = false
        panel.minSize = NSSize(width: 280, height: 360)
        panel.setContentSize(NSSize(width: preferredWidth(on: NSScreen.main), height: min(860, (NSScreen.main?.visibleFrame.height ?? 900) - 60)))
        panel.collectionBehavior = [.fullScreenAuxiliary, .moveToActiveSpace]
        panel.delegate = self
        panel.setFrameAutosaveName("PackPanelWindow")
        if panel.frame.origin == .zero { panel.center() }

        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "packpanel")
        webView = WKWebView(frame: panel.contentView!.bounds, configuration: config)
        webView.autoresizingMask = [.width, .height]
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.setValue(false, forKey: "drawsBackground")
        panel.contentView!.addSubview(webView)
        panel.backgroundColor = NSColor(name: nil) { appearance in
            appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
                ? NSColor(srgbRed: 0.137, green: 0.137, blue: 0.149, alpha: 1)
                : NSColor(srgbRed: 0.914, green: 0.914, blue: 0.925, alpha: 1)
        }
    }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Show Pack Panel", action: #selector(showPanel), keyEquivalent: "1")
        appMenu.addItem(withTitle: "Snap to PhotoCraft", action: #selector(snapToPhotoCraft), keyEquivalent: "d")
        appMenu.addItem(withTitle: "Hide to Floating Icon", action: #selector(collapseToIcon), keyEquivalent: "j")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit Pack Panel", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        NSApp.mainMenu = main
    }

    // MARK: docking

    /// PhotoCraft's main document window, in Cocoa screen coordinates.
    func photoCraftFrame() -> NSRect? {
        guard let pc = NSRunningApplication.runningApplications(withBundleIdentifier: photoCraftBundleID).first,
              let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
                as? [[String: Any]] else { return nil }
        let pid = pc.processIdentifier
        var best: CGRect?
        for info in list {
            guard (info[kCGWindowOwnerPID as String] as? Int32) == pid,
                  (info[kCGWindowLayer as String] as? Int) == 0,
                  let b = info[kCGWindowBounds as String] as? [String: CGFloat],
                  let w = b["Width"], let h = b["Height"], w > 400, h > 300 else { continue }
            let r = CGRect(x: b["X"] ?? 0, y: b["Y"] ?? 0, width: w, height: h)
            if best == nil || r.width * r.height > best!.width * best!.height { best = r }
        }
        guard let r = best, let primary = NSScreen.screens.first else { return nil }
        return NSRect(x: r.minX, y: primary.frame.maxY - r.maxY, width: r.width, height: r.height)
    }

    // PhotoCraft keeps its own panels in a column on the right (icon rail + dock,
    // about 330 pt) and its menus/options bar across the top (68 pt) and status bar
    // at the bottom (24 pt). When there is no room beside its window, the panel sits
    // just inside, left of that column, so it never covers PhotoCraft's Layers panel.
    let topChrome: CGFloat = 68
    let bottomChrome: CGFloat = 24
    var dockInset: CGFloat {
        get { CGFloat(UserDefaults.standard.object(forKey: "dockInset") as? Double ?? 330) }
        set { UserDefaults.standard.set(Double(newValue), forKey: "dockInset") }
    }

    /// A width that suits the screen: about a quarter of it, between 300 and 440 pt,
    /// unless the person has resized the panel themselves (remembered per screen width).
    func preferredWidth(on screen: NSScreen?) -> CGFloat {
        let sw = screen?.visibleFrame.width ?? 1440
        if let saved = UserDefaults.standard.object(forKey: "panelWidth-\(Int(sw))") as? Double { return CGFloat(saved) }
        return max(300, min(440, (sw * 0.24).rounded()))
    }

    func windowDidEndLiveResize(_ notification: Notification) {
        let sw = (panel.screen ?? NSScreen.main)?.visibleFrame.width ?? 1440
        UserDefaults.standard.set(Double(panel.frame.width), forKey: "panelWidth-\(Int(sw))")
        follow()
    }

    func dockFrame(for pc: NSRect) -> NSRect {
        let screen = NSScreen.screens.first { $0.frame.intersects(pc) } ?? NSScreen.main!
        let visible = screen.visibleFrame
        let width = min(preferredWidth(on: screen), max(280, pc.width * 0.45))
        if pc.maxX + width <= visible.maxX {             // room beside PhotoCraft: attach outside
            let top = min(pc.maxY, visible.maxY), bottom = max(pc.minY, visible.minY)
            return NSRect(x: pc.maxX, y: bottom, width: width, height: top - bottom).integral
        }
        let inset = max(0, min(dockInset, pc.width - width))
        let top = min(pc.maxY - topChrome, visible.maxY)
        let bottom = max(pc.minY + bottomChrome, visible.minY)
        return NSRect(x: pc.maxX - inset - width, y: bottom, width: width, height: top - bottom).integral
    }

    func follow() {
        guard docked, !collapsed, panel.isVisible, !panel.inLiveResize, NSEvent.pressedMouseButtons == 0,
              let pc = photoCraftFrame() else { return }
        let target = dockFrame(for: pc)
        if target != panel.frame {
            movingProgrammatically = true
            panel.setFrame(target, display: true)
            movingProgrammatically = false
        }
        lastDockFrame = target
    }

    @objc func snapToPhotoCraft() {
        docked = true
        UserDefaults.standard.set(true, forKey: "docked")
        follow()
    }

    func windowDidMove(_ notification: Notification) {
        // Dragging along PhotoCraft re-docks at the new spot; dragging away detaches.
        guard !movingProgrammatically, NSEvent.pressedMouseButtons != 0, let pc = photoCraftFrame() else { return }
        let f = panel.frame
        if f.intersects(pc.insetBy(dx: -24, dy: -24)) {
            docked = true
            let screen = NSScreen.screens.first { $0.frame.intersects(pc) } ?? NSScreen.main!
            if pc.maxX + f.width > screen.visibleFrame.maxX {
                dockInset = max(0, min(pc.maxX - f.maxX, pc.width - f.width))
            }
        } else {
            docked = false
        }
        UserDefaults.standard.set(docked, forKey: "docked")
    }

    @objc func appActivated(_ note: Notification) {
        guard let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication else { return }
        let photoCraftRunning = !NSRunningApplication.runningApplications(withBundleIdentifier: photoCraftBundleID).isEmpty
        if app.processIdentifier == ProcessInfo.processInfo.processIdentifier {
            if collapsed { bubble.orderFront(nil) } else { showPanel() }
        } else if app.bundleIdentifier == photoCraftBundleID {
            if collapsed { bubble.orderFront(nil) }
            else if !userHidden { panel.orderFront(nil); follow() }
        } else if photoCraftRunning {
            panel.orderOut(nil)   // like a docked panel: only visible with PhotoCraft
            bubble.orderOut(nil)
        }
    }

    // MARK: web bridge

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let type = body["type"] as? String else { return }
        switch type {
        case "inspo":
            showInspo(body["show"] as? Bool ?? false,
                      url: body["url"] as? String ?? "",
                      top: CGFloat((body["top"] as? NSNumber)?.doubleValue ?? 0),
                      bottom: CGFloat((body["bottom"] as? NSNumber)?.doubleValue ?? 0),
                      freeze: body["freeze"] as? Bool ?? false)
        case "dock":
            snapToPhotoCraft()
        case "collapse":
            collapseToIcon()
        case "badge":
            bubbleView?.badge = body["on"] as? Bool ?? false
        default:
            break
        }
    }

    func showInspo(_ show: Bool, url: String, top: CGFloat, bottom: CGFloat = 0, freeze: Bool = false) {
        guard let content = panel.contentView else { return }
        inspoGeneration += 1
        let generation = inspoGeneration
        if !show || url.isEmpty {
            guard let v = inspoView, !v.isHidden else { return }
            guard freeze else { v.isHidden = true; return }
            // A menu or dialog is opening over the Inspo page: hand the panel a picture of
            // the page to show underneath, then step aside so the menu is visible on top.
            v.takeSnapshot(with: nil) { [weak self] image, _ in
                guard let self else { return }
                let hide = { if self.inspoGeneration == generation { v.isHidden = true } }
                guard let image, let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
                      let jpg = rep.representation(using: .jpeg, properties: [.compressionFactor: 0.82]) else { hide(); return }
                let js = "window.packPanelInspoSnapshot && window.packPanelInspoSnapshot('data:image/jpeg;base64,\(jpg.base64EncodedString())')"
                self.webView.evaluateJavaScript(js) { _, _ in hide() }
            }
            return
        }
        if inspoView == nil {
            let v = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
            v.autoresizingMask = [.width, .height]
            v.uiDelegate = self
            content.addSubview(v, positioned: .above, relativeTo: webView)
            inspoView = v
        }
        guard let v = inspoView else { return }
        v.frame = NSRect(x: 0, y: bottom, width: content.bounds.width, height: max(0, content.bounds.height - top - bottom))
        if url != lastInspoURL, let target = URL(string: url) {
            lastInspoURL = url
            v.load(URLRequest(url: target))
        }
        v.isHidden = false
    }

    // Keep the panel on its own interface; open everything else in the default browser.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { decisionHandler(.cancel); return }
        let web = ["http", "https"].contains(url.scheme?.lowercased() ?? "")
        if webView === self.webView, !(url.host == "127.0.0.1" && url.port == panelPort) {
            if web { NSWorkspace.shared.open(url) }
            decisionHandler(.cancel)
            return
        }
        decisionHandler(web || url.scheme == "about" || url.scheme == "blob" || url.scheme == "data" ? .allow : .cancel)
    }

    // target=_blank links and window.open: open in the default browser.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url, ["http", "https"].contains(url.scheme?.lowercased() ?? "") {
            NSWorkspace.shared.open(url)
        }
        return nil
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        webView.reload()
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        completionHandler(nil)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
