import Foundation
import Observation

/// Localized from the package's String Catalog (SwiftUI's default bundle, Bundle.main, has none).
func L(_ key: String.LocalizationValue) -> String { String(localized: key, bundle: .module) }

/// The core as the app sees it: index status, documents, the running job. Everything here is in
/// memory only: nothing is saved, logged or restored.
@MainActor @Observable
final class AppModel {
    // ponytail: settings last for the session (launch arguments `-cliPath`, `-dataRoot` preset
    // them); persist them when someone needs non-default paths every day
    var cliPath = UserDefaults.standard.string(forKey: "cliPath") ?? CLI.startPath
    var dataRoot = UserDefaults.standard.string(forKey: "dataRoot") ?? ""
    /// No core at `cliPath`, or one of another version than the app ships (a downloaded app on its
    /// first launch, or after an update): the install sheet.
    var coreMissing = false
    /// The version of the core this app ships; nil when it ships none (`swift run`): any core will do.
    var shippedVersion = CLI.shippedWheel()?.version
    var status: Status?
    var docs: [Doc] = []
    /// Each path's last `ingest` event this session.
    var outcome: [String: Doc] = [:]
    var desktop: Desktop?
    var drive: Drive?
    var rules: [Rule] = []
    /// The sidebar shows only this category's documents; nil: all.
    var category: String?
    /// The simple window's document: the file dropped or chosen last.
    var current: String?
    /// The last document an ingest reported, whatever its status (removals excepted).
    private var latest: Doc?
    var restartClaude = false
    /// The folder of the last export this session: every ingest refreshes it.
    var exportDir: String?
    var notice: String?
    /// Values checked or unchecked in the list of values, not applied yet.
    var decisions: [String: Decision] = [:]
    var job: Job?
    var problem: String?
    private var task: Task<Void, Never>?
    private var reviewer: Reviewer?
    /// Reviews already read this session, in memory; every refresh (after any ingest) drops them.
    private var reviews: [String: Review] = [:]
    private var generation = 0

    struct Job: Equatable {
        var title: String
        var done = 0
        var total = 0
        var current = ""
    }

    enum State { case indexed, unchanged, error, missing, changed }

    /// Mask a value everywhere (`deny`), or leave it in clear (`allow`) in these documents.
    struct Decision: Equatable {
        var mask: Bool
        var docIds: [String]
    }

    struct Row: Identifiable, Equatable {
        var docId: String?
        var path: String
        var chunks: Int
        var state: State
        var categories: [String] = []
        var id: String { docId ?? path }
        var name: String { URL(filePath: path).lastPathComponent }
    }

    init(cliPath: String? = nil, dataRoot: String? = nil) {
        if let cliPath { self.cliPath = cliPath }
        if let dataRoot { self.dataRoot = dataRoot }
    }

    var cli: CLI {
        CLI(executable: URL(filePath: cliPath),
            environment: dataRoot.isEmpty ? [:] : ["MCP_SECRET_HOME": dataRoot])
    }

    /// Indexed documents, then what the running or last ingest reported about other files:
    /// documents show up while they index, and failed files get a row too.
    var rows: [Row] {
        let known = Set(docs.map(\.path))
        func state(_ status: String?) -> State {
            status == "error" ? .error : status == "unchanged" ? .unchanged : .indexed
        }
        let news = outcome.values.filter { !known.contains($0.path) && $0.status != "removed" }
            .sorted { $0.path < $1.path }
            .map { Row(docId: $0.docId, path: $0.path, chunks: $0.chunks ?? 0, state: state($0.status)) }
        return docs.map { d in
            Row(docId: d.docId, path: d.path, chunks: d.chunks ?? 0,
                state: d.exists == false ? .missing : d.stale == true ? .changed : state(outcome[d.path]?.status),
                categories: d.categories ?? [])
        } + news
    }

    /// Every category of the indexed documents.
    var categories: [String] {
        Set(docs.flatMap { $0.categories ?? [] }).sorted { $0.localizedStandardCompare($1) == .orderedAscending }
    }

    var needsSetup: Bool { !(status?.models ?? false) || !(status?.index ?? false) }

    func refresh() async {
        coreMissing = !FileManager.default.isExecutableFile(atPath: cliPath)
        if coreMissing { return }
        reviews = [:]
        generation += 1
        do {
            status = try await cli.events("status").compactMap(\.status).first
            if let shipped = shippedVersion, status?.version != shipped {
                coreMissing = true
                return
            }
            docs = try await cli.events("list").compactMap(\.doc)
            if let c = category, !categories.contains(c) { category = nil }
            if let c = current, !docs.contains(where: { $0.docId == c }) { current = nil }  // forgotten
        } catch {
            report(error)
        }
    }

    /// The indexed documents among `ids` (row ids): a file that failed to index has none.
    func docs(_ ids: Set<String>) -> [Doc] { docs.filter { $0.docId.map(ids.contains) ?? false } }

    /// `cat add|remove`: a category given to, or taken away from, documents by hand.
    func setCategory(_ docIds: [String], _ name: String, add: Bool) async {
        guard !docIds.isEmpty else { return }
        do {
            _ = try await cli.events(["cat", add ? "add" : "remove", "--"] + docIds + [name])
        } catch {
            return report(error)
        }
        await refresh()
    }

    /// `forget`: documents out of the index; their files stay where they are.
    func forget(_ docIds: [String]) async {
        guard !docIds.isEmpty else { return }
        do {
            _ = try await cli.events(["forget", "--"] + docIds)
        } catch {
            return report(error)
        }
        let gone = Set(docIds)
        for d in docs where d.docId.map(gone.contains) ?? false { outcome[d.path] = nil }  // else: a row again
        await refresh()
        if let dir = exportDir { await export(dir, announce: false) }  // a forgotten document must leave it too
    }

    func loadRules() async {
        do {
            rules = try await cli.events("rules").compactMap(\.rule)
        } catch {
            report(error)
        }
    }

    /// `rules --set`: the rule's terms, none to remove it. Categories change with it: refresh.
    func setRule(_ name: String, terms: [String]) async {
        do {
            rules = try await cli.events(["rules", "--set=\(name)", "--"] + terms).compactMap(\.rule)
        } catch {
            return report(error)
        }
        await refresh()
    }

    func ingest(_ paths: [String], force: Bool = false) async {
        guard !paths.isEmpty else { return }
        await track(L("Indexing"), ["ingest"] + (force ? ["--force"] : []) + ["--"] + paths)
    }

    /// The simple window: one file, indexed like any other, then shown. A folder or several files
    /// are for the advanced one.
    func open(_ urls: [URL]) async {
        guard urls.count == 1, (try? urls[0].resourceValues(forKeys: [.isDirectoryKey]).isDirectory) != true else {
            return problem = L("Drop one file at a time.")
        }
        latest = nil
        await ingest([urls[0].path])
        if let id = latest?.docId {
            current = id
        } else if latest?.status == "error" {
            problem = L("Could not be indexed")
        }
    }

    /// `drive sync`: the folders' files downloaded (`file` events), then ingested, one stream.
    func syncDrive() async { await track(L("Syncing Google Drive"), ["drive", "sync"]) }

    /// A command that indexes, live: the job counts files, `outcome` keeps each one's result.
    private func track(_ title: String, _ args: [String]) async {
        await perform(title) { [self] in
            for try await event in cli.run(args) {
                switch event {
                case .start(let files, _):  // drive sync: its own, then ingest's
                    job?.total = files ?? 0
                    job?.done = 0
                case .file(let path):
                    job?.current = URL(filePath: path).lastPathComponent
                    job?.done += 1
                case .doc(let d):
                    outcome[d.path] = d
                    if d.status != "removed" { latest = d }
                    job?.current = URL(filePath: d.path).lastPathComponent
                    if d.status != "removed" { job?.done += 1 }
                default: break
                }
            }
        }
        await refresh()
        if let dir = exportDir { await export(dir, announce: false) }  // a masked value must leave it too
    }

    /// `export`: the documents the agent sees, as files in `dir` for a Cowork session.
    func export(_ dir: String, announce: Bool = true) async {
        do {
            guard let done = try await cli.events("export", "--", dir).compactMap(\.exported).first else { return }
            exportDir = dir
            if announce {
                notice = L("\(done.docs) documents written to \(done.dir). In Claude Cowork, connect this folder: the documents are in pseudonymized/.")
            }
        } catch {
            report(error)
        }
    }

    /// `export --doc` of some documents, in one core call (the detector loads once): each in
    /// `dir` in its own format where it can be, else `<doc_id>.md`. Unlike the Cowork folder it
    /// keeps nothing: no README, nothing deleted, never refreshed.
    func export(_ docIds: [String], to dir: String) async {
        await perform(L("Exporting")) { [self] in
            _ = try await cli.events(["export"] + docIds.flatMap { ["--doc", $0] } + ["--", dir])
        }
    }

    /// Every indexed file again, `--force`: applies `allow` changes everywhere.
    func reindexAll() async { await reindex(docs) }

    /// These documents again, `--force`: those whose file is still there.
    func reindex(_ docs: [Doc]) async {
        // ponytail: one argv; past ~10^5 paths (ARG_MAX), split the list
        await ingest(docs.filter { $0.exists != false }.map(\.path), force: true)
    }

    func downloadModels() async {
        await perform(L("Downloading models")) { [self] in
            for try await event in cli.run(["models"]) {
                switch event {
                case .start(_, let steps): job?.total = steps ?? 0
                case .step(let n, let name):
                    job?.done = n - 1
                    job?.current = name == "docling" ? L("Document layout models") : L("Personal data detector")
                case .done: job?.done = job?.total ?? 0
                default: break
                }
            }
        }
        await refresh()
    }

    /// `drive login`, waiting for consent in the browser (Cancel stops it). The first time,
    /// `client` is the OAuth client file of a Google Cloud project.
    func connectDrive(client: String?) async {
        await perform(L("Connecting Google Drive")) { [self] in
            job?.total = 1
            job?.current = L("Allow access in your browser")
            drive = try await cli.events(["drive", "login"] + (client.map { ["--", $0] } ?? []))
                .compactMap(\.drive).first
        }
    }

    /// `drive add|remove`, then a sync: the folder's documents come, or go.
    func changeDrive(_ action: String, _ folder: String) async {
        if await driveCommand(action, "--", folder) { await syncDrive() }
    }

    /// `drive [logout]`: the account and the folders.
    @discardableResult
    func driveCommand(_ args: String...) async -> Bool {
        do {
            drive = try await cli.events(["drive"] + args).compactMap(\.drive).first
            return true
        } catch {
            report(error)
            return false
        }
    }

    /// `desktop-config`, or with `--install` / `--uninstall`.
    func desktopConfig(_ flag: String? = nil) async {
        do {
            desktop = try await cli.events(["desktop-config"] + [flag].compactMap { $0 }).compactMap(\.desktop).first
            if flag != nil { restartClaude = true }
        } catch {
            report(error)
        }
    }

    /// `deny add` (always mask) or `allow add` (never mask) a value, then the ingest that
    /// applies it to these documents: a plain one for deny (every run sweeps the deny list
    /// through what is published), `--force` for allow (the files did not change). With `doc`,
    /// the decision is for that document alone, and nothing sweeps it: `--force` either way.
    func decide(_ list: String, _ value: String, paths: [String], doc: String? = nil) async {
        do {
            _ = try await cli.events([list, "add"] + (doc.map { ["--doc", $0] } ?? []) + ["--", value])
        } catch {
            return report(error)
        }
        await ingest(paths, force: list == "allow" || doc != nil)
    }

    /// The decisions, applied: `deny add` or `allow add` each, then one ingest of all their
    /// documents, `--force` if a value is to be in clear. A failed write stops there: the ones
    /// written are applied, the rest stay. A missing file keeps what it published.
    func applyDecisions() async {
        var done: [Decision] = []
        for (value, d) in decisions.sorted(by: { $0.key < $1.key }) {
            do {
                _ = try await cli.events(d.mask ? "deny" : "allow", "add", "--", value)
            } catch {
                report(error)
                break
            }
            decisions[value] = nil
            done.append(d)
        }
        let ids = Set(done.flatMap(\.docIds))
        await ingest(docs(ids).filter { $0.exists != false }.map(\.path), force: done.contains { !$0.mask })
    }

    /// `review` through one long-lived core process: the models load once, not on every click.
    /// Reopening a document is instant: its review is kept until the next refresh.
    func review(_ docId: String) async throws -> Review {
        if let cached = reviews[docId] { return cached }
        let asked = generation, review = try await reviewing().review(docId)
        if asked == generation { reviews[docId] = review }  // not one read before an ingest ended
        return review
    }

    /// Starts that process before the first click.
    func warm() { reviewing().warm() }

    private func reviewing() -> Reviewer {
        if let reviewer, reviewer.cli == cli { return reviewer }
        let fresh = Reviewer(cli: cli)  // the old one, if any, quits with its stdin
        reviewer = fresh
        return fresh
    }

    /// One job at a time; `cancel()` stops it (the core gets SIGTERM and rolls back).
    private func perform(_ title: String, _ work: @escaping @MainActor () async throws -> Void) async {
        guard job == nil else { return }
        job = Job(title: title)
        let task = Task {
            do { try await work() } catch is CancellationError {} catch { report(error) }
        }
        self.task = task
        await task.value
        job = nil
        self.task = nil
    }

    func cancel() { task?.cancel() }

    func report(_ error: Error) {
        if coreMissing { return }  // the install sheet says it
        if let e = error as? CLIError {
            problem = e.text
        } else {
            problem = L("The core did not answer: check the CLI path in Settings. \(error.localizedDescription)")
        }
    }
}

/// One document: the original with what ingest masks in it (`review`), next to what the
/// agent sees (`show`).
@MainActor @Observable
final class DocumentModel {
    let app: AppModel
    let docId: String
    var review: Review?
    var chunks: [Chunk] = []
    var loading = false
    /// The selected span, an index in `review.spans`.
    var selected: Int?
    /// Original text selected outside any span: the candidate for "Always mask".
    var selection = ""

    init(app: AppModel, docId: String) {
        self.app = app
        self.docId = docId
    }

    var selectedSpan: Span? { selected.flatMap { review?.spans[$0] } }

    /// Which occurrence of its token the selected span is, counting from the top.
    var occurrence: Int {
        guard let i = selected, let spans = review?.spans else { return 0 }
        return spans[..<i].filter { $0.token == spans[i].token }.count
    }

    /// What the agent sees comes back at once; the original once the reviewer has read it.
    func load() async {
        loading = true
        defer { loading = false }
        let app = app, id = docId
        async let original = app.review(id)
        do {
            chunks = try await app.cli.events("show", id).compactMap(\.chunk)
        } catch let e as CLIError where e.error == "no_document" {
            chunks = []  // an empty file publishes no chunk
        } catch {
            app.report(error)
        }
        do {
            review = try await original
        } catch {
            app.report(error)
        }
        if let i = selected, i >= review?.spans.count ?? 0 { selected = nil }
    }

    /// Next (+1) or previous (-1) span, wrapping around.
    func step(_ delta: Int) {
        guard let n = review?.spans.count, n > 0 else { return }
        selected = ((selected ?? (delta > 0 ? -1 : n)) + delta + n) % n
        selection = ""
    }

    /// The selected text, masked in every document, or with `here` in this one only.
    func maskAlways(here: Bool = false) async {
        let value = selection.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty, let path = review?.path else { return }
        await app.decide("deny", value, paths: [path], doc: here ? docId : nil)
        selection = ""
        await load()
    }

    /// The selected span's value, in clear in every document, or with `here` in this one only.
    func dontMask(here: Bool = false) async {
        guard let span = selectedSpan, let review else { return }
        await app.decide("allow", review.value(span), paths: [review.path], doc: here ? docId : nil)
        selected = nil
        await load()
    }
}

extension Review {
    /// The span's text: offsets count Unicode code points.
    func value(_ span: Span) -> String {
        let u = text.unicodeScalars
        let a = u.index(u.startIndex, offsetBy: span.start)
        return String(u[a..<u.index(a, offsetBy: span.end - span.start)])
    }
}

extension Event {
    var status: Status? { if case .status(let s) = self { s } else { nil } }
    var doc: Doc? { if case .doc(let d) = self { d } else { nil } }
    var chunk: Chunk? { if case .chunk(let c) = self { c } else { nil } }
    var review: Review? { if case .review(let r) = self { r } else { nil } }
    var desktop: Desktop? { if case .desktop(let d) = self { d } else { nil } }
    var rule: Rule? { if case .rule(let r) = self { r } else { nil } }
    var token: Token? { if case .token(let t) = self { t } else { nil } }
    var value: Value? { if case .value(let v) = self { v } else { nil } }
    var exported: Exported? { if case .exported(let e) = self { e } else { nil } }
    var drive: Drive? { if case .drive(let d) = self { d } else { nil } }
}

extension CLIError {
    /// What to tell the user, by error code (docs/cli-json.md).
    var text: String {
        switch error {
        case "no_index": L("Nothing is indexed yet: drop some documents first.")
        case "no_document": L("This document is no longer in the index.")
        case "bad_value": L("The selected text is empty.")
        case "bad_dir": L("Choose a folder outside mcp-secret's data folder.")
        case "bad_config": L("Claude Desktop's configuration file is not valid: fix or delete it, then try again.")
        case "no_client": L("Choose the OAuth client file (Desktop app) of your Google Cloud project.")
        case "not_connected": L("Google Drive is not connected, or Google refused the login: connect again.")
        case "no_folder": L("That is not a Google Drive folder you can open.")
        default: L("The core failed (\(message)).")
        }
    }
}
