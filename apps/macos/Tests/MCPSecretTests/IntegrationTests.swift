import Foundation
import Testing
@testable import MCPSecret

let repo = URL(filePath: CLI.defaultPath).deletingLastPathComponent().deletingLastPathComponent()
    .deletingLastPathComponent()  // <repo>/.venv/bin/mcp-secret

/// Three generated documents (fixtures/generate.py, synthetic only) in a fresh folder.
func fixtures() throws -> URL {
    let out = repo.appending(path: "fixtures/out")
    if !FileManager.default.fileExists(atPath: out.appending(path: "00_contratto.txt").path) {
        let p = Process()
        p.executableURL = repo.appending(path: ".venv/bin/python")
        p.arguments = [repo.appending(path: "fixtures/generate.py").path]
        try p.run()
        p.waitUntilExit()
    }
    let docs = FileManager.default.temporaryDirectory.appending(path: "mcp-secret-ui-\(UUID().uuidString)/docs")
    try FileManager.default.createDirectory(at: docs, withIntermediateDirectories: true)
    for name in ["00_contratto.txt", "02_mail.txt", "03_cv.txt"] {
        try FileManager.default.copyItem(at: out.appending(path: name), to: docs.appending(path: name))
    }
    return docs
}

/// The real core on a temporary data root: drop documents, open one, mask a value that
/// slipped through, unmask one that should not have been masked.
@MainActor @Test(.enabled(if: FileManager.default.isExecutableFile(atPath: CLI.defaultPath)))
func endToEnd() async throws {
    let docs = try fixtures()
    let app = AppModel(cliPath: CLI.defaultPath, dataRoot: docs.deletingLastPathComponent().appending(path: "data").path)
    await app.refresh()
    #expect(app.status?.index == false && app.needsSetup && app.rows.isEmpty)

    await app.ingest([docs.path])
    #expect(app.problem == nil)
    #expect(app.rows.map(\.name) == ["00_contratto.txt", "02_mail.txt", "03_cv.txt"])
    #expect(app.rows.allSatisfy { $0.state == .indexed && $0.chunks > 0 })
    #expect(app.status?.docs == 3 && !app.needsSetup)

    let doc = DocumentModel(app: app, docId: try #require(app.rows.first?.docId))
    await doc.load()
    // the reviewer is warm now: opening a document again takes a fraction of the first time
    let clock = ContinuousClock(), started = clock.now
    await doc.load()
    #expect(clock.now - started < .seconds(3))
    let review = try #require(doc.review)
    #expect(review.exists && !review.stale && !doc.chunks.isEmpty)
    var agent: String { doc.chunks.map(\.text).joined(separator: "\n").lowercased() }
    for span in review.spans {
        #expect(agent.contains(span.token.lowercased()))
        #expect(!agent.contains(review.value(span).lowercased()), "\(span.type) published in clear")
    }

    // a word nobody masked is in clear; "Always mask" takes it out of what the agent sees
    let word = try #require(Set(agent.split(whereSeparator: { !$0.isLetter }).map(String.init)
        .filter { $0.count >= 8 }).sorted().first)
    doc.selection = word
    await doc.maskAlways()
    #expect(app.problem == nil)
    #expect(!agent.split(whereSeparator: { !$0.isLetter }).contains { $0 == word })

    // "Don't mask" an email: it comes back in clear in this document
    doc.selected = try #require(doc.review?.spans.firstIndex { $0.type == "EMAIL" })
    let email = try #require(doc.selectedSpan.map { doc.review!.value($0) })
    #expect(!agent.contains(email.lowercased()))
    await doc.dontMask()
    #expect(app.problem == nil)
    #expect(agent.contains(email.lowercased()))
    #expect(app.rows.first?.state == .indexed)
}
