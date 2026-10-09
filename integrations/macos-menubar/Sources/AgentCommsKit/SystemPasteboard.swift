#if canImport(AppKit)
import AppKit

/// `TokenPasteboard` over an `NSPasteboard` (the general pasteboard by default; tests use a private named one).
public final class SystemPasteboard: TokenPasteboard {
    private let pasteboard: NSPasteboard

    public init(_ pasteboard: NSPasteboard = .general) {
        self.pasteboard = pasteboard
    }

    public var changeCount: Int { pasteboard.changeCount }

    /// `.currentHostOnly` keeps the new contents on this Mac: Universal Clipboard does not offer them to the
    /// human's other devices, where the 60 s clear could not reach them.
    public func prepareForNewContents(currentHostOnly: Bool) -> Int {
        pasteboard.prepareForNewContents(with: currentHostOnly ? .currentHostOnly : [])
    }

    /// One item with the string and the marker types, written in a single `writeObjects`, so a clipboard manager
    /// polling the pasteboard never sees the string without its concealed/transient markers.
    public func writeItem(string: String, markerTypes: [String]) -> Bool {
        let item = NSPasteboardItem()
        guard item.setString(string, forType: .string) else { return false }
        for type in markerTypes {
            guard item.setData(Data(), forType: NSPasteboard.PasteboardType(type)) else { return false }
        }
        return pasteboard.writeObjects([item])
    }

    public func clearContents() {
        pasteboard.clearContents()
    }
}
#endif
