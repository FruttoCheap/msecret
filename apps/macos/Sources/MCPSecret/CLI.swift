import Foundation

/// The only component that launches the core: `mcp-secret --json <args>`, stdout decoded line by
/// line (docs/cli-json.md), cancelled with SIGTERM. Views never launch processes.
struct CLI: Sendable, Equatable {
    var executable: URL
    var environment: [String: String] = [:]

    /// <repo>/.venv/bin/mcp-secret, the repo being where this file was compiled.
    static let defaultPath = URL(filePath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent()
        .appending(path: ".venv/bin/mcp-secret").path

    func process(_ args: [String], stdout: Pipe) -> Process {
        let process = Process()
        process.executableURL = executable
        process.arguments = ["--json"] + args
        process.environment = ProcessInfo.processInfo.environment.merging(environment) { $1 }
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = stdout
        // ponytail: stderr is dropped (it may quote documents); error lines carry what we show
        process.standardError = FileHandle.nullDevice
        return process
    }

    /// Every event but `error`, which ends the stream by throwing a `CLIError`. A run that exits
    /// non-zero without `done` (crash, kill) throws too; `ingest` exits 1 when a file failed and
    /// still ends with `done`.
    func run(_ args: [String]) -> AsyncThrowingStream<Event, Error> {
        AsyncThrowingStream { continuation in
            let out = Pipe(), process = process(args, stdout: out), lines = CLI.lines(out)
            let exit = AsyncStream<Int32> { c in
                process.terminationHandler = { c.yield($0.terminationStatus); c.finish() }
            }
            let task = Task {
                do {
                    try process.run()
                    var failure: CLIError?, done = false
                    for await line in lines {
                        let event = try Event.decode(line)
                        if case .error(let e) = event { failure = e; continue }
                        if case .done = event { done = true }
                        continuation.yield(event)
                    }
                    var status: Int32 = 0
                    for await s in exit { status = s }
                    if let failure { throw failure }
                    if status != 0 && !done { throw CLIError(error: "failed", message: "exit \(status)") }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in
                task.cancel()
                if process.isRunning { process.terminate() }  // ingest rolls back the open document
            }
        }
    }

    /// All the events of a short command.
    func events(_ args: String...) async throws -> [Event] { try await events(args) }

    func events(_ args: [String]) async throws -> [Event] {
        var all: [Event] = []
        for try await event in run(args) { all.append(event) }
        return all
    }

    /// What the agent sees of a document, as `export` writes it: its chunks a blank line apart.
    func anonymized(_ docId: String) async throws -> String {
        var chunks: [Chunk] = []
        do {
            chunks = try await events("show", docId).compactMap(\.chunk)
        } catch let e as CLIError where e.error == "no_document" {}  // an empty file publishes no chunk
        try Task.checkCancellation()  // a cancelled stream ends early: half a document
        return chunks.map(\.text).joined(separator: "\n\n") + "\n"
    }

    /// `<docId>.md` in `folder`: the file's URL.
    @discardableResult
    func export(_ docId: String, to folder: URL) async throws -> URL {
        let file = folder.appending(path: "\(docId).md")
        try await anonymized(docId).write(to: file, atomically: true, encoding: .utf8)
        return file
    }

    /// A pipe's lines as they come. Not FileHandle.bytes: it reads every handle on one shared
    /// actor, so a command with nothing to say yet (an ingest between files) would stall the rest.
    static func lines(_ pipe: Pipe) -> AsyncStream<String> {
        let rest = Rest()
        return AsyncStream { c in
            pipe.fileHandleForReading.readabilityHandler = { handle in
                let data = handle.availableData
                for line in rest.lines(after: data) { c.yield(line) }
                if data.isEmpty { handle.readabilityHandler = nil; c.finish() }
            }
        }
    }

    /// `review -`, running: document ids go in on stdin, one `review` or `error` line comes out
    /// for each, in order.
    struct Worker {
        let process: Process
        let input: FileHandle
        var lines: AsyncStream<String>.Iterator

        mutating func review(_ docId: String) async throws -> Review {
            try input.write(contentsOf: Data("\(docId)\n".utf8))
            guard let line = await lines.next() else { throw CLIError(error: "failed", message: "the reviewer quit") }
            switch try Event.decode(line) {
            case .review(let r): return r
            case .error(let e): throw e
            case let other: throw CLIError(error: "failed", message: "unexpected \(other)")
            }
        }
    }

    func worker() throws -> Worker {
        let out = Pipe(), input = Pipe(), process = process(["review", "-"], stdout: out)
        process.standardInput = input
        _ = fcntl(input.fileHandleForWriting.fileDescriptor, F_SETNOSIGPIPE, 1)  // dead worker: an error, not SIGPIPE
        let lines = CLI.lines(out).makeAsyncIterator()
        try process.run()
        return Worker(process: process, input: input.fileHandleForWriting, lines: lines)
    }
}

/// What a pipe sent after its last newline. A readability handler runs one call at a time.
private final class Rest: @unchecked Sendable {
    var data = Data()

    func lines(after more: Data) -> [String] {
        data.append(more)
        var lines: [String] = []
        while let newline = data.firstIndex(of: UInt8(ascii: "\n")) {
            lines.append(String(decoding: data[..<newline], as: UTF8.self))
            data.removeSubrange(...newline)
        }
        return lines
    }
}

/// Reviews through one long-lived `review -`: the core loads its models once (seconds), then
/// answers each document in a fraction of a second. Requests run one at a time, in order, so
/// answers never shift; a request whose view went away is still answered and dropped. The worker
/// starts on first use (or `warm()`), restarts if it dies, and exits when this goes away or the
/// app quits (its stdin closes).
final class Reviewer: Sendable {
    let cli: CLI
    private let requests: AsyncStream<(String, CheckedContinuation<Review, Error>)?>.Continuation

    init(cli: CLI) {
        self.cli = cli
        let (stream, requests) = AsyncStream<(String, CheckedContinuation<Review, Error>)?>.makeStream()
        self.requests = requests
        Task {
            var worker: CLI.Worker?
            for await request in stream {
                do {
                    if worker?.process.isRunning != true { worker = try cli.worker() }
                    if let (docId, reply) = request { reply.resume(returning: try await worker!.review(docId)) }
                } catch {
                    request?.1.resume(throwing: error)
                }
            }
        }
    }

    deinit { requests.finish() }

    /// Starts the worker ahead of the first review.
    func warm() { requests.yield(nil) }

    func review(_ docId: String) async throws -> Review {
        try await withCheckedThrowingContinuation { requests.yield((docId, $0)) }
    }
}

/// One line of `mcp-secret --json`.
enum Event: Equatable, Sendable {
    case start(files: Int?, steps: Int?)
    case step(Int, name: String)
    case doc(Doc)
    case done
    case chunk(Chunk)
    case review(Review)
    case status(Status)
    case desktop(Desktop)
    case rule(Rule)
    case token(Token)
    case exported(Exported)
    case drive(Drive)
    case file(path: String)
    case edited(changed: Bool)
    case error(CLIError)

    static func decode(_ line: String) throws -> Event {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        decoder.dateDecodingStrategy = .iso8601
        return try decoder.decode(Event.self, from: Data(line.utf8))
    }
}

extension Event: Decodable {
    private enum Key: String, CodingKey { case event, files, steps, step, name, changed, path }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Key.self)
        switch try c.decode(String.self, forKey: .event) {
        case "start": self = .start(files: try c.decodeIfPresent(Int.self, forKey: .files),
                                    steps: try c.decodeIfPresent(Int.self, forKey: .steps))
        case "step": self = .step(try c.decode(Int.self, forKey: .step), name: try c.decode(String.self, forKey: .name))
        case "doc": self = .doc(try Doc(from: decoder))
        case "done": self = .done
        case "chunk": self = .chunk(try Chunk(from: decoder))
        case "review": self = .review(try Review(from: decoder))
        case "status": self = .status(try Status(from: decoder))
        case "desktop": self = .desktop(try Desktop(from: decoder))
        case "rule": self = .rule(try Rule(from: decoder))
        case "token": self = .token(try Token(from: decoder))
        case "export": self = .exported(try Exported(from: decoder))
        case "drive": self = .drive(try Drive(from: decoder))
        case "file": self = .file(path: try c.decode(String.self, forKey: .path))
        case "deny", "allow", "cat": self = .edited(changed: try c.decode(Bool.self, forKey: .changed))
        case "error": self = .error(try CLIError(from: decoder))
        case let other:
            throw DecodingError.dataCorruptedError(forKey: .event, in: c, debugDescription: "unknown event \(other)")
        }
    }
}

/// A document: a `doc` line of `ingest` (status, error) or of `list` (exists, stale, categories).
struct Doc: Decodable, Equatable, Sendable {
    var docId: String?
    var path: String
    var chunks: Int?
    var status: String?
    var error: String?
    var exists: Bool?
    var stale: Bool?
    var categories: [String]?
}

/// A keyword rule of the automatic categories.
struct Rule: Decodable, Equatable, Sendable {
    var name: String
    var terms: [String]
}

/// A token with its value in clear and the documents it is published in.
struct Token: Decodable, Equatable, Sendable {
    var token: String
    var type: String
    var value: String?
    var docIds: [String]
}

/// An `export`: the folder written, the files in its `docs/`, the stale ones deleted.
struct Exported: Decodable, Equatable, Sendable {
    var dir: String
    var docs: Int
    var removed: Int
}

struct Chunk: Decodable, Equatable, Sendable {
    var chunkId: Int
    var text: String
}

/// A masked value: `start`/`end` count Unicode code points of `Review.text`.
struct Span: Decodable, Equatable, Sendable {
    var start: Int
    var end: Int
    var type: String
    var token: String
}

struct Review: Decodable, Equatable, Sendable {
    var docId: String
    var path: String
    var exists: Bool
    var stale: Bool
    var text: String
    var spans: [Span]
}

struct Status: Decodable, Equatable, Sendable {
    var root: String
    var models: Bool
    var index: Bool
    var docs: Int
    var chunks: Int
}

/// Google Drive: an OAuth client given, the account connected, the folders mirrored.
struct Drive: Decodable, Equatable, Sendable {
    var client: Bool
    var connected: Bool
    var folders: [Folder]

    struct Folder: Decodable, Equatable, Sendable, Identifiable {
        var id: String
        var name: String
    }
}

struct Desktop: Decodable, Equatable, Sendable {
    var config: String
    var configured: Bool
    var command: String?
    var commandOk: Bool
    var lastLog: Date?
}

struct CLIError: Error, Decodable, Equatable, Sendable {
    var error: String
    var message: String
}
