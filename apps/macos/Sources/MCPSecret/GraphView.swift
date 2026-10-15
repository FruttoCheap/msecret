import SwiftUI

/// Documents, linked by what they share: the same token (a person, an IBAN...) or category.
struct Graph: Equatable, Sendable {
    struct Node: Identifiable, Equatable, Sendable {
        var id: String
        var path: String
        var categories: [String]
        var label: String { URL(filePath: path).lastPathComponent }
        /// The category it is colored by: its folder's, unless taken away; else the first.
        var main: String? {
            let folder = URL(filePath: path).deletingLastPathComponent().lastPathComponent
            return categories.contains(folder) ? folder : categories.first
        }
    }

    struct Edge: Equatable, Sendable {
        var a: Int
        var b: Int
        /// What both documents hold: tokens, and categories as `category:<name>`.
        var shared: [String]
        var weight: Int { shared.count }
    }

    var nodes: [Node] = []
    var edges: [Edge] = []
    /// Every token and category (`category:<name>`) in two documents or more, with its nodes;
    /// the excluded ones too.
    var links: [String: [Int]] = [:]

    init() {}

    /// One node per document. An edge where at least `minWeight` tokens and categories, but the
    /// `excluded`, are in both documents.
    init(docs: [Doc], tokens: [Token], excluded: Set<String>, minWeight: Int) {
        nodes = docs.compactMap { d in d.docId.map { Node(id: $0, path: d.path, categories: d.categories ?? []) } }
        let at = Dictionary(uniqueKeysWithValues: nodes.indices.map { (nodes[$0].id, $0) })
        var of: [String: [Int]] = [:]
        for t in tokens { of[t.token] = t.docIds.compactMap { at[$0] } }
        for (i, n) in nodes.enumerated() { for c in n.categories { of["category:\(c)", default: []].append(i) } }
        links = of.filter { $0.value.count > 1 }
        var shared: [SIMD2<Int>: [String]] = [:]
        // ponytail: every pair of each link's documents, O(sum k²); a token in thousands of documents is slow until excluded
        for (key, ix) in links where !excluded.contains(key) {
            for j in ix.indices {
                for i in 0..<j { shared[SIMD2(min(ix[i], ix[j]), max(ix[i], ix[j])), default: []].append(key) }
            }
        }
        edges = shared.filter { $0.value.count >= max(minWeight, 1) }
            .map { Edge(a: $0.key.x, b: $0.key.y, shared: $0.value.sorted()) }
            .sorted { ($0.a, $0.b) < ($1.a, $1.b) }
    }

    /// The nodes linked to node `i`, heaviest link first.
    func neighbors(_ i: Int) -> [(node: Int, weight: Int)] {
        edges.compactMap { e in e.a == i ? (e.b, e.weight) : e.b == i ? (e.a, e.weight) : nil }
            .sorted { ($0.weight, $1.node) > ($1.weight, $0.node) }
    }

    /// Force-directed layout (Fruchterman–Reingold) fitted to the unit square: linked nodes pull
    /// together, every node pushes the others away. No randomness: same graph, same picture.
    static func layout(_ g: Graph, iterations: Int = 200) -> [CGPoint] {
        let n = g.nodes.count
        guard n > 1 else { return Array(repeating: CGPoint(x: 0.5, y: 0.5), count: n) }
        var p = (0..<n).map { i in  // a sunflower spiral: spread out from the start
            let r = 0.5 * (Double(i) + 0.5).squareRoot() / Double(n).squareRoot(), a = Double(i) * 2.399963
            return SIMD2(0.5 + r * cos(a), 0.5 + r * sin(a))
        }
        let k = (1 / Double(n)).squareRoot()
        var heat = 0.1
        // ponytail: O(n²) repulsion per step; Barnes–Hut if graphs grow past ~2000 nodes
        for _ in 0..<iterations {
            var d = [SIMD2<Double>](repeating: .zero, count: n)
            for i in 0..<n {
                for j in (i + 1)..<n {
                    let v = p[i] - p[j], dist = max((v * v).sum().squareRoot(), 1e-3)
                    let f = v * (k * k / (dist * dist))
                    d[i] += f
                    d[j] -= f
                }
                d[i] -= (p[i] - 0.5) * 0.05  // gravity: nodes without links stay near the rest
            }
            for e in g.edges {
                let v = p[e.a] - p[e.b], dist = max((v * v).sum().squareRoot(), 1e-3)
                let f = v * (dist / k * log2(1 + Double(e.weight)))
                d[e.a] -= f
                d[e.b] += f
            }
            for i in 0..<n {
                let len = max((d[i] * d[i]).sum().squareRoot(), 1e-9)
                p[i] += d[i] * (min(len, heat) / len)
            }
            heat *= 0.97
        }
        let lo = p.reduce(SIMD2(Double.infinity, .infinity)) { pointwiseMin($0, $1) }
        let hi = p.reduce(SIMD2(-Double.infinity, -.infinity)) { pointwiseMax($0, $1) }
        let span = pointwiseMax(hi - lo, SIMD2(1e-9, 1e-9))
        return p.map { q in
            let u = (q - lo) / span
            return CGPoint(x: u.x, y: u.y)
        }
    }
}

/// A category's color: its place among `all`, on a wheel of ten. None: gray.
// ponytail: colors repeat past ten categories; a color per category in Settings if that confuses
func categoryColor(_ c: String?, _ all: [String]) -> Color {
    guard let c, let i = all.firstIndex(of: c) else { return .secondary }
    return [Color.blue, .orange, .green, .red, .purple, .teal, .yellow, .brown, .indigo, .pink][i % 10]
}

/// What the graph shows, for the session: filters, selection, zoom, and the laid-out graph.
@MainActor @Observable
final class GraphModel {
    /// Token types that link documents; empty: all.
    var types: Set<String> = []
    var minWeight = 1
    /// Tokens and categories (`category:<name>`) that link nothing, chosen by hand.
    var excluded: Set<String> = []
    /// Tokens listed with their value in clear instead of the token.
    var showValues = false
    var graph = Graph()
    /// Each node's place in the unit square.
    var points: [CGPoint] = []
    var loading = false
    var selected: Int?
    var scale = 1.0
    var offset = CGSize.zero
    /// The last `tokens`, by token; asked again only when documents or types change.
    private(set) var tokens: [String: Token] = [:]
    private var fetched: Fetch?

    struct Fetch: Equatable {
        var docs: [Doc]
        var types: Set<String>
    }

    /// What the graph is made of: it is rebuilt when this changes.
    struct Inputs: Equatable {
        var docs: [Doc]
        var category: String?
        var types: Set<String>
        var excluded: Set<String>
        var minWeight: Int
    }

    var selectedNode: Graph.Node? { selected.flatMap { $0 < graph.nodes.count ? graph.nodes[$0] : nil } }

    /// A link's name: the category, the token, or its value in clear.
    func label(_ key: String) -> String {
        key.hasPrefix("category:") ? String(key.dropFirst(9)) : showValues ? tokens[key]?.value ?? key : key
    }

    /// The documents of the sidebar's category, linked, laid out off the main thread.
    func load(_ app: AppModel) async {
        let cat = app.category, excluded = excluded, minWeight = minWeight
        let docs = app.docs.filter { d in cat.map { d.categories?.contains($0) ?? false } ?? true }
            .map { d in  // the chosen category is in every document shown: it links all, says nothing
                var d = d
                d.categories?.removeAll { $0 == cat }
                return d
            }
        guard !docs.isEmpty else {
            (graph, points, selected) = (Graph(), [], nil)
            return
        }
        loading = true
        defer { loading = false }
        do {
            let want = Fetch(docs: app.docs, types: types)
            if fetched != want {
                let args = ["tokens"] + types.sorted().flatMap { ["--type", $0] }
                let got = try await app.cli.events(args).compactMap(\.token)
                guard !Task.isCancelled else { return }  // a cancelled stream ends early: not all of them
                (tokens, fetched) = (Dictionary(got.map { ($0.token, $0) }) { a, _ in a }, want)
            }
            let toks = Array(tokens.values)
            let (g, at) = await Task.detached {
                let g = Graph(docs: docs, tokens: toks, excluded: excluded, minWeight: minWeight)
                return (g, Graph.layout(g))
            }.value
            guard !Task.isCancelled else { return }
            let keep = selectedNode?.id
            (graph, points) = (g, at)
            selected = keep.flatMap { id in g.nodes.firstIndex { $0.id == id } }
        } catch {
            app.report(error)
        }
    }
}

/// The graph of documents, colored by category. Click one for what links it, double-click to
/// open it; pinch to zoom, drag to pan.
struct GraphView: View {
    @Environment(AppModel.self) private var app
    @Bindable var model: GraphModel
    /// Opens a document.
    var open: (String) -> Void
    @State private var hover: Int?
    @State private var pinch = 1.0
    @State private var pan = CGSize.zero

    var body: some View {
        HStack(spacing: 0) {
            GeometryReader { geo in canvas(geo.size) }
                .overlay {
                    if model.graph.nodes.isEmpty {
                        if model.loading {
                            ProgressView()
                        } else {
                            ContentUnavailableView(L("Nothing to draw"), systemImage: "point.3.connected.trianglepath.dotted",
                                                   description: Text(L("Index some documents first.")))
                        }
                    }
                }
            Divider()
            GraphPanel(model: model, open: open).frame(width: 300)
        }
        .navigationTitle(L("Graph"))
        .task(id: GraphModel.Inputs(docs: app.docs, category: app.category, types: model.types,
                                    excluded: model.excluded, minWeight: model.minWeight)) {
            await model.load(app)
        }
    }

    var scale: Double { model.scale * pinch }

    func position(_ i: Int, _ size: CGSize) -> CGPoint {
        let p = model.points[i], w = size.width - 80, h = size.height - 80
        return CGPoint(x: size.width / 2 + (p.x - 0.5) * w * scale + model.offset.width + pan.width,
                       y: size.height / 2 + (p.y - 0.5) * h * scale + model.offset.height + pan.height)
    }

    let radius = 6.0

    func color(_ n: Graph.Node) -> Color { categoryColor(n.main, app.categories) }

    /// The node under `at`, the nearest if several.
    func hit(_ at: CGPoint, _ size: CGSize) -> Int? {
        let g = model.graph
        guard model.points.count == g.nodes.count else { return nil }
        return g.nodes.indices.map { i in
            let p = position(i, size)
            return (i, hypot(p.x - at.x, p.y - at.y))
        }
        .filter { $0.1 <= radius + 4 }
        .min { $0.1 < $1.1 }?.0
    }

    func canvas(_ size: CGSize) -> some View {
        Canvas { ctx, size in draw(ctx, size) }
            .contentShape(Rectangle())
            .gesture(SpatialTapGesture().onEnded { model.selected = hit($0.location, size) })
            .simultaneousGesture(SpatialTapGesture(count: 2).onEnded { v in
                if let i = hit(v.location, size) { open(model.graph.nodes[i].id) }
            })
            .simultaneousGesture(DragGesture()
                .onChanged { pan = $0.translation }
                .onEnded { v in
                    model.offset.width += v.translation.width
                    model.offset.height += v.translation.height
                    pan = .zero
                })
            .simultaneousGesture(MagnifyGesture()
                .onChanged { pinch = $0.magnification }
                .onEnded { v in
                    model.scale = min(max(model.scale * v.magnification, 0.5), 8)
                    pinch = 1
                })
            .onContinuousHover { phase in
                if case .active(let at) = phase { hover = hit(at, size) } else { hover = nil }
            }
            .accessibilityLabel(L("Graph of \(model.graph.nodes.count) documents"))
    }

    func draw(_ ctx: GraphicsContext, _ size: CGSize) {
        let g = model.graph
        guard model.points.count == g.nodes.count else { return }
        let at = g.nodes.indices.map { position($0, size) }
        let focus = model.selected ?? hover
        let near = Set(focus.map { f in [f] + g.neighbors(f).map(\.node) } ?? [])
        for e in g.edges {
            let on = e.a == focus || e.b == focus
            var line = Path()
            line.move(to: at[e.a])
            line.addLine(to: at[e.b])
            ctx.stroke(line, with: .color(on ? .accentColor : .secondary.opacity(focus == nil ? 0.3 : 0.08)),
                       lineWidth: on ? 2 : min(0.5 + 0.5 * Double(e.weight - 1), 3))
        }
        for i in g.nodes.indices {
            let box = CGRect(x: at[i].x - radius, y: at[i].y - radius, width: 2 * radius, height: 2 * radius)
            ctx.fill(Path(ellipseIn: box), with: .color(color(g.nodes[i]).opacity(focus != nil && !near.contains(i) ? 0.2 : 0.9)))
            if i == model.selected {
                ctx.stroke(Path(ellipseIn: box.insetBy(dx: -4, dy: -4)), with: .color(.accentColor), lineWidth: 2)
            }
        }
        for i in g.nodes.indices where i != hover && (near.contains(i) || scale >= 2 || g.nodes.count <= 12) {
            ctx.draw(Text(g.nodes[i].label).font(.caption)
                        .foregroundStyle(focus != nil && !near.contains(i) ? .secondary : .primary),
                     at: CGPoint(x: at[i].x, y: at[i].y + radius + 2), anchor: .top)
        }
        if let h = hover, h < at.count {  // the name and the categories
            let n = g.nodes[h]
            let text = ctx.resolve(Text(([n.label] + n.categories).joined(separator: "  ·  ")).font(.callout))
            let s = text.measure(in: size)
            let box = CGRect(x: at[h].x + 12, y: at[h].y - s.height - 16, width: s.width + 14, height: s.height + 8)
            ctx.fill(Path(roundedRect: box, cornerRadius: 6), with: .color(Color(nsColor: .windowBackgroundColor)))
            ctx.stroke(Path(roundedRect: box, cornerRadius: 6), with: .color(.secondary.opacity(0.6)))
            ctx.draw(text, at: CGPoint(x: box.midX, y: box.midY))
        }
    }
}

/// Filters and zoom; then the selected document (what links it, each link can be switched off),
/// or every category and document to pick one: what the canvas shows, as a list VoiceOver and
/// the keyboard can use.
struct GraphPanel: View {
    @Environment(AppModel.self) private var app
    @Bindable var model: GraphModel
    var open: (String) -> Void

    var body: some View {
        let g = model.graph
        List {
            Section(L("Show")) {
                Toggle(L("Values in clear"), isOn: $model.showValues)
                Stepper(L("In common per link: at least \(model.minWeight)"), value: $model.minWeight, in: 1...20)
                Menu(model.types.isEmpty ? L("All Types") : L("\(model.types.count) types")) {
                    Button(L("All Types")) { model.types = [] }
                    Divider()
                    ForEach(Highlight.order, id: \.self) { t in
                        Toggle(Highlight.name(t), isOn: Binding(get: { model.types.contains(t) },
                                                               set: { if $0 { model.types.insert(t) } else { model.types.remove(t) } }))
                    }
                }
                if !model.excluded.isEmpty {
                    Button(L("Switch On Excluded (\(model.excluded.count))")) { model.excluded = [] }
                }
                HStack {
                    Button { model.scale = max(model.scale / 1.5, 0.5) } label: { Label(L("Zoom Out"), systemImage: "minus.magnifyingglass") }
                    Button { model.scale = min(model.scale * 1.5, 8) } label: { Label(L("Zoom In"), systemImage: "plus.magnifyingglass") }
                    Button(L("Fit")) { (model.scale, model.offset) = (1, .zero) }
                }
                .labelStyle(.iconOnly)
            }
            if let i = model.selected, let n = model.selectedNode {
                Section(L("Document")) {
                    Text(verbatim: n.label).font(.headline).help(n.path)
                    if !n.categories.isEmpty { Text(verbatim: n.categories.joined(separator: ", ")).foregroundStyle(.secondary) }
                    HStack {
                        Button(L("Open")) { open(n.id) }
                        Button(L("Deselect")) { model.selected = nil }
                    }
                }
                Section(L("Linked")) {
                    ForEach(g.neighbors(i), id: \.node) { x in
                        Button { model.selected = x.node } label: { row(g.nodes[x.node], x.weight) }
                            .buttonStyle(.plain)
                    }
                }
                Section(L("In common")) {
                    ForEach(g.links.filter { $0.value.contains(i) }.sorted { ($0.value.count, $1.key) > ($1.value.count, $0.key) },
                            id: \.key) { key, ix in
                        Toggle(isOn: Binding(get: { !model.excluded.contains(key) },
                                             set: { if $0 { model.excluded.remove(key) } else { model.excluded.insert(key) } })) {
                            HStack {
                                link(key)
                                Text(verbatim: model.label(key)).lineLimit(1).truncationMode(.middle)
                                Spacer()
                                Text(verbatim: "\(ix.count)").foregroundStyle(.secondary)
                            }
                        }
                        .help(L("Off: it links no document"))
                    }
                }
            } else {
                Section(L("Categories")) {
                    ForEach(app.categories, id: \.self) { c in
                        Button { app.category = app.category == c ? nil : c } label: {
                            HStack {
                                Image(systemName: "circle.fill").foregroundStyle(categoryColor(c, app.categories))
                                    .accessibilityHidden(true)
                                Text(verbatim: c).lineLimit(1)
                                Spacer()
                                if app.category == c { Image(systemName: "checkmark").accessibilityLabel(L("Shown")) }
                            }
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                }
                Section(L("Documents")) {
                    let degree = g.edges.reduce(into: [Int](repeating: 0, count: g.nodes.count)) { $0[$1.a] += 1; $0[$1.b] += 1 }
                    ForEach(g.nodes.indices.sorted { (degree[$0], $1) > (degree[$1], $0) }, id: \.self) { i in
                        Button { model.selected = i } label: { row(g.nodes[i], degree[i]) }
                            .buttonStyle(.plain)
                    }
                }
            }
        }
    }

    /// A token's dot in its type's color, or a category's folder.
    func link(_ key: String) -> some View {
        Image(systemName: key.hasPrefix("category:") ? "folder" : "circle.fill")
            .foregroundStyle(model.tokens[key].map { Color(nsColor: Highlight.color($0.type)) } ?? .secondary)
            .accessibilityHidden(true)
    }

    func row(_ n: Graph.Node, _ count: Int) -> some View {
        HStack {
            Image(systemName: "doc.fill")
                .foregroundStyle(categoryColor(n.main, app.categories))
                .accessibilityHidden(true)
            Text(verbatim: n.label).lineLimit(1).truncationMode(.middle)
            Spacer()
            Text(verbatim: "\(count)").foregroundStyle(.secondary)
        }
        .contentShape(Rectangle())
        .help(n.path)
    }
}
