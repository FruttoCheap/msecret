import SwiftUI

@main
struct MCPSecretApp: App {
    @NSApplicationDelegateAdaptor(Delegate.self) private var delegate
    @State private var app = AppModel()

    var body: some Scene {
        Window("mcp-secret", id: "main") {
            MainView().environment(app)
        }
        .restorationBehavior(.disabled)  // no saved window state: what it shows stays in memory
        Settings {
            SettingsView().environment(app)
        }
        .restorationBehavior(.disabled)
    }
}

/// `swift run` starts a bare executable: make it a regular app, in front.
@MainActor final class Delegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

struct MainView: View {
    @Environment(AppModel.self) private var app
    @State var selection: String?
    @State var guide = false
    @State private var importing = false

    var body: some View {
        NavigationSplitView {
            Sidebar(selection: $selection, importing: $importing)
                .navigationSplitViewColumnWidth(min: 240, ideal: 290)
        } detail: {
            if guide {
                Guide(importing: $importing) { guide = false }
            } else if let id = selection {
                DocumentView(model: DocumentModel(app: app, docId: id)).id(id)
            } else {
                ContentUnavailableView(L("No document selected"), systemImage: "doc.text.magnifyingglass",
                                       description: Text(L("Pick a document on the left, or drop files and folders there.")))
            }
        }
        .frame(minWidth: 960, minHeight: 600)
        .toolbar {
            Button { importing = true } label: { Label(L("Add Documents…"), systemImage: "plus") }
                .keyboardShortcut("o")
                .disabled(app.job != nil)
            Button { Task { await app.reindexAll() } } label: { Label(L("Re-index All"), systemImage: "arrow.clockwise") }
                .help(L("Index every document again: applies what you chose not to mask everywhere."))
                .keyboardShortcut("r", modifiers: [.command, .shift])
                .disabled(app.job != nil || app.docs.isEmpty)
            Button { if let dir = chooseFolder() { Task { await app.export(dir) } } } label: {
                Label(L("Export for Cowork…"), systemImage: "square.and.arrow.up")
            }
            .help(L("Write the documents the agent sees to a folder, for Claude Cowork to work on."))
            .disabled(app.job != nil || app.docs.isEmpty)
            Button { guide = true } label: { Label(L("Setup Guide"), systemImage: "questionmark.circle") }
        }
        .fileImporter(isPresented: $importing, allowedContentTypes: [.item, .folder],
                      allowsMultipleSelection: true) { result in
            if case .success(let urls) = result { Task { await app.ingest(urls.map(\.path)) } }
        }
        .alert(L("Exported"), isPresented: Binding(get: { app.notice != nil },
                                                  set: { if !$0 { app.notice = nil } })) {
        } message: {
            Text(app.notice ?? "")
        }
        .alert(L("Something went wrong"), isPresented: Binding(get: { app.problem != nil },
                                                             set: { if !$0 { app.problem = nil } })) {
        } message: {
            Text(app.problem ?? "")
        }
        .task {
            await app.refresh()
            guide = guide || app.needsSetup
            if !app.needsSetup { app.warm() }  // the reviewer loads its models while you look
        }
    }
}

/// A folder picked, or made, in the system's open panel.
@MainActor func chooseFolder() -> String? {
    let panel = NSOpenPanel()
    panel.canChooseFiles = false
    panel.canChooseDirectories = true
    panel.canCreateDirectories = true
    panel.prompt = L("Export")
    return panel.runModal() == .OK ? panel.url?.path : nil
}

struct Sidebar: View {
    @Environment(AppModel.self) private var app
    @Binding var selection: String?
    @Binding var importing: Bool
    @State private var targeted = false

    var body: some View {
        VStack(spacing: 0) {
            List(selection: $selection) {
                ForEach(app.rows) { row in
                    RowView(row: row).tag(row.docId)
                }
            }
            .overlay {
                if app.rows.isEmpty { Text(L("No documents yet")).foregroundStyle(.secondary) }
            }
            if let job = app.job { JobView(job: job) }
            DropZone(targeted: targeted) { importing = true }
                .padding(10)
                .disabled(app.job != nil)
        }
        .dropDestination(for: URL.self) { urls, _ in
            guard app.job == nil, !urls.isEmpty else { return false }
            Task { await app.ingest(urls.map(\.path)) }
            return true
        } isTargeted: { targeted = $0 }
    }
}

struct RowView: View {
    let row: AppModel.Row

    var body: some View {
        let (icon, tint, state): (String, Color, String) = switch row.state {
        case .indexed: ("checkmark.circle", .green, L("Indexed, \(row.chunks) chunks"))
        case .unchanged: ("equal.circle", .secondary, L("Unchanged, \(row.chunks) chunks"))
        case .error: ("exclamationmark.triangle", .red, L("Could not be indexed"))
        case .missing: ("questionmark.folder", .orange, L("File missing"))
        case .changed: ("arrow.triangle.2.circlepath", .orange, L("Changed since indexing"))
        }
        HStack {
            Image(systemName: icon).foregroundStyle(tint).accessibilityHidden(true)
            VStack(alignment: .leading) {
                Text(row.name).lineLimit(1).truncationMode(.middle)
                Text(state).font(.caption).foregroundStyle(.secondary)
            }
        }
        .help(row.path)
        .accessibilityElement(children: .combine)
    }
}

struct JobView: View {
    @Environment(AppModel.self) private var app
    let job: AppModel.Job

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(L("\(job.title): \(job.done) of \(job.total)")).font(.callout)
                Spacer()
                Button(L("Cancel")) { app.cancel() }.controlSize(.small)
            }
            ProgressView(value: Double(job.done), total: Double(max(job.total, 1)))
                .accessibilityLabel(job.title)
            Text(job.current).font(.caption).foregroundStyle(.secondary)
                .lineLimit(1).truncationMode(.middle)
        }
        .padding(10)
        .background(.bar)
    }
}

struct DropZone: View {
    var targeted: Bool
    var choose: () -> Void

    var body: some View {
        VStack(spacing: 6) {
            Image(systemName: "tray.and.arrow.down").font(.title2).accessibilityHidden(true)
            Text(L("Drop files and folders here")).font(.callout)
            Button(L("Choose…"), action: choose).controlSize(.small)
        }
        .frame(maxWidth: .infinity, minHeight: 96)
        .foregroundStyle(targeted ? Color.accentColor : .secondary)
        .background {
            RoundedRectangle(cornerRadius: 10)
                .strokeBorder(style: StrokeStyle(lineWidth: targeted ? 2 : 1, dash: [5]))
                .foregroundStyle(targeted ? Color.accentColor : .secondary)
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(L("Drop files and folders here"))
    }
}

/// First launch: models, documents, Claude Desktop.
struct Guide: View {
    @Environment(AppModel.self) private var app
    @Binding var importing: Bool
    var close: () -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                Text(L("Welcome to mcp-secret")).font(.largeTitle).bold()
                Text(L("Three steps, and Claude can search your documents without ever seeing names, tax codes, IBANs or contacts."))
                    .foregroundStyle(.secondary)
                step(1, L("Download the models"),
                     L("Once, about 2 GB. It is the only step that uses the network."),
                     done: app.status?.models == true) {
                    Button(L("Download")) { Task { await app.downloadModels() } }.disabled(app.job != nil)
                }
                step(2, L("Add your documents"),
                     L("Drop files and folders on the list on the left, or choose them. They are read here, on this Mac."),
                     done: (app.status?.docs ?? 0) > 0) {
                    Button(L("Choose…")) { importing = true }
                        .disabled(app.job != nil || app.status?.models != true)
                }
                step(3, L("Connect Claude Desktop"),
                     L("Adds mcp-secret to Claude Desktop's configuration. Restart Claude afterwards."),
                     done: app.desktop?.configured == true) {
                    Button(L("Connect")) { Task { await app.desktopConfig("--install") } }
                }
                if app.restartClaude {
                    Label(L("Restart Claude Desktop to apply the change."), systemImage: "arrow.clockwise.circle")
                }
                Button(L("Done"), action: close).keyboardShortcut(.defaultAction)
            }
            .padding(40)
            .frame(maxWidth: 640, alignment: .leading)
        }
        .task { await app.desktopConfig() }
    }

    func step(_ n: Int, _ title: String, _ detail: String, done: Bool,
              @ViewBuilder action: () -> some View) -> some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: done ? "checkmark.circle.fill" : "\(n).circle")
                .font(.title).foregroundStyle(done ? .green : .secondary)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 6) {
                Text(title).font(.title3).bold()
                Text(detail).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                if done { Text(L("Completed")).foregroundStyle(.green) } else { action() }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(done ? L("Step \(n), done: \(title)") : L("Step \(n): \(title)"))
    }
}
