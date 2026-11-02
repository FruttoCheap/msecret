import SwiftUI

/// Every value ingest masks (`tokens`) and every value left in clear by choice (`allow`), each
/// with the documents it is in. Unchecking one leaves it in clear there, checking one masks it
/// again everywhere: once you choose Re-index Now, all of them in one run.
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
        .safeAreaInset(edge: .bottom) { if !app.decisions.isEmpty { apply } }
        .navigationTitle(L("Masked Values"))
        .task(id: app.docs) { await load() }
    }

    /// The decisions waiting for a re-index.
    var apply: some View {
        HStack {
            Image(systemName: "arrow.triangle.2.circlepath").accessibilityHidden(true)
            Text(L("\(app.decisions.count) changes not applied yet"))
            Spacer()
            Button(L("Discard")) { app.decisions = [:] }
            Button(L("Re-index Now")) {
                Task {
                    await app.applyDecisions()
                    await load()
                }
            }
            .help(L("A change reaches the agent only once its documents are indexed again: the text the agent sees is rebuilt from the originals."))
            .disabled(app.job != nil)
        }
        .padding(10)
        .background(Color.orange.opacity(0.15))
        .background(.bar)  // opaque: the list scrolls under it
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
                let decided = value.flatMap { app.decisions[$0] }
                Toggle(L("Mask"), isOn: Binding(get: { decided?.mask ?? masked }, set: { on in
                    guard let value else { return }
                    app.decisions[value] = on == masked ? nil : .init(mask: on, docIds: docIds)  // back: no change
                }))
                .labelsHidden()
                .disabled(value == nil)  // no value known: a token-shaped text
                Image(systemName: decided == nil ? "circle.fill" : "arrow.triangle.2.circlepath")
                    .foregroundStyle(Color(nsColor: type.map(Highlight.color) ?? .systemGray))
                    .help(decided == nil ? "" : L("Applied at the next re-index"))
                    .accessibilityLabel(L("Applied at the next re-index"))
                    .accessibilityHidden(decided == nil)
                Text(verbatim: value ?? token ?? "").lineLimit(1).truncationMode(.middle)
                if let token { Text(verbatim: token).font(.caption.monospaced()).foregroundStyle(.secondary) }
                Spacer()
                Text(L("\(docIds.count) documents")).foregroundStyle(.secondary)
            }
        }
    }
}
