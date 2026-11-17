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

    /// What a session starts with: that venv when it is there (`swift run`), else where
    /// `uv tool install` puts the core of a downloaded app.
    static var startPath: String {
        FileManager.default.isExecutableFile(atPath: defaultPath)
            ? defaultPath : NSHomeDirectory() + "/.local/bin/mcp-secret"
    }

    /// The core's wheel, which ships in the app's Resources, and its version (from the file name).
    /// nil outside a release app (scripts/release.sh).
    static func shippedWheel(resources: URL? = Bundle.main.resourceURL) -> (url: URL, version: String)? {
        guard let dir = resources,
              let name = try? FileManager.default.contentsOfDirectory(atPath: dir.path)
                  .first(where: { $0.hasSuffix(".whl") }),
              let version = name.split(separator: "-").dropFirst().first
        else { return nil }
        return (dir.appending(path: name), String(version))
    }

    /// What a first run pastes in Terminal: uv (fetched if missing), then the shipped wheel and the
    /// pins next to it. Also the update: `--reinstall` replaces a core of another version.
    /// It `cd`s into Resources and passes relative paths: uv before 0.13 cuts an absolute one at
    /// the first space ("mcp-secret 2.app", a second download).
    static func installCommand(resources: URL? = Bundle.main.resourceURL) -> String? {
        guard let wheel = shippedWheel(resources: resources) else { return nil }
        let dir = "'" + wheel.url.deletingLastPathComponent().path.replacingOccurrences(of: "'", with: #"'\''"#) + "'"
        return #"export PATH="$HOME/.local/bin:$PATH"; command -v uv >/dev/null || "#
            + "curl -LsSf https://astral.sh/uv/install.sh | sh; cd \(dir) && "
            + "uv tool install --force --reinstall --python 3.12 -c constraints.txt ./\(wheel.url.lastPathComponent)"
    }

    /// Runs `installCommand` itself, so a first run needs no Terminal: its exit status and the
    /// last lines it printed (uv's, package names: no document data), for a failure to show.
    static func install(_ command: String) async -> (ok: Bool, tail: String) {
        await Task.detached {
            let process = Process()
            process.executableURL = URL(filePath: "/bin/zsh")
            process.arguments = ["-c", command]
            let pipe = Pipe()
            process.standardOutput = pipe
            process.standardError = pipe
            process.standardInput = FileHandle.nullDevice
            do { try process.run() } catch { return (false, error.localizedDescription) }
            let out = pipe.fileHandleForReading.readDataToEndOfFile()  // drains it: uv never blocks
            process.waitUntilExit()
            let lines = String(decoding: out, as: UTF8.self).split(separator: "\n").suffix(8)
            return (process.terminationStatus == 0, lines.joined(separator: "\n"))
        }.value
    }

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

    /// `export --doc`: the document in `folder`, its original format with tokens for values
    /// (`<docId>.pdf`, `.docx`…) or what the agent sees (`<docId>.md`). The file's URL.
    @discardableResult
    func export(_ docId: String, to folder: URL) async throws -> URL {
        let done = try await events("export", "--doc", docId, "--", folder.path).compactMap(\.exported).first
        try Task.checkCancellation()  // a cancelled stream ends early: no event
        guard let name = done?.files.first else { throw CLIError(error: "failed", message: "export wrote nothing") }
        return folder.appending(path: name)
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
    case value(Value)
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
        case "value": self = .value(try Value(from: decoder))
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

/// A value of `allow` (or `deny`) and the documents that publish it in clear.
struct Value: Decodable, Equatable, Sendable {
    var value: String
    var docIds: [String]
}

/// An `export`: the folder written, the files written in it (or in its `pseudonymized/`), the
/// stale ones deleted.
struct Exported: Decodable, Equatable, Sendable {
    var dir: String
    var docs: Int
    var removed: Int
    var files: [String]
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
    /// The core's version; absent from cores older than the check.
    var version: String?
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
