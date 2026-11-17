import SwiftUI
import UniformTypeIdentifiers

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
        if let icon = Bundle.module.image(forResource: "AppIcon")
            ?? Bundle.module.url(forResource: "AppIcon", withExtension: "icns").flatMap(NSImage.init(contentsOf:))
            ?? Bundle.module.url(forResource: "AppIcon", withExtension: "png").flatMap(NSImage.init(contentsOf:)) {
            NSApp.applicationIconImage = icon
        }
        NSApp.activate()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

struct MainView: View {
    @Environment(AppModel.self) private var app
    /// Row ids; one document opens, several wait for an action (right-click, drag, Delete).
    @State var selection: Set<String> = []
    @State var guide = false
    /// The full interface; the window opens on the simple one (one file in, its anonymized text out).
    @State var advanced = false
    @State private var importing = false
    enum Pane { case documents, graph, values }
    @State var pane = Pane.documents
    @State private var graph = GraphModel()
    /// The advanced interface takes folders too.
    private var importable: [UTType] { advanced ? [.item, .folder] : [.item] }

    var body: some View {
        Group {
            if advanced {
                NavigationSplitView {
                    Sidebar(selection: $selection, importing: $importing)
                        .navigationSplitViewColumnWidth(min: 240, ideal: 290)
                } detail: {
                    if guide {
                        Guide(importing: $importing) { guide = false }
                    } else if pane == .graph {
                        GraphView(model: graph) { id in
                            selection = [id]
                            pane = .documents
                        }
                    } else if pane == .values {
                        ValuesView { id in
                            selection = [id]
                            pane = .documents
                        }
                    } else if selection.count == 1, let id = selection.first {
                        DocumentView(model: DocumentModel(app: app, docId: id)).id(id)
                    } else if selection.count > 1 {
                        ContentUnavailableView(L("\(selection.count) documents selected"), systemImage: "doc.on.doc",
                                               description: Text(L("Right-click for actions, or drag them out to export what the agent sees.")))
                    } else {
                        ContentUnavailableView(L("No document selected"), systemImage: "doc.text.magnifyingglass",
                                               description: Text(L("Pick a document on the left, or drop files and folders there.")))
                    }
                }
            } else if guide {
                Guide(importing: $importing) { guide = false }
            } else {
                SimpleView(importing: $importing)
            }
        }
        .frame(minWidth: 960, minHeight: 600)
        .onChange(of: selection) { if !selection.isEmpty { pane = .documents } }
        .toolbar {
            Button { advanced.toggle() } label: {
                Label(advanced ? L("Simple View") : L("Advanced View"),
                      systemImage: advanced ? "rectangle.split.2x1" : "slider.horizontal.3")
            }
            .keyboardShortcut("a", modifiers: [.command, .shift])
            if advanced {
                Picker(L("View"), selection: $pane) {
                    Label(L("Documents"), systemImage: "doc.text").tag(Pane.documents)
                    Label(L("Graph"), systemImage: "point.3.connected.trianglepath.dotted").tag(Pane.graph)
                    Label(L("Masked Values"), systemImage: "eye.slash").tag(Pane.values)
                }
                .pickerStyle(.segmented)
                .help(L("Documents one by one, the graph of what links them, or every value masked and where."))
                Button { importing = true } label: { Label(L("Add Documents…"), systemImage: "plus") }
                    .keyboardShortcut("o")
                    .disabled(app.job != nil)
                Button { Task { await app.reindexAll() } } label: { Label(L("Re-index All"), systemImage: "arrow.clockwise") }
                    .help(L("Index every document again: applies what you chose not to mask everywhere."))
                    .keyboardShortcut("r", modifiers: [.command, .shift])
                    .disabled(app.job != nil || app.docs.isEmpty)
                if app.drive?.folders.isEmpty == false {  // folders are added in Settings
                    Button { Task { await app.syncDrive() } } label: {
                        Label(L("Sync Google Drive"), systemImage: "icloud.and.arrow.down")
                    }
                    .help(L("Download what changed in your Google Drive folders, then index it."))
                    .disabled(app.job != nil || app.drive?.connected != true)
                }
                Button { if let dir = chooseFolder() { Task { await app.export(dir) } } } label: {
                    Label(L("Export for Cowork…"), systemImage: "square.and.arrow.up")
                }
                .help(L("Write the documents the agent sees to a folder, for Claude Cowork to work on."))
                .disabled(app.job != nil || app.docs.isEmpty)
                Button { guide = true } label: { Label(L("Setup Guide"), systemImage: "questionmark.circle") }
                SettingsLink { Label(L("Settings…"), systemImage: "gearshape") }  // last: the far right
            } else {
                Button { importing = true } label: { Label(L("Open File…"), systemImage: "doc") }
                    .keyboardShortcut("o")
                    .disabled(app.job != nil)
            }
        }
        .fileImporter(isPresented: $importing, allowedContentTypes: importable,
                      allowsMultipleSelection: advanced) { result in
            if case .success(let urls) = result {
                Task { if advanced { await app.ingest(urls.map(\.path)) } else { await app.open(urls) } }
            }
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
        .sheet(isPresented: Binding(get: { app.coreMissing }, set: { _ in })) {
            CoreMissing().interactiveDismissDisabled()
        }
        .task(id: app.coreMissing) {  // again once the core is installed
            await app.refresh()
            guard !app.coreMissing else { return }
            guide = guide || app.needsSetup
            if !app.needsSetup { app.warm() }  // the reviewer loads its models while you look
            await app.driveCommand()
        }
    }
}

/// First launch of a downloaded app: the core is not installed. One command in Terminal does it.
struct CoreMissing: View {
    @Environment(AppModel.self) private var app
    // outside a release app (`swift run`): the repo's own setup
    private let command = CLI.installCommand() ?? "uv sync"
    @State private var installing = false
    @State private var failed: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(L("Install the core")).font(.title).bold()
            Text(L("mcp-secret needs its core, a command-line program that is missing or is not the version of this app. Installing it takes a few minutes and uses the network."))
                .fixedSize(horizontal: false, vertical: true)
            if let failed {
                Text(L("The installation failed. Its last lines:")).foregroundStyle(.red)
                Text(verbatim: failed)
                    .font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            HStack {
                if installing {
                    ProgressView().controlSize(.small)
                    Text(L("Installing… a few minutes"))
                }
                Spacer()
                Button(L("Install")) {
                    Task {
                        installing = true
                        failed = nil
                        let (ok, tail) = await CLI.install(command)
                        if ok { await app.refresh() } else { failed = tail }
                        installing = false
                    }
                }
                .keyboardShortcut(.defaultAction).disabled(installing)
            }
            DisclosureGroup(L("Or in Terminal")) {
                VStack(alignment: .leading, spacing: 10) {
                    Text(verbatim: command)
                        .font(.system(.callout, design: .monospaced)).textSelection(.enabled)
                        .padding(8).frame(maxWidth: .infinity, alignment: .leading)
                        .background(.quaternary, in: .rect(cornerRadius: 6))
                    HStack {
                        Button(L("Copy Command")) {
                            NSPasteboard.general.clearContents()
                            NSPasteboard.general.setString(command, forType: .string)
                        }
                        Button(L("Open Terminal")) {
                            NSWorkspace.shared.open(URL(filePath: "/System/Applications/Utilities/Terminal.app"))
                        }
                        Spacer()
                        Button(L("Check Again")) { Task { await app.refresh() } }
                    }
                }
                .padding(.top, 6)
            }
        }
        .padding(30)
        .frame(width: 560)
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
    @Binding var selection: Set<String>
    @Binding var importing: Bool
    @State private var targeted = false
    /// The documents getting a new category.
    @State private var naming: [String] = []
    /// The documents about to leave the index, once confirmed.
    @State private var removing: [String] = []
    @State private var newName = ""

    var body: some View {
        @Bindable var app = app
        VStack(spacing: 0) {
            if !app.categories.isEmpty {
                Picker(L("Category"), selection: $app.category) {
                    Text(L("All Documents")).tag(String?.none)
                    Divider()
                    ForEach(app.categories, id: \.self) { Text(verbatim: $0).tag(String?.some($0)) }
                }
                .padding([.horizontal, .top], 10)
            }
            List(selection: $selection) {
                ForEach(rows) { row in
                    if let id = row.docId {
                        RowView(row: row).draggable(Anonymized(cli: app.cli, docId: id))
                    } else {
                        RowView(row: row).selectionDisabled()  // a file that failed to index: nothing to act on
                    }
                }
            }
            .contextMenu(forSelectionType: String.self) { ids in
                let docs = app.docs(ids)  // the selection, or the one row right-clicked outside it
                if !docs.isEmpty { actions(docs) }
            }
            .onDeleteCommand { if app.job == nil { removing = app.docs(selection).compactMap(\.docId) } }
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
        .alert(L("New Category"), isPresented: Binding(get: { !naming.isEmpty }, set: { if !$0 { naming = [] } })) {
            TextField(L("Name"), text: $newName)
            Button(L("Add")) {
                if !newName.trimmingCharacters(in: .whitespaces).isEmpty {
                    let (ids, name) = (naming, newName)
                    Task { await app.setCategory(ids, name, add: true) }
                }
                newName = ""
            }
            Button(L("Cancel"), role: .cancel) { newName = "" }
        }
        .confirmationDialog(L("Remove \(removing.count) documents from the index?"),
                            isPresented: Binding(get: { !removing.isEmpty }, set: { if !$0 { removing = [] } }),
                            titleVisibility: .visible) {
            Button(L("Remove"), role: .destructive) {
                let ids = removing
                selection.subtract(ids)
                Task { await app.forget(ids) }
            }
        } message: {
            Text(L("The files stay where they are. Adding them again indexes them anew."))
        }
    }

    /// What the selected documents can have done to them.
    @ViewBuilder func actions(_ docs: [Doc]) -> some View {
        let ids = docs.compactMap(\.docId)
        Group {
            categoryMenu(docs)
            Button(L("Re-index")) { Task { await app.reindex(docs) } }
            Button(L("Export Anonymized…")) {
                if let dir = chooseFolder() { Task { await app.export(ids, to: dir) } }
            }
            Divider()
            Button(L("Remove from Index…"), role: .destructive) { removing = ids }
        }
        .disabled(app.job != nil)
    }

    /// The rows of the chosen category.
    var rows: [AppModel.Row] { app.rows.filter { row in app.category.map(row.categories.contains) ?? true } }

    /// Every category as a checkbox, checked when all of these documents have it, and a new one.
    func categoryMenu(_ docs: [Doc]) -> some View {
        let ids = docs.compactMap(\.docId)
        return Menu(L("Categories")) {
            ForEach(app.categories, id: \.self) { c in
                Toggle(c, isOn: Binding(get: { docs.allSatisfy { $0.categories?.contains(c) ?? false } },
                                        set: { on in Task { await app.setCategory(ids, c, add: on) } }))
            }
            if !app.categories.isEmpty { Divider() }
            Button(L("New Category…")) { naming = ids }
        }
    }
}

// TEMPORARY: remove once the drag works
func dragTrace(_ line: String) {
    let url = URL(filePath: "/tmp/mcp-secret-drag.log")
    let data = Data("\(Date().timeIntervalSince1970) \(line)\n".utf8)
    if let h = try? FileHandle(forWritingTo: url) { h.seekToEndOfFile(); h.write(data); try? h.close() }
    else { try? data.write(to: url) }
}

/// A document dragged out of the list, written when it is dropped by `export --doc`: the original
/// with tokens for values (`<doc_id>.pdf`, `.docx`…) or what the agent sees (`<doc_id>.md`).
/// ponytail: declared Markdown, the type is known only once the core wrote the file; Finder copies
/// the file URL, name and format as written. A generic type (.data) turns it into a file promise
/// suggesting the name "data".
struct Anonymized: Transferable {
    var cli: CLI
    var docId: String

    static var transferRepresentation: some TransferRepresentation {
        FileRepresentation(exportedContentType: UTType(filenameExtension: "md") ?? .plainText) { item in
            // ponytail: one folder per drag in $TMPDIR, left to the system to clear
            dragTrace("export start \(item.docId) cancelled=\(Task.isCancelled)")
            do {
                let folder = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
                try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
                let file = try await item.cli.export(item.docId, to: folder)
                dragTrace("export done \(file.path) cancelled=\(Task.isCancelled)")
                return SentTransferredFile(file)
            } catch {
                dragTrace("export failed \(error)")
                throw error
            }
        }
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
                Text(([state] + row.categories).joined(separator: " · ")).font(.caption).foregroundStyle(.secondary)
                    .lineLimit(1)
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

/// One file in, what the agent sees of it out: the window opens on this. The file is indexed like
/// any other, so the agent finds it; DocumentView has both previews and the drag-out.
struct SimpleView: View {
    @Environment(AppModel.self) private var app
    @Binding var importing: Bool
    @State private var targeted = false

    var body: some View {
        Group {
            if let id = app.current {
                DocumentView(model: DocumentModel(app: app, docId: id)).id(id)
            } else {
                DropZone(targeted: targeted, title: L("Drop a file here")) { importing = true }
                    .frame(maxWidth: 420).padding(40).disabled(app.job != nil)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .safeAreaInset(edge: .bottom, spacing: 0) { if let job = app.job { JobView(job: job) } }
        .dropDestination(for: URL.self) { urls, _ in
            guard app.job == nil else { return false }
            Task { await app.open(urls) }
            return true
        } isTargeted: { targeted = $0 }
    }
}

struct DropZone: View {
    var targeted: Bool
    var title = L("Drop files and folders here")
    var choose: () -> Void

    var body: some View {
        VStack(spacing: 6) {
            Image(systemName: "tray.and.arrow.down").font(.title2).accessibilityHidden(true)
            Text(title).font(.callout)
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
        .accessibilityLabel(title)
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
                     L("Drop your documents in the window, or choose them. They are read here, on this Mac."),
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
