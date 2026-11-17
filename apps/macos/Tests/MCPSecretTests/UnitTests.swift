import AppKit
import SwiftUI
import Foundation
import Testing
@testable import MCPSecret

/// A stand-in `mcp-secret`: answers each command with recorded `--json` output and an exit
/// code, and logs its arguments, one call per line. `review` is the `review -` worker: it answers
/// every id read on stdin (logged as "review <id>"); ids starting with "f" are unknown.
struct Recorded {
    let dir = FileManager.default.temporaryDirectory.appending(path: "mcp-secret-ui-\(UUID().uuidString)")

    /// `scripts`: shell run for a command instead of a canned answer.
    init(_ answers: [String: (out: String, code: Int32)], scripts: [String: String] = [:]) throws {
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let log = dir.appending(path: "log").path
        var sh = "#!/bin/sh\necho \"$*\" >> '\(log)'\ncase \"$2\" in\n"
        for (command, (out, code)) in answers where command != "review" {
            sh += "\(command))" + (out.isEmpty ? "" : " cat <<'EOF'\n\(out)\nEOF\n") + " exit \(code);;\n"
        }
        for (command, script) in scripts { sh += "\(command)) \(script)\n exit 0;;\n" }
        if let (out, code) = answers["review"] {
            sh += #"""
            review) while read -r id; do
            echo "review $id" >> '@LOG@'
            case "$id" in
            f*) printf '{"event": "error", "error": "no_document", "message": "no document %s"}\n' "$id";;
            *) cat <<'EOF'
            @OUT@
            EOF
            ;;
            esac
            done
            exit @CODE@;;

            """#.replacingOccurrences(of: "@LOG@", with: log).replacingOccurrences(of: "@OUT@", with: out)
                .replacingOccurrences(of: "@CODE@", with: String(code))
        }
        try (sh + "esac\n").write(to: dir.appending(path: "mcp-secret"), atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: path)
    }

    var path: String { dir.appending(path: "mcp-secret").path }
    var cli: CLI { CLI(executable: URL(filePath: path)) }
    var calls: [String] {
        ((try? String(contentsOf: dir.appending(path: "log"), encoding: .utf8)) ?? "")
            .split(separator: "\n").map(String.init)
    }
}

// Recorded from the real CLI on synthetic documents.
let STATUS = #"{"event": "status", "root": "/tmp/data", "models": true, "index": true, "docs": 3, "chunks": 4, "version": "0.1.0"}"#
let LIST = """
{"event": "doc", "doc_id": "aaaaaaaaaaaaaaaa", "path": "/docs/a.txt", "chunks": 1, "exists": true, "stale": false}
{"event": "doc", "doc_id": "bbbbbbbbbbbbbbbb", "path": "/docs/b.txt", "chunks": 2, "exists": false, "stale": false}
{"event": "doc", "doc_id": "cccccccccccccccc", "path": "/docs/c.txt", "chunks": 1, "exists": true, "stale": true}
"""
let INGEST = """
{"event": "start", "files": 2}
{"event": "doc", "doc_id": "aaaaaaaaaaaaaaaa", "path": "/docs/a.txt", "chunks": 1, "status": "indexed", "error": null}
{"event": "doc", "doc_id": null, "path": "/docs/z.pdf", "chunks": null, "status": "error", "error": "PermissionError"}
{"event": "done"}
"""
let TEXT = "Contratto tra Mario Rossi (CF RSSMRA80A01H501U) e la ditta Alfa.\n\nPagamento entro marzo.\n"
let REVIEW = #"{"event": "review", "doc_id": "aaaaaaaaaaaaaaaa", "path": "/docs/a.txt", "exists": true, "stale": false, "text": "Contratto tra Mario Rossi (CF RSSMRA80A01H501U) e la ditta Alfa.\n\nPagamento entro marzo.\n", "spans": [{"start": 14, "end": 25, "type": "PERSONA", "token": "[PERSONA_1]"}, {"start": 30, "end": 46, "type": "CF", "token": "[CF_1]"}, {"start": 59, "end": 63, "type": "ORG", "token": "[ORG_1]"}]}"#
let SHOW = #"{"event": "chunk", "chunk_id": 7, "text": "Contratto tra [PERSONA_1] (CF [CF_1]) e la ditta [ORG_1].\n\nPagamento entro marzo.\n"}"#
let DESKTOP = #"{"event": "desktop", "config": "/Users/me/Library/Application Support/Claude/claude_desktop_config.json", "configured": true, "command": "/repo/.venv/bin/mcp-secret", "command_ok": true, "last_log": "2026-10-13T11:31:28Z"}"#
let DRIVE = #"{"event": "drive", "client": true, "connected": true, "folders": [{"id": "1AbCdEfGhIjK", "name": "Clienti"}]}"#
// `drive sync`: its own start and file events, then those of the ingest it becomes
let DRIVE_SYNC = """
{"event": "start", "files": 1}
{"event": "file", "path": "/data/private/drive/Clienti/a.docx", "status": "downloaded", "error": null}
{"event": "start", "files": 1}
{"event": "doc", "doc_id": "aaaaaaaaaaaaaaaa", "path": "/data/private/drive/Clienti/a.docx", "chunks": 1, "status": "indexed", "error": null}
{"event": "done"}
"""

@Suite struct Decoding {
    @Test func everyEvent() throws {
        #expect(try Event.decode(STATUS) == .status(Status(root: "/tmp/data", models: true, index: true, docs: 3, chunks: 4, version: "0.1.0")))
        #expect(try Event.decode(#"{"event": "start", "files": 2}"#) == .start(files: 2, steps: nil))
        #expect(try Event.decode(#"{"event": "start", "steps": 2}"#) == .start(files: nil, steps: 2))
        #expect(try Event.decode(#"{"event": "step", "step": 1, "name": "docling"}"#) == .step(1, name: "docling"))
        #expect(try Event.decode(#"{"event": "done"}"#) == .done)
        #expect(try Event.decode(#"{"event": "export", "dir": "/tmp/w", "docs": 2, "removed": 1, "files": ["a.pdf", "b.md"]}"#)
                == .exported(Exported(dir: "/tmp/w", docs: 2, removed: 1, files: ["a.pdf", "b.md"])))
        #expect(try Event.decode(#"{"event": "deny", "action": "add", "changed": true}"#) == .edited(changed: true))
        #expect(try Event.decode(#"{"event": "allow", "action": "remove", "changed": false}"#) == .edited(changed: false))
        #expect(try Event.decode(SHOW) == .chunk(Chunk(chunkId: 7, text: "Contratto tra [PERSONA_1] (CF [CF_1]) e la ditta [ORG_1].\n\nPagamento entro marzo.\n")))
        let lines = INGEST.split(separator: "\n").map { try? Event.decode(String($0)) }
        #expect(lines[2] == .doc(Doc(docId: nil, path: "/docs/z.pdf", chunks: nil, status: "error", error: "PermissionError")))
        #expect(try Event.decode(LIST.components(separatedBy: "\n")[1]).doc?.exists == false)
        let review = try #require(try Event.decode(REVIEW).review)
        #expect(review.text == TEXT && review.spans.count == 3)
        #expect(review.value(review.spans[0]) == "Mario Rossi" && review.value(review.spans[2]) == "Alfa")
        let desktop = try #require(try Event.decode(DESKTOP).desktop)
        #expect(desktop.commandOk && desktop.lastLog == Date(timeIntervalSince1970: 1_791_891_088))
        #expect(try Event.decode(DRIVE).drive == Drive(client: true, connected: true,
                                                       folders: [.init(id: "1AbCdEfGhIjK", name: "Clienti")]))
        #expect(try Event.decode(DRIVE_SYNC.components(separatedBy: "\n")[1]) == .file(path: "/data/private/drive/Clienti/a.docx"))
    }

    @Test func errors() throws {
        #expect(try Event.decode(#"{"event": "error", "error": "no_document", "message": "no document x"}"#)
            == .error(CLIError(error: "no_document", message: "no document x")))
        #expect(throws: DecodingError.self) { try Event.decode(#"{"event": "surprise"}"#) }
        #expect(throws: DecodingError.self) { try Event.decode("Traceback (most recent call last):") }
    }

    @Test func codePointOffsets() throws {
        // offsets count code points: 😀 is one, though two UTF-16 units
        let line = #"{"event": "review", "doc_id": "x", "path": "/p", "exists": true, "stale": false, "text": "\ud83d\ude00 Zo\u00eb Lee", "spans": [{"start": 2, "end": 9, "type": "PERSONA", "token": "[PERSONA_1]"}]}"#
        let review = try #require(try Event.decode(line).review)
        #expect(review.value(review.spans[0]) == "Zoë Lee")
    }
}

@Suite struct Runner {
    @Test func streamsEventsAndThrowsTheErrorLine() async throws {
        let core = try Recorded([
            "list": (LIST, 0),
            "show": (#"{"event": "error", "error": "no_document", "message": "no document x"}"#, 1),
            "ingest": (INGEST, 1),  // a file failed: exit 1, but the run ended with done
            "status": ("", 139),  // crashed: no error line, no done
        ])
        #expect(try await core.cli.events("list").count == 3)
        await #expect(throws: CLIError(error: "no_document", message: "no document x")) {
            try await core.cli.events("show", "x")
        }
        #expect(try await core.cli.events("ingest", "/docs").last == .done)
        await #expect(throws: CLIError(error: "failed", message: "exit 139")) { try await core.cli.events("status") }
        #expect(core.calls == ["--json list", "--json show x", "--json ingest /docs", "--json status"])
    }

    @Test func cancellingSendsSIGTERM() async throws {
        let core = try Recorded(["ingest": ("", 0)])
        let script = try String(contentsOfFile: core.path, encoding: .utf8)
        // the stand-in sets its trap, reports a start, then waits; SIGTERM gets logged
        let wait = #"trap 'echo SIGTERM >> LOG; exit 143' TERM; echo '{"event": "start", "files": 1}'; sleep 30 & wait;;"#
        try script.replacingOccurrences(of: "ingest) exit 0;;", with: "ingest) " + wait)
            .replacingOccurrences(of: "LOG", with: core.dir.appending(path: "log").path)
            .write(toFile: core.path, atomically: true, encoding: .utf8)
        let clock = ContinuousClock(), started = clock.now
        let (running, ran) = AsyncStream<Void>.makeStream()
        let task = Task { for try await _ in core.cli.run(["ingest", "/docs"]) { ran.yield() } }
        for await _ in running { break }
        task.cancel()  // what AppModel.cancel() does
        _ = await task.result
        while !core.calls.contains("SIGTERM") && clock.now - started < .seconds(5) {
            try await Task.sleep(for: .milliseconds(50))
        }
        #expect(core.calls == ["--json ingest /docs", "SIGTERM"])
        #expect(clock.now - started < .seconds(5))
    }

    @Test func aSilentCommandDoesNotStallTheOthers() async throws {
        let core = try Recorded(["list": (LIST, 0), "ingest": ("", 0)])
        let script = try String(contentsOfFile: core.path, encoding: .utf8)
        try script.replacingOccurrences(of: "ingest) exit 0;;", with: "ingest) sleep 2;;")
            .write(toFile: core.path, atomically: true, encoding: .utf8)
        let clock = ContinuousClock(), started = clock.now
        async let slow = core.cli.events("ingest", "/docs")
        try await Task.sleep(for: .milliseconds(200))
        #expect(try await core.cli.events("list").count == 3)
        #expect(clock.now - started < .seconds(1))
        _ = try await slow
    }

    @Test func aLineLongerThanAPipeChunk() async throws {
        let text = String(repeating: "Lorem ipsum dolor sit amet. ", count: 20_000)  // 560 KB
        let line = #"{"event": "chunk", "chunk_id": 1, "text": "\#(text)"}"#
        let core = try Recorded(["show": (line + "\n" + line, 0)])
        #expect(try await core.cli.events("show", "x") == Array(repeating: .chunk(Chunk(chunkId: 1, text: text)), count: 2))
    }

    @Test func oneReviewerAnswersInOrder() async throws {
        let core = try Recorded(["review": (REVIEW, 0)])
        let reviewer = Reviewer(cli: core.cli)
        async let first = reviewer.review("a1"), second = reviewer.review("a2")
        #expect(try await [first, second].map(\.docId) == ["aaaaaaaaaaaaaaaa", "aaaaaaaaaaaaaaaa"])
        await #expect(throws: CLIError(error: "no_document", message: "no document ffff")) {
            try await reviewer.review("ffff")
        }
        #expect(try await reviewer.review("a3").spans.count == 3)  // an error does not end it
        #expect(core.calls.filter { $0.hasPrefix("--json") } == ["--json review -"])
        #expect(Set(core.calls.filter { $0.hasPrefix("review ") }) == ["review a1", "review a2", "review ffff", "review a3"])
    }

    @Test func missingExecutable() async {
        let cli = CLI(executable: URL(filePath: "/nonexistent/mcp-secret"))
        await #expect(throws: (any Error).self) { try await cli.events("status") }
    }
}

@MainActor @Suite struct Models {
    @Test func rowsCarryTheirState() async throws {
        let core = try Recorded(["status": (STATUS, 0), "list": (LIST, 0), "ingest": (INGEST, 1)])
        let app = AppModel(cliPath: core.path, dataRoot: "/tmp/data")
        await app.refresh()
        #expect(app.status?.docs == 3 && !app.needsSetup)
        #expect(app.rows.map(\.state) == [.indexed, .missing, .changed])
        await app.ingest(["/docs"])
        #expect(app.problem == nil && app.job == nil)
        #expect(app.outcome["/docs/a.txt"]?.status == "indexed")
        // the file that failed on its first ingest has a row of its own; it cannot be opened
        #expect(app.rows.map(\.state) == [.indexed, .missing, .changed, .error])
        #expect(app.rows.last?.docId == nil && app.rows.last?.name == "z.pdf")
        #expect(core.calls == ["--json status", "--json list", "--json ingest -- /docs", "--json status", "--json list"])
    }

    @Test func maskAlwaysThenDontMask() async throws {
        let core = try Recorded(["status": (STATUS, 0), "list": (LIST, 0), "review": (REVIEW, 0), "show": (SHOW, 0),
                                 "deny": (#"{"event": "deny", "action": "add", "changed": true}"#, 0),
                                 "allow": (#"{"event": "allow", "action": "add", "changed": true}"#, 0),
                                 "ingest": (INGEST, 0)])
        let app = AppModel(cliPath: core.path)
        let doc = DocumentModel(app: app, docId: "aaaaaaaaaaaaaaaa")
        await doc.load()
        #expect(doc.review?.spans.count == 3 && doc.chunks.map(\.chunkId) == [7])
        await DocumentModel(app: app, docId: "aaaaaaaaaaaaaaaa").load()  // reopened: from memory
        #expect(core.calls.filter { $0.hasPrefix("review ") }.count == 1)
        doc.selection = " marzo "
        await doc.maskAlways()
        doc.step(1)
        #expect(doc.selectedSpan?.token == "[PERSONA_1]")
        await doc.dontMask()
        #expect(app.problem == nil && doc.selected == nil)
        // each decision, then the ingest that applies it to this document, then a reload
        #expect(core.calls.filter { $0.contains(" add ") || $0.contains(" ingest ") } == [
            "--json deny add -- marzo", "--json ingest -- /docs/a.txt",
            "--json allow add -- Mario Rossi", "--json ingest --force -- /docs/a.txt",
        ])
        // one reviewer process for the three loads
        #expect(core.calls.filter { $0.hasPrefix("--json review") } == ["--json review -"])
        #expect(core.calls.filter { $0.hasPrefix("review ") }.count == 3)
    }

    @Test func maskOrLeaveInClearInThisDocumentOnly() async throws {
        let core = try Recorded(["status": (STATUS, 0), "list": (LIST, 0), "review": (REVIEW, 0), "show": (SHOW, 0),
                                 "deny": (#"{"event": "deny", "action": "add", "doc_id": "aaaaaaaaaaaaaaaa", "changed": true}"#, 0),
                                 "allow": (#"{"event": "allow", "action": "add", "doc_id": "aaaaaaaaaaaaaaaa", "changed": true}"#, 0),
                                 "ingest": (INGEST, 0)])
        let app = AppModel(cliPath: core.path)
        let doc = DocumentModel(app: app, docId: "aaaaaaaaaaaaaaaa")
        await doc.load()
        doc.selection = " marzo "
        await doc.maskAlways(here: true)
        doc.step(1)
        await doc.dontMask(here: true)
        #expect(app.problem == nil && doc.selected == nil)
        // `--doc`, and a forced ingest of this document each time: nothing else would apply it
        #expect(core.calls.filter { $0.contains(" add ") || $0.contains(" ingest ") } == [
            "--json deny add --doc aaaaaaaaaaaaaaaa -- marzo", "--json ingest --force -- /docs/a.txt",
            "--json allow add --doc aaaaaaaaaaaaaaaa -- Mario Rossi", "--json ingest --force -- /docs/a.txt",
        ])
    }

    @Test func connectClaudeDesktop() async throws {
        let core = try Recorded(["desktop-config": (DESKTOP, 0)])
        let app = AppModel(cliPath: core.path)
        await app.desktopConfig()
        #expect(app.desktop?.configured == true && !app.restartClaude)
        await app.desktopConfig("--install")
        #expect(app.restartClaude && app.problem == nil)
        #expect(core.calls == ["--json desktop-config", "--json desktop-config --install"])
    }

    @Test func addingADriveFolderSyncsIt() async throws {
        let core = try Recorded(["drive": (DRIVE_SYNC, 0), "status": (STATUS, 0), "list": (LIST, 0)])
        let app = AppModel(cliPath: core.path)
        await app.changeDrive("add", "https://drive.google.com/drive/folders/1AbCdEfGhIjK")
        #expect(app.problem == nil && app.job == nil)
        #expect(app.outcome["/data/private/drive/Clienti/a.docx"]?.status == "indexed")
        #expect(core.calls == ["--json drive add -- https://drive.google.com/drive/folders/1AbCdEfGhIjK",
                               "--json drive sync", "--json status", "--json list"])
        let offline = try Recorded(["drive": (#"{"event": "error", "error": "not_connected", "message": "x"}"#, 1)])
        let other = AppModel(cliPath: offline.path)
        await other.changeDrive("remove", "1AbCdEfGhIjK")
        #expect(other.problem == CLIError(error: "not_connected", message: "").text)
        #expect(offline.calls == ["--json drive remove -- 1AbCdEfGhIjK"])  // no sync after a failure
    }

    @Test func errorsReachTheUserAndStopTheAction() async throws {
        let core = try Recorded(["deny": (#"{"event": "error", "error": "bad_value", "message": "empty value"}"#, 1)])
        let app = AppModel(cliPath: core.path)
        await app.decide("deny", "x", paths: ["/docs/a.txt"])
        #expect(app.problem == CLIError(error: "bad_value", message: "").text)
        #expect(core.calls == ["--json deny add -- x"])  // no ingest after a failed deny
    }

    @Test func decisionsFromTheValuesListApplyInOneReindex() async throws {
        let core = try Recorded(["status": (STATUS, 0), "list": (LIST, 0), "ingest": (INGEST, 0),
                                 "allow": (#"{"event": "allow", "action": "add", "changed": true}"#, 0),
                                 "deny": (#"{"event": "deny", "action": "add", "changed": true}"#, 0)])
        let app = AppModel(cliPath: core.path)
        await app.refresh()
        app.decisions = ["mario rossi": .init(mask: false, docIds: ["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"]),  // b's file is gone
                         "frittata": .init(mask: true, docIds: ["cccccccccccccccc"])]
        await app.applyDecisions()
        #expect(app.problem == nil && app.decisions.isEmpty)
        #expect(core.calls.filter { !$0.hasSuffix("status") && !$0.hasSuffix("list") } == [
            "--json deny add -- frittata", "--json allow add -- mario rossi",
            "--json ingest --force -- /docs/a.txt /docs/c.txt"])
        #expect(try Event.decode(#"{"event": "value", "value": "frittata", "doc_ids": ["aaaaaaaaaaaaaaaa"]}"#)
            == .value(Value(value: "frittata", docIds: ["aaaaaaaaaaaaaaaa"])))
    }

    @Test func stepWrapsAround() throws {
        let doc = DocumentModel(app: AppModel(), docId: "a")
        doc.review = try Event.decode(REVIEW).review
        doc.step(-1)
        #expect(doc.selected == 2)
        doc.step(1)
        #expect(doc.selected == 0)
        doc.selection = "x"
        doc.step(1)
        #expect(doc.selected == 1 && doc.selection.isEmpty)
    }
}

@Suite struct Highlighting {
    let review = Review(docId: "x", path: "/p", exists: true, stale: false,
                        text: "😀 Mario Rossi abita a Roma, Mario Rossi.",
                        spans: [Span(start: 2, end: 13, type: "PERSONA", token: "[PERSONA_1]"),
                                Span(start: 22, end: 26, type: "LUOGO", token: "[LUOGO_1]"),
                                Span(start: 28, end: 39, type: "PERSONA", token: "[PERSONA_1]")])

    @Test func originalTagsEachSpanWithItsToken() {
        let (text, ranges) = Highlight.original(review, selected: 1)
        #expect(text.string == "😀 Mario Rossi\u{2009}[PERSONA_1] abita a Roma\u{2009}[LUOGO_1], Mario Rossi\u{2009}[PERSONA_1].")
        #expect(ranges.map { text.attributedSubstring(from: $0).string } == ["Mario Rossi", "Roma", "Mario Rossi"])
        #expect(text.attribute(.underlineStyle, at: ranges[1].location, effectiveRange: nil) != nil)  // selected
        #expect(text.attribute(.underlineStyle, at: ranges[0].location, effectiveRange: nil) == nil)
    }

    @Test func pickFindsSpansAndPlainText() {
        let (text, ranges) = Highlight.original(review, selected: nil)
        let s = text.string as NSString
        #expect(Highlight.pick(NSRange(location: ranges[1].location + 1, length: 0), in: text) == (1, ""))
        #expect(Highlight.pick(s.range(of: "[LUOGO_1]"), in: text) == (1, ""))  // its tag
        #expect(Highlight.pick(ranges[2], in: text) == (2, ""))
        #expect(Highlight.pick(s.range(of: "abita"), in: text) == (nil, "abita"))
        #expect(Highlight.pick(NSRange(location: 0, length: 0), in: text) == (nil, ""))
        // across a span: the original text, without the tag
        let across = NSUnionRange(s.range(of: "abita"), s.range(of: "[LUOGO_1]"))
        #expect(Highlight.pick(across, in: text) == (nil, "abita a Roma"))
    }

    @Test func agentMarksTheSelectedToken() {
        let chunks = [Chunk(chunkId: 1, text: "[PERSONA_1] abita a [LUOGO_1].\n"),
                      Chunk(chunkId: 2, text: "Firmato [PERSONA_1].")]
        let (text, hits) = Highlight.agent(chunks, token: "[PERSONA_1]")
        #expect(hits.map { text.attributedSubstring(from: $0).string } == ["[PERSONA_1]", "[PERSONA_1]"])
        let luogo = (text.string as NSString).range(of: "[LUOGO_1]")
        #expect(text.attribute(.backgroundColor, at: luogo.location, effectiveRange: nil) != nil)
        #expect(text.attribute(.underlineStyle, at: luogo.location, effectiveRange: nil) == nil)
        #expect(Highlight.agent(chunks, token: nil).1.isEmpty)
    }
}

@Test func everyStringHasItalian() throws {
    let dir = URL(filePath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().appending(path: "Sources/MCPSecret")
    let json = try JSONSerialization.jsonObject(with: Data(contentsOf: dir.appending(path: "Localizable.xcstrings")))
    let strings = (json as? [String: Any])?["strings"] as? [String: [String: Any]] ?? [:]
    let italian = strings.keys.filter { (strings[$0]?["localizations"] as? [String: Any])?["it"] != nil }
    var keys = Array(Highlight.names.values)
    for file in try FileManager.default.contentsOfDirectory(atPath: dir.path) where file.hasSuffix(".swift") {
        let code = try String(contentsOf: dir.appending(path: file), encoding: .utf8)
        keys += code.matches(of: /L\("((?:[^"\\]|\\.)*)"\)/).map { String($0.1) }
    }
    #expect(keys.count > 80)
    for key in keys {  // an interpolation is a %@ or %lld in the catalog
        let parts = key.split(separator: /\\\([^)]*\)/, omittingEmptySubsequences: false)
            .map { NSRegularExpression.escapedPattern(for: String($0)) }
        let pattern = try Regex(parts.joined(separator: "(%@|%lld)"))
        #expect(italian.contains { $0.wholeMatch(of: pattern) != nil }, "no Italian for \(key)")
    }
}

@Test func italianIsCompiledIn() throws {
    let it = try #require(Bundle.module.path(forResource: "it", ofType: "lproj").flatMap(Bundle.init(path:)))
    #expect(it.localizedString(forKey: "What the agent sees", value: nil, table: nil) == "Cosa vede l'agente")
    #expect(it.localizedString(forKey: "Tax code", value: nil, table: nil) == "Codice fiscale")
}

let TOKENS = """
{"event": "token", "token": "[PERSONA_1]", "type": "PERSONA", "value": "mario rossi", "doc_ids": ["aaaaaaaaaaaaaaaa", "cccccccccccccccc"]}
{"event": "token", "token": "[IBAN_1]", "type": "IBAN", "value": "it60x0542811101000000123456", "doc_ids": ["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"]}
{"event": "token", "token": "[ORG_1]", "type": "ORG", "value": null, "doc_ids": ["bbbbbbbbbbbbbbbb"]}
"""
let CATEGORIZED = """
{"event": "doc", "doc_id": "aaaaaaaaaaaaaaaa", "path": "/docs/Clienti/a.txt", "chunks": 1, "exists": true, "stale": false, "categories": ["Clienti", "Fatture"]}
{"event": "doc", "doc_id": "bbbbbbbbbbbbbbbb", "path": "/docs/Verbali/b.txt", "chunks": 2, "exists": true, "stale": false, "categories": ["Verbali"]}
{"event": "doc", "doc_id": "cccccccccccccccc", "path": "/docs/Clienti/c.txt", "chunks": 1, "exists": true, "stale": false, "categories": ["Clienti"]}
"""

@Suite struct Categories {
    func events(_ lines: String) -> [Event] { lines.split(separator: "\n").compactMap { try? Event.decode(String($0)) } }

    @Test func decoding() throws {
        #expect(try Event.decode(#"{"event": "cat", "action": "add", "changed": true}"#) == .edited(changed: true))
        #expect(try Event.decode(#"{"event": "rule", "name": "Fatture", "terms": ["fattur*", "iva"]}"#)
            == .rule(Rule(name: "Fatture", terms: ["fattur*", "iva"])))
        let tokens = events(TOKENS).compactMap(\.token)
        #expect(tokens.count == 3 && tokens[0].docIds.count == 2 && tokens[2].value == nil)
        #expect(events(CATEGORIZED).compactMap(\.doc).map(\.categories) == [["Clienti", "Fatture"], ["Verbali"], ["Clienti"]])
        #expect(try Event.decode(LIST.components(separatedBy: "\n")[0]).doc?.categories == nil)  // before categories
    }

    @Test func graphLinksDocumentsByTokensAndCategories() {
        let docs = events(CATEGORIZED).compactMap(\.doc), tokens = events(TOKENS).compactMap(\.token)
        let g = Graph(docs: docs, tokens: tokens, excluded: [], minWeight: 1)
        #expect(g.nodes.map(\.label) == ["a.txt", "b.txt", "c.txt"] && g.nodes.map(\.main) == ["Clienti", "Verbali", "Clienti"])
        #expect(g.edges == [Graph.Edge(a: 0, b: 1, shared: ["[IBAN_1]"]),
                            Graph.Edge(a: 0, b: 2, shared: ["[PERSONA_1]", "category:Clienti"])])
        #expect(Set(g.links.keys) == ["[PERSONA_1]", "[IBAN_1]", "category:Clienti"])  // in one document: no link
        #expect(g.neighbors(0).map(\.weight) == [2, 1])
        #expect(Graph(docs: docs, tokens: tokens, excluded: [], minWeight: 2).edges.map(\.b) == [2])
        let off = Graph(docs: docs, tokens: tokens, excluded: ["category:Clienti", "[IBAN_1]"], minWeight: 1)
        #expect(off.edges == [Graph.Edge(a: 0, b: 2, shared: ["[PERSONA_1]"])] && off.links == g.links)
    }

    @Test func layoutFitsTheSquareAndPullsLinksTogether() {
        let g = Graph(docs: events(CATEGORIZED).compactMap(\.doc), tokens: events(TOKENS).compactMap(\.token),
                      excluded: [], minWeight: 1)
        let p = Graph.layout(g)
        #expect(p.count == g.nodes.count && p == Graph.layout(g))  // deterministic
        #expect(p.allSatisfy { (0...1).contains($0.x) && (0...1).contains($0.y) })
        func dist(_ a: Int, _ b: Int) -> Double { hypot(p[a].x - p[b].x, p[a].y - p[b].y) }
        #expect(dist(0, 2) < dist(1, 2))  // c sits by a, its strongest link, away from b
        #expect(Graph.layout(Graph()) == [] && Graph.layout(Graph(docs: [], tokens: [], excluded: [], minWeight: 1)) == [])
    }

    @MainActor @Test func byHandAndByRule() async throws {
        let core = try Recorded(["status": (STATUS, 0), "list": (CATEGORIZED, 0),
                                 "cat": (#"{"event": "cat", "action": "add", "changed": true}"#, 0),
                                 "rules": (#"{"event": "rule", "name": "Fatture", "terms": ["fattur*", "iva"]}"#, 0)])
        let app = AppModel(cliPath: core.path)
        app.category = "Gone"
        await app.refresh()
        #expect(app.categories == ["Clienti", "Fatture", "Verbali"] && app.category == nil)
        #expect(app.rows.map(\.categories) == [["Clienti", "Fatture"], ["Verbali"], ["Clienti"]])
        await app.setCategory(["bbbbbbbbbbbbbbbb"], "-Urgente", add: true)
        await app.setRule("Fatture", terms: ["fattur*", " iva"])
        #expect(app.rules == [Rule(name: "Fatture", terms: ["fattur*", "iva"])] && app.problem == nil)
        #expect(core.calls.filter { !$0.hasSuffix("status") && !$0.hasSuffix("list") } == [
            "--json cat add -- bbbbbbbbbbbbbbbb -Urgente", "--json rules --set=Fatture -- fattur*  iva"])
        #expect(core.calls.filter { $0.hasSuffix("list") }.count == 3)  // each change refreshes
    }

    @MainActor @Test func graphModelAsksTokensOnceAndFollowsTheCategory() async throws {
        let core = try Recorded(["status": (STATUS, 0), "list": (CATEGORIZED, 0), "tokens": (TOKENS, 0)])
        let app = AppModel(cliPath: core.path)
        let graph = GraphModel()
        await graph.load(app)
        #expect(graph.graph.nodes.isEmpty && core.calls.isEmpty)  // nothing indexed: no call
        await app.refresh()
        await graph.load(app)
        #expect(graph.graph.nodes.count == 3 && graph.points.count == 3)
        graph.selected = 2
        graph.excluded = ["category:Clienti"]
        await graph.load(app)
        #expect(graph.graph.edges.map(\.shared) == [["[IBAN_1]"], ["[PERSONA_1]"]])
        #expect(core.calls.filter { $0.contains("tokens") }.count == 1)  // same documents: no new call
        app.category = "Clienti"
        await graph.load(app)
        #expect(graph.graph.nodes.map(\.label) == ["a.txt", "c.txt"] && graph.selectedNode?.label == "c.txt")
        #expect(graph.graph.nodes[0].categories == ["Fatture"] && graph.graph.nodes[0].main == "Fatture")  // the chosen one links all
        graph.types = ["IBAN", "PERSONA"]
        await graph.load(app)
        #expect(core.calls.last == "--json tokens --type IBAN --type PERSONA")
        #expect(graph.label("[IBAN_1]") == "[IBAN_1]" && graph.label("category:Clienti") == "Clienti")
        graph.showValues = true
        #expect(graph.label("[IBAN_1]") == "it60x0542811101000000123456")
        #expect(graph.label("[ORG_1]") == "[ORG_1]")  // no value: the token
    }
}

@MainActor @Suite struct Selection {
    let a = try! Event.decode(LIST.components(separatedBy: "\n")[0]).doc!

    @Test func actionsReachEveryDocumentOfTheSelection() async throws {
        let core = try Recorded(["status": (STATUS, 0), "list": (LIST, 0), "ingest": (INGEST, 0), "forget": ("", 0),
                                 "cat": (#"{"event": "cat", "action": "add", "changed": true}"#, 0)])
        let app = AppModel(cliPath: core.path)
        await app.refresh()
        let picked = app.docs(["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb", "/docs/z.pdf"])  // a failed file has no document
        #expect(picked.map(\.path) == ["/docs/a.txt", "/docs/b.txt"])
        await app.reindex(picked)  // b's file is gone: left out
        await app.setCategory(["aaaaaaaaaaaaaaaa", "cccccccccccccccc"], "Urgente", add: true)
        await app.forget(["aaaaaaaaaaaaaaaa", "cccccccccccccccc"])
        await app.setCategory([], "Urgente", add: false)
        await app.forget([])  // nothing selected: no call
        #expect(app.problem == nil)
        #expect(core.calls.filter { !$0.hasSuffix("status") && !$0.hasSuffix("list") } == [
            "--json ingest --force -- /docs/a.txt",
            "--json cat add -- aaaaaaaaaaaaaaaa cccccccccccccccc Urgente",
            "--json forget -- aaaaaaaaaaaaaaaa cccccccccccccccc"])
    }

    @Test func aForgottenDocumentLeavesNoRowAndLeavesTheCoworkFolder() async throws {
        let rest = LIST.components(separatedBy: "\n").dropFirst().joined(separator: "\n")
        let core = try Recorded(["status": (STATUS, 0), "list": (rest, 0), "forget": ("", 0),
                                 "export": (#"{"event": "export", "dir": "/tmp/w", "docs": 2, "removed": 1, "files": []}"#, 0)])
        let app = AppModel(cliPath: core.path)
        app.docs = [a]
        app.outcome[a.path] = Doc(docId: a.docId, path: a.path, chunks: 1, status: "indexed", error: nil)
        app.exportDir = "/tmp/w"
        await app.forget([a.docId!])
        #expect(app.rows.map(\.path) == ["/docs/b.txt", "/docs/c.txt"])  // not back as a new row
        #expect(core.calls.last == "--json export -- /tmp/w")
    }

    @Test func exportOfSomeDocumentsIsOneCoreCall() async throws {
        let core = try Recorded(["export": (#"{"event": "export", "dir": "/out", "docs": 2, "removed": 0, "files": ["aaaaaaaaaaaaaaaa.pdf", "cccccccccccccccc.md"]}"#, 0)])
        let app = AppModel(cliPath: core.path)
        await app.export(["aaaaaaaaaaaaaaaa", "cccccccccccccccc"], to: "/out")
        #expect(app.job == nil && app.problem == nil)
        #expect(core.calls == ["--json export --doc aaaaaaaaaaaaaaaa --doc cccccccccccccccc -- /out"])
    }

    @Test func aCancelledExportHandsNoFile() async throws {
        let core = try Recorded(["export": (#"{"event": "export", "dir": "/out", "docs": 1, "removed": 0, "files": ["aaaaaaaaaaaaaaaa.md"]}"#, 0)])
        let task = Task { try await core.cli.export("aaaaaaaaaaaaaaaa", to: core.dir) }
        task.cancel()
        await #expect(throws: CancellationError.self) { try await task.value }
    }
}

/// What the sidebar hands AppKit when its rows are dragged: for each row, the file name and text
/// of its pasteboard writer (`public.file-url`), and whether the table lets a drag start there.
@MainActor func dragged(from app: AppModel, selecting ids: Set<String>) async throws -> [(name: String, text: String)] {
    _ = NSApplication.shared
    let window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: 1180, height: 760), styleMask: [.titled],
                          backing: .buffered, defer: false)
    window.isReleasedWhenClosed = false  // ARC owns it
    window.contentView = NSHostingView(rootView: MainView(selection: ids, advanced: true).environment(app))
    window.orderFrontRegardless()
    defer { window.close() }
    func outline(_ v: NSView) -> NSOutlineView? { (v as? NSOutlineView) ?? v.subviews.lazy.compactMap(outline).first }
    var list: NSOutlineView?
    for _ in 0..<50 where list?.numberOfRows != app.rows.count || app.rows.isEmpty {  // it fills in a moment after the window shows
        try await Task.sleep(for: .milliseconds(100))
        list = outline(window.contentView!)
    }
    let table = try #require(list)
    #expect(table.selectedRowIndexes.count == ids.count)
    return try (0..<table.numberOfRows).map { row in
        let rect = table.rect(ofRow: row)
        #expect(table.canDragRows(with: IndexSet(integer: row), at: NSPoint(x: rect.midX, y: rect.midY)))
        let writer = try #require(table.dataSource?.outlineView?(table, pasteboardWriterForItem: table.item(atRow: row) as Any))
        #expect(writer.writableTypes(for: NSPasteboard(name: .drag)).map(\.rawValue) == ["net.daringfireball.markdown", "public.file-url"])
        let t0 = ContinuousClock.now
        let data = try #require(writer.pasteboardPropertyList(forType: .fileURL) as? Data)
        print("TIMING row", row, ContinuousClock.now - t0)
        let file = try #require(URL(dataRepresentation: data, relativeTo: nil))
        return (file.lastPathComponent, try String(contentsOf: file, encoding: .utf8))
    }
}

@MainActor @Suite struct DragPayload {
    @Test func everyRowOffersItsAnonymizedFile() async throws {
        // `--json export --doc <id> -- <dir>`: the core writes the file and names it
        let write = #"echo agent > "$6/$4.md"; printf '{"event": "export", "dir": "%s", "docs": 1, "removed": 0, "files": ["%s.md"]}\n' "$6" "$4""#
        let core = try Recorded(["status": (STATUS, 0), "list": (LIST, 0)], scripts: ["export": write])
        let app = AppModel(cliPath: core.path)
        await app.refresh()
        let files = try await dragged(from: app, selecting: ["aaaaaaaaaaaaaaaa", "cccccccccccccccc"])
        #expect(files.map(\.name) == ["aaaaaaaaaaaaaaaa.md", "bbbbbbbbbbbbbbbb.md", "cccccccccccccccc.md"])
        #expect(files.allSatisfy { $0.text == "agent\n" })
    }

    /// The real core, as the app runs it.
    @Test(.enabled(if: FileManager.default.isExecutableFile(atPath: CLI.defaultPath)))
    func realCoreDocumentsDragOut() async throws {
        let docs = try fixtures()
        let app = AppModel(cliPath: CLI.defaultPath, dataRoot: docs.deletingLastPathComponent().appending(path: "data").path)
        await app.ingest([docs.path])
        let ids = app.rows.compactMap(\.docId)
        #expect(ids.count == 3)
        let files = try await dragged(from: app, selecting: Set(ids))
        #expect(files.map(\.name) == ids.map { "\($0).md" })
        for (file, id) in zip(files, ids) {
            let shown = try await app.cli.events("show", id).compactMap(\.chunk).map(\.text).joined(separator: "\n\n")
            #expect(!shown.isEmpty && file.text.hasSuffix(shown + "\n"))  // after its pseudonymized name
        }
    }
}

@Suite struct Install {
    @Test func commandNamesTheShippedWheelAndPins() throws {
        let dir = FileManager.default.temporaryDirectory.appending(path: "it's \(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        #expect(CLI.installCommand(resources: dir) == nil)  // no wheel: not a release app
        try Data().write(to: dir.appending(path: "mcp_secret-0.1.0-py3-none-any.whl"))
        let command = try #require(CLI.installCommand(resources: dir))
        #expect(command.contains("uv tool install"))
        #expect(command.contains("cd '\(dir.path.replacingOccurrences(of: "'", with: #"'\''"#))' && uv tool install"))
        #expect(command.hasSuffix("-c constraints.txt ./mcp_secret-0.1.0-py3-none-any.whl"))
        #expect(CLI.shippedWheel(resources: dir)?.version == "0.1.0")
    }

    @Test func installRunsTheCommandAndKeepsItsLastLines() async {
        #expect(await CLI.install("true") == (true, ""))
        let (ok, tail) = await CLI.install("for i in $(seq 1 20); do echo $i; done; echo oops >&2; exit 3")
        #expect(!ok && tail == "14\n15\n16\n17\n18\n19\n20\noops")
    }

    @MainActor @Test func aCoreOfAnotherVersionShowsTheInstallSheetAgain() async throws {
        let core = try Recorded(["status": (STATUS, 0), "list": (LIST, 0)])  // STATUS: version 0.1.0
        let app = AppModel(cliPath: core.path, dataRoot: "/tmp/data")
        app.shippedVersion = "0.2.0"
        await app.refresh()
        #expect(app.coreMissing && core.calls == ["--json status"])  // nothing else runs against it
        app.shippedVersion = "0.1.0"
        await app.refresh()
        #expect(!app.coreMissing && app.docs.count == 3)
        app.shippedVersion = nil  // `swift run`: no wheel shipped, any core will do
        await app.refresh()
        #expect(!app.coreMissing)
    }
}
