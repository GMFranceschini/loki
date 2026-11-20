// Rasterise an SVG to a square PNG using AppKit, which reads SVG natively on
// macOS 13 and later. This exists so the icon can be rebuilt with nothing
// installed beyond the system itself; scripts/make_icon.sh prefers
// rsvg-convert or Inkscape when either is present.
//
//   swift svg2png.swift <in.svg> <out.png> <pixels>

import AppKit
import Foundation

func fail(_ message: String) -> Never {
    FileHandle.standardError.write("svg2png: \(message)\n".data(using: .utf8)!)
    exit(1)
}

let args = CommandLine.arguments
guard args.count > 3, let px = Int(args[3]), px > 0 else {
    fail("usage: svg2png.swift <in.svg> <out.png> <pixels>")
}
guard let source = NSImage(contentsOfFile: args[1]) else {
    fail("could not read \(args[1]) as an image")
}
guard let rep = NSBitmapImageRep(
    bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px,
    bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
    colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
) else { fail("cannot allocate a \(px)x\(px) bitmap") }

NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
NSGraphicsContext.current?.imageInterpolation = .high
NSColor.clear.set()
NSRect(x: 0, y: 0, width: px, height: px).fill(using: .copy)
source.draw(in: NSRect(x: 0, y: 0, width: px, height: px))
NSGraphicsContext.current = nil
NSGraphicsContext.restoreGraphicsState()

guard let data = rep.representation(using: .png, properties: [:]) else {
    fail("could not encode the PNG")
}
do { try data.write(to: URL(fileURLWithPath: args[2])) }
catch { fail("could not write \(args[2]): \(error)") }
