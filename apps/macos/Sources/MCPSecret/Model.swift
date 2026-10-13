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
    var cliPath = UserDefaults.standard.string(forKey: "cliPath") ?? CLI.defaultPath
    var dataRoot = UserDefaults.standard.string(forKey: "dataRoot") ?? ""
    var status: Status?
    var docs: [Doc] = []
    /// Each path's last `ingest` event this session.
    var outcome: [String: Doc] = [:]
    var desktop: Desktop?
    var restartClaude = false
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

    struct Row: Identifiable, Equatable {
        var docId: String?
        var path: String
        var chunks: Int
        var state: State
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
                state: d.exists == false ? .missing : d.stale == true ? .changed : state(outcome[d.path]?.status))
        } + news
    }

    var needsSetup: Bool { !(status?.models ?? false) || !(status?.index ?? false) }

    func refresh() async {
        reviews = [:]
        generation += 1
        do {
            status = try await cli.events("status").compactMap(\.status).first
            docs = try await cli.events("list").compactMap(\.doc)
        } catch {
            report(error)
        }
    }

    /// `ingest`, live: the job counts files, `outcome` keeps each one's result.
    func ingest(_ paths: [String], force: Bool = false) async {
        guard !paths.isEmpty else { return }
        await perform(L("Indexing")) { [self] in
            for try await event in cli.run(["ingest"] + (force ? ["--force"] : []) + ["--"] + paths) {
                switch event {
                case .start(let files, _): job?.total = files ?? 0
                case .doc(let d):
                    outcome[d.path] = d
                    job?.current = URL(filePath: d.path).lastPathComponent
                    if d.status != "removed" { job?.done += 1 }
                default: break
                }
            }
        }
        await refresh()
    }

    /// Every indexed file again, `--force`: applies `allow` changes everywhere.
    func reindexAll() async {
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
    /// applies it to this document: a plain one for deny (every run sweeps the deny list
    /// through what is published), `--force` for allow (the file did not change).
    func decide(_ list: String, _ value: String, path: String) async {
        do {
            _ = try await cli.events(list, "add", "--", value)
        } catch {
            return report(error)
        }
        await ingest([path], force: list == "allow")
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
            do { try await work() } catch { report(error) }
        }
        self.task = task
        await task.value
        job = nil
        self.task = nil
    }

    func cancel() { task?.cancel() }

    func report(_ error: Error) {
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

    func maskAlways() async {
        let value = selection.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty, let path = review?.path else { return }
        await app.decide("deny", value, path: path)
        selection = ""
        await load()
    }

    func dontMask() async {
        guard let span = selectedSpan, let review else { return }
        await app.decide("allow", review.value(span), path: review.path)
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
}

extension CLIError {
    /// What to tell the user, by error code (docs/cli-json.md).
    var text: String {
        switch error {
        case "no_index": L("Nothing is indexed yet: drop some documents first.")
        case "no_document": L("This document is no longer in the index.")
        case "bad_value": L("The selected text is empty.")
        case "bad_config": L("Claude Desktop's configuration file is not valid: fix or delete it, then try again.")
        default: L("The core failed (\(message)).")
        }
    }
}
