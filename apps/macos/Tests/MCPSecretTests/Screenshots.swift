import AppKit
import SwiftUI
import Testing
@testable import MCPSecret

/// `SCREENSHOTS=<dir> swift test --filter screenshots`: the main screens in light and dark, on
/// generated documents and the real core. The windows flash on screen while it runs. English:
/// the test runner's main bundle declares no Italian.
@MainActor @Test(.enabled(if: ProcessInfo.processInfo.environment["SCREENSHOTS"] != nil))
func screenshots() async throws {
    let out = URL(filePath: ProcessInfo.processInfo.environment["SCREENSHOTS"]!)
    let docs = try fixtures()
    let root = docs.deletingLastPathComponent()
    let empty = AppModel(cliPath: CLI.defaultPath, dataRoot: root.appending(path: "empty").path)
    let app = AppModel(cliPath: CLI.defaultPath, dataRoot: root.appending(path: "data").path)
    await app.ingest([docs.path])
    let id = try #require(app.rows.first?.docId)
    _ = NSApplication.shared
    for dark in [false, true] {
        let mode = dark ? "dark" : "light"
        // first run: the guide, and the whole fixtures folder indexing in the sidebar
        let busy = AppModel(cliPath: CLI.defaultPath, dataRoot: root.appending(path: "busy-\(mode)").path)
        let indexing = Task { await busy.ingest([repo.appending(path: "fixtures/out").path]) }
        await snap(MainView().environment(busy), size: NSSize(width: 1180, height: 760), dark: dark,
                   to: out.appending(path: "progress-\(mode).png"))
        busy.cancel()
        await indexing.value
        await snap(MainView().environment(empty), size: NSSize(width: 1180, height: 760), dark: dark,
                   to: out.appending(path: "guide-\(mode).png"))
        await snap(MainView(selection: id).environment(app), size: NSSize(width: 1380, height: 820), dark: dark,
                   to: out.appending(path: "document-\(mode).png")) { window in
            // click into the third masked value of the original, as a user would
            let original = try #require(window.contentView.flatMap { textViews(in: $0).first })
            let tag = (original.string as NSString).range(of: "[PERSONA_")
            original.setSelectedRange(NSRange(location: tag.location - 3, length: 0))
        }
        await snap(SettingsView().environment(app), size: NSSize(width: 540, height: 620), dark: dark,
                   to: out.appending(path: "settings-\(mode).png"))
    }
}

@MainActor func textViews(in view: NSView) -> [NSTextView] {
    ((view as? NSTextView).map { [$0] } ?? []) + view.subviews.flatMap(textViews)
}

/// Shows `view` in a window, lets it load (the core answers in a few seconds), optionally acts
/// on it, then draws the whole window, title bar and toolbar included, to a PNG.
@MainActor
func snap(_ view: some View, size: NSSize, dark: Bool, to url: URL,
          act: ((NSWindow) throws -> Void)? = nil) async {
    let window = NSWindow(contentRect: NSRect(origin: NSPoint(x: 80, y: 80), size: size),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
    window.isReleasedWhenClosed = false  // ARC owns it
    window.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
    window.title = "mcp-secret"
    window.contentView = NSHostingView(rootView: view)
    window.orderFrontRegardless()
    await settle(seconds: 12)
    if let act {
        try? act(window)
        await settle(seconds: 2)
    }
    // the window server's own image (vibrancy included) when allowed, else AppKit's drawing
    let rep: NSBitmapImageRep
    if let image = windowImage(window.windowNumber) {
        rep = NSBitmapImageRep(cgImage: image)
    } else {
        let frame = window.contentView!.superview!
        rep = frame.bitmapImageRepForCachingDisplay(in: frame.bounds)!
        frame.cacheDisplay(in: frame.bounds, to: rep)
    }
    try? rep.representation(using: .png, properties: [:])?.write(to: url)
    window.close()
}

/// Runs the main run loop (SwiftUI updates, main-actor work) for a while.
@MainActor
func settle(seconds: Double) async {
    let end = Date().addingTimeInterval(seconds)
    while Date() < end {
        spin()
        await Task.yield()
    }
}

@MainActor func spin() { RunLoop.main.run(until: Date().addingTimeInterval(0.05)) }

/// CGWindowListCreateImage is gone from the SDK, not from CoreGraphics; a process may capture
/// its own windows without the screen recording permission.
func windowImage(_ id: Int) -> CGImage? {
    typealias Capture = @convention(c) (CGRect, UInt32, UInt32, UInt32) -> Unmanaged<CGImage>?
    guard let f = dlsym(UnsafeMutableRawPointer(bitPattern: -2), "CGWindowListCreateImage") else { return nil }
    // .null rect: the window's bounds; 8 = optionIncludingWindow; 1 = boundsIgnoreFraming
    return unsafeBitCast(f, to: Capture.self)(.null, 8, UInt32(id), 1)?.takeRetainedValue()
}
