import AppKit
import SwiftUI

/// Original on the left (`review`), what the agent sees on the right (`show`). A span selected
/// on the left lights up its token on the right.
struct DocumentView: View {
    @Environment(AppModel.self) private var app
    @State var model: DocumentModel
    @State private var confirm = false
    /// What the confirmation is for: this document only, or every one.
    @State private var here = false

    var body: some View {
        VStack(spacing: 0) {
            if let review = model.review, review.stale {
                banner(L("This file changed after it was indexed: the original shows its new text, the agent still sees the old one."),
                       action: L("Re-index")) {
                    Task { await app.ingest([review.path]); await model.load() }
                }
            }
            HSplitView {
                original
                agent
            }
            if let review = model.review {
                Divider()
                actions(review)
            }
        }
        .navigationTitle(model.review.map { URL(filePath: $0.path).lastPathComponent } ?? "")
        .task { await model.load() }
        .onChange(of: model.selected) {  // VoiceOver hears what ⌘[ / ⌘] landed on
            if let span = model.selectedSpan, let review = model.review {
                AccessibilityNotification.Announcement("\(span.token), \(review.value(span))").post()
            }
        }
    }

    var original: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(L("Original")).font(.headline)
            if let review = model.review {
                let (text, ranges) = Highlight.original(review, selected: model.selected)
                Legend(types: review.spans.map(\.type))
                if review.exists {
                    TextPane(text: text, focus: model.selected.map { ranges[$0] }, label: L("Original")) { range in
                        (model.selected, model.selection) = Highlight.pick(range, in: text)
                    }
                } else {
                    ContentUnavailableView(L("File missing"), systemImage: "questionmark.folder",
                                           description: Text(L("The original is gone: only what the agent sees is left.")))
                }
            } else if model.loading {
                ProgressView(L("Reading the original…")).frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .padding(12)
        .frame(minWidth: 300, maxWidth: .infinity, maxHeight: .infinity)
    }

    var agent: some View {
        let token = model.selectedSpan?.token
        let (text, hits) = Highlight.agent(model.chunks, token: token)
        return VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(L("What the agent sees")).font(.headline)
                Spacer()
                // the file itself, to drag out; Save… does the same without a drag
                Label { Text(verbatim: "\(model.docId).md") } icon: { Image(systemName: "doc.text") }
                    .padding(.horizontal, 8).padding(.vertical, 3)
                    .background(.quaternary, in: .rect(cornerRadius: 6))
                    .draggable(Anonymized(cli: app.cli, docId: model.docId))
                    .help(L("Drag this file to a folder or another app: it holds only what the agent sees."))
                Button(L("Save…")) { if let dir = chooseFolder() { Task { await app.export([model.docId], to: dir) } } }
                    .controlSize(.small).disabled(app.job != nil)
            }
            Text(token.map { L("Occurrences of \($0): \(hits.count)") } ?? L("Select a highlighted value to find its token here."))
                .font(.callout).foregroundStyle(.secondary)
            TextPane(text: text, focus: hits.isEmpty ? nil : hits[min(model.occurrence, hits.count - 1)],
                     label: L("What the agent sees"))
        }
        .padding(12)
        .frame(minWidth: 300, maxWidth: .infinity, maxHeight: .infinity)
    }

    func actions(_ review: Review) -> some View {
        let value = model.selectedSpan.map(review.value) ?? ""
        return HStack(spacing: 10) {
            Button { model.step(-1) } label: { Label(L("Previous"), systemImage: "chevron.up") }
                .keyboardShortcut("[", modifiers: .command)
            Button { model.step(1) } label: { Label(L("Next"), systemImage: "chevron.down") }
                .keyboardShortcut("]", modifiers: .command)
            Divider().frame(height: 18)
            if let span = model.selectedSpan {
                Text(verbatim: "\(span.token)  \(review.value(span))").lineLimit(1).truncationMode(.middle)
                Spacer()
                Button(L("Don't Mask Here…")) { (here, confirm) = (true, true) }
                Button(L("Never Mask…")) { (here, confirm) = (false, true) }
                    .keyboardShortcut("u", modifiers: [.command, .shift])
            } else if !model.selection.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                Text(verbatim: "“\(model.selection)”").lineLimit(1).truncationMode(.middle)
                Spacer()
                Button(L("Mask Here")) { Task { await model.maskAlways(here: true) } }
                Button(L("Always Mask")) { Task { await model.maskAlways() } }
                    .keyboardShortcut("m", modifiers: [.command, .shift])
            } else {
                Text(L("Select a highlighted value, or select text the agent should not see."))
                    .foregroundStyle(.secondary)
                Spacer()
            }
        }
        .labelStyle(.iconOnly)
        .disabled(app.job != nil || model.loading)
        .padding(10)
        .confirmationDialog(L("Publish this value in clear?"), isPresented: $confirm) {
            Button(L("Don't Mask")) { Task { await model.dontMask(here: here) } }
        } message: {
            Text(here ? L("“\(value)” becomes visible to the agent in this document only.")
                      : L("“\(value)” becomes visible to the agent in this document. Other documents keep it masked until you re-index them (Re-index All)."))
        }
    }

    func banner(_ text: String, action: String, _ run: @escaping () -> Void) -> some View {
        HStack {
            Image(systemName: "arrow.triangle.2.circlepath").accessibilityHidden(true)
            Text(text).fixedSize(horizontal: false, vertical: true)
            Spacer()
            Button(action, action: run).disabled(app.job != nil)
        }
        .padding(10)
        .background(Color.orange.opacity(0.15))
    }
}

/// Which color means which type, with the type's name: color is never the only cue.
struct Legend: View {
    var types: [String]

    var body: some View {
        let present = Highlight.order.filter(types.contains)
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), alignment: .leading)], alignment: .leading, spacing: 4) {
            ForEach(present, id: \.self) { type in
                HStack(spacing: 6) {
                    RoundedRectangle(cornerRadius: 3).fill(Color(nsColor: Highlight.color(type)).opacity(0.5))
                        .frame(width: 14, height: 14).accessibilityHidden(true)
                    Text(verbatim: "\(type) · \(Highlight.name(type))").font(.caption)
                }
                .accessibilityElement(children: .combine)
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(L("Legend"))
    }
}

extension NSAttributedString.Key {
    /// Index in `Review.spans` of the span this text belongs to (its value or its token tag).
    static let span = NSAttributedString.Key("mcpSecretSpan")
    /// The token tag shown after a span: display only, not part of the original.
    static let tag = NSAttributedString.Key("mcpSecretTag")
}

enum Highlight {
    static let order = ["PERSONA", "ORG", "LUOGO", "INDIRIZZO", "CF", "PIVA", "IBAN", "EMAIL", "TELEFONO",
                        "TARGA", "DOCUMENTO", "DATA_NASCITA"]
    static let colors: [String: NSColor] = [
        "PERSONA": .systemBlue, "ORG": .systemPurple, "LUOGO": .systemGreen, "INDIRIZZO": .systemTeal,
        "CF": .systemOrange, "PIVA": .systemBrown, "IBAN": .systemRed, "EMAIL": .systemPink,
        "TELEFONO": .systemIndigo, "TARGA": .systemYellow, "DOCUMENTO": .systemMint, "DATA_NASCITA": .systemCyan,
    ]
    static let names = [
        "PERSONA": "Person", "ORG": "Organization", "LUOGO": "Place", "INDIRIZZO": "Address",
        "CF": "Tax code", "PIVA": "VAT number", "IBAN": "Bank account", "EMAIL": "Email",
        "TELEFONO": "Phone", "TARGA": "License plate", "DOCUMENTO": "ID document", "DATA_NASCITA": "Date of birth",
    ]
    static var token: Regex<(Substring, Substring)> { /\[([A-Z]+(?:_[A-Z]+)*)_\d+\]/ }

    static func color(_ type: String) -> NSColor { colors[type] ?? .systemGray }
    static func name(_ type: String) -> String {
        names[type].map { Bundle.module.localizedString(forKey: $0, value: nil, table: nil) } ?? type
    }

    static var body: [NSAttributedString.Key: Any] {
        [.font: NSFont.systemFont(ofSize: NSFont.systemFontSize), .foregroundColor: NSColor.labelColor]
    }

    static func mark(_ type: String, on: Bool) -> [NSAttributedString.Key: Any] {
        var a = body
        a[.backgroundColor] = color(type).withAlphaComponent(on ? 0.55 : 0.22)
        if on { a[.underlineStyle] = NSUnderlineStyle.thick.rawValue }  // more than a darker color
        return a
    }

    /// The original, each span tinted by type and followed by its token as a small tag. Returns
    /// each span's range in the result (UTF-16, for AppKit).
    static func original(_ review: Review, selected: Int?) -> (NSAttributedString, [NSRange]) {
        let scalars = Array(review.text.unicodeScalars), out = NSMutableAttributedString()
        func text(_ a: Int, _ b: Int) -> String { String(String.UnicodeScalarView(scalars[a..<b])) }
        var ranges: [NSRange] = [], at = 0
        for (i, s) in review.spans.enumerated() {
            out.append(NSAttributedString(string: text(at, s.start), attributes: body))
            var a = mark(s.type, on: i == selected)
            a[.span] = i
            let start = out.length
            out.append(NSAttributedString(string: text(s.start, s.end), attributes: a))
            ranges.append(NSRange(location: start, length: out.length - start))
            a[.tag] = true
            a[.font] = NSFont.monospacedSystemFont(ofSize: NSFont.smallSystemFontSize, weight: .semibold)
            a[.foregroundColor] = NSColor.secondaryLabelColor
            a[.underlineStyle] = nil
            out.append(NSAttributedString(string: "\u{2009}\(s.token)", attributes: a))
            at = s.end
        }
        out.append(NSAttributedString(string: text(at, scalars.count), attributes: body))
        return (out, ranges)
    }

    /// What the agent sees, chunk after chunk, tokens tinted by type; the occurrences of `token`
    /// stand out, and their ranges are returned.
    static func agent(_ chunks: [Chunk], token: String?) -> (NSAttributedString, [NSRange]) {
        let out = NSMutableAttributedString()
        let head: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: NSFont.smallSystemFontSize),
                                                   .foregroundColor: NSColor.tertiaryLabelColor]
        for c in chunks {
            out.append(NSAttributedString(string: L("Chunk \(c.chunkId)") + "\n", attributes: head))
            out.append(NSAttributedString(string: c.text.trimmingCharacters(in: .newlines) + "\n\n", attributes: body))
        }
        var hits: [NSRange] = []
        let string = out.string
        for m in string.matches(of: Highlight.token) {
            let r = NSRange(m.range, in: string), on = String(m.0) == token
            out.addAttributes(mark(String(m.1), on: on), range: r)
            if on { hits.append(r) }
        }
        return (out, hits)
    }

    /// What a selection in `original()` points at: a caret or selection on one span (value or
    /// tag) selects that span; anything else is original text for "Always mask", tags left out.
    static func pick(_ range: NSRange, in s: NSAttributedString) -> (span: Int?, text: String) {
        guard range.location < s.length else { return (nil, "") }
        if range.length == 0 { return (s.attribute(.span, at: range.location, effectiveRange: nil) as? Int, "") }
        var spans = Set<Int>(), text = "", outside = ""
        s.enumerateAttributes(in: NSIntersectionRange(range, NSRange(location: 0, length: s.length))) { a, r, _ in
            if let i = a[.span] as? Int { spans.insert(i) }
            guard a[.tag] == nil else { return }
            let piece = s.attributedSubstring(from: r).string
            text += piece
            if a[.span] == nil { outside += piece }
        }
        if spans.count == 1, outside.allSatisfy(\.isWhitespace) { return (spans.first, "") }
        return (nil, text)
    }
}

/// Read-only, selectable rich text (NSTextView): keyboard selection, VoiceOver, find (⌘F).
struct TextPane: NSViewRepresentable {
    var text: NSAttributedString
    /// Scrolled to and flashed when it changes.
    var focus: NSRange?
    var label: String
    var onSelect: ((NSRange) -> Void)?

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSTextView.scrollableTextView()
        let view = scroll.documentView as! NSTextView
        view.isEditable = false
        view.usesFindBar = true
        view.textContainerInset = NSSize(width: 8, height: 10)
        view.delegate = context.coordinator
        view.setAccessibilityLabel(label)
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        let view = scroll.documentView as! NSTextView, c = context.coordinator
        c.onSelect = onSelect
        c.updating = true  // our own edits move the selection: not the user's choice
        defer { c.updating = false }
        if view.string != text.string {
            view.textStorage?.setAttributedString(text)
        } else if let storage = view.textStorage, !storage.isEqual(to: text) {
            storage.beginEditing()  // same text, new styles: restyle in place, the selection stays
            text.enumerateAttributes(in: NSRange(location: 0, length: text.length)) { a, r, _ in
                storage.setAttributes(a, range: r)
            }
            storage.endEditing()
        }
        if let focus, focus != c.focus, NSMaxRange(focus) <= text.length {
            view.scrollRangeToVisible(focus)
            view.showFindIndicator(for: focus)
        }
        c.focus = focus
    }

    @MainActor final class Coordinator: NSObject, NSTextViewDelegate {
        var onSelect: ((NSRange) -> Void)?
        var updating = false
        var focus: NSRange?

        func textViewDidChangeSelection(_ notification: Notification) {
            guard !updating, let view = notification.object as? NSTextView else { return }
            onSelect?(view.selectedRange())
        }
    }
}
