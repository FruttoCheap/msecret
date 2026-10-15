import AppKit
import Foundation
import Testing
@testable import MCPSecret

/// A stand-in `mcp-secret`: answers each command with recorded `--json` output and an exit
/// code, and logs its arguments, one call per line. `review` is the `review -` worker: it answers
/// every id read on stdin (logged as "review <id>"); ids starting with "f" are unknown.
struct Recorded {
    let dir = FileManager.default.temporaryDirectory.appending(path: "mcp-secret-ui-\(UUID().uuidString)")

    init(_ answers: [String: (out: String, code: Int32)]) throws {
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let log = dir.appending(path: "log").path
        var sh = "#!/bin/sh\necho \"$*\" >> '\(log)'\ncase \"$2\" in\n"
        for (command, (out, code)) in answers where command != "review" {
            sh += "\(command))" + (out.isEmpty ? "" : " cat <<'EOF'\n\(out)\nEOF\n") + " exit \(code);;\n"
        }
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
let STATUS = #"{"event": "status", "root": "/tmp/data", "models": true, "index": true, "docs": 3, "chunks": 4}"#
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

@Suite struct Decoding {
    @Test func everyEvent() throws {
        #expect(try Event.decode(STATUS) == .status(Status(root: "/tmp/data", models: true, index: true, docs: 3, chunks: 4)))
        #expect(try Event.decode(#"{"event": "start", "files": 2}"#) == .start(files: 2, steps: nil))
        #expect(try Event.decode(#"{"event": "start", "steps": 2}"#) == .start(files: nil, steps: 2))
        #expect(try Event.decode(#"{"event": "step", "step": 1, "name": "docling"}"#) == .step(1, name: "docling"))
        #expect(try Event.decode(#"{"event": "done"}"#) == .done)
        #expect(try Event.decode(#"{"event": "export", "dir": "/tmp/w", "docs": 2, "removed": 1}"#)
                == .exported(Exported(dir: "/tmp/w", docs: 2, removed: 1)))
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

    @Test func connectClaudeDesktop() async throws {
        let core = try Recorded(["desktop-config": (DESKTOP, 0)])
        let app = AppModel(cliPath: core.path)
        await app.desktopConfig()
        #expect(app.desktop?.configured == true && !app.restartClaude)
        await app.desktopConfig("--install")
        #expect(app.restartClaude && app.problem == nil)
        #expect(core.calls == ["--json desktop-config", "--json desktop-config --install"])
    }

    @Test func errorsReachTheUserAndStopTheAction() async throws {
        let core = try Recorded(["deny": (#"{"event": "error", "error": "bad_value", "message": "empty value"}"#, 1)])
        let app = AppModel(cliPath: core.path)
        await app.decide("deny", "x", path: "/docs/a.txt")
        #expect(app.problem == CLIError(error: "bad_value", message: "").text)
        #expect(core.calls == ["--json deny add -- x"])  // no ingest after a failed deny
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
