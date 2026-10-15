import SwiftUI

/// Every value ingest masks (`tokens`) and every value left in clear by choice (`allow`), each
/// with the documents it is in. Unchecking one publishes it in clear there; checking one masks
/// it again, everywhere.
struct ValuesView: View {
    @Environment(AppModel.self) private var app
    /// Opens a document.
    var open: (String) -> Void
    @State private var masked: [Token] = []
    @State private var clear: [Value] = []
    @State private var search = ""

    var body: some View {
        let names = Dictionary(app.docs.compactMap { d in d.docId.map { ($0, URL(filePath: d.path).lastPathComponent) } }) { a, _ in a }
        List {
            Section(L("Masked")) {
                ForEach(masked.filter { shown($0.value ?? $0.token) }, id: \.token) { t in
                    row(t.value, token: t.token, type: t.type, docIds: t.docIds, masked: true, names: names)
                }
            }
            Section(L("In clear")) {
                ForEach(clear.filter { shown($0.value) }, id: \.value) { v in
                    row(v.value, token: nil, type: nil, docIds: v.docIds, masked: false, names: names)
                }
            }
        }
        .searchable(text: $search, prompt: L("Value or token"))
        .overlay {
            if masked.isEmpty && clear.isEmpty {
                ContentUnavailableView(L("Nothing masked yet"), systemImage: "eye.slash",
                                       description: Text(L("Index some documents first.")))
            }
        }
        .navigationTitle(L("Masked Values"))
        .task(id: app.docs) { await load() }
    }

    func shown(_ s: String) -> Bool { search.isEmpty || s.localizedCaseInsensitiveContains(search) }

    func load() async {
        guard !app.docs.isEmpty else { return (masked, clear) = ([], []) }
        do {
            async let t = app.cli.events("tokens"), v = app.cli.events("allow")
            let (tokens, values) = try await (t, v)
            guard !Task.isCancelled else { return }  // a cancelled stream ends early: not all of them
            (masked, clear) = (tokens.compactMap(\.token), values.compactMap(\.value))
        } catch {
            app.report(error)
        }
    }

    /// The checkbox masks the value; open it for its documents.
    func row(_ value: String?, token: String?, type: String?, docIds: [String], masked: Bool,
             names: [String: String]) -> some View {
        DisclosureGroup {
            ForEach(docIds, id: \.self) { id in
                Button(names[id] ?? id) { open(id) }.buttonStyle(.link).help(L("Open"))
            }
        } label: {
            HStack {
                Toggle(L("Mask"), isOn: Binding(get: { masked }, set: { on in
                    guard let value else { return }
                    Task {
                        await app.mask(value, on, docIds: docIds)
                        await load()
                    }
                }))
                .labelsHidden()
                .disabled(app.job != nil || value == nil)  // no value known: a token-shaped text
                Image(systemName: "circle.fill")
                    .foregroundStyle(Color(nsColor: type.map(Highlight.color) ?? .systemGray))
                    .accessibilityHidden(true)
                Text(verbatim: value ?? token ?? "").lineLimit(1).truncationMode(.middle)
                if let token { Text(verbatim: token).font(.caption.monospaced()).foregroundStyle(.secondary) }
                Spacer()
                Text(L("\(docIds.count) documents")).foregroundStyle(.secondary)
            }
        }
    }
}
