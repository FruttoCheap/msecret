import Foundation

/// The only component that launches the core: `mcp-secret --json <args>`, stdout decoded line by
/// line (docs/cli-json.md), cancelled with SIGTERM. Views never launch processes.
struct CLI: Sendable {
    var executable: URL
    var environment: [String: String] = [:]

    /// <repo>/.venv/bin/mcp-secret, the repo being where this file was compiled.
    static let defaultPath = URL(filePath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent()
        .appending(path: ".venv/bin/mcp-secret").path

    /// Every event but `error`, which ends the stream by throwing a `CLIError`. A run that exits
    /// non-zero without `done` (crash, kill) throws too; `ingest` exits 1 when a file failed and
    /// still ends with `done`.
    func run(_ args: [String]) -> AsyncThrowingStream<Event, Error> {
        AsyncThrowingStream { continuation in
            let process = Process(), out = Pipe()
            process.executableURL = executable
            process.arguments = ["--json"] + args
            process.environment = ProcessInfo.processInfo.environment.merging(environment) { $1 }
            process.standardInput = FileHandle.nullDevice
            process.standardOutput = out
            // ponytail: stderr is dropped (it may quote documents); error lines carry what we show
            process.standardError = FileHandle.nullDevice
            let exit = AsyncStream<Int32> { c in
                process.terminationHandler = { c.yield($0.terminationStatus); c.finish() }
            }
            let task = Task {
                do {
                    try process.run()
                    var failure: CLIError?, done = false
                    for try await line in out.fileHandleForReading.bytes.lines {
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
    func events(_ args: String...) async throws -> [Event] {
        var all: [Event] = []
        for try await event in run(args) { all.append(event) }
        return all
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
    private enum Key: String, CodingKey { case event, files, steps, step, name, changed }

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
        case "deny", "allow": self = .edited(changed: try c.decode(Bool.self, forKey: .changed))
        case "error": self = .error(try CLIError(from: decoder))
        case let other:
            throw DecodingError.dataCorruptedError(forKey: .event, in: c, debugDescription: "unknown event \(other)")
        }
    }
}

/// A document: a `doc` line of `ingest` (status, error) or of `list` (exists, stale).
struct Doc: Decodable, Equatable, Sendable {
    var docId: String?
    var path: String
    var chunks: Int?
    var status: String?
    var error: String?
    var exists: Bool?
    var stale: Bool?
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
