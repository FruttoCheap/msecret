import SwiftUI

/// Categories and tokens, linked by the documents they share: a category to the tokens in its
/// documents, a token to the tokens in the same documents. Documents are not nodes.
struct Graph: Equatable, Sendable {
    struct Node: Identifiable, Equatable, Sendable {
        var id: String
        var label: String
        /// The token's type; nil for a category.
        var type: String?
        var value: String?
        var docIds: Set<String>
        var isCategory: Bool { type == nil }
    }

    struct Edge: Equatable, Sendable {
        var a: Int
        var b: Int
        /// Documents both ends are in.
        var weight: Int
    }

    var nodes: [Node] = []
    var edges: [Edge] = []

    init() {}

    /// Categories first, then tokens. An edge where at least `minWeight` documents hold both
    /// ends; two categories are never linked.
    init(tokens: [Token], docs: [Doc], minWeight: Int) {
        var cats: [String: Set<String>] = [:]
        for d in docs {
            guard let id = d.docId else { continue }
            for c in d.categories ?? [] { cats[c, default: []].insert(id) }
        }
        nodes = cats.keys.sorted { $0.localizedStandardCompare($1) == .orderedAscending }
            .map { Node(id: "category:\($0)", label: $0, type: nil, value: nil, docIds: cats[$0]!) }
        nodes += tokens.map { Node(id: $0.token, label: $0.token, type: $0.type, value: $0.value, docIds: Set($0.docIds)) }
        // ponytail: every pair, O(n²) set intersections; a doc -> nodes index past ~2000 nodes
        for j in nodes.indices where !nodes[j].isCategory {
            for i in 0..<j {
                let w = nodes[i].docIds.intersection(nodes[j].docIds).count
                if w >= max(minWeight, 1) { edges.append(Edge(a: i, b: j, weight: w)) }
            }
        }
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

/// What the graph shows, for the session: filters, selection, zoom, and the laid-out graph.
@MainActor @Observable
final class GraphModel {
    /// Token types shown; empty: all.
    var types: Set<String> = []
    var minWeight = 1
    /// Tokens labelled with their value in clear instead of the token.
    var showValues = false
    var graph = Graph()
    /// Each node's place in the unit square.
    var points: [CGPoint] = []
    var loading = false
    var selected: Int?
    var scale = 1.0
    var offset = CGSize.zero

    /// What the graph is made of: it is rebuilt when this changes.
    struct Inputs: Equatable {
        var docs: [Doc]
        var types: Set<String>
        var minWeight: Int
    }

    var selectedNode: Graph.Node? { selected.flatMap { $0 < graph.nodes.count ? graph.nodes[$0] : nil } }

    func label(_ n: Graph.Node) -> String { showValues ? n.value ?? n.label : n.label }

    /// `tokens`, then the graph with the documents' categories, laid out off the main thread.
    func load(_ app: AppModel) async {
        let docs = app.docs, minWeight = minWeight
        guard !docs.isEmpty else {
            (graph, points, selected) = (Graph(), [], nil)
            return
        }
        loading = true
        defer { loading = false }
        do {
            let args = ["tokens"] + types.sorted().flatMap { ["--type", $0] }
            let tokens = try await app.cli.events(args).compactMap(\.token)
            let (g, at) = await Task.detached {
                let g = Graph(tokens: tokens, docs: docs, minWeight: minWeight)
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

/// The graph of categories and tokens. Click a node for its documents and links; hover for its
/// value; pinch to zoom, drag to pan.
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
                                                   description: Text(L("Index some documents first, or show more token types.")))
                        }
                    }
                }
            Divider()
            GraphPanel(model: model, open: open).frame(width: 300)
        }
        .navigationTitle(L("Graph"))
        .task(id: GraphModel.Inputs(docs: app.docs, types: model.types, minWeight: model.minWeight)) {
            await model.load(app)
        }
    }

    var scale: Double { model.scale * pinch }

    func position(_ i: Int, _ size: CGSize) -> CGPoint {
        let p = model.points[i], w = size.width - 80, h = size.height - 80
        return CGPoint(x: size.width / 2 + (p.x - 0.5) * w * scale + model.offset.width + pan.width,
                       y: size.height / 2 + (p.y - 0.5) * h * scale + model.offset.height + pan.height)
    }

    func radius(_ n: Graph.Node) -> Double {
        n.isCategory ? 8 : min(3 + 2 * Double(n.docIds.count).squareRoot(), 12)
    }

    /// The node under `at`, the nearest if several.
    func hit(_ at: CGPoint, _ size: CGSize) -> Int? {
        let g = model.graph
        guard model.points.count == g.nodes.count else { return nil }
        return g.nodes.indices.map { i in
            let p = position(i, size)
            return (i, hypot(p.x - at.x, p.y - at.y))
        }
        .filter { $0.1 <= radius(g.nodes[$0.0]) + 4 }
        .min { $0.1 < $1.1 }?.0
    }

    func canvas(_ size: CGSize) -> some View {
        Canvas { ctx, size in draw(ctx, size) }
            .contentShape(Rectangle())
            .gesture(SpatialTapGesture().onEnded { model.selected = hit($0.location, size) })
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
            .accessibilityLabel(L("Graph of \(model.graph.nodes.count) categories and tokens"))
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
        // tokens, then categories over them, then every label over all nodes
        let order = g.nodes.indices.filter { !g.nodes[$0].isCategory } + g.nodes.indices.filter { g.nodes[$0].isCategory }
        for i in order {
            let n = g.nodes[i], r = radius(n), box = CGRect(x: at[i].x - r, y: at[i].y - r, width: 2 * r, height: 2 * r)
            let color = n.type.map { Color(nsColor: Highlight.color($0)) } ?? .primary
            ctx.fill(n.isCategory ? Path(roundedRect: box, cornerRadius: 3) : Path(ellipseIn: box),
                     with: .color(color.opacity(focus != nil && !near.contains(i) ? 0.2 : 0.9)))
            if i == model.selected {
                ctx.stroke(Path(ellipseIn: box.insetBy(dx: -4, dy: -4)), with: .color(.accentColor), lineWidth: 2)
            }
        }
        for i in order where i != hover {
            let n = g.nodes[i], dim = focus != nil && !near.contains(i)
            if n.isCategory || near.contains(i) || scale >= 2 {
                ctx.draw(Text(model.label(n)).font(n.isCategory ? .callout.bold() : .caption)
                            .foregroundStyle(dim ? .secondary : .primary),
                         at: CGPoint(x: at[i].x, y: at[i].y + radius(n) + 2), anchor: .top)
            }
        }
        if let h = hover, h < at.count {  // token and value side by side, whatever the toggle
            let n = g.nodes[h]
            let line = n.isCategory ? L("\(n.label): \(n.docIds.count) documents")
                : [n.label, n.value].compactMap { $0 }.joined(separator: "  ")
            let text = ctx.resolve(Text(line).font(.callout)), s = text.measure(in: size)
            let box = CGRect(x: at[h].x + 12, y: at[h].y - s.height - 16, width: s.width + 14, height: s.height + 8)
            ctx.fill(Path(roundedRect: box, cornerRadius: 6), with: .color(Color(nsColor: .windowBackgroundColor)))
            ctx.stroke(Path(roundedRect: box, cornerRadius: 6), with: .color(.secondary.opacity(0.6)))
            ctx.draw(text, at: CGPoint(x: box.midX, y: box.midY))
        }
    }
}

/// Filters and zoom; then the selected node (its documents and links), or every node to pick
/// one: what the canvas shows, as a list VoiceOver and the keyboard can use.
struct GraphPanel: View {
    @Environment(AppModel.self) private var app
    @Bindable var model: GraphModel
    var open: (String) -> Void

    var body: some View {
        let g = model.graph
        List {
            Section(L("Show")) {
                Toggle(L("Values in clear"), isOn: $model.showValues)
                Stepper(L("Shared documents per link: at least \(model.minWeight)"), value: $model.minWeight, in: 1...20)
                Menu(model.types.isEmpty ? L("All Types") : L("\(model.types.count) types")) {
                    Button(L("All Types")) { model.types = [] }
                    Divider()
                    ForEach(Highlight.order, id: \.self) { t in
                        Toggle(Highlight.name(t), isOn: Binding(get: { model.types.contains(t) },
                                                               set: { if $0 { model.types.insert(t) } else { model.types.remove(t) } }))
                    }
                }
                HStack {
                    Button { model.scale = max(model.scale / 1.5, 0.5) } label: { Label(L("Zoom Out"), systemImage: "minus.magnifyingglass") }
                    Button { model.scale = min(model.scale * 1.5, 8) } label: { Label(L("Zoom In"), systemImage: "plus.magnifyingglass") }
                    Button(L("Fit")) { (model.scale, model.offset) = (1, .zero) }
                }
                .labelStyle(.iconOnly)
            }
            if let i = model.selected, let n = model.selectedNode {
                Section(n.type.map(Highlight.name) ?? L("Category")) {
                    Text(verbatim: n.label).font(.headline).textSelection(.enabled)
                    if let v = n.value { Text(verbatim: v).textSelection(.enabled) }
                    Button(L("Deselect")) { model.selected = nil }
                }
                Section(L("Documents (\(n.docIds.count))")) {
                    ForEach(app.docs.filter { n.docIds.contains($0.docId ?? "") }, id: \.path) { d in
                        Button(URL(filePath: d.path).lastPathComponent) { d.docId.map(open) }
                            .buttonStyle(.link).help(d.path)
                    }
                }
                Section(L("Linked")) {
                    ForEach(g.neighbors(i), id: \.node) { x in
                        Button { model.selected = x.node } label: { row(g.nodes[x.node], x.weight) }
                            .buttonStyle(.plain)
                    }
                }
            } else {
                Section(L("Nodes")) {
                    ForEach(g.nodes.indices.sorted { g.nodes[$0].docIds.count > g.nodes[$1].docIds.count }, id: \.self) { i in
                        Button { model.selected = i } label: { row(g.nodes[i], g.nodes[i].docIds.count) }
                            .buttonStyle(.plain)
                    }
                }
            }
        }
    }

    func row(_ n: Graph.Node, _ count: Int) -> some View {
        HStack {
            Image(systemName: n.isCategory ? "folder" : "circle.fill")
                .foregroundStyle(n.type.map { Color(nsColor: Highlight.color($0)) } ?? .secondary)
                .accessibilityHidden(true)
            Text(verbatim: model.label(n)).lineLimit(1).truncationMode(.middle)
            Spacer()
            Text(verbatim: "\(count)").foregroundStyle(.secondary)
        }
        .contentShape(Rectangle())
    }
}
